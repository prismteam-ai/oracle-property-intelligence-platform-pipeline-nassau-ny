"""Derived Oyster Bay permit tables (rebuilt in full from ``etrakit_*`` source tables; idempotent).

- ``etrakit_permit_facts``: roofing classification (basis + confidence), is_open / years_open / past_expiration,
  close date, and the normalized lot key.
- ``etrakit_permit_property_link``: permit -> property by section-block-lot (exact lot key; multiple candidates are
  recorded as ``ambiguous`` and never used for roof age). ``etrakit_unmatched_sbl`` lists permits with no property.
- ``property_roof_age``: latest *closed* roofing permit where one exists, else the NSI census-block estimate, with the
  basis labelled on every row. An open roofing permit, or a roofing keyword alone, is not proof of a completed roof.
"""
from __future__ import annotations

import gzip
import json
from datetime import date

from . import config, permit_classify as C


def _rows(con, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def build(con, run_id: str, as_of: date, roof_threshold: int) -> dict:
    permits = _rows(con, "SELECT * FROM etrakit_permits WHERE is_active")
    contacts: dict[str, list] = {}
    for r in _rows(con, "SELECT permit_no, seq, contact_type, name FROM etrakit_contacts WHERE is_active ORDER BY permit_no, seq"):
        contacts.setdefault(r["permit_no"], []).append({"contact_type": r["contact_type"], "name": r["name"]})
    insp: dict[str, list] = {}
    for r in _rows(con, "SELECT permit_no, seq, type, result, completed_date FROM etrakit_inspections WHERE is_active ORDER BY permit_no, seq"):
        insp.setdefault(r["permit_no"], []).append({"type": r["type"], "result": r["result"],
                                                    "completed_date": str(r["completed_date"]) if r["completed_date"] else None})
    facts = []
    for p in permits:
        detail = {k: p.get(k) for k in ("permit_type", "permit_subtype", "job_description", "status")}
        for k in ("applied_date", "issued_date", "finaled_date", "expiration_date"):
            detail[k] = str(p[k]) if p.get(k) else None
        detail["contacts"] = contacts.get(p["permit_no"], [])
        detail["inspections"] = insp.get(p["permit_no"], [])
        detail["fees"] = json.loads(p["fees_json"]) if p.get("fees_json") else []
        roof = C.classify_roofing(detail)
        st = C.derive_status(detail, as_of)
        lk, suffix = C.lot_key(p.get("grid_sbl"))
        trades = [c for c in detail["contacts"] if (c["contact_type"] or "").upper() not in ("OWNER", "APPLICANT")]
        facts.append({
            "permit_no": p["permit_no"], "block_key": p["block_key"], "grid_sbl": p["grid_sbl"], "lot_key": lk, "lot_suffix_text": suffix,
            "permit_type": p["permit_type"], "permit_subtype": p["permit_subtype"], "status": p["status"],
            "applied_date": detail["applied_date"], "issued_date": detail["issued_date"], "finaled_date": detail["finaled_date"],
            "is_roofing": roof["is_roofing"], "roofing_confidence": roof["confidence"], "roofing_basis_summary": roof["basis_summary"],
            "roofing_bases_json": json.dumps(roof["bases"]), "trade_contacts_json": json.dumps(trades),
            **{k: v for k, v in st.items()}, "as_of": as_of.isoformat(), "derived_run": run_id})
    out = config.DATA_DIR / "stage" / "etrakit_facts.ndjson.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out, "wt") as fh:
        for f in facts:
            fh.write(json.dumps(f) + "\n")
    if facts:
        con.execute(f"CREATE OR REPLACE TABLE etrakit_permit_facts AS SELECT * FROM read_ndjson_auto('{out}', compression='gzip')")
    else:
        con.execute("CREATE OR REPLACE TABLE etrakit_permit_facts (permit_no VARCHAR, lot_key VARCHAR, is_roofing BOOLEAN)")

    con.execute("""
    CREATE OR REPLACE TABLE etrakit_permit_property_link AS
    WITH c AS (
      SELECT f.permit_no, rk.record_key AS property_key, rk.sbl_key
      FROM etrakit_permit_facts f
      JOIN roll_keys rk ON rk.lot_key = f.lot_key AND rk.roll_year = '2025'
      JOIN properties p ON p.property_key = rk.record_key AND p.swis_code = '282489'),
    n AS (SELECT permit_no, count(*) AS candidates FROM c GROUP BY 1)
    SELECT c.permit_no, c.property_key, c.sbl_key, n.candidates,
           CASE WHEN n.candidates = 1 THEN 'unique' ELSE 'ambiguous' END AS link_status,
           'section_block_lot_exact' AS method
    FROM c JOIN n USING (permit_no)""")
    con.execute("""
    CREATE OR REPLACE TABLE etrakit_unmatched_sbl AS
    SELECT f.permit_no, f.block_key, f.grid_sbl, f.lot_key,
           CASE WHEN f.lot_key IS NULL THEN 'sbl_unparseable' ELSE 'no_property_with_lot_key' END AS reason
    FROM etrakit_permit_facts f LEFT JOIN etrakit_permit_property_link l USING (permit_no) WHERE l.permit_no IS NULL""")

    build_roof_age(con, run_id, as_of, roof_threshold)
    row = con.execute("""SELECT count(*), count(*) FILTER (WHERE is_roofing), count(*) FILTER (WHERE is_open),
        count(*) FILTER (WHERE is_open AND past_expiration),
        (SELECT count(*) FROM etrakit_permit_property_link WHERE link_status='unique'),
        (SELECT count(DISTINCT permit_no) FROM etrakit_permit_property_link WHERE link_status='ambiguous'),
        (SELECT count(*) FROM etrakit_unmatched_sbl) FROM etrakit_permit_facts""").fetchone()
    return dict(zip(["permits", "roofing", "open", "open_past_expiration", "linked_unique", "linked_ambiguous", "unmatched"], row))


def build_roof_age(con, run_id: str, as_of: date, threshold: int) -> None:
    """property_roof_age: permit-derived where a closed roofing permit is linked, else the NSI estimate (labelled)."""
    if not (con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='etrakit_permit_facts'").fetchone()[0]
            and con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='properties'").fetchone()[0]):
        return
    y = as_of.year
    con.execute(f"""
    CREATE OR REPLACE TABLE property_roof_age AS
    WITH roof AS (
      SELECT l.property_key, f.permit_no, f.roofing_confidence, f.status_class, f.is_open, f.years_open, f.past_expiration,
             coalesce(f.finaled_date, f.close_date, f.issued_date) AS event_date,
             CASE WHEN f.finaled_date IS NOT NULL THEN 'finaled_date' WHEN f.close_date IS NOT NULL THEN f.close_date_basis
                  ELSE 'issued_date_of_closed_permit' END AS event_basis
      FROM etrakit_permit_facts f JOIN etrakit_permit_property_link l USING (permit_no)
      WHERE f.is_roofing AND l.link_status = 'unique'),
    done AS (SELECT * , row_number() OVER (PARTITION BY property_key ORDER BY event_date DESC, permit_no DESC) AS rn
             FROM roof WHERE status_class = 'closed' AND event_date IS NOT NULL),
    opn AS (SELECT property_key, count(*) AS open_roofing_permits, max(years_open) AS longest_open_roofing_years,
                   min(permit_no) AS an_open_roofing_permit FROM roof WHERE is_open GROUP BY 1)
    SELECT p.property_key, p.print_key_code, p.situs_address,
           CASE WHEN d.permit_no IS NOT NULL THEN 'roofing_permit_closed'
                WHEN p.est_year_built IS NOT NULL THEN 'nsi_census_block_median_estimate' ELSE 'unknown' END AS roof_age_basis,
           CASE WHEN d.permit_no IS NOT NULL THEN CAST(year(CAST(d.event_date AS DATE)) AS INTEGER) ELSE p.est_year_built END AS roof_year,
           CASE WHEN d.permit_no IS NOT NULL THEN {y} - year(CAST(d.event_date AS DATE)) ELSE {y} - p.est_year_built END AS roof_age_years,
           CASE WHEN d.permit_no IS NOT NULL THEN d.roofing_confidence WHEN p.est_year_built IS NOT NULL THEN 'low' END AS confidence,
           CASE WHEN d.permit_no IS NOT NULL THEN ({y} - year(CAST(d.event_date AS DATE))) > {threshold}
                WHEN p.est_year_built IS NOT NULL THEN ({y} - p.est_year_built) > {threshold} END AS roof_older_than_threshold,
           {threshold} AS threshold_years, d.permit_no AS evidence_permit_no, d.event_date AS evidence_date, d.event_basis AS evidence_date_basis,
           p.est_year_built AS nsi_est_year_built, p.est_roof_age_years AS nsi_est_roof_age_years,
           coalesce(o.open_roofing_permits, 0) AS open_roofing_permits, o.longest_open_roofing_years, o.an_open_roofing_permit,
           CASE WHEN d.permit_no IS NOT NULL THEN 'Latest closed roofing permit (classified by portal text/roles, not proof of full replacement).'
                WHEN p.est_year_built IS NOT NULL THEN 'NSI med_yr_blt = census-block median year built; assumes original roof; no permit evidence.'
                ELSE 'No roofing permit and no NSI structure match.' END AS caveat,
           DATE '{as_of.isoformat()}' AS as_of, '{run_id}' AS derived_run
    FROM properties p
    LEFT JOIN done d ON d.property_key = p.property_key AND d.rn = 1
    LEFT JOIN opn o ON o.property_key = p.property_key""")
