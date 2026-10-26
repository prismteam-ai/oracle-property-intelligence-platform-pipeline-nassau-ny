"""Source adapters. Each adapter has:

- ``watermark()``: a cheap metadata fingerprint used to skip unchanged sources;
- ``fetch(run_id)``: captures raw pages (immutable, gzip, manifest) and writes a staged
  NDJSON file whose rows carry provenance columns ``_source``, ``_source_url``,
  ``_fetched_at``, ``_artifact_sha256``.

Adapters only read public, anonymous endpoints that discovery verified (no login, no
CAPTCHA, no challenge pages).
"""
from __future__ import annotations

import concurrent.futures as cf
import gzip
import hashlib
import json
import time
from pathlib import Path

from . import config
from .fetch import ArtifactStore, FetchError, http_get, utcnow

STAGE_DIR = config.DATA_DIR / "stage"


def _stage_path(source: str, run_id: str, name: str) -> Path:
    d = STAGE_DIR / source / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.ndjson.gz"


def validate_json_payload(body: bytes) -> None:
    """Raise ValueError when a 200 response carries a JSON error object (ArcGIS/Socrata)."""
    try:
        data = json.loads(body)
    except Exception as exc:
        raise ValueError(f"non-JSON payload: {exc}")
    if isinstance(data, dict) and ("error" in data or data.get("errorCode")):
        raise ValueError(f"error payload: {str(data)[:200]}")


def _prov(art) -> dict:
    return {"_source": art.source, "_source_url": art.url, "_fetched_at": art.fetched_at,
            "_artifact_sha256": art.sha256}


class _Writer:
    def __init__(self, path: Path):
        self.path = path
        self.tmp = path.with_suffix(".tmp")
        self.fh = gzip.open(self.tmp, "wt")
        self.n = 0

    def write(self, row: dict) -> None:
        self.fh.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
        self.n += 1

    def close(self) -> int:
        self.fh.close()
        self.tmp.replace(self.path)
        return self.n


# ---------------------------------------------------------------- Socrata
def _socrata_watermark(meta_url: str) -> dict:
    _, body = http_get(meta_url, timeout=60)
    m = json.loads(body)
    return {"rowsUpdatedAt": m.get("rowsUpdatedAt"), "viewLastModified": m.get("viewLastModified")}


def _socrata_pages(store: ArtifactStore, prefix: str, url: str, where: str, select: str | None,
                   page_size: int, order: str = ":id"):
    _, body = http_get(url, {"$select": "count(*) as n", "$where": where}, timeout=120)
    total = int(json.loads(body)[0]["n"])
    offsets = list(range(0, total, page_size))
    for i, off in enumerate(offsets):
        params = {"$where": where, "$order": order, "$limit": page_size, "$offset": off}
        if select:
            params["$select"] = select
        art = store.fetch(f"{prefix}_p{i:04d}", url, params, timeout=300, validate=validate_json_payload)
        yield art, json.loads(store.read(art))
    return total


class AssessmentRoll:
    cfg = config.SOCRATA_ROLL
    name = cfg["name"]

    def watermark(self) -> dict:
        return _socrata_watermark(self.cfg["metadata_url"])

    def fetch(self, run_id: str, years: list[int]) -> dict[int, Path]:
        store = ArtifactStore(self.name, run_id)
        excl = ",".join(f"'{s}'" for s in self.cfg["exclude_swis"])
        out = {}
        for year in years:
            path = _stage_path(self.name, run_id, f"roll_{year}")
            if path.exists():
                out[year] = path
                continue
            where = f"upper(county_name)='NASSAU' AND roll_year='{year}' AND swis_code not in({excl})"
            w = _Writer(path)
            for art, rows in _socrata_pages(store, f"roll_{year}", self.cfg["resource_url"], where,
                                            ",".join(self.cfg["columns"]), self.cfg["page_size"]):
                for r in rows:
                    w.write({**{c: r.get(c) for c in self.cfg["columns"]}, **_prov(art)})
            w.close()
            out[year] = path
        return out


