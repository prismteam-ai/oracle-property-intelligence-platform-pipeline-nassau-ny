"""Derive the Oyster Bay eTRAKiT demo scope from the ``properties`` table (offline, idempotent).

Scope: Town of Oyster Bay unincorporated parcels (SWIS 282489 = "Oyster Bay" outside villages;
incorporated villages issue their own permits and are not in this portal) with a point within
RADIUS_MI of Hicksville (40.7684, -73.5251). Whole section-blocks are taken: every block that
contains at least one in-radius parcel is searched in full, so parcels just outside the circle
but in the same block are included.

Coordinates: NYS centroid lat/lon, else GIS polygon point-on-surface (``coalesce(lat, polygon_lat)``).
Parcels with neither (about 0.8% of SWIS 282489) cannot be placed and are listed as unplaced.

eTRAKiT search key: ``<section>-<block>-`` (operator-verified form, e.g. ``46-334-``); a block that
returns the 100-result cap is split by lot prefix (``46-334-1`` ... ``46-334-9``).
Print key -> eTRAKiT: ``46.-334-12`` -> section 46, block 334, lot 12. Padding of the lot and the
handling of ``.A`` and ``/n`` suffixes are UNVERIFIED until a logged-in probe confirms them.

    .venv/bin/python -m pipeline.etrakit_scope
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import config, db

CENTER = (40.7684, -73.5251)  # Hicksville
RADIUS_MI = 1.0   # operator decision 2026-10-25: 3 mi estimated 62k-87k permits (50-75 h), too long
SWIS = "282489"


def build(con, run_id: str, radius: float = RADIUS_MI) -> dict:
    RADIUS = radius
    lat0, lon0 = CENTER
    con.execute(f"""
    CREATE OR REPLACE TABLE etrakit_scope_parcels AS
    WITH p AS (
      SELECT property_key, print_key_code, sbl_key, property_class, situs_address,
             coalesce(lat, polygon_lat) AS lat, coalesce(lon, polygon_lon) AS lon,
             regexp_extract(print_key_code, '^([0-9]+)\\.-([A-Za-z0-9]+)-([0-9]+)(.*)$', ['sec','blk','lot','rest']) AS k
      FROM properties WHERE swis_code = '{SWIS}'),
    d AS (
      SELECT *, 3958.8 * 2 * asin(sqrt(pow(sin(radians(lat - {lat0}) / 2), 2) +
             cos(radians({lat0})) * cos(radians(lat)) * pow(sin(radians(lon - ({lon0})) / 2), 2))) AS miles_from_hicksville
      FROM p WHERE lat IS NOT NULL),
    blocks AS (SELECT DISTINCT k.sec AS sec, k.blk AS blk FROM d WHERE miles_from_hicksville <= {RADIUS})
    SELECT p.property_key, p.print_key_code, p.sbl_key, p.property_class, p.situs_address,
           p.k.sec AS section, p.k.blk AS block, p.k.lot AS lot, p.k.rest AS lot_suffix,
           p.k.sec || '-' || p.k.blk || '-' AS block_search_key,
           p.k.sec || '-' || p.k.blk || '-' || p.k.lot AS etrakit_sbl_guess,
           d.miles_from_hicksville, (d.miles_from_hicksville <= {RADIUS}) AS in_radius,
           '{run_id}' AS derived_run
    FROM p JOIN blocks b ON b.sec = p.k.sec AND b.blk = p.k.blk
    LEFT JOIN d USING (property_key)""")
    con.execute("""
    CREATE OR REPLACE TABLE etrakit_scope_blocks AS
    SELECT section, block, block_search_key, count(*) AS parcels, count(*) FILTER (WHERE in_radius) AS parcels_in_radius,
           min(miles_from_hicksville) AS nearest_miles, CAST(NULL AS VARCHAR) AS harvest_status
    FROM etrakit_scope_parcels GROUP BY 1, 2, 3""")
    out = con.execute("""SELECT count(*), count(*) FILTER (WHERE in_radius), count(DISTINCT block_search_key),
                         count(DISTINCT section), count(*) FILTER (WHERE miles_from_hicksville IS NULL)
                         FROM etrakit_scope_parcels""").fetchone()
    return {"parcels": out[0], "parcels_in_radius": out[1], "blocks": out[2], "sections": out[3], "unplaced_parcels_in_blocks": out[4],
            "center": CENTER, "radius_miles": radius, "swis": SWIS}


def record_run(con, run_id: str, mode: str, status: str, deltas: dict, limitations: list[str]) -> None:
    con.execute("DELETE FROM pipeline_runs WHERE run_id=?", [run_id])
    con.execute("""INSERT INTO pipeline_runs (run_id, started_at, finished_at, status, mode, code_version, parameters,
                   sources, record_counts, deltas, limitations) VALUES (?, now(), now(), ?, ?, ?, ?, ?, ?, ?, ?)""",
                [run_id, status, mode, db_git(), json.dumps({"stage": mode}), json.dumps([{"source": "oys-trk.aspgov.com eTRAKiT"}]),
                 json.dumps({}), json.dumps(deltas, default=str), json.dumps(limitations)])


def db_git() -> str:
    import subprocess
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=config.REPO_ROOT, text=True).strip()
    except Exception:
        return "unknown"


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--radius", type=float, default=RADIUS_MI)
    ap.add_argument("--archive-previous-as", default=None, help="copy the current scope tables to *_<suffix> first, e.g. r3")
    args = ap.parse_args(argv)
    run_id = "etrakit-scope-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with db.single_writer():
        con = db.connect()
        if args.archive_previous_as and db.table_exists(con, "etrakit_scope_blocks"):
            for t in ("etrakit_scope_parcels", "etrakit_scope_blocks"):
                con.execute(f"CREATE OR REPLACE TABLE {t}_{args.archive_previous_as} AS SELECT * FROM {t}")
        stats = build(con, run_id, args.radius)
        record_run(con, run_id, "etrakit-scope", "succeeded", {"scope": stats}, [
            f"Scope is whole section-blocks containing at least one parcel within {args.radius} miles of Hicksville (SWIS 282489 only).",
            "eTRAKiT SBL formatting (lot padding, '.A' and '/n' suffixes) is unverified until a logged-in probe.",
        ])
        con.execute("CHECKPOINT")
    print(json.dumps(stats, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
