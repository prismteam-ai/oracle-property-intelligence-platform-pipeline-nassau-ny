# IPFS publication (Filebase, CIDv1, CAR)

Eligible Nassau artifacts are published to public IPFS as content-addressed snapshots. The CID is the artifact's identity. Gateway URLs in the manifest are derived from CIDs and are conveniences only.

## Publish history

| Run | Manifest CID | Data |
|---|---|---|
| `ipfs-20261026T032236Z` (first, pipeline check) | `bafkreiejx3tod3xmksfno5gpsqs2kcibgitd4wrfah6z5rz7eti4g2yc6q` | 443 linked permits (490 total) |
| `ipfs-20261026T152300Z` (**latest, final harvest**) | `bafkreibaghprubas2gbh6aieotgshggvdgywwmodzm3l5habdgyry565ae` | 3,986 linked permits (4,561 total) |

Both runs stay pinned and retrievable; the latest manifest names the first as its `previous_run`. Manifests and verification results are committed in `docs/ipfs/` and every run is listed in `docs/ipfs/history.json`. Only the artifacts whose data changed got new CIDs: 6 of the 11 changed (the two CRM tables, `property_roof_age`, the two permit tables and `pipeline_runs`); the other 5 kept their CIDs, which shows CIDs are stable for identical bytes.

Rows below are from the latest run.

## What is published

| Artifact | Rows | What it is |
|---|---:|---|
| `crm_properties` | 422,304 | Exactly what the roofing-crm map loads as `properties.parquet`: mappable properties with roof age, owner and ownership signals. |
| `crm_permits` | 3,986 | Exactly what the app loads as `permits.parquet`: eTRAKiT permits linked to a property, with roofing class and open duration. |
| `properties` | 426,460 | Property backbone (2025 roll joined to GIS, centroids, NSI). |
| `property_roof_age` | 426,460 | Final roof age: latest roofing permit, else the NSI estimate, with basis and confidence. |
| `roof_age_lineage` | 395,554 | NSI-based roof-age lineage. |
| `ownership_signals` | 450,299 | Owner and deed deltas across the 2021 to 2025 rolls. |
| `etrakit_permit_facts` | 4,561 | Derived permit facts (roofing basis/confidence, open/closed, years open). |
| `etrakit_permit_property_link` | 3,986 | Permit to property links. |
| `overture_places` | 78,054 | Overture Maps places for Nassau (businesses). |
| `corporations` | 409,304 | NYS active corporations with a Nassau address. |
| `pipeline_runs` | 15 | Run history with counts, deltas and limitations. |

Not published: raw page captures, other intermediate tables, and `etrakit_contacts` (it holds applicant phone numbers and emails; contractor names travel in `crm_permits.contractor_name` / `trade_contacts`). Owner names and mailing addresses are public assessment-roll fields and are included because the owner/out-of-area queries need them.

Eligibility rule: final derived outputs and the normalized public sources the UI and agent query. The list lives in `ARTIFACTS` in `pipeline/ipfs_publish.py`.

## How it works

1. Export each artifact from `data/nassau.duckdb` (read-only) to Parquet (ZSTD).
2. Pack one CARv1 per Parquet with `ipfs-car pack --no-wrap`: CIDv1, and the root is the file itself, so `/ipfs/<cid>` returns the Parquet bytes. Files over one block get a `bafybei...` (dag-pb UnixFS file) root; single-block files get `bafkrei...` (raw leaf).
3. Upload to Filebase's S3 API (bucket `nassau-property-pipeline`, `https://s3.filebase.com`, boto3):
   - the CAR with metadata `import: car`, so Filebase pins the DAG under our root CID;
   - the plain Parquet too. Filebase assigns it a CIDv0 (`Qm...`) with its own chunking, which differs from the CAR root. It is recorded in the manifest but is not the artifact's identity.
   - The CID Filebase attaches is read back from `x-amz-meta-cid` (HeadObject). For all 11 artifacts and the manifest it equals the locally computed CAR root (`filebase_cid_matches_root: true`), and `pinning-status` is `pinned`.
4. Write `manifest.json` (per artifact: name, `cid`, `cid_version`, `codec`, `multicodec`, `size`, `sha256`, `row_count`, `generated_at`, CAR details, upload details, derived gateway URLs). `ipns` is null: no IPNS is used.
5. Pack and upload the manifest itself the same way. Its CID can't be inside itself, so it is recorded in `docs/ipfs/history.json`.
6. Verify every CID on public gateways (below) and write `verification-<run_id>.json`.

