"""HTTP fetch with retries, plus immutable raw-artifact capture with provenance.

Every fetched page is stored gzip-compressed under data/raw/<source>/<run_id>/ next to
a manifest.jsonl row with url, fetched_at, sha256 (of the raw bytes), byte count, and
path. Pages already captured for the same run are reused, so a re-invoked run resumes.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class FetchError(RuntimeError):
    pass


def http_get(url: str, params: dict | None = None, timeout: int = 120, retries: int = 4,
             backoff: float = 2.0) -> tuple[str, bytes]:
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": config.USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            return full, body
        except urllib.error.HTTPError as exc:  # 4xx other than 429 are not retried
            last = exc
            if exc.code in (400, 401, 403, 404):
                raise FetchError(f"HTTP {exc.code} for {full}") from exc
        except Exception as exc:  # timeouts, resets
            last = exc
        time.sleep(backoff * (2 ** attempt))
    raise FetchError(f"giving up after {retries + 1} attempts: {full}: {last}")


@dataclass
class Artifact:
    source: str
    url: str
    fetched_at: str
    sha256: str
    nbytes: int
    path: str


class ArtifactStore:
    """Per-(source, run) raw capture directory with a manifest and resume support."""

    def __init__(self, source: str, run_id: str):
        self.source = source
        self.run_id = run_id
        self.dir = config.RAW_DIR / source / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest = self.dir / "manifest.jsonl"
        self._lock = threading.Lock()
        self._known: dict[str, Artifact] = {}
        if self.manifest.exists():
            for line in self.manifest.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    self._known[row["name"]] = Artifact(**{k: row[k] for k in Artifact.__dataclass_fields__})

    def get(self, name: str) -> Artifact | None:
        art = self._known.get(name)
        if art and Path(art.path).exists():
            return art
        return None

    def put(self, name: str, url: str, body: bytes, fetched_at: str) -> Artifact:
        path = self.dir / f"{name}.gz"
        tmp = path.with_suffix(".gz.tmp")
        with gzip.open(tmp, "wb") as fh:
            fh.write(body)
        tmp.replace(path)
        art = Artifact(self.source, url, fetched_at, hashlib.sha256(body).hexdigest(), len(body), str(path))
        with self._lock:
            self._known[name] = art
            with self.manifest.open("a") as fh:
                fh.write(json.dumps({"name": name, **art.__dict__}) + "\n")
        return art

    def fetch(self, name: str, url: str, params: dict | None = None, timeout: int = 120,
              validate=None, attempts: int = 5, backoff: float = 5.0) -> Artifact:
        """Fetch once per (run, name). ``validate(body)`` must raise ValueError for an error
        payload (e.g. an HTTP 200 carrying an ArcGIS error object); invalid bodies are never
        cached and are retried with exponential backoff."""
        cached = self.get(name)
        if cached:
            if validate is None:
                return cached
            try:
                validate(self.read(cached))
                return cached
            except ValueError:
                pass  # a previously cached error payload: refetch
        last: Exception | None = None
        for attempt in range(attempts):
            fetched_at = utcnow()
            full, body = http_get(url, params, timeout=timeout)
            try:
                if validate is not None:
                    validate(body)
                return self.put(name, full, body, fetched_at)
            except ValueError as exc:
                last = exc
                time.sleep(backoff * (2 ** attempt))
        raise FetchError(f"invalid payload after {attempts} attempts for {name}: {last}")

    def read(self, art: Artifact) -> bytes:
        with gzip.open(art.path, "rb") as fh:
            return fh.read()

    def mark_complete(self, meta: dict) -> None:
        (self.dir / "_COMPLETE.json").write_text(json.dumps({"completed_at": utcnow(), **meta}, indent=1))

    def is_complete(self) -> bool:
        return (self.dir / "_COMPLETE.json").exists()
