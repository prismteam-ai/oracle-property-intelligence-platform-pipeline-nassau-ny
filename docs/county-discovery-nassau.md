# Nassau County, NY (FIPS 36059): county discovery

Stage: discovery and readiness (sections 0–6), then **stage 2: parcel backbone built into a local DuckDB store** (section 7). No permit harvest, BBB, or publication.
Probe date: 2026-10-25, from a US egress IP (`ipinfo.io/country` = `US`).
Skills applied: `onboard-county` (intake/readiness stage), `county-discovery`, `county-readiness-preflight`.
Machine-readable catalog: [`docs/nassau-sources.yaml`](nassau-sources.yaml). The readiness skills require this file before seeding, so it is drafted here.

Legend: **[V]** = verified by a live probe in this session (curl/HTTP/REST, results recorded below). **[S]** = from web search or vendor/press pages, not probed. **[U]** = unverified estimate or assumption.

Deviations from the stock skill instructions, at the operator's request: findings live in this repo's `docs/` rather than `skills/use-oracle/runtime/docs/`. Nothing was pushed, and no PR was opened to `elephant-xyz/Counties-trasform-scripts`. Playwright was installed in stage 2 (project-local) for exactly one OpenGov check (section 7.6); the other browser-only checks remain open items. The Elephant MCP server failed to connect in this session and was not used.

---

## 0. Intake / readiness summary

| Item | Value |
|---|---|
| County / state / FIPS / slug | Nassau / NY / `36059` / `nassau` |
| Scope | Discovery only (operator instruction). Pilot/full not started. |
| Canonical parcel population | 2025 county assessment roll on data.ny.gov `7vem-aaz7`: **426,460** county-roll rows (410,896 taxable roll section 1) **[V]** |
| GIS geometry | Nassau GIS `Hosted/Parcels_2026_Clean`: **407,648** polygons **[V]** |
| Permit authorities | **69**: 3 towns (unincorporated areas), 2 cities, 64 villages **[V]** (Nassau GIS `My_Nassau/6 Villages_Cities`) |
| Readiness validator | `validate-county-readiness.py docs/nassau-sources.yaml` returned **exit 1, overall BLOCKED** **[V]** |
| Blocked gates | Discovery: `parcel`, `permit`, `destination`. **After stage 2:** `parcel` PASS (operator-approved exception), `destination` PASS (local DuckDB), `permit` still BLOCKED (54 villages `needs-review`). |
| Preparation allowed / execution allowed | `true` / `false` |
| Prior art | No Nassau flow, transform, or findings in the bundled runtime (`counties/` holds only duval and pinellas) **[V]**. The Counties-trasform-scripts repo was not checked (no network push/PR in scope). |

---

## 1. Parcel / appraisal sources

Nassau is **not** in the NYS public tax-parcel polygon layer. `NYS_Tax_Parcels_Public/FeatureServer/1` returns `COUNTY_NAME='Nassau'` count = **0** (Suffolk = 586,600) **[V]**. Nassau **is** present in the NYS centroid-point layer and in the ORPTS roll dataset (below).

### 1.1 Field matrix

| Source | SBL / parcel ID | Site address | Owner name | Owner mailing | Year built | Last sale date/price | Property class | Coordinates / geometry |
|---|---|---|---|---|---|---|---|---|
| A. Nassau GIS `Hosted/Parcels_2026_Clean` | yes: `parid`, `parcelkey`, `t_section`/`t_block`/`t_lot_number_pref`/`t_lot_suffix`, condo bldg/unit **[V]** | yes, `parcel_address` (one string with ZIP) **[V]** | no **[V]** | no **[V]** | no **[V]** | no **[V]** | yes: `t_prop_luc` (e.g. 2100), `category_description` (e.g. 210.01), `t_class`, Nassau tax `class`, `luc` **[V]** | **polygons** (native EPSG:2263; `outSR=4326` works) **[V]**. Also town, school district, land acres, roll_year 2026. |
| B. Nassau GIS `Layers/MapServer/1` (Parcels) | `PARCEL`, `PARCELKEY`, `PARID` **[V]** | `PARCEL_ADDRESS` **[V]** | no | no | no | no | no | polygons; 407,500 features, maxRecordCount 25,000 **[V]** |
| C. NYS ITS `NYS_Tax_Parcel_Centroid_Points` (Nassau subset) | `PRINT_KEY`, 20-char `SBL`, `SWIS_SBL_ID`, `MUNI_PARCEL_ID` **[V]** | `PARCEL_ADDR`, `LOC_ST_NBR`, `LOC_STREET` (LOC_ZIP null in sample) **[V]** | **yes**: `PRIMARY_OWNER` non-null 422,177 / 423,430; `ADD_OWNER`, `OWNER_TYPE` **[V]** | **yes**: `MAIL_ADDR` non-null 422,070; PO box, city, state, ZIP **[V]** | **no**: `YR_BLT` non-null = **0** for Nassau **[V]** | no (`BOOK`/`PAGE` null in sample) **[V]** | `PROP_CLASS`, `ROLL_SECTION`, AV, full market value **[V]** | **points** (lat/lon), 423,430 Nassau rows, ROLL_YR 2025 **[V]**. Building inventory fields (SQFT_LIVING, beds, baths) all null for Nassau **[V]**. |
| D. data.ny.gov `7vem-aaz7` "Property Assessment Data from Local Assessment Rolls" | `print_key_code`, `swis_code` **[V]** | number, street, suffix **[V]** | **yes**: primary owner first/last and up to 3 additional owners. 444,153/444,153 non-null owner_last (2025) **[V]** | **yes**: street 443,862 and ZIP 444,094 non-null (2025) **[V]** | no | no sale date/price; has **`deed_book` + `page`** columns **[V]** (useful as a transfer-change signal) | `property_class` + description, `tax_class`, `roll_section` **[V]** | none (grid coordinates are `0`) **[V]**. Also front/depth, land/total AV, full market value, up to 10 exemptions. **Roll years 2021–2025** each ~442k–444k rows **[V]**. |
| E. Nassau Land Records Viewer (LRV), `lrv.nassaucountyny.gov` | SBL / address search form **[V]** | yes | yes **[S]** (site text: "assessment roll data") | **[U]** | **[U]** expected on property detail | site text says "comparable sales" **[V text]**. Last sale **[U]** | yes **[U]** | linked to County GIS **[V text]** |
| F. Dept. of Assessment site (`nassaucountyny.gov/assessment`) | informational; links to LRV, AROW/ARC appeal tools | | | | | | | |

