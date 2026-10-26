# Schema: `data/nassau.duckdb`

Read from the live DuckDB file (`DESCRIBE`) on 2026-10-25, while the eTRAKiT harvest was still running. The eTRAKiT tables hold only the batches loaded so far (490 permits at read time) and will grow. Types are DuckDB types. Dates stored as `VARCHAR` are as scraped from the portal; `DATE` columns are cleaned (placeholder dates removed).

## Quick answers

| Question | Answer |
|---|---|
| Latitude / longitude | `properties.lat`, `properties.lon` (state centroid point; `properties.coord_method` says how). `properties.polygon_lat`, `properties.polygon_lon` are the parcel-polygon centroid, kept separately. |
| Which column identifies a roofing permit | `etrakit_permit_facts.is_roofing` (BOOLEAN, true only for medium or high confidence). Join key to the permit is `permit_no`. |
| Roofing confidence level | `etrakit_permit_facts.roofing_confidence` (`none`, `low`, `medium`, `high`). Basis in `roofing_basis_summary` and `roofing_bases_json`. |
| "Open" statuses | `etrakit_permit_facts.status_class = 'open'` / `is_open = true`. Raw `status` values mapped to open: OPEN, ISSUED, APPROVED, APPLIED, PENDING, IN REVIEW, EXPIRED, ACTIVE, ON HOLD, UNDER REVIEW. An unrecognised status is treated as open and flagged `status_known = false` (seen so far: `REJECT`, 2 permits). A finaled date overrides an open status to closed. |
| "Closed" statuses | `status_class = 'closed'` / `is_open = false`. Raw values: CLOSED, FINALED, FINAL, COMPLETE, COMPLETED, CO ISSUED, CERTIFICATE ISSUED, APPROVED FINAL. |
| Void (neither open nor closed) | `status_class = 'void'`, `is_open = false`. Raw values: VOID, VOIDED, CANCELLED, CANCELED, WITHDRAWN, DENIED, REJECTED, REFUSED. |
| Where roof age lives | Both: (1) `properties` carries the NSI-based estimate columns (`est_year_built`, `est_roof_age_years`, `roof_age_confidence`, ...). (2) `roof_age_lineage` is a separate table with the same NSI estimate plus lineage. (3) `property_roof_age` is the table that combines the latest roofing permit with the NSI fallback. Use `property_roof_age` for the final answer. |
| BBB score | **No BBB column exists anywhere.** A scan of `information_schema.columns` for names containing "bbb" returned 0 rows, and no BBB tables were built. BBB is not ingested at this stage. |

Observed values at read time:
- `etrakit_permit_facts` (490 permits): `status` CLOSED 312, EXPIRED 87, OPEN 71, ISSUED 17, REJECT 2, REJECTED 1. `roofing_confidence` none 448, low 42, medium/high 0, so `is_roofing` is false for all 490.
- `property_roof_age` (426,460): `roof_age_basis` is `nsi_census_block_median_estimate` (confidence `low`) for 395,554 and `unknown` for 30,906. No permit-based roof age yet.

---

## `properties` (426,460 rows, one per 2025 roll parcel)