Outputs: `data/ipfs/<run_id>/` (Parquet, CARs, manifest, verification; gitignored) and, committed, `docs/ipfs/manifest-<run_id>.json`, `docs/ipfs/verification-<run_id>.json` and `docs/ipfs/history.json`.

## Reproduce

```
.venv/bin/pip install -r requirements.txt            # adds boto3
(cd tools/ipfs-car && npm install)                    # ipfs-car; Node is also used for gateway checks
# .env needs FILEBASE_ACCESS_KEY and FILEBASE_SECRET_KEY (never printed or logged)
.venv/bin/python -m pipeline.ipfs_publish             # all artifacts
.venv/bin/python -m pipeline.ipfs_publish --only crm_permits,pipeline_runs   # a subset
```

Options: `--db PATH`, `--skip-upload`, `--skip-verify`. The exit code is non-zero if a gating gateway check fails.

## Re-publishing and immutability

CIDs are content hashes, so any data change produces new CIDs. That is expected: re-run the same command after the eTRAKiT harvest finishes to capture the final permit counts. Previous runs stay pinned (objects are keyed `<run_id>/...`), their CIDs stay in `docs/ipfs/history.json`, and each manifest names its `previous_run`. Nothing published is mutated. Artifacts whose Parquet bytes are identical re-publish with the same CID (observed: 5 of 11 artifacts kept their CIDs on the second run).

## Gateway verification

Each artifact CID and the manifest CID is fetched in full and compared to the manifest `size` and `sha256`, with the HTTP status and timestamp recorded in `verification-<run_id>.json`. Fetches run through Node (`tools/ipfs-car/verify-gateway.mjs`) because the system Python/curl TLS stack here can't reach several gateways.

| Gateway | Role | Result (both runs, all 12 CIDs each) |
|---|---|---|
| `https://gateway.pinata.cloud` | gating | 200, size and sha256 match on all 12, both runs |
| `https://dget.top` | gating | 200, size and sha256 match on all 12 |
| `https://ipfs.filebase.io` | informational (the pinning vendor's own gateway) | 200, match on all 12 |
| `https://ipfs.io` | named in the assignment, attempted once per CID | 429 on all 12, both runs |
| `https://dweb.link` | named in the assignment, attempted once per CID | 429 on all 12, both runs |

**Known deviation from the spec's example gateways.** `ipfs.io` and `dweb.link` now answer plain HTTP clients with `429` and the message "This IPFS gateway is switching to a service worker gateway only" (response headers carry a `sunset` of 2026-09-21 and `retry-after: 900`). A real browser gets a Cloudflare "Just a moment..." challenge instead, and their trustless `?format=raw` requests redirect to `trustless-gateway.link`, which this network cannot reach (TLS handshake fails). These responses are not rate limiting that more patience would fix, and the challenge was not bypassed. So the two-independent-gateway proof uses Pinata and dget.top, which are operated by different parties from each other and from Filebase. The two assignment-named gateways are still tried on every run and their responses are recorded, so a person can check them in an ordinary browser session.

## Why there is no Oracle-hosted infrastructure cost

- **Storage and pinning:** Filebase's S3-compatible IPFS bucket (free tier) stores and pins the objects. Oracle runs no IPFS node and no storage server.
- **Serving:** retrieval goes through public gateways (Pinata, dget.top, Filebase's, and others). Oracle runs none.
- **Query:** the roofing-crm UI queries the Parquet in the visitor's browser with DuckDB-WASM, and the site is static files. Switching `src/config.ts` to the `ipfs` data source and filling in the CIDs from the manifest makes the app read from IPFS. No API, database server or query service runs at request time.
- **Publishing:** the publish step is a script run on demand (a laptop or a CI job). Nothing runs between publishes.

Limits worth stating plainly:
- **One pinning vendor:** all objects are pinned only at Filebase for now. The spec asks for retrievability that survives any single vendor, so before the demo the CARs (in `data/ipfs/<run_id>/`) should be pinned at a second service or imported into another node. Every CAR re-imports under the same root CID.
- **Free-tier headroom:** each run uploads each artifact twice (CAR plus plain Parquet), about 230 MB per run, and each re-publish adds a new set. Check Filebase's current free-tier storage cap and retention terms; this document does not assert them.
- **Cost of the claim:** "no Oracle-hosted cost" holds for storage, serving and query. Someone still has to run the publish script, and the LLM Worker planned for the agent is metered separately.
