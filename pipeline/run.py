"""Run the Nassau parcel-backbone pipeline.

    .venv/bin/python -m pipeline.run                 # incremental (skip sources whose watermark is unchanged)
    .venv/bin/python -m pipeline.run --full-refresh  # re-fetch every source and diff by record hash
    .venv/bin/python -m pipeline.run --sources roll,gis --roof-age-threshold 20

Each run gets a run_id and a row in pipeline_runs (timestamps, sources, per-source
counts, deltas, limitations). Steps are idempotent: raw pages are content-addressed and
reused on resume, merges are hash-based, derived tables are deterministic rebuilds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import traceback
from datetime import date, datetime, timezone

from . import config, db, derive, etrakit_derive
from .sources import (AssessmentRoll, Corporations, GisParcels, Nsi, NysCentroids, OverturePlaces,
                      TownBoundary)

ALL_SOURCES = ["roll", "gis", "centroids", "nsi", "corporations", "overture"]


def _ndjson_select(path, cols: list[str]) -> str:
    sel = ", ".join(f"json_extract_string(json, '$.\"{c}\"') AS \"{c}\"" for c in cols)
    return f"SELECT {sel} FROM read_ndjson_objects('{path}', compression='gzip')"


def _ndjson_keys(path, sample: int = 5000) -> list[str]:
    import gzip
    keys: dict[str, None] = {}
    with gzip.open(path, "rt") as fh:
        for i, line in enumerate(fh):
            for k in json.loads(line):
                keys.setdefault(k, None)
            if i >= sample:
                break
    return list(keys)


def _manifest_sha(source: str, run_id: str) -> str | None:
    m = config.RAW_DIR / source / run_id / "manifest.jsonl"
    return hashlib.sha256(m.read_bytes()).hexdigest() if m.exists() else None


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=config.REPO_ROOT, text=True).strip()
    except Exception:
        return "unknown"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full-refresh", action="store_true", help="ignore watermarks; re-fetch and diff every source")
    ap.add_argument("--sources", default=",".join(ALL_SOURCES))
    ap.add_argument("--roof-age-threshold", type=int, default=config.DEFAULT_ROOF_AGE_THRESHOLD_YEARS)
    ap.add_argument("--as-of", default=date.today().isoformat())
    ap.add_argument("--run-id", default=None, help="resume a run id (reuses captured raw pages)")
    args = ap.parse_args(argv)
    wanted = [s.strip() for s in args.sources.split(",") if s.strip()]
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    as_of = date.fromisoformat(args.as_of)

    with db.single_writer():
        con = db.connect()
        params = {"roof_age_threshold_years": args.roof_age_threshold, "as_of": args.as_of,
                  "sources": wanted, "full_refresh": args.full_refresh}
        con.execute("DELETE FROM pipeline_runs WHERE run_id=?", [run_id])
        con.execute("INSERT INTO pipeline_runs (run_id, started_at, status, mode, code_version, parameters) VALUES (?, now(), 'running', ?, ?, ?)",
                    [run_id, "full-refresh" if args.full_refresh else "incremental", _git_sha(), json.dumps(params)])
        sources_log, deltas = [], {}
        any_change = False
        t_run = time.time()

        def step(source_key: str, scope: str, adapter, fetch_and_merge, table: str, change_scope: str = ""):
            nonlocal any_change
            t0 = time.time()
            done = db.completed_step(con, run_id, adapter.name, scope, table, change_scope)
            if done is not None:  # resumed run: this step already merged in this run
                done["resumed"] = True
                done.setdefault("seconds", None)
                deltas[f"{source_key}:{scope}"] = done
                sources_log.append({"source": adapter.name, "scope": scope, "action": done["action"], "resumed": True})
                if done["new"] or done["changed"] or done["removed"]:
                    any_change = True
                print(f"[{run_id}] {source_key}:{scope} already completed in this run: {done}", flush=True)
                return
            wm = adapter.watermark()
            prev = db.last_watermark(con, adapter.name, scope)
            if not args.full_refresh and prev is not None and prev == wm and "always fetch" not in json.dumps(wm):
                d = {"staged": 0, "new": 0, "changed": 0, "unchanged": None, "removed": 0, "action": "skipped-unchanged"}
                db.record_snapshot(con, run_id, adapter.name, scope, wm, "skipped-unchanged", delta=d)
            else:
                d = fetch_and_merge()
                d["action"] = "fetched"
                db.record_snapshot(con, run_id, adapter.name, scope, wm, "fetched", d.get("staged"),
                                   str(config.RAW_DIR / adapter.name / run_id), _manifest_sha(adapter.name, run_id), delta=d)
            d["seconds"] = round(time.time() - t0, 1)
            deltas[f"{source_key}:{scope}"] = d
            sources_log.append({"source": adapter.name, "scope": scope, "watermark": wm, "action": d["action"]})
            if d["new"] or d["changed"] or d["removed"]:
                any_change = True
            print(f"[{run_id}] {source_key}:{scope} {d}", flush=True)

        try:
            if "roll" in wanted:
                roll = AssessmentRoll()
                for y in config.SOCRATA_ROLL["years"]:
                    def fm(y=y):
                        path = roll.fetch(run_id, [y])[y]
                        cols = config.SOCRATA_ROLL["columns"] + db.PROVENANCE_COLS
                        return db.merge(con, run_id, "assessment_roll", _ndjson_select(path, cols),
                                        "swis_code || '|' || print_key_code || '|' || coalesce(parcel_address_number,'') || '|' || coalesce(parcel_address_street,'')",
                                        config.SOCRATA_ROLL["columns"], scope_col="roll_year", scope_val=str(y))
                    step("roll", str(y), roll, fm, "assessment_roll", f"roll_year={y}")

            if "gis" in wanted:
                gis = GisParcels()
                def fm():
                    path = gis.fetch(run_id)
                    cols = config.GIS_PARCELS["out_fields"] + ["geometry_geojson"] + db.PROVENANCE_COLS
                    biz = [c for c in cols if c not in db.PROVENANCE_COLS and c != "last_edited_date"] + ["last_edited_date"]
                    return db.merge(con, run_id, "gis_parcels", _ndjson_select(path, cols),
                                    "coalesce(parid, 'oid:' || objectid)", biz)
                step("gis", "all", gis, fm, "gis_parcels")

            if "centroids" in wanted:
                cen = NysCentroids()
                def fm():
                    path = cen.fetch(run_id)
                    cols = config.NYS_CENTROIDS["out_fields"] + ["lon", "lat"] + db.PROVENANCE_COLS
                    biz = [c for c in cols if c not in db.PROVENANCE_COLS and c != "OBJECTID"]
                    return db.merge(con, run_id, "nys_centroids", _ndjson_select(path, cols),
                                    "coalesce(SWIS_SBL_ID, 'oid:' || OBJECTID)", biz)
                step("centroids", "nassau", cen, fm, "nys_centroids")

            if "nsi" in wanted:
                nsi = Nsi()
                def fm():
                    path = nsi.fetch(run_id)
                    cols = _ndjson_keys(path)
                    biz = [c for c in cols if c not in db.PROVENANCE_COLS]
                    return db.merge(con, run_id, "nsi_structures", _ndjson_select(path, cols), "fd_id", biz)
                step("nsi", config.FIPS, nsi, fm, "nsi_structures")

            if "corporations" in wanted:
                corp = Corporations()
                def fm():
                    path = corp.fetch(run_id)
                    cols = _ndjson_keys(path, sample=50000)
                    biz = [c for c in cols if c not in db.PROVENANCE_COLS]
                    return db.merge(con, run_id, "corporations", _ndjson_select(path, cols), "dos_id", biz)
                step("corporations", "nassau", corp, fm, "corporations")

            if "overture" in wanted:
                ov = OverturePlaces()
                def fm():
                    boundary = TownBoundary().fetch(run_id)
                    pq = ov.fetch(run_id, con, boundary)
                    meta = json.loads((pq.parent / "provenance.json").read_text())
                    stage = (f"SELECT *, '{ov.name}' AS _source, '{meta['url']}' AS _source_url, "
                             f"'{meta['fetched_at']}' AS _fetched_at, '{meta['sha256']}' AS _artifact_sha256, "
                             f"'{meta['release']}' AS overture_release FROM read_parquet('{pq}')")
                    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{pq}')").fetchall()]
                    return db.merge(con, run_id, "overture_places", stage, "id", cols + ["overture_release"])
                step("overture", OverturePlaces.cfg["release"], ov, fm, "overture_places")

            derived_stats = None
            # Rebuild derived tables whenever a source changed, this is the first run, the
            # requested roof-age-threshold/as-of differs from the last *succeeded* run's settings,
            # or the immediately preceding run did not succeed (so its derived tables may be stale
            # or only partially written even though source rows look unchanged now).
            last_succeeded = con.execute(
                "SELECT parameters FROM pipeline_runs WHERE run_id <> ? AND status='succeeded' "
                "ORDER BY started_at DESC LIMIT 1", [run_id]).fetchone()
            settings_changed = False
            if last_succeeded is not None:
                prev_params = json.loads(last_succeeded[0])
                settings_changed = (prev_params.get("roof_age_threshold_years") != args.roof_age_threshold
                                     or prev_params.get("as_of") != args.as_of)
            prev_run = con.execute(
                "SELECT status FROM pipeline_runs WHERE run_id <> ? ORDER BY started_at DESC LIMIT 1",
                [run_id]).fetchone()
            prev_failed = prev_run is not None and prev_run[0] != "succeeded"
            if any_change or args.full_refresh or not db.table_exists(con, "properties") or settings_changed or prev_failed:
                if not any_change and not args.full_refresh and db.table_exists(con, "properties"):
                    reason = "settings changed" if settings_changed else "previous run did not succeed"
                    print(f"[{run_id}] no source changes, but rebuilding derived tables anyway ({reason})", flush=True)
                t0 = time.time()
                derived_stats = derive.build(con, run_id, args.roof_age_threshold, as_of)
                # properties (and property_key) just changed; re-link eTRAKiT permits and rebuild
                # property_roof_age against the fresh keys now, rather than waiting for the next
                # etrakit_load batch to happen to run and overwrite them with current data.
                if db.table_exists(con, "etrakit_permits"):
                    etrakit_stats = etrakit_derive.build(con, run_id, as_of, args.roof_age_threshold)
                    derived_stats = {**derived_stats, "etrakit_relink": etrakit_stats}
                print(f"[{run_id}] derived tables rebuilt in {time.time() - t0:.1f}s {derived_stats}", flush=True)
            else:
                print(f"[{run_id}] no source changes; derived tables left as-is", flush=True)

            counts = table_counts(con)
            con.execute("""UPDATE pipeline_runs SET finished_at=now(), status='succeeded', sources=?, record_counts=?,
                           deltas=?, limitations=? WHERE run_id=?""",
                        [json.dumps(sources_log), json.dumps(counts), json.dumps({**deltas, "_derived": derived_stats}, default=str),
                         json.dumps(config.LIMITATIONS), run_id])
            con.execute("CHECKPOINT")
            print_summary(run_id, counts, deltas, time.time() - t_run)
            return 0
        except Exception as exc:
            con.execute("UPDATE pipeline_runs SET finished_at=now(), status='failed', sources=?, deltas=?, error=? WHERE run_id=?",
                        [json.dumps(sources_log), json.dumps(deltas, default=str), f"{type(exc).__name__}: {exc}", run_id])
            traceback.print_exc()
            return 1


def table_counts(con) -> dict:
    out = {}
    for (t,) in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main' AND table_type='BASE TABLE' ORDER BY 1").fetchall():
        cols = {r[0] for r in con.execute(f"DESCRIBE {t}").fetchall()}
        total = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        active = con.execute(f"SELECT count(*) FROM {t} WHERE is_active").fetchone()[0] if "is_active" in cols else None
        out[t] = {"rows": total, "active": active}
    return out


def print_summary(run_id, counts, deltas, seconds):
    print(f"\n=== run {run_id} finished in {seconds/60:.1f} min ===")
    print(f"{'source:scope':38} {'action':18} {'staged':>8} {'new':>8} {'changed':>8} {'removed':>8} {'secs':>7}")
    for k, d in deltas.items():
        print(f"{k:38} {d['action']:18} {d['staged'] or 0:>8} {d['new']:>8} {d['changed']:>8} {d['removed']:>8} {str(d.get('seconds')):>7}")
    print(f"\n{'table':28} {'rows':>10} {'active':>10}")
    for t, c in counts.items():
        print(f"{t:28} {c['rows']:>10} {'' if c['active'] is None else c['active']:>10}")


if __name__ == "__main__":
    sys.exit(main())