The LRV home page says it provides "assessment roll data, district information, tax maps, property photographs, past taxes, tax rates, exemptions with amounts and comparable sales" **[V]**. Its search form (section/block/lot, house number, street, town, ZIP) carries a **Google reCAPTCHA v2** widget (`data-sitekey` present) **[V]**. Per-parcel detail (`/info/<year>/<PARID>/`, e.g. `/info/2026/32011  02110/`) also returns the captcha page anonymously **[V]**. The page states "If you would like to bypass captcha, please CREATE AN ACCOUNT" (`/login/regform.php`) **[V]**. That is the sanctioned route; it needs a **human** to register and accept the terms. We did **not** solve or bypass any CAPTCHA. `arow.nassaucountyny.gov` did not resolve **[V]**. `mynassauproperty.com` redirects to LRV **[V]**.

The ArcGIS Hub lists a third-party "Nassau County NY Parcels" layer (`services6.arcgis.com/drkDXByy6E3bYW3a/...`). It returns `Token Required` **[V]**. It is not official and is excluded.

`opennassau.nassaucountyny.gov` (Socrata) is live but has **1 public asset** (a parks story); no parcel or permit data **[V]**. The Nassau GIS `FeatureServices` folder returns `Token Required` **[V]**.

### 1.2 Parcel identifier (SBL) and join rule [V]

- Official name: **Section-Block-Lot (SBL)**. The NYS form is `print_key_code` `S.-B-L`, e.g. `32.-11-211`.
- County forms: `PARID` = `SSBBB  LLLLX` (two spaces; e.g. `32011  02110`) and `PARCELKEY` = `SS BBB LLLLX` (e.g. `32 011 02110`). Blocks can be longer or alphanumeric (seen: `64102`).
- Join: section zero-padded to 2, block zero-padded to 3, lot zero-padded to 4, then a 1-character suffix (default `0`).
  - Proven pair 1: `32.-11-211` (SWIS 282001, "2 SUPERIOR RD") = GIS `PARID 32011  02110`, "2 SUPERIOR RD FLORAL PARK 11001".
  - Proven pair 2: `1.-111-139` (SWIS 282209, "6 PRESTON RD") = GIS `PARCELKEY 01 111 01390`, "6 PRESTON RD GREAT NECK 11023".
- Keep identifiers as text. Never key parcels on digits only. Lot suffix and condo unit edge cases still need a full-join test.

### 1.3 Counts and the GIS-vs-roll gap [V]

| Population | Count |
|---|---|
| 2025 roll rows, Nassau, all SWIS | 444,153 |
| minus city-roll duplicates (SWIS 280500 Glen Cove 8,019; 280900 Long Beach 9,674) | **426,460** county roll |
| of which roll section 1 / 5 / 6 / 8 | 410,896 / 1,614 / 2,082 / 11,868 |
| GIS `Parcels_2026_Clean` polygons | 407,648 (406,778 with non-null condo-unit field) |
| GIS `Layers/MapServer/1` polygons | 407,500 |
| NYS centroid points (Nassau) | 423,430 (sections 1 = 410,593, 8 = 11,374, 6 = 212, null = 1,251) |

The gap is 4.41% against the full roll and **0.79% against taxable section 1**. Most of the difference is special-franchise (5), utility (6), and wholly-exempt (8) rows with no tax-lot polygon. The validator blocks GIS-only seeding above 2%. Proposed decision: **seed from the roll and join geometry by SBL**. This needs an operator-approved exception in `approved_exceptions`.

GIS town codes **[V]**: `t_town` 1 = Hempstead (223,621), 2 = North Hempstead (68,945), 3 = Oyster Bay (98,700), 4 = Glen Cove (7,622), 5 = Long Beach (7,890), null = 870.

### 1.4 Usage-type vocabulary [V]

These are NYS ORPTS property class codes. `t_class` holds the first digit:
- 1 = agricultural (337)
- 2 = residential (361,658)
- 3 = vacant (14,144)
- 4 = commercial (19,472)
- 5 = recreation (916)
- 6 = community services (6,640)
- 7 = industrial (1,113)
- 8 = public services (1,677)
- 9 = parks/forest (821)

Codes 4xx and 7xx map to commercial/industrial for permit eligibility. Nassau's own tax `class` (1–4) is a separate field.

### 1.5 Access, anti-bot posture, rate, and timing

