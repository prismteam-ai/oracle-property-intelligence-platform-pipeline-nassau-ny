"""Static configuration for the Nassau County, NY (FIPS 36059) Oracle pipeline.

Everything here is public source metadata. No credentials are used or stored.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("NASSAU_DATA_DIR", REPO_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
DB_PATH = Path(os.environ.get("NASSAU_DB_PATH", DATA_DIR / "nassau.duckdb"))
LOCK_PATH = DATA_DIR / ".pipeline.lock"
DUCKDB_EXT_DIR = REPO_ROOT / ".duckdb" / "extensions"

COUNTY = "Nassau"
STATE = "NY"
FIPS = "36059"
SLUG = "nassau"

USER_AGENT = "oracle-nassau-pipeline/0.1 (public-records research; low-rate)"

# Roof-age threshold (years). Override with --roof-age-threshold or env NASSAU_ROOF_AGE_YEARS.
DEFAULT_ROOF_AGE_THRESHOLD_YEARS = int(os.environ.get("NASSAU_ROOF_AGE_YEARS", "15"))

# ---------------------------------------------------------------- sources
SOCRATA_ROLL = {
    "name": "nys_orpts_assessment_roll",
    "dataset": "7vem-aaz7",
    "resource_url": "https://data.ny.gov/resource/7vem-aaz7.json",
    "metadata_url": "https://data.ny.gov/api/views/7vem-aaz7.json",
    "years": [2021, 2022, 2023, 2024, 2025],
    "seed_year": 2025,
    # Glen Cove and Long Beach appear twice: once on the city roll and once on the county
    # roll. Keep the county roll (280600, 281000) and drop the city-roll SWIS.
    "exclude_swis": ["280500", "280900"],
    "page_size": 50000,
    "columns": [
        "roll_year", "county_name", "municipality_code", "municipality_name",
        "school_district_code", "school_district_name", "swis_code", "tax_class",
        "roll_section", "property_class", "property_class_description", "print_key_code",
        "parcel_address_number", "parcel_address_street", "parcel_address_suff",
        "front", "depth", "deed_book", "page",
        "primary_owner_first_name", "primary_owner_mi", "primary_owner_last_name",
        "primary_owner_suffix", "additional_owner_1_first", "additional_owner_1_last_name",
        "mailing_address_prefix", "mailing_address_number", "mailing_address_street",
        "mailing_address_suff", "mailing_address_city", "mailing_address_state",
        "mailing_address_zip", "mailing_address_country", "mailing_address_po_box",
        "full_market_value", "assessment_land", "assessment_total",
        "county_taxable_value", "town_taxable_value", "school_taxable",
    ],
}

GIS_PARCELS = {
    "name": "nassau_gis_parcels_2026_clean",
    "layer_url": "https://gis.nassaucountyny.gov/server/rest/services/Hosted/Parcels_2026_Clean/FeatureServer/0",
    "page_size": 2000,
    "concurrency": 3,
    "out_fields": [
        "objectid", "parid", "parcelkey", "t_section", "t_block", "t_lot_number_pref",
        "t_lot_suffix", "t_condo_building", "t_condo_unit_number", "parcel_address",
        "t_town", "t_school_district", "school_dist", "roll_year", "t_prop_luc",
        "t_class", "class", "luc", "category_description", "land_acres", "ncid",
        "last_edited_date",
    ],
}

TOWN_BOUNDARY = {
    "name": "nassau_gis_towncity",
    "layer_url": "https://gis.nassaucountyny.gov/server/rest/services/Hosted/TownCity/FeatureServer/0",
}

NYS_CENTROIDS = {
    "name": "nys_tax_parcel_centroid_points",
    "layer_url": "https://gisservices.its.ny.gov/arcgis/rest/services/NYS_Tax_Parcel_Centroid_Points/FeatureServer/0",
    "where": "COUNTY_NAME='Nassau'",
    "oid_step": 1000,
    "concurrency": 2,
    "out_fields": [
        "OBJECTID", "SWIS", "MUNI_NAME", "CITYTOWN_NAME", "PRINT_KEY", "SBL", "SWIS_SBL_ID",
        "SWIS_PRINT_KEY_ID", "MUNI_PARCEL_ID", "PARCEL_ADDR", "LOC_ST_NBR", "LOC_STREET",
        "LOC_UNIT", "LOC_ZIP", "PROP_CLASS", "ROLL_SECTION", "FULL_MARKET_VAL",
        "PRIMARY_OWNER", "ADD_OWNER", "OWNER_TYPE", "MAIL_ADDR", "PO_BOX", "MAIL_CITY",
        "MAIL_STATE", "MAIL_ZIP", "ROLL_YR", "SPATIAL_YR", "CALC_ACRES",
    ],
}

NSI = {
    "name": "usace_nsi",
    "api_url": "https://nsi.sec.usace.army.mil/nsiapi/structures",
    "docs_url": "https://www.hec.usace.army.mil/confluence/nsi/technicalreferences/latest/technical-documentation",
    # Nassau bbox (WGS84) from the county TownCity layer extent, split into tiles.
    "bbox": (-73.7683, 40.5724, -73.4194, 40.9192),
    "tiles": (6, 6),
    "concurrency": 2,
}

CORPORATIONS = {
    "name": "nys_dos_active_corporations",
    "dataset": "n9v6-gdp6",
    "resource_url": "https://data.ny.gov/resource/n9v6-gdp6.json",
    "metadata_url": "https://data.ny.gov/api/views/n9v6-gdp6.json",
    "where": "upper(county)='NASSAU'",
    "page_size": 50000,
}

OVERTURE = {
    "name": "overture_places",
    "release": "2026-10-21.0",
    "s3_glob": "s3://overturemaps-us-west-2/release/2026-10-21.0/theme=places/type=place/*.parquet",
    "bbox": (-73.7683, 40.5724, -73.4194, 40.9192),
}

LIMITATIONS = [
    "Ownership-change signals cover roll years 2021-2025 only (5 rolls). They cannot prove "
    "'no sale in 10+ years'; a parcel with no observed change is 'no change observed "
    "2021-2025', not 'unchanged for 10+ years'.",
    "Deed book/page is ~66% populated on the 2025 Nassau roll; missing values are not treated as changes.",
    "Roof age in `properties`/`roof_age_lineage` is NSI-only: NSI med_yr_blt is the median year "
    "built of structures in the structure's 2010 Census Block (not the structure's own year "
    "built), and these two tables apply no re-roof evidence. eTRAKiT permit evidence, where a "
    "closed roofing permit is linked to the property, is applied in `property_roof_age` instead "
    "(see etrakit_derive.build_roof_age); that table falls back to the same NSI estimate only "
    "when no closed roofing permit is linked.",
    "Out-of-area owner uses the mailing ZIP vs the set of Nassau situs ZIPs; ZIPs that "
    "straddle Queens/Suffolk (e.g. 11001, 11040, 11735) count as in-area.",
    "BBB and contractor licences are not ingested in this stage; eTRAKiT permits (Oyster Bay scope) are.",
]
