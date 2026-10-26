# Nassau parcel-backbone pipeline (local DuckDB)

An internal reconciliation store for Nassau County, NY (FIPS 36059). It is **not** a public publication source; Atlas/CAR publication is a later, separate stage. No hosted infrastructure: one local DuckDB file, public anonymous sources only, and no credentials.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # duckdb 1.1.3, playwright 1.48.0
.venv/bin/python -m pipeline.run                    # incremental: skip sources whose metadata watermark is unchanged
.venv/bin/python -m pipeline.run --full-refresh     # re-fetch every source and diff by record hash
.venv/bin/python -m pipeline.run --roof-age-threshold 20 --as-of 2026-10-25
.venv/bin/python -m pipeline.run --run-id <id>      # resume: reuse captured pages, skip steps already merged
```

Everything written lives under `data/` (gitignored):
- `data/nassau.duckdb`: the store;
- `data/raw/<source>/<run_id>/`: gzip pages plus `manifest.jsonl` (url, fetched_at, sha256, bytes);
- `data/stage/`: staged NDJSON with provenance columns;
- `data/logs/`: run logs.

DuckDB extensions install to `.duckdb/extensions` and Chromium to `.playwright-browsers`, both project-local and gitignored.

## Sources (all anonymous, verified in discovery)

| Table | Source | Key | Watermark |
|---|---|---|---|
| `assessment_roll` | data.ny.gov `7vem-aaz7`, roll years 2021–2025, Nassau county-roll SWIS (city-roll SWIS 280500/280900 dropped) | `swis / print_key / addr_no / street` (+`#n` for 0.16% exact duplicates) per `roll_year` scope | Socrata `rowsUpdatedAt` |
| `gis_parcels` | Nassau GIS `Hosted/Parcels_2026_Clean` (GeoJSON, EPSG:4326) | `parid` | count + max OID + max edit date |
| `nys_centroids` | NYS ITS `NYS_Tax_Parcel_Centroid_Points`, `COUNTY_NAME='Nassau'` | `SWIS_SBL_ID` | min/max OID + count |
| `nsi_structures` | USACE NSI API, 6×6 bbox tiles, filtered to `cbfips` 36059* | `fd_id` | none (always fetched) |
| `corporations` | data.ny.gov `n9v6-gdp6`, `county='NASSAU'` | `dos_id` | Socrata `rowsUpdatedAt` |
| `overture_places` | Overture `release/2026-10-21.0` places, bbox then clipped to the Nassau TownCity union | Overture `id` | pinned release |

Every source row carries `_source`, `_source_url` (the exact page request), `_fetched_at`, and `_artifact_sha256`, plus merge metadata: `record_key`, `record_hash`, `first_seen_run`, `last_seen_run`, `last_changed_run`, `is_active`, `removed_run`.

## Incremental contract

- `record_hash` = md5 over the business columns (provenance excluded).
- **new** key → insert. **Changed** hash → replace (keeps `first_seen_run`). **Same** hash → only `last_seen_run` moves.
- An active key **missing** from the new snapshot → tombstone (`is_active=false`, plus a `tombstones` row).
- `record_changes` logs every new/changed/removed key per run.
- `source_snapshots` logs each source step: watermark, action, staged rows, raw dir, manifest sha256, delta.
- `pipeline_runs` logs each run: run_id, start/finish, status, mode, git sha, parameters, sources, per-table counts, deltas, limitations, error.
- Raw page fetches validate the payload. A 200 carrying an ArcGIS/Socrata error object is never cached; it is retried with backoff, and failing ArcGIS windows are split in half.
- One writer per county (exclusive `flock` on `data/.pipeline.lock`).

## Derived tables (deterministic rebuild when any source changed)

- **`roll_keys`, `gis_keys`, `gis_lots`, `parcel_geoms`**: SBL normalization. `S.-B-L[.X][/U]` and `SS BBB LLLLX` both become `sec-block-lot-suffix`. Numeric condo groups (`.167/1`) map to GIS suffix `U`.
- **`parcel_gis_match`**: `sbl_exact`, or a unique-lot fallback only when the situs address agrees.
- **`parcel_centroid_match`**: SWIS + print key (+ address to choose between condo units).
- **`ownership_history` → `ownership_events` → `ownership_signals`**: owner-name and deed book/page deltas across 2021–2025, plus `deed_book_calibration`.
- **`nassau_situs_zips`**: the out-of-area rule (below).
- **`nsi_parcel_match` → `parcel_structures`**: NSI point-in-polygon through a 0.001° grid prefilter; the smallest containing lot wins.
- **`properties`**: one row per 2025 county-roll parcel, with coordinates, owner, mailing, out-of-area class, ownership signals, and the roof-age estimate.
- **`roof_age_lineage`**: basis, confidence, as-of date, threshold, and caveat per estimate.

### Out-of-area owner rule

`owner_location_class`:
- `foreign`: mailing country set and not US. The column is empty for all Nassau rows, so this class is currently never assigned.
- `out_of_state`: mailing state ≠ NY.
- `in_nassau`: mailing ZIP5 is one of the 110 ZIPs seen in Nassau GIS situs addresses.
- `ny_outside_nassau`: any other NY ZIP.
- `unknown`: no state and no ZIP.

The out-of-area flag is `out_of_state` or `ny_outside_nassau`. Caveat: ZIPs that straddle Queens or Suffolk (e.g. 11001, 11040, 11735) count as in-Nassau.

### Ownership caveat

The rolls cover 2021–2025 only. `no_change_observed_2021_2025` means no owner-name or deed book/page change was observed across those five rolls. It **cannot prove "no sale in 10+ years."** `deed_book_est_first_roll_year` is a linear fit of roll year on the median new deed book (2022–2025). Inside the calibrated book range it is `calibrated_range`; below it, `extrapolated_before_calibration`, which is a low-confidence model estimate, not a recorded date.

### Roof-age caveat

`est_year_built` is NSI `med_yr_blt`, which the NSI technical documentation defines as "the median year built of structures within the Census Block". It is an area statistic, not the structure's own year built. This NSI-only estimate applies no re-roof evidence; permit evidence, where a closed roofing permit is linked, is applied separately in `property_roof_age`. Confidence is `low`.
