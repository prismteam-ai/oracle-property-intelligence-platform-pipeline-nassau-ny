"""Load Oyster Bay eTRAKiT block checkpoints into DuckDB; one ``pipeline_runs`` row per batch.

    .venv/bin/python -m pipeline.etrakit_load                 # load every block file that is new or changed
    .venv/bin/python -m pipeline.etrakit_load --decide-scope  # write etrakit_scope_decision from counts + budget

Idempotent: a block file is merged only when its sha256 differs from ``etrakit_loaded_blocks``; permits merge by
content hash (new / changed / unchanged counted into the batch row); the derived tables are rebuilt from the source
tables every batch. The harvester calls ``load_batch`` after every N blocks and at the end, never holding the DuckDB
file between batches.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from . import config, db, etrakit_derive
from .etrakit_harvest import BLOCK_DIR, ROOT, SCOPE_JSON, write_json

SOURCE = "oyster_bay_etrakit"
LIMITATIONS = [
    "Portal results are capped at 100 rows per query; capped queries are split by lot prefix (see etrakit_blocks.capped_queries).",
    "The portal also returns a few fewer rows than its own reported total even under the cap (e.g. 53 of 55); prefix splitting does "
    "not recover them. The per-block shortfall is recorded in etrakit_blocks.shortfall.",
    "Scope is a deliberate subset (1-mile radius, then highest-permit blocks up to a budget): see etrakit_scope_decision.",
    "Job Description is empty on all pilot permits; roofing classification relies on roles, contractor names, fee and inspection codes.",
    "CLOSED permits carry no Finaled Date; close date is the last completed final inspection when present.",
    "Contacts are raw portal text; no company, person or licence identity is resolved or stamped.",
]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_tables(con) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS etrakit_loaded_blocks (block_key VARCHAR PRIMARY KEY, file_sha256 VARCHAR, loaded_run VARCHAR, loaded_at TIMESTAMPTZ)")
    con.execute("""CREATE TABLE IF NOT EXISTS etrakit_blocks (block_key VARCHAR PRIMARY KEY, status VARCHAR, reported_total INTEGER,
        unique_permits INTEGER, shortfall INTEGER, capped_queries JSON, queries JSON, failures JSON, harvest_run VARCHAR, finished_at VARCHAR)""")


def _stage(block_files: list[Path], batch_id: str) -> dict[str, Path]:
    d = config.DATA_DIR / "stage" / "etrakit" / batch_id
    d.mkdir(parents=True, exist_ok=True)
    paths = {k: d / f"{k}.ndjson.gz" for k in ("permits", "contacts", "inspections")}
    fhs = {k: gzip.open(p, "wt") for k, p in paths.items()}
    try:
        for bf in block_files:
            st = json.loads(bf.read_text())
            for no, p in st["permits"].items():
                dt = p["detail"]
                prov = {"_source": SOURCE, "_source_url": p["raw"]["url"], "_fetched_at": p["raw"]["fetched_at"],
                        "_artifact_sha256": p["raw"]["sha256"]}
                fhs["permits"].write(json.dumps({
                    "permit_no": no, "record_id": p["record_id"], "block_key": st["block_key"], "grid_sbl": p["grid_sbl"],
                    "permit_type": dt["permit_type"], "permit_subtype": dt["permit_subtype"], "job_description": dt["job_description"],
                    "status": dt["status"], "applied_date": dt["applied_date"], "approved_date": dt["approved_date"],
                    "issued_date": dt["issued_date"], "finaled_date": dt["finaled_date"], "expiration_date": dt["expiration_date"],
                    "site_address": dt["site_address"], "site_city_state_zip": dt["site_city_state_zip"], "property_type": dt["property_type"],
                    "site_parid": dt["site_parid"], "site_section": dt["site_section"], "site_block": dt["site_block"], "site_lot": dt["site_lot"],
                    "site_subdivision": dt["site_subdivision"], "site_village": dt["site_village"], "lot_size_sf": dt["lot_size_sf"],
                    "fees_charged": dt["fees_charged"], "fees_balance_due": dt["fees_balance_due"],
                    "fees_json": json.dumps(dt["fees"]), "tab_counts_json": json.dumps(dt["tab_counts"]),
                    "verify_problems_json": json.dumps(p["verify_problems"]), "raw_path": p["raw"]["path"], **prov}) + "\n")
                for c in dt["contacts"]:
                    fhs["contacts"].write(json.dumps({"permit_no": no, "seq": c["seq"], "contact_type": c["contact_type"], "name": c["name"],
                                                      "phone": c["phone"], "email": c["email"], "address": c["address"],
                                                      "city_state_zip": c["city_state_zip"], **prov}) + "\n")
                for i in dt["inspections"]:
                    fhs["inspections"].write(json.dumps({"permit_no": no, "seq": i["seq"], "type": i["type"], "result": i["result"],
                                                         "scheduled_date": i["scheduled_date"], "completed_date": i["completed_date"], **prov}) + "\n")
    finally:
        for f in fhs.values():
            f.close()
    return paths


def _sel(path: Path, cols: list[str]) -> str:
    sel = ", ".join(f"json_extract_string(json, '$.\"{c}\"') AS \"{c}\"" for c in cols)
    return f"SELECT {sel} FROM read_ndjson_objects('{path}', compression='gzip')"


PERMIT_COLS = ["permit_no", "record_id", "block_key", "grid_sbl", "permit_type", "permit_subtype", "job_description", "status", "applied_date",
               "approved_date", "issued_date", "finaled_date", "expiration_date", "site_address", "site_city_state_zip", "property_type",
               "site_parid", "site_section", "site_block", "site_lot", "site_subdivision", "site_village", "lot_size_sf", "fees_charged",
               "fees_balance_due", "fees_json", "tab_counts_json", "verify_problems_json", "raw_path"]
CONTACT_COLS = ["permit_no", "seq", "contact_type", "name", "phone", "email", "address", "city_state_zip"]
INSP_COLS = ["permit_no", "seq", "type", "result", "scheduled_date", "completed_date"]
PROV = ["_source", "_source_url", "_fetched_at", "_artifact_sha256"]


def load_batch(batch_id: str, as_of: date | None = None, threshold: int = config.DEFAULT_ROOF_AGE_THRESHOLD_YEARS,
               harvest_run: str | None = None, log=print) -> dict:
    as_of = as_of or date.today()
    BLOCK_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    with db.single_writer():
        con = db.connect()
        ensure_tables(con)
        con.execute("DELETE FROM pipeline_runs WHERE run_id=?", [batch_id])
        con.execute("""INSERT INTO pipeline_runs (run_id, started_at, status, mode, code_version, parameters)
                       VALUES (?, now(), 'running', 'etrakit-batch', ?, ?)""",
                    [batch_id, _git(), json.dumps({"harvest_run": harvest_run, "as_of": as_of.isoformat(), "roof_age_threshold_years": threshold})])
        try:
            loaded = dict(con.execute("SELECT block_key, file_sha256 FROM etrakit_loaded_blocks").fetchall())
            todo, keys = [], []
            for bf in sorted(BLOCK_DIR.glob("*.json")):
                st = json.loads(bf.read_text())
                if st.get("permits") and loaded.get(st["block_key"]) != _sha(bf):
                    todo.append(bf); keys.append(st["block_key"])
            deltas = {}
            if todo:
                paths = _stage(todo, batch_id)
                deltas["permits"] = db.merge(con, batch_id, "etrakit_permits", _sel(paths["permits"], PERMIT_COLS + PROV), "permit_no",
                                             PERMIT_COLS, tombstone=False)
                deltas["contacts"] = db.merge(con, batch_id, "etrakit_contacts", _sel(paths["contacts"], CONTACT_COLS + PROV),
                                              "permit_no || '|' || seq", CONTACT_COLS, tombstone=False)
                deltas["inspections"] = db.merge(con, batch_id, "etrakit_inspections", _sel(paths["inspections"], INSP_COLS + PROV),
                                                 "permit_no || '|' || seq", INSP_COLS, tombstone=False)
                for bf in todo:
                    st = json.loads(bf.read_text())
                    con.execute("DELETE FROM etrakit_blocks WHERE block_key=?", [st["block_key"]])
                    con.execute("INSERT INTO etrakit_blocks VALUES (?,?,?,?,?,?,?,?,?,?)", [
                        st["block_key"], st["status"], st.get("reported_total"), st.get("unique_permits", len(st["permits"])), st.get("shortfall"),
                        json.dumps(st.get("capped_queries", [])), json.dumps([{k: v for k, v in q.items() if k != "grid_artifacts"} for q in st["queries"]]),
                        json.dumps(st["failures"]), st.get("run_id"), st.get("finished_at")])
                    con.execute("DELETE FROM etrakit_loaded_blocks WHERE block_key=?", [st["block_key"]])
                    con.execute("INSERT INTO etrakit_loaded_blocks VALUES (?,?,?, now())", [st["block_key"], _sha(bf), batch_id])
            else:
                log("no new or changed block files")
            summary = None
            if db.table_exists(con, "etrakit_permits") and db.table_exists(con, "properties"):
                summary = etrakit_derive.build(con, batch_id, as_of, threshold)
            vocab = {}
            if db.table_exists(con, "etrakit_permits"):
                vocab = {r[0]: r[1] for r in con.execute("SELECT status, count(*) FROM etrakit_permits WHERE is_active GROUP BY 1").fetchall()}
            counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in
                      ("etrakit_permits", "etrakit_contacts", "etrakit_inspections", "etrakit_blocks", "etrakit_permit_facts",
                       "etrakit_permit_property_link", "etrakit_unmatched_sbl", "property_roof_age") if db.table_exists(con, t)}
            short = con.execute("SELECT coalesce(sum(shortfall),0), count(*) FILTER (WHERE shortfall>0), count(*) FROM etrakit_blocks").fetchone()
            lim = LIMITATIONS + [f"Blocks loaded so far: {short[2]}; blocks with a portal shortfall: {short[1]} (total {short[0]} records counted by the portal but never returned)."]
            con.execute("""UPDATE pipeline_runs SET finished_at=now(), status='succeeded', sources=?, record_counts=?, deltas=?, limitations=?
                           WHERE run_id=?""", [json.dumps([{"source": SOURCE, "blocks": keys}]), json.dumps(counts),
                                               json.dumps({**deltas, "derived": summary, "status_vocabulary": vocab}, default=str), json.dumps(lim), batch_id])
            con.execute("CHECKPOINT")
            d = deltas.get("permits", {})
            log(f"batch {batch_id}: blocks={len(keys)} permits new={d.get('new', 0)} changed={d.get('changed', 0)} unchanged={d.get('unchanged', 0)} derived={summary}")
            return {"blocks": len(keys), "deltas": deltas, "derived": summary, "counts": counts}
        except Exception as exc:
            con.execute("UPDATE pipeline_runs SET finished_at=now(), status='failed', error=? WHERE run_id=?", [f"{type(exc).__name__}: {exc}", batch_id])
            raise


def _git() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=config.REPO_ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _estimate(v: dict, ratio: float) -> tuple[int, int]:
    """(capturable permits, requests) for a counted block. Cap-hit blocks recover ~ratio of the reported total through
    lot-prefix splitting (9 child searches plus their pages); the portal's own shortfall is not recoverable."""
    import math
    cap = round(v["total"] * ratio) if v.get("cap_hit") else (v.get("returned") or 0)
    pages = max(0, math.ceil((v.get("returned") or 0) / 20) - 1)
    req = cap + 1 + pages + ((9 + math.ceil(v["total"] / 20)) if v.get("cap_hit") else 0) if cap else 1
    return cap, req


