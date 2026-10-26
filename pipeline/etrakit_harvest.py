"""Oyster Bay eTRAKiT harvest engine (checkpointed, resumable) and CLI.

    python -m pipeline.etrakit_harvest scope-export          # DuckDB scope -> data/etrakit/scope_blocks.json
    python -m pipeline.etrakit_harvest sample --blocks 16    # count-only search sample for size estimate
    python -m pipeline.etrakit_harvest pilot --permits 25    # capture the first N permits of the nearest block
    python -m pipeline.etrakit_harvest harvest               # full scope (run detached with scripts/etrakit_harvest.sh)

Checkpointing: one JSON file per block under data/etrakit/blocks/ (permits, queries, failures, status),
rewritten atomically after every captured permit. A re-run resumes: captured permits are skipped and
blocks marked ``complete`` are not revisited. The harvester never opens the DuckDB file while running
(a later ``pipeline.etrakit_load`` step merges block files), so it never blocks other pipeline writers.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import random
import signal
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, etrakit_parse as P
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout

from .etrakit import FIELD_SBL, SEARCH_URL, DetailStateLost, EtrakitClient, HarvestError
from .etrakit_session import LoginFailed, PortalBlocked
from .fetch import ArtifactStore, utcnow

SOURCE = "oyster_bay_etrakit"
ROOT = config.DATA_DIR / "etrakit"
BLOCK_DIR = ROOT / "blocks"
SCOPE_JSON = ROOT / "scope_blocks.json"
STATUS_JSON = ROOT / "status.json"
PID_FILE = ROOT / "harvest.pid"
MAX_PAGES_PER_QUERY = 10      # safety valve; the portal shows at most ~5 pages
MAX_LOT_PREFIX_DIGITS = 4
RESULT_CAP = 100              # observed: a query never returns more than 100 rows (5 pages x 20)


class _Budget(Exception):
    pass


class _Stopped(Exception):
    pass


def slug(block_key: str) -> str:
    return block_key.strip("-").replace("/", "_")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    tmp.replace(path)


class Harvester:
    def __init__(self, client: EtrakitClient, run_id: str, log=print, permit_budget: int | None = None):
        self.client, self.run_id, self.log = client, run_id, log
        self.store = ArtifactStore(SOURCE, run_id)
        self.budget = permit_budget
        self.captured_this_run = 0
        self.stop = False
        self.totals = {"permits": 0, "blocks_complete": 0, "queries": 0, "failures": 0}

    # ------------------------------------------------------------ one block
    def block_path(self, block_key: str) -> Path:
        return BLOCK_DIR / f"{slug(block_key)}.json"

    def load_block(self, block_key: str) -> dict:
        p = self.block_path(block_key)
        if p.exists():
            return json.loads(p.read_text())
        return {"block_key": block_key, "status": "new", "permits": {}, "queries": [], "failures": []}

    def harvest_block(self, block_key: str) -> dict:
        st = self.load_block(block_key)
        if st["status"] == "complete":
            return st
        st.update(run_id=self.run_id, started_at=st.get("started_at") or now_iso())
        try:
            self._run_query(st, block_key, "BEGINS WITH", 0)
            truncated_open = [q for q in st["queries"] if q.get("unresolved")]
            st["status"] = "complete" if not st["failures"] and not truncated_open else "incomplete"
        except _Budget:
            st["status"] = "limited"
        except _Stopped:
            st["status"] = "interrupted"
        st["finished_at"] = now_iso()
        top = st["queries"][0] if st["queries"] else {}
        st["reported_total"] = top.get("total")
        st["unique_permits"] = len(st["permits"])
        st["shortfall"] = max(0, (top.get("total") or 0) - len(st["permits"]))   # records the portal counts but never returns
        st["capped_queries"] = [q["value"] for q in st["queries"] if q.get("cap_hit")]
        write_json(self.block_path(block_key), st)
        return st

    def _grid_artifact(self, st: dict, value: str, oper: str, page_no: int, raw: str) -> dict:
        qn = len(st["queries"]) + 1
        url = f"{SEARCH_URL}#search={FIELD_SBL}|{oper}|{value}|page={page_no}"
        art = self.store.put(f"{slug(st['block_key'])}__q{qn}_p{page_no}.grid", url, raw.encode(), utcnow())
        return {"path": art.path, "sha256": art.sha256, "url": art.url, "fetched_at": art.fetched_at}

    def _run_query(self, st: dict, value: str, oper: str, depth: int) -> None:
        raw, g = self.client.search(FIELD_SBL, oper, value)
        q = {"value": value, "operator": oper, "total": g["total"], "returned": g["returned"], "pages": g["pages"],
             "truncated": bool(g["total"] and g["returned"] and g["returned"] < g["total"]), "rows_seen": 0,
             "new_permits": 0, "ran_at": now_iso(), "grid_artifacts": []}
        # Two kinds of truncation, measured in the pilot/sample: (1) the 100-row cap (returned == 100) which
        # lot-prefix splitting recovers; (2) a small "short by N" gap (e.g. 53 of 55) that persists even for tiny
        # queries and is NOT recovered by splitting (children summed to the same 53). Only (1) is split.
        q["cap_hit"] = bool(g["returned"] and g["returned"] >= RESULT_CAP)
        q["short_by"] = (g["total"] - g["returned"]) if q["truncated"] else 0
        st["queries"].append(q)
        self.totals["queries"] += 1
        page_no = 1
        while True:
            q["grid_artifacts"].append(self._grid_artifact(st, value, oper, page_no, raw))
            for row in g["rows"]:
                q["rows_seen"] += 1
                if row["permit_no"] in st["permits"] and st["permits"][row["permit_no"]].get("detail"):
                    continue
                self._capture(st, q, row, value, oper, page_no)
            if g["pages"] and g["page"] and g["page"] < g["pages"] and page_no < MAX_PAGES_PER_QUERY:
                raw, g = self.client.next_page()
                page_no += 1
            else:
                break
        if q["total"] is None:
            q["total"] = q["returned"] = q["rows_seen"]
        # truncated result ("returned N of M"): split the lot-number prefix until every record is reachable
        if q["cap_hit"]:
            digits = len(value.rsplit("-", 1)[1]) if not value.endswith("-") else 0
            if oper == "EQUALS" or digits >= MAX_LOT_PREFIX_DIGITS:   # a single lot with >= 100 permits cannot be split by prefix
                q["unresolved"] = True
                self.log(f"  UNRESOLVED truncated query {value!r}: {q['returned']} of {q['total']}")
                return
            children = []
            if not value.endswith("-"):
                children.append(("EQUALS", value))
            start = "0" if not value.endswith("-") else "1"
            children += [("BEGINS WITH", value + str(d)) for d in range(int(start), 10)]
            for c_oper, c_val in children:
                self._run_query(st, c_val, c_oper, depth + 1)

    def _recover_grid(self, oper: str, value: str, page_no: int) -> None:
        """Re-run the current query and return to the page being walked (row indexes stay valid on that page)."""
        self.log(f"  results grid lost; re-running {value!r} and returning to page {page_no}")
        self.client.search(FIELD_SBL, oper, value)
        for _ in range(page_no - 1):
            self.client.next_page()

    def _capture(self, st: dict, q: dict, row: dict, value: str, oper: str, page_no: int) -> None:
        if self.stop:
            raise _Stopped()
        if self.budget is not None and self.captured_this_run >= self.budget:
            raise _Budget()
        try:
            html = self.client.open_detail(row["row_index"], row["permit_no"])
            d = P.parse_detail(html)
            if d["incomplete"]:   # tabs/rows not rendered yet: re-open this permit once
                html = self.client.open_detail(row["row_index"], row["permit_no"])
                d = P.parse_detail(html)
        except HarvestError as exc:
            # one permit failed after its retries: record it (a later run retries it, since it has no saved detail) and move on
            st["failures"].append({"permit_no": row["permit_no"], "stage": "detail", "error": str(exc)[:200], "at": now_iso()})
            self.totals["failures"] += 1
            if isinstance(exc, DetailStateLost):
                self._recover_grid(oper, value, page_no)   # without the grid, every following row click would fail too
            return
        fetched_at = utcnow()
        url = f"{SEARCH_URL}#search={FIELD_SBL}|{oper}|{value}|page={page_no}|permit={row['permit_no']}"
        art = self.store.put(f"{slug(st['block_key'])}__{slug(row['permit_no'])}.detail", url, html.encode(), fetched_at)
        problems = []
        if d["incomplete"]:
            problems.append(f"detail incomplete after re-open: tab counts {d['tab_counts']} vs contacts={len(d['contacts'])} inspections={len(d['inspections'])}")
        if d["permit_no_in_panel"] and d["permit_no_in_panel"] != row["permit_no"]:
            problems.append(f"detail panel is for {d['permit_no_in_panel']}, clicked {row['permit_no']}")
        if d["site_parid"] and row["sbl"] and d["site_parid"].strip() != row["sbl"].strip():   # an empty PARID is data, not a mismatch
            problems.append(f"site PARID {d['site_parid']!r} differs from grid SBL {row['sbl']!r}")
        if problems:
            st["failures"].append({"permit_no": row["permit_no"], "stage": "verify", "error": "; ".join(problems), "at": now_iso()})
            self.totals["failures"] += 1
        st["permits"][row["permit_no"]] = {
            "permit_no": row["permit_no"], "record_id": row["record_id"], "grid_sbl": row["sbl"], "detail": d,
            "verify_problems": problems, "query": {"operator": oper, "value": value, "page": page_no},
            "raw": {"path": art.path, "sha256": art.sha256, "url": art.url, "fetched_at": art.fetched_at},
            "run_id": self.run_id,
        }
        q["new_permits"] += 1
        self.captured_this_run += 1
        self.totals["permits"] += 1
        if self.captured_this_run % 50 == 0:   # heartbeat: big blocks otherwise run silent for many minutes
            self.log(f"  progress: {st['block_key']} {len(st['permits'])} permits in block, {self.captured_this_run} this run, "
                     f"portal requests={sum(self.client.requests.values())}")
        write_json(self.block_path(st["block_key"]), st)   # checkpoint after every permit


# ---------------------------------------------------------------- status / process helpers
def write_status(h: Harvester, scope_blocks: list[str], done: int, last_block: str | None, started: float, state: str) -> None:
    elapsed = time.time() - started
    reqs = dict(h.client.requests)
    write_json(STATUS_JSON, {
        "state": state, "run_id": h.run_id, "pid": os.getpid(), "updated_at": now_iso(), "elapsed_s": round(elapsed),
        "blocks_total": len(scope_blocks), "blocks_done_this_run": done, "last_block": last_block,
        "permits_captured_this_run": h.captured_this_run, "totals": h.totals, "portal_requests": reqs,
        "portal_requests_total": sum(reqs.values()), "http_statuses": dict(h.client.statuses),
        "eta_hours_remaining": round((len(scope_blocks) - done) * (elapsed / max(done, 1)) / 3600, 1) if done else None})


def load_scope() -> list[dict]:
    if not SCOPE_JSON.exists():
        export_scope()
    return json.loads(SCOPE_JSON.read_text())


def export_scope() -> None:
    import duckdb
    con = duckdb.connect(str(config.DB_PATH), read_only=True)
    rows = con.execute("""SELECT block_search_key, section, block, parcels, parcels_in_radius, round(nearest_miles, 3)
                          FROM etrakit_scope_blocks ORDER BY nearest_miles, block_search_key""").fetchall()
    write_json(SCOPE_JSON, [{"block_key": r[0], "section": r[1], "block": r[2], "parcels": r[3],
                             "parcels_in_radius": r[4], "nearest_miles": r[5]} for r in rows])
    print(f"scope exported: {len(rows)} blocks -> {SCOPE_JSON}")


def make_run_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def log_to(path: Path | None):
    def _log(msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
    return _log


# ---------------------------------------------------------------- commands
def cmd_sample(args) -> int:
    scope = load_scope()
    rnd = random.Random(args.seed)
    by_section: dict[str, list[dict]] = {}
    for b in scope:
        by_section.setdefault(b["section"], []).append(b)
    n = args.blocks
    picks: list[dict] = []
    total_blocks = len(scope)
    for sec, blocks in sorted(by_section.items()):
        k = max(1, round(n * len(blocks) / total_blocks))
        picks += rnd.sample(blocks, min(k, len(blocks)))
    run_id = make_run_id("etrakit-sample")
    log = log_to(None)
    results = []
    try:
        with EtrakitClient() as c:
            log(f"logged in; sampling {len(picks)} blocks (count-only searches)")
            for b in picks:
                r = count_block(c, b["block_key"])
                results.append({**b, **r})
                log(f"  {b['block_key']:10} parcels={b['parcels']:4} permits_total={r['total']:4} returned={r['returned']} pages={r['pages']}")
            split_probe = None
            if args.probe_split:
                split_probe = probe_split(c, args.probe_split, log)
            req = dict(c.requests)
    except (PortalBlocked, LoginFailed, HarvestError) as exc:
        log(f"STOP: {type(exc).__name__}: {exc}")
        return 2
    totals = [r["total"] for r in results]
    per_parcel = [r["total"] / r["parcels"] for r in results]
    mean_b, sd_b = statistics.mean(totals), (statistics.stdev(totals) if len(totals) > 1 else 0)
    est = mean_b * total_blocks
    ci = 1.96 * sd_b / (len(totals) ** 0.5) * total_blocks
    ratio = sum(totals) / sum(r["parcels"] for r in results)
    est_ratio = ratio * sum(b["parcels"] for b in scope)
    summary = {"run_id": run_id, "sample_blocks": len(results), "scope_blocks": total_blocks, "mean_permits_per_block": round(mean_b, 1),
               "sd": round(sd_b, 1), "estimate_by_block_mean": round(est), "ci95_plus_minus": round(ci),
               "permits_per_parcel": round(ratio, 3), "estimate_by_parcel_ratio": round(est_ratio),
               "truncated_blocks": [r["block_key"] for r in results if r["returned"] < r["total"]],
               "max_block_total": max(totals), "portal_requests": req, "split_probe": split_probe, "results": results}
    write_json(ROOT / f"sample_{run_id}.json", summary)
    log("SAMPLE SUMMARY " + json.dumps({k: v for k, v in summary.items() if k != "results"}))
    return 0


def count_block(c: EtrakitClient, block_key: str) -> dict:
    """Count-only: one block search (plus the last page when the count is not in the portal's message)."""
    raw, g = c.search(FIELD_SBL, "BEGINS WITH", block_key)
    if g["total"] is not None:
        total, returned, how = g["total"], g["returned"], "message"
    elif g["pages"] and g["pages"] > 1:
        _, last = c.last_page()
        total = returned = (g["pages"] - 1) * 20 + len(last["rows"]); how = "last-page"
    else:
        total = returned = len(g["rows"]); how = "single-page"
    return {"total": total, "returned": returned, "pages": g["pages"], "how": how, "cap_hit": returned >= RESULT_CAP,
            "short_by": max(0, total - returned), "counted_at": now_iso()}


def cmd_count_all(args) -> int:
    """Resumable count-only pass over every block in the scope -> data/etrakit/block_counts.json."""
    scope = load_scope()
    path = ROOT / "block_counts.json"
    counts = json.loads(path.read_text()) if path.exists() else {}
    todo = [b for b in scope if b["block_key"] not in counts]
    log = log_to(None)
    log(f"count-all: {len(scope)} blocks, {len(counts)} already counted, {len(todo)} to go")
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with EtrakitClient() as c:
            for i, b in enumerate(todo, 1):
                counts[b["block_key"]] = {**count_block(c, b["block_key"]), "parcels": b["parcels"]}
                if i % 10 == 0 or i == len(todo):
                    write_json(path, counts)
                    log(f"  counted {len(counts)}/{len(scope)}  requests={sum(c.requests.values())}")
            write_json(path, counts)
    except (PortalBlocked, LoginFailed, HarvestError) as exc:
        write_json(path, counts)
        log(f"STOP: {type(exc).__name__}: {exc}")
        return 2
    log("count-all complete")
    return 0


def probe_split(c: EtrakitClient, block_key: str, log) -> dict:
    """Count-only: run the block query and its lot-prefix children; the union of permit numbers shows
    whether splitting recovers every record of a truncated ("returned N of M") block."""
    def collect(oper: str, value: str):
        raw, g = c.search(FIELD_SBL, oper, value)
        nums = {r["permit_no"] for r in g["rows"]}
        info = {"value": value, "operator": oper, "total": g["total"], "returned": g["returned"], "pages": g["pages"]}
        while g["pages"] and g["page"] and g["page"] < g["pages"]:
            raw, g = c.next_page()
            nums |= {r["permit_no"] for r in g["rows"]}
        info["permits_seen"] = len(nums)
        return nums, info
    base, base_info = collect("BEGINS WITH", block_key)
    union, children = set(), []
    for d in range(1, 10):
        nums, info = collect("BEGINS WITH", f"{block_key}{d}")
        union |= nums; children.append(info)
        log(f"  split {block_key}{d}: total={info['total']} returned={info['returned']} seen={info['permits_seen']}")
    return {"block": block_key, "base": base_info, "children": children, "union_children": len(union),
            "union_all": len(union | base), "recovered_all": (base_info["total"] is not None and len(union | base) >= base_info["total"])}


BATCH_EVERY_BLOCKS = 20


def _load_into_db(run_id: str, n: int, log) -> None:
    """One pipeline_runs row per batch. Never lets a DB problem stop the harvest (block files are the checkpoint)."""
    try:
        from . import etrakit_load
        etrakit_load.load_batch(f"{run_id}-b{n:03d}", harvest_run=run_id, log=log)
    except BaseException as exc:   # noqa: BLE001 - includes SystemExit from a busy single-writer lock
        log(f"batch load {n} skipped ({type(exc).__name__}: {str(exc)[:160]}); block files are intact and will load next batch")


def run_batch(blocks: list[dict], run_id: str, budget: int | None, log) -> int:
    started = time.time()
    done = 0
    batches = 0
    state = "running"
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    try:
        with EtrakitClient() as c:
            h = Harvester(c, run_id, log=log, permit_budget=budget)
            signal.signal(signal.SIGTERM, lambda *_: setattr(h, "stop", True))
            signal.signal(signal.SIGINT, lambda *_: setattr(h, "stop", True))
            log(f"run {run_id}: logged in; {len(blocks)} blocks queued; permit budget={budget}")
            keys = [b["block_key"] for b in blocks]
            for b in blocks:
                if h.stop:
                    state = "stopped"
                    break
                before = h.captured_this_run
                st = h.harvest_block(b["block_key"])
                done += 1
                if st["status"] == "complete":
                    h.totals["blocks_complete"] += 1
                log(f"block {b['block_key']} -> {st['status']}: permits={len(st['permits'])} (+{h.captured_this_run - before} new) "
                    f"queries={len(st['queries'])} failures={len(st['failures'])} reqs={sum(c.requests.values())}")
                write_status(h, keys, done, b["block_key"], started, "running")
                if st["status"] == "limited":
                    break
                if done % BATCH_EVERY_BLOCKS == 0:
                    batches += 1
                    _load_into_db(run_id, batches, log)
            else:
                state = "finished"
            write_status(h, keys, done, blocks[min(done, len(blocks)) - 1]["block_key"] if blocks else None, started, state)
            log(f"run {run_id}: {state}; permits={h.captured_this_run} requests={dict(c.requests)} statuses={dict(c.statuses)}")
            batches += 1
            _load_into_db(run_id, batches, log)
            return 0
    except PortalBlocked as exc:
        log(f"STOP (portal block, not retried): {exc}")
        _load_into_db(run_id, batches + 1, log)
        write_json(STATUS_JSON, {"state": "blocked", "run_id": run_id, "reason": str(exc), "updated_at": now_iso()})
        return 3
    except LoginFailed as exc:
        log(f"STOP (login): {exc}")
        write_json(STATUS_JSON, {"state": "login_failed", "run_id": run_id, "reason": str(exc), "updated_at": now_iso()})
        return 2
    except HarvestError as exc:
        log(f"STOP (action failed after retries): {exc}")
        write_json(STATUS_JSON, {"state": "failed", "run_id": run_id, "reason": str(exc), "updated_at": now_iso()})
        return 4
    except (PWTimeout, PWError) as exc:   # a Playwright call outside the per-action retries: transient, so restart the session
        log(f"STOP (unhandled Playwright error, restarting session): {type(exc).__name__}: {str(exc)[:200]}")
        _load_into_db(run_id, batches + 1, log)
        write_json(STATUS_JSON, {"state": "failed", "run_id": run_id, "reason": f"{type(exc).__name__}: {str(exc)[:200]}", "updated_at": now_iso()})
        return 4
    finally:
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()


def harvest_blocks() -> list[dict]:
    p = ROOT / "harvest_blocks.json"
    return json.loads(p.read_text()) if p.exists() else load_scope()


def supervised(blocks: list[dict], run_id: str, log, max_restarts: int = 6) -> int:
    """Run the harvest; restart (new session, resume from block files) after transient failures only."""
    for attempt in range(max_restarts + 1):
        code = run_batch(blocks, run_id, None, log)
        if code != 4:          # 0 finished/stopped, 2 login, 3 portal block: never retried around
            return code
        wait = min(1800, 120 * 2 ** attempt)
        log(f"transient failure; restart {attempt + 1}/{max_restarts} in {wait}s (completed blocks are skipped)")
        time.sleep(wait)
    log("giving up after repeated transient failures")
    return 4


def cmd_start(args) -> int:
    """Launch the harvest fully detached from this terminal/session (new session, nohup semantics)."""
    import subprocess
    if PID_FILE.exists():
        try:
            os.kill(int(PID_FILE.read_text()), 0)
            print(f"already running (pid {PID_FILE.read_text().strip()})"); return 1
        except (ProcessLookupError, ValueError):
            PID_FILE.unlink()
    logf = open(config.DATA_DIR / "logs" / "etrakit.log", "ab")
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(config.REPO_ROOT / ".playwright-browsers"))
    proc = subprocess.Popen([sys.executable, "-u", "-m", "pipeline.etrakit_harvest", "harvest"], cwd=config.REPO_ROOT, env=env,
                            stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(proc.pid))
    print(f"started pid {proc.pid}; log data/logs/etrakit.log; pid file {PID_FILE}")
    return 0