| Source | Access | Anti-bot | Probe timing | Full-download estimate |
|---|---|---|---|---|
| A. GIS Parcels_2026_Clean | ArcGIS REST query, pagination supported, maxRecordCount 2,000 | none observed | 3 pages × 2,000 rows with geometry (outSR 4326): 1.68 s / 1.85 s / 1.78 s, ~2.5 MB each **[V]** | 204 pages: ~6 min serial, ~2–3 min at concurrency 3 **[U]**; ~0.5 GB JSON |
| C. NYS centroid points | ArcGIS REST, maxRecordCount 1,000 | none observed | offset paging, all fields: 13.7 / 39.1 / 12.9 / 13.1 s per 1,000. **OBJECTID-range** paging with 19 trimmed fields: 8.4 s (cold), then 1.67 s; 2 in parallel: 1.31 s each **[V]**. OBJECTID span 2,133,853–2,557,282 | ~424 pages: ~10 min serial or ~5 min at concurrency 2 with OID ranges **[U]**. Avoid offset paging (~95 min). |
| D. data.ny.gov `7vem-aaz7` | Socrata SODA (JSON/CSV), `$where` filters, `$limit` up to 50k | none (no app token used) | aggregate queries < 1 s. Comparable 50k-row CSV page on `n9v6-gdp6`: 2.4 s / 13.5 MB **[V]** | ~9 pages per roll year; ~1–2 min per year; 5 years in ~10 min **[U]** |
| E. LRV | HTML per parcel | **reCAPTCHA v2** anonymously; registered account removes the captcha | n/a | Not a bulk path. Use runtime per-parcel lookup only after a human creates an account and the terms are reviewed. Throughput unmeasured. |

### 1.6 What is missing

**Year built and sale date/price are not available in any open bulk source probed.** `YR_BLT` and all residential-inventory fields are null for every Nassau row in the NYS centroid layer **[V]**.

Candidate routes:
1. LRV with a registered account, per parcel at runtime **[U]**.
2. The ORPTS Sales Web inside the Municipal Data Portal. Per tax.ny.gov it holds ~10 years of RP-5217 transfers outside NYC, but it is a **municipal-official credentialed** portal **[S/U]**.
3. A FOIL request to the Nassau County Department of Assessment for the roll with inventory (year built) and sales **[U]**.

Ownership-change proxies available now:
- Changes in owner name and `deed_book`/`page` across the 2021–2025 rolls **[V fields exist]**. Five years of history cannot prove "no transfer in 10+ years", so deed book/page must be calibrated to recording year **[U]**.
- Out-of-area owners come directly from mailing state/ZIP vs site ZIP **[V fields]**.

---

## 2. Permit sources (69 authorities)

Permits are **not county-level**. Each town handles its unincorporated hamlets. Each of the 64 villages and 2 cities runs its own building department. The county has no unified historical permit system (`assumes_unified_countywide_history: false`).

Parcel share by authority, from NYS centroid `MUNI_NAME` **[V]**:
- Town of Hempstead (unincorporated): 157,220
- Town of Oyster Bay (unincorporated): 81,210
- Town of North Hempstead (unincorporated): 32,245
- Valley Stream 11,240; Freeport 10,591; Long Beach 9,850; Village of Hempstead 9,254; Glen Cove 7,980; Garden City 7,792; Rockville Centre 6,964; Lynbrook 6,132; Massapequa Park 5,904; Mineola 5,710; Floral Park 4,939; Westbury 4,741

The unincorporated town areas hold about **64%** of parcels.