| Column | Type | Meaning |
|---|---|---|
| property_key | VARCHAR | Primary key for the property. |
| swis_code | VARCHAR | NYS municipality code (SWIS) for the roll row. |
| town_or_city | VARCHAR | Town or city name from the roll. |
| print_key_code | VARCHAR | NYS print key, `S.-B-L[.X][/U]`. |
| sbl_key | VARCHAR | Normalized section-block-lot used to join GIS, state points and permits. |
| roll_section | VARCHAR | Roll section (taxable, utility, etc.). |
| property_class | VARCHAR | NYS property class code. |
| property_class_description | VARCHAR | Text for the class code. |
| tax_class | VARCHAR | Tax class from the roll. |
| situs_address | VARCHAR | Site (property) address. |
| full_market_value | DOUBLE | Full market value from the roll. |
| assessment_total | DOUBLE | Total assessed value. |
| owner_name | VARCHAR | Primary owner name. |
| additional_owner_name | VARCHAR | Second owner name. |
| mailing_street | VARCHAR | Owner mailing street. |
| mailing_po_box | VARCHAR | Owner mailing PO box. |
| mailing_city | VARCHAR | Owner mailing city. |
| mailing_state | VARCHAR | Owner mailing state. |
| mailing_zip5 | VARCHAR | Owner mailing 5-digit ZIP. |
| mailing_country | VARCHAR | Owner mailing country (empty for every row, so foreign owners can't be detected). |
| owner_location_class | VARCHAR | Out-of-area owner flag: `in_nassau` 414,653, `ny_outside_nassau` 7,316, `out_of_state` 4,439, `unknown` 52. |
| mailing_matches_situs | BOOLEAN | True when the mailing address equals the site address. |
| lat | DOUBLE | Latitude, from the NYS centroid point. Null on 4,295 properties. |
| lon | DOUBLE | Longitude, from the NYS centroid point. |
| coord_method | VARCHAR | How lat/lon were matched: `nys_centroid_swis_printkey_address` 422,072, `nys_centroid_swis_printkey` 93, null 4,295. |
| gis_record_key | VARCHAR | Key of the matched Nassau GIS parcel. |
| gis_parid | VARCHAR | County parcel ID of the matched GIS parcel. |
| gis_match_method | VARCHAR | How the GIS parcel was matched. |
| polygon_lat | DOUBLE | Latitude of the GIS parcel polygon centroid. |
| polygon_lon | DOUBLE | Longitude of the GIS parcel polygon centroid. |
| ownership_signal | VARCHAR | `transfer_observed_2021_2025` 91,529; `no_change_observed_2021_2025` 333,207; `insufficient_history` 1,724. Five rolls cannot prove "no sale in 10+ years". |
| roll_years_observed | BIGINT | Number of rolls (2021 to 2025) the parcel appears in. |
| owner_changes_observed | HUGEINT | Count of owner-name changes between rolls. |
| deed_changes_observed | HUGEINT | Count of deed book/page changes between rolls. |
| last_owner_change_roll_year | INTEGER | Roll year of the latest owner-name change. |
| last_deed_change_roll_year | INTEGER | Roll year of the latest deed change. |
| current_deed_book | VARCHAR | Deed book on the latest roll. |
| current_deed_page | VARCHAR | Deed page on the latest roll. |
| deed_book_est_first_roll_year | DOUBLE | Estimated recording year from the deed book number (extrapolated for many rows; a lead only). |
| deed_book_estimate_basis | VARCHAR | Basis of that estimate. |
| nsi_structures | BIGINT | Number of NSI structures matched to the parcel. |
| nsi_primary_fd_id | VARCHAR | NSI id of the primary structure. |
| nsi_primary_occtype | VARCHAR | NSI occupancy type of the primary structure. |
| est_year_built | INTEGER | NSI `med_yr_blt`, the census-block median year built (not the building's own year). Null for 30,906. |
| est_year_built_basis | VARCHAR | `nsi_med_yr_blt_census_block_median`. |
| est_roof_age_years | INTEGER | Estimated roof age, NSI-based only (does not use permits). |
| est_roof_older_than_threshold | BOOLEAN | True when the estimate exceeds the threshold. |
| roof_age_threshold_years | INTEGER | Configured threshold (default 15). |
| roof_age_confidence | VARCHAR | `low` for all 395,554 estimated rows. |
| roof_age_as_of | DATE | Date the age was computed against. |
| _source | VARCHAR | Provenance: source name. |
| _source_url | VARCHAR | Provenance: source URL. |
| _fetched_at | VARCHAR | Provenance: fetch time. |
| _artifact_sha256 | VARCHAR | Provenance: hash of the raw page. |
| derived_run | VARCHAR | `pipeline_runs.run_id` that derived this row. |

## `roof_age_lineage` (395,554 rows)

NSI-based roof-age estimate per property, with its lineage. Permit evidence is not here.

| Column | Type | Meaning |
|---|---|---|
| property_key | VARCHAR | Join key to `properties`. |
| est_year_built | INTEGER | NSI census-block median year built. |
| est_year_built_basis | VARCHAR | Basis label for the estimate. |
| est_roof_age_years | INTEGER | Estimated roof age in years. |
| est_roof_older_than_threshold | BOOLEAN | True when older than the threshold. |
| roof_age_threshold_years | INTEGER | Threshold used (default 15). |
| roof_age_confidence | VARCHAR | Confidence label (`low`). |
| roof_age_as_of | DATE | Date the age was computed against. |
| nsi_primary_fd_id | VARCHAR | NSI id of the structure behind the estimate. |
| gis_record_key | VARCHAR | GIS parcel the structure was matched to. |
| caveat | VARCHAR | Text caveat attached to the estimate. |
| derived_run | VARCHAR | Run that derived the row. |

## `ownership_signals` (450,299 rows, one per parcel-history key)

| Column | Type | Meaning |
|---|---|---|
| parcel_hist_key | VARCHAR | Parcel key across roll years. |
| roll_years_observed | BIGINT | Number of rolls the parcel appears in. |
| first_roll_year | INTEGER | First roll year observed. |
| last_roll_year | INTEGER | Last roll year observed. |
| distinct_owner_names | BIGINT | Number of different owner names seen. |
| owner_changes_observed | HUGEINT | Owner-name changes between consecutive rolls. |
| deed_changes_observed | HUGEINT | Deed book/page changes between consecutive rolls. |
| last_owner_change_roll_year | INTEGER | Roll year of the latest owner change. |
| last_deed_change_roll_year | INTEGER | Roll year of the latest deed change. |
| current_deed_book | VARCHAR | Deed book on the latest roll. |
| current_deed_page | VARCHAR | Deed page on the latest roll. |
| ownership_signal | VARCHAR | Summary label (see `properties.ownership_signal`). |
| deed_book_est_first_roll_year | DOUBLE | Estimated recording year from deed book (extrapolated; candidate signal only). |
| deed_book_estimate_basis | VARCHAR | Basis of that estimate. |

## `property_roof_age` (426,460 rows)

Final roof-age answer: latest roofing permit where one exists, else the NSI estimate.

| Column | Type | Meaning |
|---|---|---|
| property_key | VARCHAR | Join key to `properties`. |
| print_key_code | VARCHAR | NYS print key. |
| situs_address | VARCHAR | Site address. |
| roof_age_basis | VARCHAR | Where the age comes from: `nsi_census_block_median_estimate` or `unknown` today; a permit basis once roofing permits exist. |
| roof_year | INTEGER | Year the roof is taken to date from. |
| roof_age_years | BIGINT | Roof age in years. |
| confidence | VARCHAR | Confidence of the chosen basis. |
| roof_older_than_threshold | BOOLEAN | True when older than the threshold. |
| threshold_years | INTEGER | Threshold used (default 15). |
| evidence_permit_no | VARCHAR | Permit number of the roofing permit used, if any. |
| evidence_date | DATE | Date taken from that permit. |
| evidence_date_basis | VARCHAR | Which permit date was used. |
| nsi_est_year_built | INTEGER | NSI fallback year built. |
| nsi_est_roof_age_years | INTEGER | NSI fallback roof age. |
| open_roofing_permits | BIGINT | Count of open roofing permits on the property. |
| longest_open_roofing_years | DOUBLE | Longest time an open roofing permit has been open. |
| an_open_roofing_permit | VARCHAR | One open roofing permit number. |
| caveat | VARCHAR | Text caveat attached to the estimate. |
| as_of | DATE | Date computed against. |
| derived_run | VARCHAR | Run that derived the row. |

---

## eTRAKiT permit tables (Town of Oyster Bay)

Tables with provenance columns carry the same eight fields, described once here: `_source`, `_source_url`, `_fetched_at`, `_artifact_sha256` (source, URL, fetch time, raw page hash), `record_hash` (content hash for change detection), `record_key` (stable key), `first_seen_run`, `last_seen_run`, `last_changed_run` (run ids), `is_active` (BOOLEAN, false once removed), `removed_run`.

### `etrakit_permits` (raw parsed permit, 490 rows so far)

| Column | Type | Meaning |
|---|---|---|
| permit_no | VARCHAR | Permit number (key). |
| record_id | VARCHAR | Portal record id. |
| block_key | VARCHAR | Block search the permit was found under, e.g. `46-334-`. |
| grid_sbl | VARCHAR | Section-block-lot shown in the search grid. |
| permit_type | VARCHAR | Permit Info: type. |
| permit_subtype | VARCHAR | Permit Info: subtype. |
| job_description | VARCHAR | Permit Info: job description (empty on all pilot permits). |
| status | VARCHAR | Raw portal status. |
| applied_date | VARCHAR | Applied date as scraped. |
| approved_date | VARCHAR | Approved date as scraped. |
| issued_date | VARCHAR | Issued date as scraped. |
| finaled_date | VARCHAR | Finaled date as scraped. |
| expiration_date | VARCHAR | Expiration date as scraped. |
| site_address | VARCHAR | Site Info: address. |
| site_city_state_zip | VARCHAR | Site Info: city, state, ZIP. |
| property_type | VARCHAR | Site Info: property type. |
| site_parid | VARCHAR | Site Info: PARID/SBL. |
| site_section | VARCHAR | Site Info: section. |
| site_block | VARCHAR | Site Info: block. |
| site_lot | VARCHAR | Site Info: lot. |
| site_subdivision | VARCHAR | Site Info: subdivision. |
| site_village | VARCHAR | Site Info: village. |
| lot_size_sf | VARCHAR | Lot size in square feet. |
| fees_charged | VARCHAR | Total fees charged. |
| fees_balance_due | VARCHAR | Fee balance due. |
| fees_json | VARCHAR | Fee lines as JSON. |
| tab_counts_json | VARCHAR | Row counts per tab, used for verification. |
| verify_problems_json | VARCHAR | Parse-verification problems, if any. |
| raw_path | VARCHAR | Path of the saved raw HTML (under `data/`, credentials scrubbed). |
| provenance | | `_source`, `_source_url`, `_fetched_at`, `_artifact_sha256`, `record_hash`, `record_key`, `first_seen_run`, `last_seen_run`, `last_changed_run`, `is_active`, `removed_run` (VARCHAR except `is_active` BOOLEAN). |

### `etrakit_permit_facts` (derived, one per permit)

| Column | Type | Meaning |
|---|---|---|
| permit_no | VARCHAR | Permit number. |
| block_key | VARCHAR | Block search key. |
| grid_sbl | VARCHAR | Section-block-lot from the grid. |
| lot_key | VARCHAR | Normalized lot key used for linking. |
| lot_suffix_text | VARCHAR | Lot suffix text. |
| permit_type | VARCHAR | Type. |
| permit_subtype | VARCHAR | Subtype. |
| status | VARCHAR | Raw status. |
| applied_date | DATE | Cleaned applied date (1900 placeholders removed). |
| issued_date | DATE | Cleaned issued date. |
| finaled_date | DATE | Cleaned finaled date. |
| is_roofing | BOOLEAN | True only for medium or high roofing confidence. |
| roofing_confidence | VARCHAR | `none`, `low`, `medium`, `high`. |
| roofing_basis_summary | VARCHAR | Comma-separated bases, e.g. `contact_role_roofer`, `contractor_name_contains_roof`, `job_description_roof_keyword`. |
| roofing_bases_json | VARCHAR | Each basis with confidence and evidence text. |
| trade_contacts_json | VARCHAR | Trade contacts (plumber, electrician, ...) as JSON. |
| status_class | VARCHAR | `open`, `closed`, `void`, `unknown`. |
| status_known | BOOLEAN | False when the raw status isn't in the known lists. |
| is_open | BOOLEAN | True when `status_class` is open or unknown. |
| close_date | DATE | Finaled date, else latest completed final inspection. |
| close_date_basis | VARCHAR | `finaled_date` or `last_completed_final_inspection`. |
| open_since | DATE | Issued date, else applied date (open permits). |
| open_since_basis | VARCHAR | `issued_date` or `applied_date_not_issued`. |
| years_open | DOUBLE | Years since `open_since`, open permits only; empty when the date is a placeholder. |
| expiration_date_valid | DATE | Expiration date if valid. |
| past_expiration | BOOLEAN | Open and expiration date is before `as_of`. |
| applied_date_placeholder | BOOLEAN | True when the applied date was a placeholder (e.g. 1900-03-10). |
| as_of | DATE | Date derived fields were computed against. |
| derived_run | VARCHAR | Run that derived the row. |

### `etrakit_contacts`

| Column | Type | Meaning |
|---|---|---|
| permit_no | VARCHAR | Permit number. |
| seq | VARCHAR | Row order within the permit. |
| contact_type | VARCHAR | Role, e.g. APPLICANT, OWNER, CONTRACTOR, PLUMBER. |
| name | VARCHAR | Contact name (public record). |
| phone | VARCHAR | Phone. |
| email | VARCHAR | Email. |
| address | VARCHAR | Street address. |
| city_state_zip | VARCHAR | City, state, ZIP. |
| provenance | | Same eight provenance fields as above. |

### `etrakit_inspections`

| Column | Type | Meaning |
|---|---|---|
| permit_no | VARCHAR | Permit number. |
| seq | VARCHAR | Row order within the permit. |
| type | VARCHAR | Inspection type, e.g. P-FINAL. |
| result | VARCHAR | Inspection result. |
| scheduled_date | VARCHAR | Scheduled date as scraped. |
| completed_date | VARCHAR | Completed date as scraped. |
| provenance | | Same eight provenance fields as above. |

### `etrakit_permit_property_link`

| Column | Type | Meaning |
|---|---|---|
| permit_no | VARCHAR | Permit number. |
| property_key | VARCHAR | Matched `properties.property_key`. |
| sbl_key | VARCHAR | Section-block-lot used for the match. |
| candidates | BIGINT | Number of candidate properties found. |
| link_status | VARCHAR | `unique` (443 so far). |
| method | VARCHAR | `section_block_lot_exact`. |

### `etrakit_unmatched_sbl`

| Column | Type | Meaning |
|---|---|---|
| permit_no | VARCHAR | Permit with no property match. |
| block_key | VARCHAR | Block it was found under. |
| grid_sbl | VARCHAR | Section-block-lot from the grid. |
| lot_key | VARCHAR | Normalized lot key. |
| reason | VARCHAR | Why it didn't link (e.g. lot not in the 2025 roll). |

### `etrakit_blocks` (harvest status per block)

| Column | Type | Meaning |
|---|---|---|
| block_key | VARCHAR | Block search key. |
| status | VARCHAR | `complete`, `limited`, `interrupted`. |
| reported_total | INTEGER | Total the portal reports for the block. |
| unique_permits | INTEGER | Distinct permits captured. |
| shortfall | INTEGER | Reported minus captured. |
| capped_queries | JSON | Queries that hit the 100-row cap. |
| queries | JSON | Queries run. |
| failures | JSON | Failures encountered. |
| harvest_run | VARCHAR | Run that harvested the block. |
| finished_at | VARCHAR | Finish time. |

### `etrakit_loaded_blocks`

| Column | Type | Meaning |
|---|---|---|
| block_key | VARCHAR | Block loaded into DuckDB. |
| file_sha256 | VARCHAR | Hash of the block file loaded. |
| loaded_run | VARCHAR | Load run id. |
| loaded_at | TIMESTAMP WITH TIME ZONE | Load time. |

### `etrakit_scope_decision` (documented scope cut, 1-mile radius)

| Column | Type | Meaning |
|---|---|---|
| block_key | VARCHAR | Block search key. |
| section | VARCHAR | Section. |
| block | VARCHAR | Block. |
| parcels | INTEGER | Parcels in the block. |
| nearest_miles | DOUBLE | Distance of the nearest parcel to Hicksville. |
| counted_total | INTEGER | Permits the portal reports for the block. |
| counted_returned | INTEGER | Permits actually returned when counted. |
| short_by | INTEGER | Portal shortfall. |
| cap_hit | BOOLEAN | True when the block exceeds the 100-row cap. |
| capturable | INTEGER | Permits expected to be capturable. |
| est_requests | INTEGER | Estimated requests to harvest. |
| rank | INTEGER | Rank by permit count. |
| included | BOOLEAN | True if in the harvest scope (106 included, 268 excluded). |
| cumulative_included_permits | INTEGER | Running total against the 11,000-permit budget. |
| reason | VARCHAR | Why the block was included or excluded. |
| decided_run | VARCHAR | Run that made the decision. |
| decided_at | TIMESTAMP WITH TIME ZONE | Decision time. |

### Scope helper tables

`etrakit_scope_parcels` and `etrakit_scope_parcels_r3` (1-mile and the earlier 3-mile scope; all VARCHAR unless noted): `property_key`, `print_key_code`, `sbl_key`, `property_class`, `situs_address`, `section`, `block`, `lot`, `lot_suffix`, `block_search_key`, `etrakit_sbl_guess`, `miles_from_hicksville` (DOUBLE, distance from the Hicksville point), `in_radius` (BOOLEAN), `derived_run`.

`etrakit_scope_blocks` and `etrakit_scope_blocks_r3`: `section`, `block`, `block_search_key` (VARCHAR), `parcels`, `parcels_in_radius` (BIGINT), `nearest_miles` (DOUBLE), `harvest_status` (VARCHAR).
