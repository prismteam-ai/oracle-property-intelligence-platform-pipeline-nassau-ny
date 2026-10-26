"""Credentialed Playwright session for the Town of Oyster Bay eTRAKiT portal.

Safety rules enforced here (operator instructions):
- credentials come only from ``.env`` (ETRAKIT_USERNAME / ETRAKIT_PASSWORD); they are never
  printed, logged, or written: every string leaving this module goes through ``scrub()``;
- one browser action at a time, spaced >= MIN_INTERVAL seconds, exponential backoff on
  transient errors;
- any captcha, challenge, HTTP 403/429, or lockout wording raises ``PortalBlocked``; the
  caller must stop. Nothing here retries around a block or changes the browser fingerprint.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import config

BASE = "https://oys-trk.aspgov.com/eTRAKiT/"
MIN_INTERVAL = 1.0  # seconds between portal actions (about 1 request per second)

BLOCK_RX = re.compile(
    r"captcha|recaptcha|turnstile|challenge-platform|just a moment|access denied|"
    r"too many (requests|attempts)|temporarily (locked|blocked)|account (is )?locked|"
    r"locked out|rate limit|unusual traffic|forbidden", re.I)


class PortalBlocked(RuntimeError):
    """A captcha, block page, 403/429 or lockout warning appeared. Stop; do not bypass."""


class LoginFailed(RuntimeError):
    pass


def _load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


class Credentials:
    def __init__(self) -> None:
        env = _load_env(config.REPO_ROOT / ".env")
        self.username = env.get("ETRAKIT_USERNAME") or os.environ.get("ETRAKIT_USERNAME", "")
        self.password = env.get("ETRAKIT_PASSWORD") or os.environ.get("ETRAKIT_PASSWORD", "")
        if not self.username or not self.password:
            raise SystemExit("ETRAKIT_USERNAME / ETRAKIT_PASSWORD missing from .env")
        # also scrub URL-encoded and HTML-escaped variants
        import html
        import urllib.parse
        secrets = {self.username, self.password}
        for s in list(secrets):
            secrets.add(urllib.parse.quote(s))
            secrets.add(urllib.parse.quote_plus(s))
            secrets.add(html.escape(s))
        self._secrets = sorted((s for s in secrets if s), key=len, reverse=True)

    def scrub(self, text: str) -> str:
        if not text:
            return text
        for s in self._secrets:
            text = text.replace(s, "[REDACTED]")
        return text


class Pacer:
    def __init__(self, min_interval: float = MIN_INTERVAL):
        self.min_interval = min_interval
        self._last = 0.0
        self.actions = 0

    def wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()
        self.actions += 1


def check_block(creds: Credentials, status: int | None, text: str, where: str) -> None:
    if status in (403, 429):
        raise PortalBlocked(f"HTTP {status} at {where}")
    m = BLOCK_RX.search(text or "")
    if m:
        raise PortalBlocked(f"block/captcha/lockout wording '{m.group(0)}' at {where}")