def cmd_stop(args) -> int:
    if not PID_FILE.exists():
        print("no pid file; not running"); return 1
    pid = int(PID_FILE.read_text())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"sent SIGTERM to {pid}: it finishes the current permit, checkpoints, loads a final batch, and exits")
    except ProcessLookupError:
        print("process not found; removing stale pid file"); PID_FILE.unlink()
    return 0


def cmd_reparse(args) -> int:
    """Offline: re-run the detail parser over the saved raw HTML of every captured permit and rebuild verify problems."""
    changed = 0
    for bf in sorted(BLOCK_DIR.glob("*.json")):
        st = json.loads(bf.read_text())
        dirty = False
        for no, p in st["permits"].items():
            html = gzip.open(p["raw"]["path"]).read().decode()
            d = P.parse_detail(html)
            problems = []
            if d["incomplete"]:
                problems.append(f"detail incomplete: tab counts {d['tab_counts']} vs contacts={len(d['contacts'])} inspections={len(d['inspections'])}")
            if d["permit_no_in_panel"] and d["permit_no_in_panel"] != no:
                problems.append(f"detail panel is for {d['permit_no_in_panel']}, clicked {no}")
            if d["site_parid"] and p["grid_sbl"] and d["site_parid"].strip() != p["grid_sbl"].strip():
                problems.append(f"site PARID {d['site_parid']!r} differs from grid SBL {p['grid_sbl']!r}")
            if d != p["detail"] or problems != p["verify_problems"]:
                p["detail"], p["verify_problems"] = d, problems
                dirty = True
        if dirty:
            keep = [f for f in st["failures"] if f.get("stage") != "verify"]
            keep += [{"permit_no": no, "stage": "verify", "error": "; ".join(p["verify_problems"]), "at": now_iso()}
                     for no, p in st["permits"].items() if p["verify_problems"]]
            st["failures"] = keep
            if st["status"] == "incomplete" and not keep and not [q for q in st["queries"] if q.get("unresolved")]:
                st["status"] = "complete"
            write_json(bf, st)
            changed += 1
    print(f"reparse: {changed} block files updated")
    return 0