| Jurisdiction | Vendor / portal | Evidence | Public search? | Roofing / status / dates visible? | Catalog status |
|---|---|---|---|---|---|
| **Town of Hempstead** | **OpenGov Permitting & Licensing** (ViewpointCloud tenant `hempsteadny`; `hempsteadny.viewpointcloud.com` redirects to `hempsteadny.portal.opengov.com`) | Town building page links it **[V]**. `api-east.viewpointcloud.com/v2/hempsteadny/record_types` returns 200 with 28 types, incl. "Building Permit"; 26 types `view_access=public` **[V]**. `/records` returns **403 forbidden**; `search.viewpointcloud.com/graphql` returns **403 "Missing Authorization"** **[V]**. Portal config embeds reCAPTCHA and Cloudflare Turnstile site keys **[V]**. | Anonymous enumeration **forbidden** via API **[V]**. Whether the SPA offers an anonymous public search needs a Playwright check **[U]**. | OpenGov records carry status, dates, and contacts **[U for this tenant]**. No separate "Roof" record type; roofing likely falls under "Building Permit" **[V/U]** | blocked |
| **Town of North Hempstead** | **OpenGov** (tenant `northhempsteadny`; `mytonh.com` redirects to `northhempsteadny.portal.opengov.com`) **[V]** | 67 record types incl. Residential/Commercial Building Permit, Solar Panel Permit, and "FOIL Request - Building Department" **[V]**. Same 403s **[V]**. An audit doc mentions an earlier Citizenserve contract **[S]**. | forbidden anonymously **[V]** | as above | blocked |
| **Town of Oyster Bay** | **CentralSquare eTRAKiT** at `oys-trk.aspgov.com/eTRAKiT/` (linked from the town's building portal page) **[V]** | `/eTRAKiT/` 200 in 2.7 s. `/Search/permit.aspx` **redirects to login** (`lt=either`: Public or Contractor). Public self-registration offered; no CAPTCHA marker **[V]**. Login page embeds a roster of **10,503** registered contractor accounts (32 with "roof" in the name) **[V]**. Candidate names only, not legal identity. A March 2025 press release describes a new online Building Division Portal **[S]**. | **login required** **[V]** | eTRAKiT normally exposes permit type, status, applied/issued/finaled dates, and contractor **[U for this tenant]** | blocked |
| **City of Glen Cove** | **OpenGov** (tenant `glencoveny`: 12 record types incl. Building Permit Application) **[V]**. OpenGov press release **[S]** | same platform contract | forbidden anonymously **[V]** | new system, little history **[U]** | blocked |
| **City of Long Beach** | none found. `longbeachny.gov/building` 200, no portal links; no OpenGov tenant `longbeachny` **[V]** | | no | | unavailable |
| Village of Freeport | **OpenGov** (`freeportny`, "Building Permit Application"); village page `/992/OpenGov` **[V/S]** | | forbidden anonymously | | blocked |
| Village of Valley Stream | **OpenGov** (`valleystreamny`) **[V]** | | forbidden anonymously | | blocked |
| Village of North Hills | **OpenGov** (`northhillsvillageny`) **[V]** | | forbidden anonymously | | blocked |
| Village of Hempstead | **Granicus SmartGov** (`vlg-hempstead-ny.smartgovcommunity.com`) **[V]** | Public portal shows "Sign Up or Log In" **[V]** | login required | | blocked |
| Villages of Floral Park, New Hyde Park, Hewlett Harbor | **iWorQ** (`*.portal.iworq.net`) **[V]** | New Hyde Park search by parcel, address, contractor, permit #, and date range, with Status and Contractor columns, **but the form is `search-form-captcha` (g-recaptcha) plus an access-code gate** **[V]**. Hewlett Harbor lists a $75 "Roof" permit type **[V]**. | captcha-gated; **not bypassed** | roofing permit type exists (Hewlett Harbor) **[V]** | blocked |
| Villages of Old Westbury, Plandome, East Williston | **Edmunds GovTech** WIPP / OPA **[V]** | JS SPA; permit lookup not inspected | unknown (Playwright) | | needs-review |
| Village of East Hills | **Cloudpermit** (per village tutorial page) **[S]**. Homepage behind a Cloudflare challenge **[V]** | Cloudpermit is usually applicant-login only **[U]** | unknown | | needs-review |
| Village of Rockville Centre | paper / in person (instruction packets) **[S]**. Homepage Cloudflare-challenged **[V]** | | no | | manual-only |
| Villages of Garden City, Mineola | paper forms / downloads; no portal found **[S]** | | no | | manual-only |
| Village of Lynbrook | only an iWorQ **parking** portal found **[V]** | | unknown | | needs-review |
| Villages of Massapequa Park, Baxter Estates, Brookville, Cove Neck, Great Neck Estates, Kings Point, Laurel Hollow, Roslyn, Williston Park | homepage returns a **Cloudflare "Just a moment" challenge** to curl and Python **[V]**; not bypassed | | unknown | | needs-review |
| Remaining ~40 small villages | automated homepage plus building/permit-link scan found **no** vendor signature (Accela, Tyler/EnerGov, eTRAKiT, Citizenserve, CityView, Cloudpermit, iWorQ, SmartGov, OpenGov, ViewPermit, GovPilot, BS&A, MyGov, Edmunds) **[V]**. Likely PDF/paper processes **[U]** | | unknown | | needs-review |

Vendors **not** observed anywhere in Nassau: Accela, Tyler EnerGov, ViewPermit, CityView. The vendor families present are **OpenGov** (2 towns, 1 city, 3+ villages; ~52% of parcels), **CentralSquare eTRAKiT** (Oyster Bay; ~19%), **iWorQ**, **Granicus SmartGov**, **Edmunds GovTech**, **Cloudpermit**, and paper.

**Bottom line on permits: no anonymously harvestable bulk permit source was verified for any Nassau jurisdiction.** Every portal found is gated by an API that forbids anonymous listing (OpenGov), a login (eTRAKiT, SmartGov), or reCAPTCHA (iWorQ, LRV). Records-request routes (NY FOIL) are catalogued for every blocked or manual-only row in `nassau-sources.yaml`. NY FOIL needs an acknowledgment within 5 business days and typically up to 20 business days to fulfil **[U]**, which is outside a 6-hour build.

Whether these towns require a permit for an in-kind re-roof is **[U]**; check the town code on ecode360 (HE0207, NO0081, oy1221). If they do not, roofing permit coverage will be structurally sparse.

---

## 3. Coordinates, businesses, contractors, BBB

| Source | What it gives | Access | Probe result | Full-download estimate |
|---|---|---|---|---|
| **Coordinates** | Parcel polygons (GIS A) plus centroid points (NYS C). County geocoder at `gis.nassaucountyny.gov/server/rest/services/GeocodeService/Nassau_Address_Geocoder/GeocodeServer` **[V URL from LRV bundle]** | REST | §1.5 | covered by §1.5 |
| **Overture Maps places** | POIs with categories, websites, phones | public S3 GeoParquet (`overturemaps-us-west-2`) | Latest release **2026-10-21.0**; `theme=places/type=place` = 16 files, **11.9 GB** global **[V]**. Nassau bbox (from county TownCity extent, WGS84): **xmin -73.7683, ymin 40.5724, xmax -73.4194, ymax 40.9192** **[V]** | Nassau count **not measured**: DuckDB is not installed and was not installed. Rough expectation is tens of thousands of places **[U]**. Bbox-pushdown extract typically takes minutes **[U]**. Clip to the county polygon after the bbox filter, per the `overture-places-ingest` skill. |
| **NYS DOS corporations** | Active entities: dos_id, name, filing date, county, entity type, process/registered-agent/chairman/location addresses | data.ny.gov `n9v6-gdp6` (SODA, CSV) | 4,305,051 active statewide; **409,304 with county = NASSAU**; 266 Nassau names containing "ROOF" **[V]**. 50k-row CSV page: 2.4 s / 13.5 MB **[V]** | ~9 pages: **< 1 min** **[V-derived]** |
| **NYS DOS entity detail** (NY equivalent of Sunbiz document-number detail) | per-entity status and history by `dosID` | `apps.dos.ny.gov/PublicInquiryWeb/api/PublicInquiry/*` JSON | `GetComplexSearchMatchingEntities` POST returned 200 `EntityMatchFound` anonymously **[V]**. `apps.dos.ny.gov/publicInquiry/` UI did not respond to curl **[V]** | runtime lookup per contractor; rate limits unmeasured **[U]** |
| **Nassau County Dept. of Consumer Affairs home-improvement licenses** | company, DBA, **license number** (observed formats: `H` + 10 digits, e.g. `H2069750000`, and 6-digit, e.g. `203951`), status, phone, address, category (Home Improvement roofing/HVAC/etc.), insurance flags | Oracle APEX app `apex5.nassaucountyny.gov/ords/f?p=288:500:::NO` ("Search Businesses With Valid Licenses") | 200 in 1.1 s, **no CAPTCHA**, anonymous; filters by license, company, address, insurance; 15 rows per page; **Download Results CSV/Excel** button **[V]**. A direct `FLOW_EXCEL_OUTPUT` URL returned HTML, not CSV; the download needs a real browser session **[V]**. Valid licenses only; no lapsed history **[V text]**. OCA does not license electricians or plumbers **[V text]**. | One browser-session CSV export: minutes **[U]**. Total row count not displayed **[U]** |
| **Statewide contractor licence** | NY has no statewide home-improvement contractor licence; licensing is county/NYC **[U]** | | | |
| **BBB** | roofing profiles and ratings | bbb.org category pages | `/us/ny/garden-city/category/roofing-contractors` and `/us/ny/hempstead/...` return **403 Cloudflare challenge** from local egress **[V]**. robots.txt disallows query-string URLs, including category pagination (per bbb-harvest skill; robots.txt fetched) **[V]**. | Per the `bbb-harvest` skill, a 403 is a source-access boundary. Stop local requests and use the **AWS remote BBB execution** path (Duval reference: ~240 roofing profiles per category). Do **not** treat any advertised "N results" total as harvestable (`advertised_listing_count` trap); no count recorded. |

Identity rule for NY: a contractor's legal identity comes from **Nassau OCA licence number → DOS `dos_id`**, with temporal validity. eTRAKiT roster names, BBB, and Overture remain candidates and enrichment only. The Florida-specific Sunbiz/DBPR steps in the Oracle system do not apply to NY. The equivalents are NYS DOS (corporate registry) and Nassau OCA (licensing authority).

---

## 4. Access method / anti-bot / rate / time, consolidated

| Source | Method | Anti-bot | Observed rate | Est. full time | Mode |
|---|---|---|---|---|---|
| Nassau GIS parcels | REST bulk | none | ~1.8 s / 2,000 rows | 2–6 min | bulk artifact |
| NYS centroid points (owner, mailing, lat/lon) | REST bulk (OID ranges) | none | ~1.3–1.7 s / 1,000 rows, conc 2 OK | 5–10 min | bulk artifact |
| data.ny.gov roll 2021–2025 | SODA bulk | none | ~2.4 s / 50k rows (comparable dataset) | ~10 min | bulk artifact |
| LRV detail (year built, sales) | HTML per parcel | reCAPTCHA (account removes it) | n/a | n/a until a human creates the account | runtime retrieval, bounded cohort |
| OpenGov (TOH, TONH, Glen Cove, Freeport, Valley Stream, North Hills) | REST/GraphQL | auth required for records; reCAPTCHA/Turnstile keys in config | metadata 200 < 0.5 s | blocked | FOIL (api-first) plus Playwright check |
| eTRAKiT (Oyster Bay) | ASP.NET WebForms | login (public account) | home 2.7 s | unmeasured | after the human creates an account |
| iWorQ villages | HTML | reCAPTCHA | 0.2–0.6 s page load | blocked | FOIL |
| SmartGov (Village of Hempstead) | portal | login | 0.35–0.9 s | blocked | FOIL |
| NYS DOS corporations | SODA bulk | none | 2.4 s / 50k | < 1 min | bulk artifact |
| NYS DOS entity detail | JSON API | none observed | single call OK | runtime | runtime lookup |
| Nassau OCA licences | APEX report | none | 1.1 s / page (15 rows) | minutes via browser CSV | bulk artifact (browser) |
| Overture places | S3 GeoParquet | none | listing OK | minutes (needs DuckDB) | bulk artifact |
| BBB | HTML | Cloudflare 403 locally | n/a | AWS remote | enrichment |

---

## 5. Recommended ~6-hour scope (roofing prioritized, realistic)

Large-scale, real Nassau data that can be built in ~6 hours comes from the **property, owner, coordinate, business, and licence** sources. **Roofing permits are the binding constraint**: no anonymous bulk permit source exists. Present permit coverage as a documented source limitation, and start the request routes immediately.

1. **Hour 0–1: parcel backbone (~426k properties).** Pull the 2025 county roll (`7vem-aaz7`, county-roll SWIS only) as the canonical seed. Join Nassau GIS polygons (`Parcels_2026_Clean`) and NYS centroid points by SBL using the proven join rule. Load the 2021–2024 rolls for owner-name and `deed_book/page` deltas. Delivers parcel ID, address, owner, mailing address, class, values, and polygon plus point coordinates for radius search. Also delivers out-of-area-owner and ownership-change signals, the latter with the 5-year caveat. Requires the operator to approve the 4.4% roll-vs-GIS exception (section 5/6/8 rows have no geometry).
2. **Hour 1–2: identity and business.** NYS DOS active corporations for Nassau (409k), Nassau OCA licence CSV (browser session), and the Overture 2026-10-21.0 places clip. This needs DuckDB installed, which was not done in discovery. Queue BBB roofing on the AWS remote path.
3. **Hour 0–6 in parallel: permits, ordered by parcel share × feasibility.**
   - a. **Town of Oyster Bay eTRAKiT** (~81k unincorporated parcels, ~19%). Needs a human to create a public account and review the terms. Then build an eTRAKiT vendor module (`county-permit-adapter`), probe throughput at concurrency 1/2/4, and harvest roofing and building permits with status, applied/issued/finaled dates, and contractor.
   - b. **OpenGov tenants** (TOH, TONH, Glen Cove, Freeport, Valley Stream, North Hills; ~52%). Run a Playwright session to see whether the public portal offers anonymous search. If it is Turnstile/reCAPTCHA-gated or login-only, stop. One OpenGov adapter would serve all six tenants.
   - c. **File FOIL requests now** (records-first or api-first per catalog) to TOH, TONH, TOB, Glen Cove, Long Beach, and the larger villages (Valley Stream, Freeport, Village of Hempstead, Garden City, Rockville Centre, Lynbrook, Massapequa Park, Mineola). Ask for roofing and building permit exports with SBL, status, and issue/final dates. Responses land after the 6-hour window.
4. **Roof age.** Year built is not openly available, so the roof-age basis is the "last roofing permit" wherever (3a)/(3b) yield data, otherwise `unknown`, with caveats recorded. Optional: with an operator-created LRV account, fetch year built at runtime for a bounded demo cohort (e.g. a 5-mile radius), never bulk.
5. **Hour 5–6: reconcile and publish** per the Oracle pipeline: Query DB reconciliation by SBL, then Atlas/CAR publication per data group. Not started; destination and publication readiness are unproven.

Required human actions:
- Approve the parcel-gap exception.
- Create the eTRAKiT (Oyster Bay) public account, and optionally an LRV account, after terms review.
- Send the FOIL requests.
- Provide or prove the internal destination and the publication upload node.
- Allow Playwright and DuckDB installs for the next stage.

Next automated actions while blocked:
- Classify the 54 `needs-review` villages with a Playwright pass.
- Scaffold the eTRAKiT and OpenGov adapters against fixtures.
- Prepare the FOIL request texts.
- Prepare the AWS BBB run.

---

## 6. Risks

- **Bot posture:** reCAPTCHA (LRV, iWorQ), Turnstile/reCAPTCHA keys (OpenGov), Cloudflare challenges (BBB, 11+ village sites). None will be bypassed.
- **Login walls:** eTRAKiT, SmartGov, LRV (account route). Account creation and terms acceptance are human decisions.
- **History depth:** OpenGov and eTRAKiT rollouts are recent (2025 for TOB per press **[S]**). Older permits may exist only on paper.
- **Geo-blocking:** none observed from US egress. Non-US egress is untested.
- **Data quality:** `LOC_ZIP` is null in centroid samples. Roll grid coordinates are 0. Glen Cove and Long Beach appear on both a city roll and a county roll (dedupe by SWIS).
- **Privacy:** owner names and mailing addresses are public roll data, but publication must follow the privacy-limited Atlas rules. No owner names were printed or committed in this discovery.

## 7. Stage 2: parcel backbone (2026-10-25)

### 7.1 Operator decisions (recorded)

- **APPROVED:** the 4.41% roll-vs-GIS parcel gap exception. The gap is roll sections 5/6/8 (franchise, utility, exempt) with no tax-lot polygon; the taxable section-1 gap is 0.79%. Recorded in `nassau-sources.yaml` under `approved_exceptions`; the validator's parcel gate now passes.
- **APPROVED:** DuckDB 1.1.3 and Playwright 1.48.0 installs, all project-local: `.venv/`, `.duckdb/extensions`, `.playwright-browsers/`, all gitignored.
- **Destination:** a local DuckDB file `data/nassau.duckdb`, gitignored, with no hosted database. `destination.proven: true`, backed by two identity sources: the code-configured path, and `duckdb_databases()` plus `pipeline_runs` in the opened file.
- **FOIL:** drafts in [`docs/foil/`](foil/README.md), **not sent**.
- No accounts created and no captcha, challenge, or login bypassed. The operator will create the Oyster Bay eTRAKiT account. No permit harvest, BBB, or Atlas publication in this stage.
- **Readiness note:** the validator still returns BLOCKED, solely on the `permit` gate (54 villages `needs-review`). The parcel backbone was built on explicit operator direction. Permit harvest and pilot/full ingest remain gated.

### 7.2 What was built

The code (`pipeline/`) and operations doc ([`pipeline.md`](pipeline.md)) are committed; data lives only under `data/`.

| Table | Rows | Source / notes |
|---|---:|---|
| `assessment_roll` | 2,129,622 | roll years 2021 425,126 · 2022 425,623 · 2023 426,023 · 2024 426,390 · 2025 426,460 (county-roll SWIS) |
| `gis_parcels` | 407,648 | Nassau GIS `Parcels_2026_Clean`, polygons as GeoJSON (EPSG:4326) |
| `nys_centroids` | 423,430 | NYS ITS centroid points with owner and mailing address |
| `nsi_structures` | 408,465 | USACE NSI, 36 bbox tiles, `cbfips` 36059* |
| `corporations` | 409,304 | NYS DOS active corporations, `county='NASSAU'` |
| `overture_places` | 78,054 | Overture 2026-10-21.0, clipped to the Nassau TownCity union (148 roofing-related by name/taxonomy) |
| `properties` | 426,460 | one row per 2025 county-roll parcel |
| `parcel_gis_match` | 420,568 | `sbl_exact` 420,549 + `lot_unique_any_suffix_address_agrees` 19 |
| `parcel_centroid_match` | 422,165 | `swis_printkey_address` 422,072 + `swis_printkey` 93 |
| `nsi_parcel_match` / `parcel_structures` | 408,169 / 381,193 | point-in-polygon |
| `roof_age_lineage` | 395,554 | per-property estimate, basis, caveat |
| `ownership_history` / `ownership_events` / `ownership_signals` | 2,129,622 / 1,679,323 / 450,299 | 2021–2025 |
| `pipeline_runs` / `source_snapshots` / `record_changes` / `tombstones` | 3 / 30 / 3,856,523 / 0 | run history |
| support tables | | `roll_keys` 2,129,622 · `gis_keys` 407,648 · `gis_lots` 407,461 · `parcel_geoms` 407,461 · `nassau_situs_zips` 110 · `deed_book_calibration` 4 |

**Coordinates.** 422,165 properties take lat/lon from NYS centroids. Another 139 fall back to a GIS polygon point-on-surface. 4,156 have no coordinates, mostly roll sections 5/6 (franchise/utility). 420,568 have a polygon.

**Join quality.** On `sbl_exact` matches the GIS site address begins with the roll address 92.8% of the time; the rest are format differences (e.g. ranges, unit text). The first run's unguarded lot-only fallback agreed on address for only 1.6% of its 1,206 rows. It was replaced by an address-guarded fallback, which keeps 19.

### 7.3 NSI year-built finding (roof-age proxy)

- **Field:** `med_yr_blt` (integer). It is populated on all 408,465 Nassau structures.
- **Definition:** the NSI technical documentation (HEC Confluence, read through the Internet Archive because the live site returned 503) says "`med_yr_blt` — Describing the median year built of structures within the Census Block." It is an **area statistic for the structure's 2010 Census Block**, not that structure's observed year built.
- **Observed behaviour [V]:**
  - only **39 distinct values** countywide;
  - at most 2 distinct values within any of the 18,087 census blocks;
  - range **1938–2005**, with a pile-up at 1938 (21,079 structures), consistent with a "1939 or earlier" bucket floor;
  - the 2005 maximum shows the underlying vintage predates recent construction.
- **NSI `source` field:** `P` (parcel) 375,009 and `H` 33,456. The docs define `H` as "HIFLD Hospital", which cannot be right for 33k Nassau structures. Treat `source` as unverified for `H`.
- **Match rate:**
  - 408,169 / 408,465 NSI points (**99.93%**) fall inside a Nassau GIS lot polygon, and 407,546 fall on a seed property's lot;
  - **395,554 / 426,460 properties (92.8%)** have an estimate;
  - for residential class 2xx: **370,738 / 372,642 (99.5%)**.
- **Roof-age estimate:** `est_year_built = med_yr_blt` of the largest NSI structure on the lot, and `est_roof_age_years = as_of_year − est_year_built`. Basis is `nsi_med_yr_blt_census_block_median`, confidence `low`, assuming the original roof (no re-roof evidence, since permits are not ingested).
  - The threshold is configurable (`--roof-age-threshold`, default **15**).
  - **Caveat:** with a maximum of 2005, *every* estimated property exceeds 15 years (395,554 true; 30,906 unknown), so the default threshold does not discriminate. Higher thresholds do: more than 60 years for 368,402, more than 70 years for 217,184.
  - Permit-based re-roof evidence is required before roof age is decision-grade.

### 7.4 Ownership change and out-of-area owners

**Ownership signals, 2021–2025 rolls** (`properties.ownership_signal`):
- `no_change_observed_2021_2025` 333,207
- `transfer_observed_2021_2025` 91,529
- `insufficient_history` 1,724

**Five-year caveat:** "no change observed" covers five rolls only. It **cannot prove "no sale in 10+ years."**

**Deed-book estimate.** Nassau deed books are sequential. A linear fit of roll year on the median new deed book (2022–2025) gives slope 0.00649 year per book, about 154 books per year. It yields `deed_book_est_first_roll_year`:
- `calibrated_range` 89,581
- `extrapolated_before_calibration` 192,224 (low-confidence model estimate)
- `no_deed_book` 144,655

104,048 properties have an *extrapolated* estimate of 2016 or earlier. That is a candidate signal for "held 10+ years", not proof.

**Out-of-area rule** (`owner_location_class`, from mailing state and ZIP5):
- `out_of_state` (mailing state ≠ NY): 4,439
- `ny_outside_nassau` (NY, ZIP not among the 110 Nassau situs ZIPs): 7,316
- `in_nassau`: 414,653
- `unknown`: 52
- `foreign` is never assigned, because `mailing_address_country` is empty for every Nassau row.

The out-of-area flag is `out_of_state` + `ny_outside_nassau` = **11,755**. Straddling ZIPs (11001, 11040, 11735, …) count as in-Nassau. `mailing_matches_situs` (owner-occupancy proxy) is true for 252,918.

### 7.5 Incremental proof

| Run | Mode | Code | Result |
|---|---|---|---|
| `20261025T193332Z` | initial (resumed twice, see 7.7) | 83d75af | all rows **new**: roll 2,129,622 · GIS 407,648 · centroids 423,430 · NSI 408,465 · corporations 409,304 · Overture 78,054 |
| `20261025T224356Z` | `--full-refresh` (re-fetch + hash diff) | 340d4fd | **0 new / 0 changed / 0 removed** on every source; derived tables rebuilt in 7.6 s; 47 min, of which centroids took 30 min |
| `20261025T233129Z` | incremental (watermarks) | 340d4fd | all watermarked sources `skipped-unchanged`; NSI (no watermark) re-fetched, 0 changes; derive skipped; 1.1 min |

The Overture pinned release (immutable) is cached by release, so a full refresh re-diffs it but does not re-download it.

### 7.6 OpenGov Playwright check (exactly one session)

- **Setup:** stock headless Chromium 130 (Playwright 1.48), no stealth, no retry. Script `tools/opengov_check.py`.
- **Result:** `https://hempsteadny.portal.opengov.com/` rendered **"An OpenGov portal does not exist for a community named 'hempsteadny'"**.
- **What it showed:** no search input, no captcha, no login wall. The page made no record or search API calls.
- **Evidence:** screenshot `data/opengov_check/20261025T193354Z/01_home.png` (gitignored), with `network.json` and `result.json` alongside.
- **Conclusion:** the Town of Hempstead has **no anonymous public portal** at that address, even though its building page links `hempsteadny.viewpointcloud.com` and the tenant API serves record types. Anonymous permit search is not available. Route: FOIL (`docs/foil/01-town-of-hempstead.md`) or a town-provided export.

### 7.7 Failures and fixes

1. **Centroid error payloads.** In run 1 attempt 1, 46 of 424 NYS centroid windows came back as HTTP 200 with `{"error":{"code":500,"message":"Error performing query operation"}}` and were cached. The run failed at the merge.
   - Fix (commit 83d75af): payload validation before caching, exponential-backoff retry, and window splitting.
   - The DuckDB file was reset, and the run resumed under the same run id, reusing the immutable pages.
   - The first failed attempt's `pipeline_runs` row is gone because of the reset; its log is `data/logs/run1_attempt1_failed.log`.
2. **Overture schema.** In run 1 attempt 2, the Overture extract failed with `Binder Error: Referenced table "categories" not found`. Release 2026-10-21.0 has no `categories` column (verified by `DESCRIBE`); it uses `basic_category` and `taxonomy`. Fixed in commit 340d4fd, and the run resumed. Completed steps were skipped, with deltas rebuilt from `record_changes`.
3. **Unguarded GIS fallback.** The first derive's lot-only fallback was mostly wrong (1.6% address agreement). Guarded in 340d4fd; run 2 rebuilt derived tables with the fix.
4. **Docs outage.** The NSI technical docs returned 503 live; I read the Internet Archive copy instead.

**Speed limits observed:**
- NYS centroid server: 7–30 min per full pass; serves intermittent error payloads under load.
- NSI: 45 s to 11 min per pass.
- Roll: about 2–3 min per year to fetch (about 25 s when pages are reused).
- GIS: about 3 min.

---

## Appendix: probe log (2026-10-25)

- `lrv.nassaucountyny.gov/` 200 17.9 KB 0.59 s. g-recaptcha sitekey present. `/info/2026/<PARID>/` 200 with captcha. `/robots.txt` returns a server-error page.
- `gis.nassaucountyny.gov/server/rest/services` folders: Basemaps, FeatureServices (token), GeocodeService, GPServices, Health, Hosted, LocatorService, MapServices, Utilities. `Layers/MapServer/1` count 407,500 (0.55 s). `Hosted/Parcels_2026_Clean/FeatureServer/0` count 407,648.
- `gisservices.its.ny.gov/.../NYS_Tax_Parcels_Public/FeatureServer/1` Nassau 0, Suffolk 586,600. `NYS_Tax_Parcel_Centroid_Points/FeatureServer/0` Nassau 423,430.
- `data.ny.gov/resource/7vem-aaz7` Nassau roll years 2021–2025: 442,534 / 443,097 / 443,497 / 443,880 / 444,153.
- `api-east.viewpointcloud.com/v2/{tenant}/record_types` 200 for hempsteadny (28), northhempsteadny (67), glencoveny (12), freeportny (20), valleystreamny (19), northhillsvillageny (14). `/records` 403. Not found for longbeachny, gardencityny, lynbrookny, rockvillecentreny, mineolany and others tried.
- `oys-trk.aspgov.com/eTRAKiT/Search/permit.aspx` → `login.aspx?lt=either`.
- `apex5.nassaucountyny.gov/ords/f?p=288:500:::NO` 200 65 KB 1.1 s, no captcha.
- `www.bbb.org/us/ny/{garden-city,hempstead}/category/roofing-contractors` 403 Cloudflare challenge.
- `overturemaps-us-west-2` releases: 2026-09-23.0, 2026-09-23.1, 2026-10-21.0.
- `validate-county-readiness.py docs/nassau-sources.yaml` exit 1, BLOCKED (parcel, permit, destination).
