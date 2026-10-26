"""Deterministic derived tables, rebuilt from the active source rows.

Derived tables are rebuilt in full whenever any source changed (or on --full-refresh).
They are pure functions of the active source rows plus parameters, so a rebuild is
idempotent. Change detection and history live on the source tables (see db.merge).

Parcel identity: SBL. NYS print key ``S.-B-L[.X][/U]`` and county PARCELKEY ``SS BBB LLLLX``
are both reduced to ``<section int>-<block>-<lot int>-<suffix>``, where block is upper-case
text with leading zeros stripped only when it is all digits. The key is always text and
is never digits-only. Roll suffix rules: letter after '.' -> that letter; none -> '0';
numeric condo group (``.167/1``) -> 'U' (Nassau GIS draws condo lots as suffix U).
"""
from __future__ import annotations

from datetime import date

MACROS = r"""
CREATE OR REPLACE MACRO nz_block(b) AS
    CASE WHEN regexp_matches(trim(b), '^[0-9]+$') THEN CAST(CAST(trim(b) AS BIGINT) AS VARCHAR) ELSE upper(trim(b)) END;
CREATE OR REPLACE MACRO zip5(z) AS
    CASE WHEN length(regexp_replace(coalesce(z, ''), '[^0-9]', '', 'g')) >= 5
         THEN left(regexp_replace(z, '[^0-9]', '', 'g'), 5) END;
"""