def cmd_status(args) -> int:
    alive = False
    if PID_FILE.exists():
        try:
            os.kill(int(PID_FILE.read_text()), 0); alive = True
        except (ProcessLookupError, ValueError):
            pass
    st = json.loads(STATUS_JSON.read_text()) if STATUS_JSON.exists() else {}
    log = config.DATA_DIR / "logs" / "etrakit.log"
    age = round(time.time() - log.stat().st_mtime) if log.exists() else None
    done = len(list(BLOCK_DIR.glob("*.json")))
    print(json.dumps({"alive": alive, "pid": PID_FILE.read_text().strip() if PID_FILE.exists() else None, "block_files": done,
                      "log_last_write_seconds_ago": age, **{k: st.get(k) for k in ("state", "run_id", "blocks_total", "blocks_done_this_run",
                      "last_block", "permits_captured_this_run", "portal_requests_total", "http_statuses", "eta_hours_remaining", "updated_at", "reason")}}, indent=1))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scope-export")
    sub.add_parser("count-all")
    s = sub.add_parser("sample"); s.add_argument("--blocks", type=int, default=16); s.add_argument("--seed", type=int, default=7); s.add_argument("--probe-split", default=None)
    p = sub.add_parser("pilot"); p.add_argument("--permits", type=int, default=25); p.add_argument("--block", default=None)
    h = sub.add_parser("harvest"); h.add_argument("--limit-blocks", type=int, default=None)
    sub.add_parser("start"); sub.add_parser("stop"); sub.add_parser("status"); sub.add_parser("reparse")
    args = ap.parse_args(argv)
    log = log_to(None)
    if args.cmd == "scope-export":
        export_scope(); return 0
    if args.cmd == "sample":
        return cmd_sample(args)
    if args.cmd == "count-all":
        return cmd_count_all(args)
    if args.cmd == "start":
        return cmd_start(args)
    if args.cmd == "stop":
        return cmd_stop(args)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "reparse":
        return cmd_reparse(args)
    scope = load_scope()
    if args.cmd == "pilot":
        blocks = [b for b in scope if b["block_key"] == args.block] if args.block else scope[:1]
        return run_batch(blocks, make_run_id("etrakit-pilot"), args.permits, log)
    blocks = harvest_blocks()
    blocks = blocks[: args.limit_blocks] if args.limit_blocks else blocks
    return supervised(blocks, make_run_id("etrakit-harvest"), log)


if __name__ == "__main__":
    sys.exit(main())
