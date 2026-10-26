"""DuckDB internal store: run history, source snapshots, and content-hash merges.

This file is the internal reconciliation store (the Oracle "Query DB" role). It is not a
public publication source.

Merge contract (per source table, per scope):
- every staged row gets a deterministic ``record_key`` and ``record_hash`` (md5 over the
  business columns, provenance excluded);
- new key -> insert; same key + different hash -> replace (first_seen_run kept);
  same hash -> only ``last_seen_run`` moves; active key missing from the new snapshot ->
  tombstone (``is_active=false``, ``removed_run``) and a ``tombstones`` row;
- every new/changed/removed key is written to ``record_changes``.
"""
from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager

import duckdb

from . import config

PROVENANCE_COLS = ["_source", "_source_url", "_fetched_at", "_artifact_sha256"]
META_COLS = ["record_key", "record_hash", "first_seen_run", "last_seen_run", "last_changed_run",
             "is_active", "removed_run"]


@contextmanager
def single_writer():
    """One writer per county: an exclusive, non-blocking file lock."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(config.LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another Nassau pipeline writer holds data/.pipeline.lock")
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def connect(path=None) -> duckdb.DuckDBPyConnection:
    config.DUCKDB_EXT_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path or config.DB_PATH))
    con.execute(f"SET extension_directory='{config.DUCKDB_EXT_DIR}'")
    for ext in ("httpfs", "spatial"):
        con.execute(f"INSTALL {ext}; LOAD {ext};")
    con.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id VARCHAR PRIMARY KEY, started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
            status VARCHAR, mode VARCHAR, code_version VARCHAR, parameters JSON,
            sources JSON, record_counts JSON, deltas JSON, limitations JSON, error VARCHAR)""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS source_snapshots (
            run_id VARCHAR, source VARCHAR, scope VARCHAR, watermark JSON, action VARCHAR,
            staged_rows BIGINT, artifact_dir VARCHAR, manifest_sha256 VARCHAR, recorded_at TIMESTAMPTZ)""")
    con.execute("ALTER TABLE source_snapshots ADD COLUMN IF NOT EXISTS delta JSON")
    con.execute("""
        CREATE TABLE IF NOT EXISTS record_changes (
            run_id VARCHAR, table_name VARCHAR, scope VARCHAR, record_key VARCHAR, change_type VARCHAR)""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS tombstones (
            run_id VARCHAR, table_name VARCHAR, scope VARCHAR, record_key VARCHAR, reason VARCHAR,
            last_record_hash VARCHAR, created_at TIMESTAMPTZ DEFAULT now())""")
    return con


def table_exists(con, name: str) -> bool:
    return con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name=?", [name]).fetchone()[0] > 0