def build(con, run_id: str, roof_age_threshold: int, as_of: date) -> dict:
    con.execute(MACROS)

    # ---------------------------------------------------------- parcel keys
    con.execute(r"""
    CREATE OR REPLACE TABLE roll_keys AS
    WITH p AS (
      SELECT record_key, roll_year, swis_code, print_key_code,
             regexp_extract(print_key_code, '^([0-9]+)\.-([A-Za-z0-9]+)-([0-9]+)(.*)$', ['sec','blk','lot','rest']) AS k
      FROM assessment_roll WHERE is_active)
    SELECT record_key, roll_year, swis_code, print_key_code,
           k.sec AS sec, k.blk AS blk, k.lot AS lot, k.rest AS rest,
           regexp_extract(k.rest, '^\.?([A-Za-z0-9]+)', 1) AS sub,
           regexp_extract(k.rest, '/(.*)$', 1) AS unit,
           CASE WHEN k.sec = '' THEN NULL ELSE
             CAST(CAST(k.sec AS INT) AS VARCHAR) || '-' || nz_block(k.blk) || '-' || CAST(CAST(k.lot AS INT) AS VARCHAR) || '-' ||
             CASE WHEN regexp_extract(k.rest, '^\.?([A-Za-z0-9]+)', 1) = '' THEN '0'
                  WHEN regexp_matches(regexp_extract(k.rest, '^\.?([A-Za-z0-9]+)', 1), '^[0-9]+$') THEN 'U'
                  ELSE upper(regexp_extract(k.rest, '^\.?([A-Za-z0-9]+)', 1)) END
           END AS sbl_key,
           CASE WHEN k.sec = '' THEN NULL ELSE
             CAST(CAST(k.sec AS INT) AS VARCHAR) || '-' || nz_block(k.blk) || '-' || CAST(CAST(k.lot AS INT) AS VARCHAR) END AS lot_key
    FROM p""")

    con.execute(r"""
    CREATE OR REPLACE TABLE gis_keys AS
    WITH p AS (
      SELECT record_key, objectid, parid, parcelkey, parcel_address, geometry_geojson,
             regexp_extract(parcelkey, '^\s*(\S+)\s+(\S+)\s+([0-9]{4})(\S?)\s*$', ['sec','blk','lot','sfx']) AS k
      FROM gis_parcels WHERE is_active)
    SELECT record_key, CAST(objectid AS BIGINT) AS objectid, parid, parcelkey, parcel_address, geometry_geojson,
           CASE WHEN k.sec = '' THEN NULL ELSE
             CAST(CAST(k.sec AS INT) AS VARCHAR) || '-' || nz_block(k.blk) || '-' || CAST(CAST(k.lot AS INT) AS VARCHAR) || '-' ||
             CASE WHEN k.sfx = '' THEN '0' ELSE upper(k.sfx) END END AS sbl_key,
           CASE WHEN k.sec = '' THEN NULL ELSE
             CAST(CAST(k.sec AS INT) AS VARCHAR) || '-' || nz_block(k.blk) || '-' || CAST(CAST(k.lot AS INT) AS VARCHAR) END AS lot_key
    FROM p""")

    # one polygon per sbl_key (condo-unit rows share a lot polygon; keep the lowest objectid)
    con.execute("""
    CREATE OR REPLACE TABLE gis_lots AS
    SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY sbl_key ORDER BY objectid) AS rn FROM gis_keys WHERE sbl_key IS NOT NULL)
    WHERE rn = 1""")
    con.execute("""
    CREATE OR REPLACE TABLE parcel_geoms AS
    SELECT record_key AS gis_record_key, parid, sbl_key, lot_key, geom,
           ST_X(ST_PointOnSurface(geom)) AS pos_lon, ST_Y(ST_PointOnSurface(geom)) AS pos_lat,
           ST_XMin(geom) AS xmin, ST_XMax(geom) AS xmax, ST_YMin(geom) AS ymin, ST_YMax(geom) AS ymax,
           ST_Area(geom) AS area_deg2
    FROM (SELECT *, ST_GeomFromGeoJSON(geometry_geojson) AS geom FROM gis_lots WHERE geometry_geojson IS NOT NULL)""")

    # ---------------------------------------------------------- seed + geometry match
    seed_year = "2025"
    con.execute(f"""
    CREATE OR REPLACE TABLE parcel_gis_match AS
    WITH r AS (SELECT * FROM roll_keys WHERE roll_year = '{seed_year}'),
    exact AS (SELECT r.record_key, g.gis_record_key, 'sbl_exact' AS method FROM r JOIN parcel_geoms g ON r.sbl_key = g.sbl_key),
    lot_unique AS (
      SELECT lot_key, any_value(gis_record_key) AS gis_record_key FROM parcel_geoms GROUP BY lot_key HAVING count(*) = 1),
    -- Fallback only when the lot is unique in GIS AND the situs address agrees: run 1 showed
    -- an unguarded lot-only fallback agreed on address for just 1.6% of 1,206 rows.
    fallback AS (
      SELECT r.record_key, l.gis_record_key, 'lot_unique_any_suffix_address_agrees' AS method
      FROM r JOIN lot_unique l ON r.lot_key = l.lot_key
      JOIN assessment_roll a ON a.record_key = r.record_key AND a.roll_year = r.roll_year
      JOIN parcel_geoms pg ON pg.gis_record_key = l.gis_record_key
      JOIN gis_parcels gp ON gp.record_key = pg.gis_record_key AND gp.is_active
      WHERE r.record_key NOT IN (SELECT record_key FROM exact)
        AND trim(concat_ws(' ', a.parcel_address_number, a.parcel_address_street)) <> ''
        AND upper(gp.parcel_address) LIKE upper(trim(concat_ws(' ', a.parcel_address_number, a.parcel_address_street))) || '%')
    SELECT * FROM exact UNION ALL SELECT * FROM fallback""")

    con.execute(f"""
    CREATE OR REPLACE TABLE parcel_centroid_match AS
    WITH r AS (SELECT a.record_key, a.swis_code, a.print_key_code,
                      upper(trim(concat_ws(' ', a.parcel_address_number, a.parcel_address_street, a.parcel_address_suff))) AS addr
               FROM assessment_roll a WHERE a.is_active AND a.roll_year = '{seed_year}'),
    c AS (SELECT record_key AS c_key, SWIS, PRINT_KEY, upper(trim(PARCEL_ADDR)) AS addr, CAST(OBJECTID AS BIGINT) AS oid,
                 CAST(lon AS DOUBLE) AS lon, CAST(lat AS DOUBLE) AS lat FROM nys_centroids WHERE is_active),
    j AS (SELECT r.record_key, c.c_key, c.lon, c.lat,
                 CASE WHEN r.addr = c.addr THEN 'swis_printkey_address' ELSE 'swis_printkey' END AS method,
                 row_number() OVER (PARTITION BY r.record_key ORDER BY (r.addr = c.addr) DESC, c.oid) AS rn
          FROM r JOIN c ON r.swis_code = c.SWIS AND r.print_key_code = c.PRINT_KEY)
    SELECT record_key, c_key, lon, lat, method FROM j WHERE rn = 1""")

    # ---------------------------------------------------------- out-of-area owner
    con.execute("""
    CREATE OR REPLACE TABLE nassau_situs_zips AS
    SELECT DISTINCT regexp_extract(trim(parcel_address), '([0-9]{5})$', 1) AS zip5
    FROM gis_parcels WHERE is_active AND regexp_matches(trim(parcel_address), '[0-9]{5}$')""")

    # ---------------------------------------------------------- ownership history
    con.execute("""
    CREATE OR REPLACE TABLE ownership_history AS
    SELECT swis_code || '|' || print_key_code || '|' || coalesce(parcel_address_number, '') || '|' || coalesce(parcel_address_street, '') AS parcel_hist_key,
           CAST(roll_year AS INT) AS roll_year,
           upper(trim(regexp_replace(concat_ws(' ', primary_owner_last_name, primary_owner_first_name), '\\s+', ' ', 'g'))) AS owner_name_norm,
           nullif(trim(deed_book), '') AS deed_book, nullif(trim(page), '') AS deed_page
    FROM assessment_roll WHERE is_active""")
    con.execute("""
    CREATE OR REPLACE TABLE ownership_events AS
    WITH h AS (SELECT *, lag(owner_name_norm) OVER w AS prev_owner, lag(deed_book) OVER w AS prev_book,
                      lag(deed_page) OVER w AS prev_page, lag(roll_year) OVER w AS prev_year
               FROM ownership_history WINDOW w AS (PARTITION BY parcel_hist_key ORDER BY roll_year))
    SELECT parcel_hist_key, roll_year, prev_year,
           (prev_owner IS NOT NULL AND owner_name_norm IS NOT NULL AND owner_name_norm <> prev_owner) AS owner_changed,
           (prev_book IS NOT NULL AND deed_book IS NOT NULL AND (deed_book <> prev_book OR coalesce(deed_page,'') <> coalesce(prev_page,''))) AS deed_changed,
           deed_book, deed_page
    FROM h WHERE prev_year IS NOT NULL""")
    # Deed-book calibration: Nassau deed books are sequential. Books first seen on a deed
    # change in roll year y were recorded before roll year y. Fit year ~ book on the yearly
    # median new book, then estimate each current book's roll-year-of-first-appearance.
    con.execute("""
    CREATE OR REPLACE TABLE deed_book_calibration AS
    SELECT roll_year, count(*) AS n_changes,
           quantile_cont(TRY_CAST(deed_book AS DOUBLE), 0.5) AS median_new_book,
           quantile_cont(TRY_CAST(deed_book AS DOUBLE), 0.05) AS p05_new_book,
           quantile_cont(TRY_CAST(deed_book AS DOUBLE), 0.95) AS p95_new_book
    FROM ownership_events WHERE deed_changed AND TRY_CAST(deed_book AS DOUBLE) IS NOT NULL
    GROUP BY roll_year ORDER BY roll_year""")
    fit = con.execute("""SELECT regr_slope(roll_year, median_new_book), regr_intercept(roll_year, median_new_book),
                                min(p05_new_book), count(*) FROM deed_book_calibration""").fetchone()
    slope, intercept, min_book, n_years = fit
    slope = slope if slope is not None else "NULL"
    intercept = intercept if intercept is not None else "NULL"
    min_book = min_book if min_book is not None else "NULL"

    con.execute(f"""
    CREATE OR REPLACE TABLE ownership_signals AS
    WITH agg AS (
      SELECT h.parcel_hist_key, count(DISTINCT h.roll_year) AS roll_years_observed,
             min(h.roll_year) AS first_roll_year, max(h.roll_year) AS last_roll_year,
             count(DISTINCT h.owner_name_norm) AS distinct_owner_names
      FROM ownership_history h GROUP BY 1),
    ev AS (
      SELECT parcel_hist_key, sum(CAST(owner_changed AS INT)) AS owner_changes_observed,
             sum(CAST(deed_changed AS INT)) AS deed_changes_observed,
             max(CASE WHEN owner_changed THEN roll_year END) AS last_owner_change_roll_year,
             max(CASE WHEN deed_changed THEN roll_year END) AS last_deed_change_roll_year
      FROM ownership_events GROUP BY 1),
    cur AS (SELECT parcel_hist_key, deed_book, deed_page FROM ownership_history WHERE roll_year = {seed_year})
    SELECT a.*, coalesce(e.owner_changes_observed, 0) AS owner_changes_observed,
           coalesce(e.deed_changes_observed, 0) AS deed_changes_observed,
           e.last_owner_change_roll_year, e.last_deed_change_roll_year,
           c.deed_book AS current_deed_book, c.deed_page AS current_deed_page,
           CASE WHEN a.roll_years_observed < 2 THEN 'insufficient_history'
                WHEN coalesce(e.owner_changes_observed, 0) + coalesce(e.deed_changes_observed, 0) > 0 THEN 'transfer_observed_2021_2025'
                ELSE 'no_change_observed_2021_2025' END AS ownership_signal,
           CASE WHEN TRY_CAST(c.deed_book AS DOUBLE) IS NULL THEN NULL
                ELSE round({slope} * TRY_CAST(c.deed_book AS DOUBLE) + {intercept}, 1) END AS deed_book_est_first_roll_year,
           CASE WHEN TRY_CAST(c.deed_book AS DOUBLE) IS NULL THEN 'no_deed_book'
                WHEN TRY_CAST(c.deed_book AS DOUBLE) >= {min_book} THEN 'calibrated_range'
                ELSE 'extrapolated_before_calibration' END AS deed_book_estimate_basis
    FROM agg a LEFT JOIN ev e USING (parcel_hist_key) LEFT JOIN cur c USING (parcel_hist_key)""")

    # ---------------------------------------------------------- NSI structures -> parcels
    cell = 0.001
    con.execute(f"""
    CREATE OR REPLACE TABLE nsi_parcel_match AS
    WITH pts AS (SELECT fd_id, CAST(x AS DOUBLE) AS x, CAST(y AS DOUBLE) AS y,
                        CAST(floor(CAST(x AS DOUBLE)/{cell}) AS BIGINT) AS cx, CAST(floor(CAST(y AS DOUBLE)/{cell}) AS BIGINT) AS cy
                 FROM nsi_structures WHERE is_active),
    cells AS (SELECT gis_record_key, geom, area_deg2, unnest(range(CAST(floor(xmin/{cell}) AS BIGINT), CAST(floor(xmax/{cell}) AS BIGINT) + 1)) AS cx,
                     ymin, ymax FROM parcel_geoms),
    cells2 AS (SELECT gis_record_key, geom, area_deg2, cx,
                      unnest(range(CAST(floor(ymin/{cell}) AS BIGINT), CAST(floor(ymax/{cell}) AS BIGINT) + 1)) AS cy FROM cells),
    hits AS (SELECT p.fd_id, c.gis_record_key, c.area_deg2
             FROM pts p JOIN cells2 c ON p.cx = c.cx AND p.cy = c.cy
             WHERE ST_Contains(c.geom, ST_Point(p.x, p.y)))
    SELECT fd_id, gis_record_key, 'point_in_polygon' AS method FROM (
      SELECT *, row_number() OVER (PARTITION BY fd_id ORDER BY area_deg2) AS rn FROM hits) WHERE rn = 1""")

    con.execute("""
    CREATE OR REPLACE TABLE parcel_structures AS
    SELECT m.gis_record_key, count(*) AS n_structures,
           arg_max(CAST(s.med_yr_blt AS INT), CAST(s.sqft AS DOUBLE)) AS primary_med_yr_blt,
           arg_max(s.occtype, CAST(s.sqft AS DOUBLE)) AS primary_occtype,
           arg_max(s.fd_id, CAST(s.sqft AS DOUBLE)) AS primary_fd_id,
           arg_max(s.source, CAST(s.sqft AS DOUBLE)) AS primary_nsi_source,
           arg_max(s.cbfips, CAST(s.sqft AS DOUBLE)) AS primary_cbfips,
           min(CAST(s.med_yr_blt AS INT)) AS min_med_yr_blt, max(CAST(s.med_yr_blt AS INT)) AS max_med_yr_blt
    FROM nsi_parcel_match m JOIN nsi_structures s ON s.fd_id = m.fd_id AND s.is_active
    GROUP BY 1""")

    # ---------------------------------------------------------- properties (one per seed parcel)
    as_of_year = as_of.year
    con.execute(f"""
    CREATE OR REPLACE TABLE properties AS
    SELECT a.record_key AS property_key, a.swis_code, a.municipality_name AS town_or_city, a.print_key_code,
           k.sbl_key, a.roll_section, a.property_class, a.property_class_description, a.tax_class,
           trim(concat_ws(' ', a.parcel_address_number, a.parcel_address_street, a.parcel_address_suff)) AS situs_address,
           TRY_CAST(a.full_market_value AS DOUBLE) AS full_market_value,
           TRY_CAST(a.assessment_total AS DOUBLE) AS assessment_total,
           trim(concat_ws(' ', a.primary_owner_first_name, a.primary_owner_last_name)) AS owner_name,
           trim(concat_ws(' ', a.additional_owner_1_first, a.additional_owner_1_last_name)) AS additional_owner_name,
           trim(concat_ws(' ', a.mailing_address_number, a.mailing_address_street, a.mailing_address_suff)) AS mailing_street,
           a.mailing_address_po_box AS mailing_po_box, a.mailing_address_city AS mailing_city,
           a.mailing_address_state AS mailing_state, zip5(a.mailing_address_zip) AS mailing_zip5,
           a.mailing_address_country AS mailing_country,
           CASE
             WHEN a.mailing_address_country IS NOT NULL AND upper(trim(a.mailing_address_country)) NOT IN ('', 'US', 'USA', 'UNITED STATES')
               THEN 'foreign'
             WHEN a.mailing_address_state IS NULL AND zip5(a.mailing_address_zip) IS NULL THEN 'unknown'
             WHEN a.mailing_address_state IS NOT NULL AND upper(trim(a.mailing_address_state)) <> 'NY' THEN 'out_of_state'
             WHEN zip5(a.mailing_address_zip) IS NULL THEN 'unknown'
             WHEN zip5(a.mailing_address_zip) IN (SELECT zip5 FROM nassau_situs_zips) THEN 'in_nassau'
             ELSE 'ny_outside_nassau'
           END AS owner_location_class,
           (upper(trim(concat_ws(' ', a.mailing_address_number, a.mailing_address_street))) =
            upper(trim(concat_ws(' ', a.parcel_address_number, a.parcel_address_street)))) AS mailing_matches_situs,
           c.lat, c.lon, CASE WHEN c.lat IS NOT NULL THEN 'nys_centroid_' || c.method END AS coord_method,
           g.gis_record_key, pg.parid AS gis_parid, g.method AS gis_match_method,
           CASE WHEN c.lat IS NULL AND pg.pos_lat IS NOT NULL THEN pg.pos_lat END AS polygon_lat,
           CASE WHEN c.lat IS NULL AND pg.pos_lon IS NOT NULL THEN pg.pos_lon END AS polygon_lon,
           o.ownership_signal, o.roll_years_observed, o.owner_changes_observed, o.deed_changes_observed,
           o.last_owner_change_roll_year, o.last_deed_change_roll_year, o.current_deed_book, o.current_deed_page,
           o.deed_book_est_first_roll_year, o.deed_book_estimate_basis,
           ps.n_structures AS nsi_structures, ps.primary_fd_id AS nsi_primary_fd_id, ps.primary_occtype AS nsi_primary_occtype,
           ps.primary_med_yr_blt AS est_year_built,
           CASE WHEN ps.primary_med_yr_blt IS NOT NULL THEN 'nsi_med_yr_blt_census_block_median' END AS est_year_built_basis,
           CASE WHEN ps.primary_med_yr_blt IS NOT NULL THEN {as_of_year} - ps.primary_med_yr_blt END AS est_roof_age_years,
           CASE WHEN ps.primary_med_yr_blt IS NOT NULL THEN ({as_of_year} - ps.primary_med_yr_blt) > {roof_age_threshold} END AS est_roof_older_than_threshold,
           {roof_age_threshold} AS roof_age_threshold_years,
           CASE WHEN ps.primary_med_yr_blt IS NOT NULL THEN 'low' END AS roof_age_confidence,
           DATE '{as_of.isoformat()}' AS roof_age_as_of,
           a._source, a._source_url, a._fetched_at, a._artifact_sha256,
           '{run_id}' AS derived_run
    FROM assessment_roll a
    JOIN roll_keys k ON k.record_key = a.record_key AND k.roll_year = a.roll_year
    LEFT JOIN parcel_centroid_match c ON c.record_key = a.record_key
    LEFT JOIN parcel_gis_match g ON g.record_key = a.record_key
    LEFT JOIN parcel_geoms pg ON pg.gis_record_key = g.gis_record_key
    LEFT JOIN ownership_signals o ON o.parcel_hist_key = a.swis_code || '|' || a.print_key_code || '|' || coalesce(a.parcel_address_number, '') || '|' || coalesce(a.parcel_address_street, '')
    LEFT JOIN parcel_structures ps ON ps.gis_record_key = g.gis_record_key
    WHERE a.is_active AND a.roll_year = '{seed_year}'""")

    con.execute(f"""
    CREATE OR REPLACE TABLE roof_age_lineage AS
    SELECT property_key, est_year_built, est_year_built_basis, est_roof_age_years, est_roof_older_than_threshold,
           roof_age_threshold_years, roof_age_confidence, roof_age_as_of, nsi_primary_fd_id, gis_record_key,
           'NSI med_yr_blt = median year built of structures in the structure''s 2010 Census Block (USACE NSI technical documentation); '
           || 'not the structure''s own year built. Original-roof assumption: this NSI-only estimate applies no re-roof evidence '
           || '(permit evidence, where available, is applied separately in property_roof_age). '
           || 'Structure->parcel by point-in-polygon against Nassau GIS lot polygons; primary structure = largest NSI sqft.' AS caveat,
           '{run_id}' AS derived_run
    FROM properties WHERE est_year_built IS NOT NULL""")

    stats = {
        "deed_book_fit": {"slope_year_per_book": None if slope == "NULL" else slope,
                          "intercept": None if intercept == "NULL" else intercept,
                          "min_calibrated_book": None if min_book == "NULL" else min_book, "years": n_years},
    }
    return stats