class Corporations:
    cfg = config.CORPORATIONS
    name = cfg["name"]

    def watermark(self) -> dict:
        return _socrata_watermark(self.cfg["metadata_url"])

    def fetch(self, run_id: str) -> Path:
        path = _stage_path(self.name, run_id, "corporations")
        if path.exists():
            return path
        store = ArtifactStore(self.name, run_id)
        w = _Writer(path)
        for art, rows in _socrata_pages(store, "corp", self.cfg["resource_url"], self.cfg["where"], None,
                                        self.cfg["page_size"], order="dos_id"):
            for r in rows:
                w.write({**r, **_prov(art)})
        w.close()
        return path


# ---------------------------------------------------------------- ArcGIS
def _arcgis_json(url: str, params: dict, timeout: int = 180) -> dict:
    _, body = http_get(url, params, timeout=timeout)
    data = json.loads(body)
    if "error" in data:
        raise RuntimeError(f"ArcGIS error {data['error']} for {url}")
    return data


class GisParcels:
    cfg = config.GIS_PARCELS
    name = cfg["name"]

    def watermark(self) -> dict:
        q = self.cfg["layer_url"] + "/query"
        stats = json.dumps([
            {"statisticType": "count", "onStatisticField": "objectid", "outStatisticFieldName": "n"},
            {"statisticType": "max", "onStatisticField": "objectid", "outStatisticFieldName": "max_oid"},
            {"statisticType": "max", "onStatisticField": "last_edited_date", "outStatisticFieldName": "max_edit"},
        ])
        a = _arcgis_json(q, {"where": "1=1", "outStatistics": stats, "f": "json"})["features"][0]["attributes"]
        return {k.lower(): v for k, v in a.items()}

    def fetch(self, run_id: str) -> Path:
        path = _stage_path(self.name, run_id, "parcels")
        if path.exists():
            return path
        store = ArtifactStore(self.name, run_id)
        q = self.cfg["layer_url"] + "/query"
        total = _arcgis_json(q, {"where": "1=1", "returnCountOnly": "true", "f": "json"})["count"]
        size = self.cfg["page_size"]
        jobs = list(enumerate(range(0, total, size)))

        def one(job):
            i, off = job
            params = {"where": "1=1", "outFields": ",".join(self.cfg["out_fields"]), "outSR": "4326",
                      "orderByFields": "objectid", "resultOffset": off, "resultRecordCount": size,
                      "f": "geojson"}
            return store.fetch(f"parcels_p{i:04d}", q, params, timeout=300, validate=validate_json_payload)

        with cf.ThreadPoolExecutor(self.cfg["concurrency"]) as ex:
            arts = list(ex.map(one, jobs))
        w = _Writer(path)
        for art in arts:
            fc = json.loads(store.read(art))
            for f in fc.get("features", []):
                p = f.get("properties") or {}
                g = f.get("geometry")
                w.write({**{c: p.get(c) for c in self.cfg["out_fields"]},
                         "geometry_geojson": json.dumps(g, separators=(",", ":")) if g else None,
                         **_prov(art)})
        n = w.close()
        if n != total:
            raise RuntimeError(f"GIS parcels: staged {n} rows but layer count is {total}")
        return path


class TownBoundary:
    cfg = config.TOWN_BOUNDARY
    name = cfg["name"]

    def fetch(self, run_id: str) -> Path:
        store = ArtifactStore(self.name, run_id)
        art = store.fetch("towncity", self.cfg["layer_url"] + "/query",
                          {"where": "1=1", "outFields": "town,city,label", "outSR": "4326", "f": "geojson"},
                          validate=validate_json_payload)
        path = config.RAW_DIR / self.name / run_id / "towncity.geojson"
        path.write_bytes(store.read(art))
        return path


