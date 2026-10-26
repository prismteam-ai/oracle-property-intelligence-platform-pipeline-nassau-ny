"""One-shot anonymous check of the Town of Hempstead OpenGov public portal.

Question: can an anonymous browser user search permit records?

Rules: one session, stock headless Chromium (no stealth, no fingerprint changes, no
retries). If a CAPTCHA, challenge, or login wall appears, stop and record it. Outputs
(screenshots, network log, result.json) go under data/opengov_check/<timestamp>/.

    PLAYWRIGHT_BROWSERS_PATH=.playwright-browsers .venv/bin/python tools/opengov_check.py
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

PORTAL = "https://hempsteadny.portal.opengov.com/"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "opengov_check" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
GATE_RX = re.compile(r"captcha|recaptcha|turnstile|challenge|verify you are human|log in|sign in|sign up|create an account", re.I)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    net, result = [], {"portal": PORTAL, "started_at": datetime.now(timezone.utc).isoformat(), "steps": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("response", lambda r: net.append({"url": r.url, "status": r.status, "method": r.request.method})
                if any(h in r.url for h in ("viewpointcloud", "opengov", "recaptcha", "turnstile", "challenges.cloudflare")) else None)

        page.goto(PORTAL, wait_until="networkidle", timeout=60000)
        page.screenshot(path=str(OUT / "01_home.png"), full_page=True)
        body = page.inner_text("body")
        search_inputs = page.locator("input[type=search], input[placeholder*='earch' i]")
        result["steps"].append({"step": "home", "url": page.url, "title": page.title(),
                                "search_inputs": search_inputs.count(),
                                "gate_terms_visible": sorted(set(m.lower() for m in GATE_RX.findall(body)))})

        # Single search attempt through the portal's own search box, if one is offered.
        if search_inputs.count():
            box = search_inputs.first
            box.click()
            box.fill("roof")
            box.press("Enter")
            page.wait_for_load_state("networkidle", timeout=60000)
            page.wait_for_timeout(3000)
            page.screenshot(path=str(OUT / "02_after_search.png"), full_page=True)
            body2 = page.inner_text("body")
            frames = [f.url for f in page.frames if f.url and f.url != "about:blank"]
            result["steps"].append({
                "step": "search 'roof'", "url": page.url,
                "gate_terms_visible": sorted(set(m.lower() for m in GATE_RX.findall(body2))),
                "captcha_frames": [u for u in frames if re.search(r"recaptcha|turnstile|challenges", u)],
                "record_like_rows": len(re.findall(r"\b(?:BLD|BP|B)[-\s]?\d{2,}[-\d]*\b", body2)),
                "body_excerpt": body2[:1500],
            })
        else:
            result["steps"].append({"step": "search", "note": "no search input offered to anonymous user on home page"})
        browser.close()

    api = [n for n in net if "search.viewpointcloud" in n["url"] or "/records" in n["url"]]
    result["search_api_responses"] = api
    result["captcha_or_challenge_requests"] = [n for n in net if re.search(r"recaptcha|turnstile|challenges", n["url"])]
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    (OUT / "network.json").write_text(json.dumps(net, indent=1))
    (OUT / "result.json").write_text(json.dumps(result, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k != "steps"}, indent=1))
    for s in result["steps"]:
        print(json.dumps({k: v for k, v in s.items() if k != "body_excerpt"}))
    print("out:", OUT)


if __name__ == "__main__":
    main()
