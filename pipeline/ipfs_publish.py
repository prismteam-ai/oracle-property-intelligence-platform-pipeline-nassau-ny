"""Publish the Nassau pipeline's eligible artifacts to public IPFS through Filebase.

Per run: export each artifact to Parquet -> pack a CARv1 with a CIDv1 root (ipfs-car, no directory wrap, so the
root is the file itself) -> upload the CAR (Filebase imports it and pins the DAG under that same root CID) and the
raw Parquet (Filebase assigns its own CID; recorded for comparison) over the S3 API -> read back the CIDs from
``x-amz-meta-cid`` -> write manifest.json -> fetch every CID from two public gateways and compare sha256/size.

Usage:  python -m pipeline.ipfs_publish [--db PATH] [--only a,b] [--skip-upload] [--skip-verify]

Credentials come from .env (FILEBASE_ACCESS_KEY / FILEBASE_SECRET_KEY) and are never printed or written.
Gateway checks run through Node (tools/ipfs-car/verify-gateway.mjs). A re-publish with changed data produces new CIDs; earlier CIDs stay in docs/ipfs/history.json and stay pinned.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .config import DATA_DIR, DB_PATH, FIPS, REPO_ROOT

ENDPOINT = "https://s3.filebase.com"
BUCKET = "nassau-property-pipeline"
CAR_TOOL = REPO_ROOT / "tools" / "ipfs-car" / "node_modules" / "ipfs-car" / "bin.js"
OUT_ROOT = DATA_DIR / "ipfs"
DOCS_DIR = REPO_ROOT / "docs" / "ipfs"
# The assignment names ipfs.io and dweb.link, but since the Sept 2026 sunset they answer plain HTTP clients with
# 429 "switching to a service worker gateway only" (and a Cloudflare challenge to browsers), so no script can fetch
# bytes from them. They are still attempted once per artifact and recorded. Gating verification uses two other
# independent public gateways (Pinata, and the community gateway dget.top) that serve raw bytes.
REQUIRED_NAMED_GATEWAYS = ["https://ipfs.io", "https://dweb.link"]
GATEWAYS = ["https://gateway.pinata.cloud", "https://dget.top"]
EXTRA_GATEWAYS = ["https://ipfs.filebase.io"]  # the pinning vendor's own gateway: informational only
VERIFY_JS = REPO_ROOT / "tools" / "ipfs-car" / "verify-gateway.mjs"

# Roofing-CRM app tables: the exact SQL the app's export uses (roofing-crm/scripts/export-parquet.mjs), so the CIDs
# published here can be dropped into the app's `ipfs` data source.
CRM_PROPERTIES = """
    SELECT p.property_key, p.print_key_code, p.sbl_key, p.situs_address, p.town_or_city,
           p.property_class, p.property_class_description,
           COALESCE(p.lat, p.polygon_lat) AS lat, COALESCE(p.lon, p.polygon_lon) AS lon,
           CASE WHEN p.lat IS NOT NULL THEN p.coord_method ELSE 'parcel_polygon_centroid' END AS coord_method,
           p.owner_name, p.owner_location_class, p.mailing_city, p.mailing_state,
           p.ownership_signal, p.last_deed_change_roll_year, p.full_market_value,
           r.roof_age_basis, r.roof_year, r.roof_age_years, r.confidence AS roof_age_confidence,
           r.caveat AS roof_age_caveat, r.evidence_permit_no, r.threshold_years AS pipeline_threshold_years,
           CAST(NULL AS DOUBLE) AS bbb_score
    FROM properties p JOIN property_roof_age r USING (property_key)
    WHERE COALESCE(p.lat, p.polygon_lat) IS NOT NULL"""
CRM_PERMITS = """
    SELECT f.permit_no, l.property_key, f.permit_type, f.permit_subtype, f.status, f.status_class, f.status_known,
           f.is_open, f.is_roofing, f.roofing_confidence, f.roofing_basis_summary,
           f.applied_date, f.issued_date, f.finaled_date, f.close_date, f.open_since, f.open_since_basis,
           f.years_open, f.expiration_date_valid, f.past_expiration,
           x.job_description, x.fees_charged, c.contractor_name, c.trade_contacts,
           CAST(NULL AS DOUBLE) AS bbb_score
    FROM etrakit_permit_facts f
    JOIN etrakit_permit_property_link l ON l.permit_no = f.permit_no AND l.link_status = 'unique'
    LEFT JOIN etrakit_permits x ON x.permit_no = f.permit_no
    LEFT JOIN (
      SELECT permit_no,
             min(name) FILTER (WHERE upper(contact_type) = 'CONTRACTOR' AND coalesce(name,'') <> '') AS contractor_name,
             string_agg(contact_type || ': ' || name, '; ' ORDER BY seq) FILTER (WHERE upper(contact_type) <> 'OWNER' AND coalesce(name,'') <> '') AS trade_contacts
      FROM etrakit_contacts GROUP BY permit_no) c ON c.permit_no = f.permit_no"""

# name -> (description, SQL). Final derived outputs and the normalized public sources the UI/agent query; no raw
# page captures, no contact phone/email (etrakit_contacts stays local; contractor names ride in crm_permits).
ARTIFACTS: dict[str, tuple[str, str]] = {
    "crm_properties": ("Roofing-CRM map table: one row per mappable property with roof age and owner signals", CRM_PROPERTIES),
    "crm_permits": ("Roofing-CRM permit table: Oyster Bay eTRAKiT permits linked to a property, with roofing class and open duration", CRM_PERMITS),
    "properties": ("Property backbone: 2025 roll joined to GIS/centroids/NSI with ownership and roof-age fields", "SELECT * FROM properties"),
    "property_roof_age": ("Final roof age per property: latest roofing permit, else NSI census-block estimate, with basis/confidence", "SELECT * FROM property_roof_age"),
    "roof_age_lineage": ("NSI-based roof-age estimate lineage per property", "SELECT * FROM roof_age_lineage"),
    "ownership_signals": ("Ownership-change signals from the 2021-2025 rolls (owner and deed deltas)", "SELECT * FROM ownership_signals"),
    "etrakit_permit_facts": ("Derived permit facts: roofing class/basis/confidence, open/closed, years open, expiration", "SELECT * FROM etrakit_permit_facts"),
    "etrakit_permit_property_link": ("Permit to property links by section-block-lot", "SELECT * FROM etrakit_permit_property_link"),
    "overture_places": ("Overture Maps places extract for Nassau (businesses/POIs)", "SELECT * FROM overture_places"),
    "corporations": ("NYS active corporations with a Nassau address (data.ny.gov n9v6-gdp6)", "SELECT * FROM corporations"),
    "pipeline_runs": ("Pipeline run history: sources, counts, deltas, limitations", "SELECT * FROM pipeline_runs"),
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (REPO_ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def s3_client():
    import boto3
    env = load_env()
    return boto3.client("s3", endpoint_url=ENDPOINT, region_name="us-east-1",
                        aws_access_key_id=env["FILEBASE_ACCESS_KEY"], aws_secret_access_key=env["FILEBASE_SECRET_KEY"])


def pack_car(src: Path, car: Path) -> str:
    """Pack one file into a CARv1 (CIDv1 root, file is the root, no directory wrapper). Returns the root CID."""
    subprocess.run(["node", str(CAR_TOOL), "pack", str(src), "--no-wrap", "--output", str(car)], check=True, capture_output=True)
    roots = subprocess.run(["node", str(CAR_TOOL), "roots", str(car)], check=True, capture_output=True, text=True).stdout.split()
    if len(roots) != 1 or not roots[0].startswith(("bafy", "bafk")):
        raise RuntimeError(f"unexpected CAR roots for {car.name}: {roots}")
    return roots[0]


def multicodec(cid: str) -> str:
    # bafkrei... = CIDv1 raw leaf (file fit in one block); bafybei... = CIDv1 dag-pb (UnixFS file DAG).
    return "raw" if cid.startswith("bafkrei") else "dag-pb"


def upload(s3, path: Path, key: str, car: bool) -> dict:
    """Upload one object; return the CID and pinning status Filebase attached (boto3 title-cases the metadata keys)."""
    extra = {"Metadata": {"import": "car"}} if car else {}
    s3.upload_file(str(path), BUCKET, key, ExtraArgs=extra)
    for _ in range(10):  # the CID is attached once Filebase has pinned the object
        meta = {k.lower(): v for k, v in s3.head_object(Bucket=BUCKET, Key=key).get("Metadata", {}).items()}
        if meta.get("cid"):
            return {"cid": meta["cid"], "pinning_status": meta.get("pinning-status"), "key": key}
        time.sleep(3)
    return {"cid": None, "pinning_status": None, "key": key}


def fetch_verify(gateway: str, cid: str, want_sha: str, want_size: int, attempts: int = 4) -> dict:
    """Fetch gateway/ipfs/<cid> (via Node) and compare status, size and sha256. 429/403 are not retried."""
    url = f"{gateway}/ipfs/{cid}"
    log = []
    for n in range(1, attempts + 1):
        proc = subprocess.run(["node", str(VERIFY_JS), url], capture_output=True, text=True, timeout=240)
        rec = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {"url": url, "status": None, "error": proc.stderr[:200]}
        rec["attempt"] = n
        log.append(rec)
        if rec.get("status") in (200, 429, 403, 402):
            break
        time.sleep(min(60, 10 * n))
    last = log[-1]
    ok = last.get("status") == 200 and last.get("sha256") == want_sha and last.get("bytes") == want_size
    return {"gateway": gateway, "url": url, "ok": ok, "status": last.get("status"), "checked_at": last.get("checked_at"),
            "bytes": last.get("bytes"), "sha256_match": last.get("sha256") == want_sha, "size_match": last.get("bytes") == want_size,
            "error": last.get("error"), "retry_after": last.get("retry_after"), "attempts": log}


def export_artifacts(db: Path, out: Path, only: set[str] | None) -> dict[str, dict]:
    con = duckdb.connect(str(db), read_only=True)
    res = {}
    for name, (desc, sql) in ARTIFACTS.items():
        if only and name not in only:
            continue
        pq = out / f"{name}.parquet"
        con.execute(f"COPY ({sql}) TO '{pq}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        rows = con.execute(f"SELECT count(*) FROM read_parquet('{pq}')").fetchone()[0]
        res[name] = {"description": desc, "file": pq, "row_count": rows}
        print(f"exported {name}: {rows:,} rows, {pq.stat().st_size:,} bytes", flush=True)
    con.close()
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--only", help="comma-separated artifact names")
    ap.add_argument("--skip-upload", action="store_true")
    ap.add_argument("--skip-verify", action="store_true")
    a = ap.parse_args()
    only = set(a.only.split(",")) if a.only else None

    run_id = "ipfs-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_ROOT / run_id
    out.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    generated_at = utcnow()
    history_path = DOCS_DIR / "history.json"
    history = json.loads(history_path.read_text()) if history_path.exists() else {"runs": []}

    exported = export_artifacts(Path(a.db), out, only)
    s3 = None if a.skip_upload else s3_client()
    artifacts = []
    for name, e in exported.items():
        pq: Path = e["file"]
        car = out / f"{name}.car"
        cid = pack_car(pq, car)
        art = {
            "name": name, "path": f"{name}.parquet", "description": e["description"],
            "cid": cid, "cid_version": 1, "codec": "file", "multicodec": multicodec(cid),
            "size": pq.stat().st_size, "sha256": sha256_file(pq), "row_count": e["row_count"], "generated_at": generated_at,
            "car": {"path": f"{name}.car", "size": car.stat().st_size, "sha256": sha256_file(car), "root_cid": cid},
            "origins": [],
        }
        if s3:
            car_up = upload(s3, car, f"{run_id}/{name}.car", car=True)
            raw_up = upload(s3, pq, f"{run_id}/{name}.parquet", car=False)
            art["car"].update(s3_key=car_up["key"], filebase_cid=car_up["cid"], filebase_pinning_status=car_up["pinning_status"],
                              filebase_cid_matches_root=car_up["cid"] == cid)
            art["parquet_upload"] = {"s3_key": raw_up["key"], "filebase_cid": raw_up["cid"], "pinning_status": raw_up["pinning_status"],
                                     "matches_local_root": raw_up["cid"] == cid,
                                     "note": "Plain uploads get Filebase's own UnixFS encoding (CIDv0 Qm...), not the CIDv1 of the CAR import; the CAR root is the artifact's identity"}
            print(f"uploaded {name}: root {cid} | filebase(car) {car_up['cid']} match={car_up['cid'] == cid} | filebase(parquet) {raw_up['cid']}", flush=True)
        art["gateway_urls"] = {g: f"{g}/ipfs/{cid}" for g in GATEWAYS + REQUIRED_NAMED_GATEWAYS}
        artifacts.append(art)

    prev = history["runs"][-1] if history["runs"] else None
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
    manifest = {
        "manifest_version": 1, "run_id": run_id, "generated_at": generated_at, "county": "Nassau", "state": "NY", "fips": FIPS,
        "provider": {"name": "Filebase", "endpoint": ENDPOINT, "bucket": BUCKET},
        "ipns": None, "pipeline_commit": commit,
        "previous_run": {"run_id": prev["run_id"], "manifest_cid": prev.get("manifest_cid")} if prev else None,
        "note": "CIDs are content-addressed: any data change produces new CIDs. Earlier runs' CIDs stay in docs/ipfs/history.json.",
        "artifacts": artifacts,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    verification = {"run_id": run_id, "gating_gateways": GATEWAYS, "named_by_assignment_attempted_once": REQUIRED_NAMED_GATEWAYS,
                    "informational_gateways": EXTRA_GATEWAYS, "results": []}
    if not a.skip_verify and s3:
        for art in artifacts:
            for g in GATEWAYS + REQUIRED_NAMED_GATEWAYS + EXTRA_GATEWAYS:
                r = fetch_verify(g, art["cid"], art["sha256"], art["size"], attempts=1 if g in REQUIRED_NAMED_GATEWAYS else 4)
                r.update(artifact=art["name"], cid=art["cid"], gating=g in GATEWAYS)
                verification["results"].append(r)
                print(f"verify {art['name']} via {g}: status={r['status']} ok={r['ok']}", flush=True)
        verification["all_gating_ok"] = all(r["ok"] for r in verification["results"] if r["gating"])
        verification["verified_at"] = utcnow()
        (out / "verification.json").write_text(json.dumps(verification, indent=2) + "\n")

    manifest_cid = None
    if s3:
        mcar = out / "manifest.car"
        manifest_cid = pack_car(out / "manifest.json", mcar)
        mup = upload(s3, mcar, f"{run_id}/manifest.car", car=True)
        upload(s3, out / "manifest.json", f"{run_id}/manifest.json", car=False)
        if mup["cid"] != manifest_cid:
            print(f"WARNING: Filebase manifest CID {mup['cid']} != local root {manifest_cid}", flush=True)
        if not a.skip_verify:
            msha, msize = sha256_file(out / "manifest.json"), (out / "manifest.json").stat().st_size
            for g in GATEWAYS + REQUIRED_NAMED_GATEWAYS + EXTRA_GATEWAYS:
                r = fetch_verify(g, manifest_cid, msha, msize, attempts=1 if g in REQUIRED_NAMED_GATEWAYS else 4)
                r.update(artifact="manifest", cid=manifest_cid, gating=g in GATEWAYS)
                verification["results"].append(r)
                print(f"verify manifest via {g}: status={r['status']} ok={r['ok']}", flush=True)
            verification["all_gating_ok"] = all(r["ok"] for r in verification["results"] if r["gating"])
            (out / "verification.json").write_text(json.dumps(verification, indent=2) + "\n")

    history["runs"].append({"run_id": run_id, "generated_at": generated_at, "manifest_cid": manifest_cid,
                            "artifacts": {x["name"]: {"cid": x["cid"], "size": x["size"], "row_count": x["row_count"]} for x in artifacts},
                            "all_gating_gateways_ok": verification.get("all_gating_ok")})
    history_path.write_text(json.dumps(history, indent=2) + "\n")
    shutil.copy(out / "manifest.json", DOCS_DIR / f"manifest-{run_id}.json")
    if (out / "verification.json").exists():
        shutil.copy(out / "verification.json", DOCS_DIR / f"verification-{run_id}.json")
    print(f"run {run_id} complete; manifest CID {manifest_cid}")
    return 0 if verification.get("all_gating_ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