def decide_scope(permit_budget: int) -> dict:
    """Pick the highest-permit blocks of the current scope up to ``permit_budget`` capturable permits; record every block."""
    counts = json.loads((ROOT / "block_counts.json").read_text())
    scope = json.loads(SCOPE_JSON.read_text())
    noncap = [v for v in counts.values() if not v.get("cap_hit")]
    ratio = 1 - sum(v["short_by"] for v in noncap) / max(1, sum(v["total"] for v in noncap))
    est = {b["block_key"]: _estimate(counts[b["block_key"]], ratio) if b["block_key"] in counts else (0, 0) for b in scope}
    ranked = sorted(scope, key=lambda b: (-est[b["block_key"]][0], b["nearest_miles"], b["block_key"]))
    cum = cum_req = 0
    rows = []
    for rank, b in enumerate(ranked, 1):
        c = counts.get(b["block_key"])
        cap, req = est[b["block_key"]]
        include = bool(c) and cap > 0 and cum + cap <= permit_budget
        if include:
            cum += cap; cum_req += req
        rows.append({"block_key": b["block_key"], "section": b["section"], "block": b["block"], "parcels": b["parcels"],
                     "nearest_miles": b["nearest_miles"], "counted_total": (c or {}).get("total", 0), "counted_returned": (c or {}).get("returned", 0),
                     "short_by": (c or {}).get("short_by"), "cap_hit": (c or {}).get("cap_hit"), "capturable": cap, "est_requests": req,
                     "rank": rank, "included": include, "cumulative_included_permits": cum,
                     "reason": ("not counted" if not c else "excluded: no permits" if cap == 0 else
                                "included: highest-permit blocks within the permit budget" if include else "excluded: over permit budget")})
    all_cap = sum(r["capturable"] for r in rows); all_req = sum(r["est_requests"] for r in rows)
    return {"rows": rows, "included": [r for r in rows if r["included"]], "budget": permit_budget, "included_permits": cum, "included_requests": cum_req,
            "all_capturable": all_cap, "all_requests": all_req, "recovery_ratio": ratio}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", default=None)
    ap.add_argument("--as-of", default=date.today().isoformat())
    ap.add_argument("--roof-age-threshold", type=int, default=config.DEFAULT_ROOF_AGE_THRESHOLD_YEARS)
    ap.add_argument("--decide-scope", type=int, default=None, metavar="PERMIT_BUDGET")
    args = ap.parse_args(argv)
    if args.decide_scope is not None:
        res = decide_scope(args.decide_scope)
        with db.single_writer():
            con = db.connect()
            run_id = "etrakit-scope-decision-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            con.execute("DROP TABLE IF EXISTS etrakit_scope_decision")
            con.execute("""CREATE TABLE etrakit_scope_decision (block_key VARCHAR, section VARCHAR, block VARCHAR, parcels INTEGER, nearest_miles DOUBLE,
                counted_total INTEGER, counted_returned INTEGER, short_by INTEGER, cap_hit BOOLEAN, capturable INTEGER, est_requests INTEGER,
                rank INTEGER, included BOOLEAN, cumulative_included_permits INTEGER, reason VARCHAR, decided_run VARCHAR, decided_at TIMESTAMPTZ DEFAULT now())""")
            for r in res["rows"]:
                con.execute("INSERT INTO etrakit_scope_decision (block_key, section, block, parcels, nearest_miles, counted_total, counted_returned, short_by, "
                            "cap_hit, capturable, est_requests, rank, included, cumulative_included_permits, reason, decided_run) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            [r["block_key"], r["section"], r["block"], r["parcels"], r["nearest_miles"], r["counted_total"], r["counted_returned"], r["short_by"],
                             r["cap_hit"], r["capturable"], r["est_requests"], r["rank"], r["included"], r["cumulative_included_permits"], r["reason"], run_id])
            con.execute("CHECKPOINT")
        inc = sorted(res["included"], key=lambda r: (r["nearest_miles"], r["block_key"]))
        write_json(ROOT / "harvest_blocks.json", [{"block_key": r["block_key"], "section": r["section"], "block": r["block"], "parcels": r["parcels"],
                                                    "nearest_miles": r["nearest_miles"], "expected_total": r["counted_total"]} for r in inc])
        write_json(ROOT / "scope_decision_summary.json", {k: v for k, v in res.items() if k != "rows"} | {"decided_run": run_id})
        print(json.dumps({"included_blocks": len(res["included"]), "excluded_blocks": len(res["rows"]) - len(res["included"]),
                          "included_capturable_permits": res["included_permits"], "included_est_requests": res["included_requests"],
                          "all_capturable": res["all_capturable"], "all_est_requests": res["all_requests"], "budget": res["budget"],
                          "hours_at_1.1_1.3_1.5_s_per_request": [round(res["included_requests"] * t / 3600, 2) for t in (1.1, 1.3, 1.5)]}))
        return 0
    batch = args.batch_id or "etrakit-batch-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    load_batch(batch, date.fromisoformat(args.as_of), args.roof_age_threshold)
    return 0


if __name__ == "__main__":
    sys.exit(main())