def merge(con, run_id: str, table: str, stage_sql: str, key_expr: str, hash_cols: list[str],
          scope_col: str | None = None, scope_val=None, tombstone: bool = True) -> dict:
    """Merge a staged snapshot into ``table``. Returns counts of new/changed/unchanged/removed.

    ``tombstone=False`` is for partial batches (e.g. a permit harvest that covers some blocks per run): rows absent
    from the batch are left untouched instead of being tombstoned."""
    hash_expr = "md5(concat_ws('||', " + ", ".join(f"coalesce(CAST({c} AS VARCHAR), '<NULL>')" for c in hash_cols) + "))"
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _stg AS
        WITH s AS (SELECT *, ({key_expr}) AS _base_key, {hash_expr} AS record_hash FROM ({stage_sql})),
        r AS (SELECT *, count(*) OVER (PARTITION BY _base_key) AS _n,
                        row_number() OVER (PARTITION BY _base_key ORDER BY record_hash) AS _rn FROM s)
        SELECT * EXCLUDE (_base_key, _n, _rn),
               CASE WHEN _n > 1 THEN _base_key || '#' || _rn ELSE _base_key END AS record_key
        FROM r""")
    scope_txt = "" if scope_col is None else f"{scope_col}={scope_val}"
    scope_pred = "TRUE" if scope_col is None else f"{scope_col} = {scope_val!r}" if isinstance(scope_val, str) else f"{scope_col} = {scope_val}"
    if not table_exists(con, table):
        con.execute(f"""CREATE TABLE {table} AS SELECT *, CAST(NULL AS VARCHAR) AS first_seen_run,
                        CAST(NULL AS VARCHAR) AS last_seen_run, CAST(NULL AS VARCHAR) AS last_changed_run,
                        CAST(NULL AS BOOLEAN) AS is_active, CAST(NULL AS VARCHAR) AS removed_run FROM _stg WHERE false""")
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE _cmp AS
            SELECT s.record_key, s.record_hash AS new_hash, t.record_hash AS old_hash, t.is_active AS old_active,
                   t.first_seen_run AS old_first
            FROM _stg s LEFT JOIN (SELECT * FROM {table} WHERE {scope_pred}) t USING (record_key)""")
        new = con.execute("SELECT count(*) FROM _cmp WHERE old_hash IS NULL").fetchone()[0]
        changed = con.execute("SELECT count(*) FROM _cmp WHERE old_hash IS NOT NULL AND (old_hash <> new_hash OR NOT old_active)").fetchone()[0]
        unchanged = con.execute("SELECT count(*) FROM _cmp WHERE old_hash = new_hash AND old_active").fetchone()[0]
        # replace changed rows, keep first_seen_run
        con.execute(f"""DELETE FROM {table} WHERE {scope_pred} AND record_key IN
                        (SELECT record_key FROM _cmp WHERE old_hash IS NOT NULL AND (old_hash <> new_hash OR NOT old_active))""")
        con.execute(f"""
            INSERT INTO {table} BY NAME
            SELECT s.*, coalesce(c.old_first, '{run_id}') AS first_seen_run, '{run_id}' AS last_seen_run,
                   '{run_id}' AS last_changed_run, TRUE AS is_active, CAST(NULL AS VARCHAR) AS removed_run
            FROM _stg s JOIN _cmp c USING (record_key)
            WHERE c.old_hash IS NULL OR c.old_hash <> c.new_hash OR NOT c.old_active""")
        con.execute(f"""UPDATE {table} SET last_seen_run='{run_id}' WHERE {scope_pred} AND record_key IN
                        (SELECT record_key FROM _cmp WHERE old_hash = new_hash AND old_active)""")
        gone_pred = f"{scope_pred} AND is_active AND record_key NOT IN (SELECT record_key FROM _stg)" if tombstone else "FALSE"
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE _gone AS SELECT record_key, record_hash FROM {table}
            WHERE {gone_pred}""")
        removed = con.execute("SELECT count(*) FROM _gone").fetchone()[0]
        con.execute(f"""INSERT INTO tombstones (run_id, table_name, scope, record_key, reason, last_record_hash)
                        SELECT '{run_id}', '{table}', '{scope_txt}', record_key, 'absent-from-source-snapshot', record_hash FROM _gone""")
        con.execute(f"""UPDATE {table} SET is_active=false, removed_run='{run_id}'
                        WHERE {scope_pred} AND record_key IN (SELECT record_key FROM _gone)""")
        con.execute(f"""
            INSERT INTO record_changes
            SELECT '{run_id}', '{table}', '{scope_txt}', record_key,
                   CASE WHEN old_hash IS NULL THEN 'new' ELSE 'changed' END
            FROM _cmp WHERE old_hash IS NULL OR old_hash <> new_hash OR NOT old_active
            UNION ALL SELECT '{run_id}', '{table}', '{scope_txt}', record_key, 'removed' FROM _gone""")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    staged = con.execute("SELECT count(*) FROM _stg").fetchone()[0]
    return {"staged": staged, "new": new, "changed": changed, "unchanged": unchanged, "removed": removed}


def record_snapshot(con, run_id, source, scope, watermark, action, staged_rows=None, artifact_dir=None,
                    manifest_sha256=None, delta=None):
    con.execute("""INSERT INTO source_snapshots (run_id, source, scope, watermark, action, staged_rows, artifact_dir,
                   manifest_sha256, recorded_at, delta) VALUES (?,?,?,?,?,?,?,?, now(), ?)""",
                [run_id, source, scope, json.dumps(watermark), action, staged_rows, artifact_dir, manifest_sha256,
                 json.dumps(delta, default=str) if delta is not None else None])


def completed_step(con, run_id: str, source: str, scope: str, table: str, change_scope: str):
    """For a resumed run: the delta of a step already merged earlier in the same run, or None."""
    row = con.execute("""SELECT staged_rows, delta FROM source_snapshots
                         WHERE run_id=? AND source=? AND scope=? AND action IN ('fetched','skipped-unchanged')
                         ORDER BY recorded_at DESC LIMIT 1""", [run_id, source, scope]).fetchone()
    if not row:
        return None
    if row[1]:
        return json.loads(row[1])
    counts = dict(con.execute("""SELECT change_type, count(*) FROM record_changes
                                 WHERE run_id=? AND table_name=? AND scope=? GROUP BY 1""",
                              [run_id, table, change_scope]).fetchall())
    staged = row[0] or 0
    new, changed, removed = counts.get("new", 0), counts.get("changed", 0), counts.get("removed", 0)
    return {"staged": staged, "new": new, "changed": changed, "unchanged": staged - new - changed,
            "removed": removed, "action": "fetched"}


def last_watermark(con, source: str, scope: str):
    row = con.execute("""SELECT s.watermark FROM source_snapshots s JOIN pipeline_runs r USING (run_id)
                         WHERE s.source=? AND s.scope=? AND r.status='succeeded' AND s.action IN ('fetched','skipped-unchanged')
                         ORDER BY s.recorded_at DESC LIMIT 1""", [source, scope]).fetchone()
    return json.loads(row[0]) if row else None