class NysCentroids:
    cfg = config.NYS_CENTROIDS
    name = cfg["name"]

    def _oid_range(self) -> tuple[int, int, int]:
        q = self.cfg["layer_url"] + "/query"
        stats = json.dumps([
            {"statisticType": "min", "onStatisticField": "OBJECTID", "outStatisticFieldName": "mn"},
            {"statisticType": "max", "onStatisticField": "OBJECTID", "outStatisticFieldName": "mx"},
            {"statisticType": "count", "onStatisticField": "OBJECTID", "outStatisticFieldName": "n"},
        ])
        a = _arcgis_json(q, {"where": self.cfg["where"], "outStatistics": stats, "f": "json"})["features"][0]["attributes"]
        a = {k.lower(): v for k, v in a.items()}
        return int(a["mn"]), int(a["mx"]), int(a["n"])

    def watermark(self) -> dict:
        mn, mx, n = self._oid_range()
        return {"min_oid": mn, "max_oid": mx, "count": n}

    def fetch(self, run_id: str) -> Path:
        path = _stage_path(self.name, run_id, "centroids")
        if path.exists():
            return path
        store = ArtifactStore(self.name, run_id)
        q = self.cfg["layer_url"] + "/query"
        mn, mx, total = self._oid_range()
        step = self.cfg["oid_step"]
        jobs = list(enumerate(range(mn, mx + 1, step)))

        def window(lo, width):
            params = {"where": f"{self.cfg['where']} AND OBJECTID>={lo} AND OBJECTID<{lo + width}",
                      "outFields": ",".join(self.cfg["out_fields"]), "outSR": "4326", "f": "json"}
            name = f"oid_{lo}" if width == step else f"oid_{lo}_w{width}"
            return store.fetch(name, q, params, timeout=300, validate=validate_json_payload, attempts=4)

        def one(job):
            # The state server intermittently answers 200 + {"error": 500}. Retry the window;
            # if it keeps failing, split it in halves (down to 125 OIDs) so a heavy window
            # cannot block the whole county.
            i, lo = job
            pending, done = [(lo, step)], []
            while pending:
                a, w = pending.pop(0)
                try:
                    done.append(window(a, w))
                except FetchError:
                    if w <= 125:
                        raise
                    pending += [(a, w // 2), (a + w // 2, w - w // 2)]
            return done

        with cf.ThreadPoolExecutor(self.cfg["concurrency"]) as ex:
            arts = [a for group in ex.map(one, jobs) for a in group]
        w = _Writer(path)
        for art in arts:
            data = json.loads(store.read(art))
            if "error" in data:
                raise RuntimeError(f"centroid page error: {data['error']}")
            if data.get("exceededTransferLimit"):
                raise RuntimeError(f"centroid page exceeded transfer limit: {art.url}")
            for f in data.get("features", []):
                a = f["attributes"]
                g = f.get("geometry") or {}
                w.write({**{c: a.get(c) for c in self.cfg["out_fields"]},
                         "lon": g.get("x"), "lat": g.get("y"), **_prov(art)})
        n = w.close()
        if n != total:
            raise RuntimeError(f"NYS centroids: staged {n} rows but layer count is {total}")
        return path


# ---------------------------------------------------------------- NSI
class Nsi:
    cfg = config.NSI
    name = cfg["name"]

    def _tiles(self):
        x0, y0, x1, y1 = self.cfg["bbox"]
        nx, ny = self.cfg["tiles"]
        dx, dy = (x1 - x0) / nx, (y1 - y0) / ny
        for i in range(nx):
            for j in range(ny):
                a, b = round(x0 + i * dx, 6), round(y0 + j * dy, 6)
                c, d = round(x0 + (i + 1) * dx, 6), round(y0 + (j + 1) * dy, 6)
                yield f"t{i}_{j}", f"{a},{b},{c},{b},{c},{d},{a},{d},{a},{b}"

    def watermark(self) -> dict:
        # The NSI API exposes no release metadata endpoint; change detection relies on
        # record hashes after a re-fetch.
        return {"note": "no metadata watermark; always fetch"}

    def fetch(self, run_id: str) -> Path:
        path = _stage_path(self.name, run_id, "structures")
        if path.exists():
            return path
        store = ArtifactStore(self.name, run_id)

        def one(t):
            name, poly = t
            return store.fetch(name, self.cfg["api_url"], {"bbox": poly}, timeout=600, validate=validate_json_payload)

        with cf.ThreadPoolExecutor(self.cfg["concurrency"]) as ex:
            arts = list(ex.map(one, list(self._tiles())))
        seen = set()
        w = _Writer(path)
        for art in arts:
            fc = json.loads(store.read(art))
            for f in fc.get("features", []):
                p = f.get("properties") or {}
                if not str(p.get("cbfips", "")).startswith(config.FIPS):
                    continue
                if p.get("fd_id") in seen:
                    continue
                seen.add(p.get("fd_id"))
                w.write({**p, **_prov(art)})
        w.close()
        return path


# ---------------------------------------------------------------- Overture
class OverturePlaces:
    cfg = config.OVERTURE
    name = cfg["name"]

    def watermark(self) -> dict:
        return {"release": self.cfg["release"]}  # pinned release is immutable

    def fetch(self, run_id: str, con, boundary_geojson: Path) -> Path:
        """Extract the bbox from S3 with DuckDB, clip to the county boundary, write parquet."""
        out_dir = config.RAW_DIR / self.name / self.cfg["release"]
        out_dir.mkdir(parents=True, exist_ok=True)
        pq = out_dir / "nassau_places.parquet"
        meta = out_dir / "provenance.json"
        if not pq.exists():
            x0, y0, x1, y1 = self.cfg["bbox"]
            fetched_at = utcnow()
            con.execute("SET s3_region='us-west-2'")
            con.execute(f"""
                COPY (
                  WITH b AS (SELECT ST_Union_Agg(geom) AS g FROM ST_Read('{boundary_geojson}')),
                  p AS (
                    -- release 2026-10-21.0 has no `categories`; it uses basic_category + taxonomy
                    SELECT t.id, t.names.primary AS name, t.basic_category AS basic_category,
                           t.taxonomy.primary AS taxonomy_primary,
                           array_to_string(t.taxonomy.hierarchy, '>') AS taxonomy_hierarchy,
                           array_to_string(t.taxonomy.alternates, '|') AS taxonomy_alternates,
                           t.operating_status,
                           t.confidence, array_to_string(t.websites, '|') AS websites,
                           array_to_string(t.phones, '|') AS phones,
                           t.addresses[1].freeform AS address, t.addresses[1].locality AS locality,
                           t.addresses[1].postcode AS postcode, t.addresses[1].region AS region,
                           t.brand.names.primary AS brand,
                           array_to_string(list_transform(t.sources, s -> s.dataset), '|') AS source_datasets,
                           array_to_string(list_distinct(list_transform(t.sources, s -> s.license)), '|') AS source_licenses,
                           -- places are points, so bbox xmin/ymin equal the point coordinates
                           t.bbox.xmin AS lon, t.bbox.ymin AS lat
                    FROM read_parquet('{self.cfg['s3_glob']}', hive_partitioning=1) t
                    WHERE t.bbox.xmin BETWEEN {x0} AND {x1} AND t.bbox.ymin BETWEEN {y0} AND {y1}
                  )
                  SELECT p.* FROM p, b WHERE ST_Contains(b.g, ST_Point(p.lon, p.lat))
                  ORDER BY id
                ) TO '{pq}' (FORMAT PARQUET)
            """)
            sha = hashlib.sha256(pq.read_bytes()).hexdigest()
            meta.write_text(json.dumps({"source": self.name, "url": self.cfg["s3_glob"], "release": self.cfg["release"],
                                        "fetched_at": fetched_at, "sha256": sha, "bbox": self.cfg["bbox"],
                                        "clip": "Nassau GIS TownCity union"}, indent=1))
        return pq
