"""Playwright client for the Town of Oyster Bay eTRAKiT permit search.

One request at a time (~1/s), exponential backoff on transient errors, and a hard stop
(``PortalBlocked``) on any captcha, challenge, 403/429, or lockout wording. Credentials
come from ``.env`` only and never reach logs, saved HTML, or exceptions (see etrakit_session).
"""
from __future__ import annotations

import re
import time
from collections import Counter

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from . import etrakit_parse as P
from .etrakit_session import BASE, Credentials, LoginFailed, Pacer, PortalBlocked

SEARCH_URL = BASE + "Search/permit.aspx"
LOGIN_URL = BASE + "login.aspx?lt=either&rd=~/Search/permit.aspx"
FIELD_SBL = "Permit_Main.SITE_APN"
FIELD_PERMIT = "Permit_Main.PERMIT_NO"
GRID = "#ctl00_cplMain_rgSearchRslts"

# Strong block signals only, matched against *visible* text (not page source: the page always
# loads Google's reCAPTCHA script, which is not a challenge).
STRONG_BLOCK_RX = re.compile(
    r"verify (that )?you are (a )?human|are you a robot|complete the captcha|solve the captcha|"
    r"just a moment|checking your browser|access denied|you have been blocked|too many requests|"
    r"too many (login )?attempts|account (has been |is )?(temporarily )?locked|locked out|"
    r"unusual traffic|rate limit exceeded", re.I)


class HarvestError(RuntimeError):
    """Transient failures exhausted their retries for one action."""


class DetailStateLost(HarvestError):
    """The results grid is gone (session bounced to login, or the page reloaded): the caller must re-run its search."""


DETAIL_ATTEMPTS = 3


class EtrakitClient:
    def __init__(self, creds: Credentials | None = None, headless: bool = True, max_logins: int = 3):
        self.creds = creds or Credentials()
        self.pacer = Pacer()
        self.headless = headless
        self.max_logins = max_logins
        self.logins = 0
        self.requests: Counter = Counter()      # portal document/xhr/fetch requests by phase
        self.statuses: Counter = Counter()
        self.phase = "login"
        self._flag: str | None = None
        self._pw = self._browser = self.page = None
        self._field = None
        self._oper = None
        self._page_no = 1

    # ---------------------------------------------------------------- lifecycle
    def __enter__(self) -> "EtrakitClient":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self.page = self._browser.new_context().new_page()
        self.page.set_default_timeout(60000)
        # politeness: no images, fonts or media
        self.page.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "font", "media") else r.continue_())
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
        self.login()
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _on_request(self, req) -> None:
        if "aspgov.com" in req.url and req.resource_type in ("document", "xhr", "fetch"):
            self.requests[self.phase] += 1

    def _on_response(self, resp) -> None:
        if "aspgov.com" in resp.url and resp.request.resource_type in ("document", "xhr", "fetch"):
            self.statuses[resp.status] += 1
            if resp.status in (403, 429):
                self._flag = f"HTTP {resp.status} from portal ({self.phase})"

    # ---------------------------------------------------------------- guards
    def guard(self, where: str) -> None:
        if self._flag:
            raise PortalBlocked(self._flag)
        page = self.page
        body = page.inner_text("body")
        m = STRONG_BLOCK_RX.search(body)
        if m:
            raise PortalBlocked(f"block wording '{m.group(0)}' at {where}")
        if page.locator("#cplMain_UpdatePanelreCAPTCHA").count() and page.inner_html("#cplMain_UpdatePanelreCAPTCHA").strip():
            raise PortalBlocked(f"reCAPTCHA panel populated at {where}")
        if page.locator("iframe[src*='recaptcha'], iframe[title*='reCAPTCHA' i], iframe[src*='challenges.cloudflare']").count():
            raise PortalBlocked(f"captcha/challenge iframe at {where}")

    def _guard_quietly(self, where: str) -> None:
        """guard() after a failed action: a portal block still stops the run, but a timeout while reading the page
        to look for a block must not replace the original error (it would escape every retry loop)."""
        try:
            self.guard(where)
        except (PWTimeout, PWError):
            pass

    def _act(self, label: str, fn, retries: int = 3, pace: bool = True):
        """Run one portal action with a block guard and exponential backoff. ``pace`` spaces actions that send a
        request to the portal (>= 1 s apart); purely local steps (fill a box, wait for the DOM) are not paced."""
        last = None
        for attempt in range(retries + 1):
            if pace:
                self.pacer.wait()
            try:
                out = fn()
                self.guard(label)
                return out
            except (PortalBlocked, LoginFailed):
                raise
            except (PWTimeout, PWError) as exc:
                last = exc
                self._guard_quietly(label + " (after error)")
                time.sleep(2.0 * (2 ** attempt))
        raise HarvestError(f"{label}: {self.creds.scrub(str(last))[:200]}")

    # ---------------------------------------------------------------- login
    def login(self) -> None:
        if self.logins >= self.max_logins:
            raise LoginFailed("login limit for this run reached (no more attempts)")
        self.logins += 1
        self.phase = "login"
        page = self.page
        self._act("login page", lambda: page.goto(LOGIN_URL, wait_until="domcontentloaded"))
        self._act("fill user", lambda: page.fill("#cplMain_txtPublicUserName", self.creds.username), pace=False)
        self._act("fill password", lambda: page.fill("#cplMain_txtPublicPassword", self.creds.password), pace=False)
        self._act("submit login", lambda: (page.click("#cplMain_btnPublicLogin"), page.wait_for_load_state("domcontentloaded")))
        time.sleep(1.5)
        body = page.inner_text("body")
        if "Invalid Login" in body:
            raise LoginFailed("portal answered Invalid Login (no retry)")
        m = re.search(r"LOGGED IN AS:\s*([^\n]+)", body)
        if m:  # the account holder's display name is personal data: scrub it from everything saved
            self.creds._secrets.append(m.group(1).strip())
        if "Search/permit.aspx" not in page.url:
            self._act("open permit search", lambda: page.goto(SEARCH_URL, wait_until="domcontentloaded"))
        self.guard("after login")
        self._field = self._oper = None

    def _ensure_session(self) -> None:
        if "login.aspx" in self.page.url.lower():
            self.login()
            self._field = self._oper = None     # a fresh page has the default search-by / operator selected
        elif not self.page.locator("#cplMain_ddSearchBy").count():   # wandered off the search page: go back to it
            self._act("reopen permit search", lambda: self.page.goto(SEARCH_URL, wait_until="domcontentloaded"))
            self._field = self._oper = None

    # ---------------------------------------------------------------- search
    def _post(self, trigger, label: str, timeout: int = 90000, settle: bool = True):
        """Fire an action that causes one UpdatePanel postback; wait for it (and, if ``settle``, network idle)."""
        def run():
            with self.page.expect_response(lambda r: r.request.method == "POST" and "permit.aspx" in r.url, timeout=timeout):
                trigger()
            if settle:
                self.page.wait_for_load_state("networkidle", timeout=timeout)
        return self._act(label, run)

    _SIG_JS = """() => { const g = document.getElementById('ctl00_cplMain_rgSearchRslts');
        const rows = g ? [...g.querySelectorAll('tr.rgRow, tr.rgAltRow')].map(r => r.innerText.replace(/\\s+/g, ' ')).join('|') : '';
        const m = document.getElementById('cplMain_lblMoreResults');
        const pg = (document.body.innerText.match(/page\\s+\\d+\\s+of\\s+\\d+/i) || [''])[0];
        return rows + '#' + (m ? m.innerText : '') + '#' + pg; }"""

    def _grid_sig(self) -> str:
        return self.page.evaluate(self._SIG_JS)

    def _wait_results(self, expect_page: int | None = None, prev_sig: str | None = None) -> None:
        """Wait until the grid shows NEW results: rows that differ from ``prev_sig`` (stale grids linger after a
        new search), on the expected page, or the portal's "no results" text."""
        js = """([expectPage, prevSig, sigFn]) => {
            const txt = document.body.innerText;
            if (/no results to match your search/i.test(txt)) return true;
            const g = document.getElementById('ctl00_cplMain_rgSearchRslts');
            if (!(g && g.querySelectorAll('tr.rgRow, tr.rgAltRow').length > 0)) return false;
            if (expectPage) { const m = txt.match(/page\\s+(\\d+)\\s+of\\s+\\d+/i); if (!(m && parseInt(m[1], 10) === expectPage)) return false; }
            if (prevSig !== null && prevSig !== undefined) { return (eval('(' + sigFn + ')'))() !== prevSig; }
            return true; }"""
        def run():
            try:
                self.page.wait_for_function(js, arg=[expect_page, prev_sig, self._SIG_JS], timeout=60000 if prev_sig is None else 25000)
            except PWTimeout:
                if prev_sig is None:
                    raise
                # identical results to the previous query (e.g. a repeated search): accept the current grid
        self._act("wait for results", run, pace=False)

    def grid_snapshot(self) -> tuple[str, dict]:
        """Raw grid HTML (scrubbed) plus parsed rows/counts; the count message is read before any row click."""
        page = self.page
        grid = page.evaluate("() => { const g = document.getElementById('ctl00_cplMain_rgSearchRslts'); return g ? g.outerHTML : ''; }")
        msg = page.evaluate("() => { const m = document.getElementById('cplMain_lblMoreResults'); return m ? m.outerHTML : ''; }")
        raw = self.creds.scrub(msg + "\n" + grid)
        parsed = P.parse_grid(raw)
        return raw, parsed

    def search(self, field: str, oper: str, value: str) -> tuple[str, dict]:
        """Run one search; returns the raw grid HTML and its parsed first page."""
        self._ensure_session()
        self.phase = "search"
        page = self.page
        if self._field != field:
            self._post(lambda: page.select_option("#cplMain_ddSearchBy", field), "set search-by")
            self._field, self._oper = field, None
        if self._oper != oper:
            self._act("set operator", lambda: page.select_option("#cplMain_ddSearchOper", oper), pace=False)
            self._oper = oper
        self._act("fill value", lambda: page.fill("#cplMain_txtSearchString", value), pace=False)
        prev = self._grid_sig()
        self._post(lambda: page.click("#ctl00_cplMain_btnSearch"), f"search {value}")
        self._wait_results(prev_sig=prev)
        raw, g = self.grid_snapshot()
        # the grid keeps its page index across searches (it opens on the last page if the previous query ended
        # deeper than this one has): always start from page 1 so no first-page rows are skipped
        if g["page"] and g["page"] > 1:
            self.phase = "page"
            self._post(lambda: self.page.evaluate("changePage('first')"), "first page")
            self._wait_results(expect_page=1)
            raw, g = self.grid_snapshot()
        self._page_no = g["page"] or 1
        return raw, g

    def next_page(self) -> tuple[str, dict]:
        self.phase = "page"
        self._post(lambda: self.page.evaluate("changePage('next')"), "next page")
        self._page_no += 1
        self._wait_results(expect_page=self._page_no)
        return self.grid_snapshot()

    def last_page(self) -> tuple[str, dict]:
        self.phase = "page"
        self._post(lambda: self.page.evaluate("changePage('last')"), "last page")
        self._wait_results()
        raw, g = self.grid_snapshot()
        self._page_no = g["page"] or self._page_no
        return raw, g

    # ---------------------------------------------------------------- detail
    def open_detail(self, row_index: int, permit_no: str | None = None) -> str:
        """Click a grid row; returns the scrubbed detail-panel HTML (all tabs arrive in one response).

        Every Playwright call in the detail path can time out (the panel never renders after a slow or dropped
        postback). Each attempt re-clicks the row (a fresh postback, no page reload, so the grid and its page index
        survive); after DETAIL_ATTEMPTS the permit is reported as a HarvestError and the caller logs it and moves on.
        If the grid itself is gone, DetailStateLost tells the caller to re-run its search first."""
        last: Exception | None = None
        for attempt in range(DETAIL_ATTEMPTS):
            try:
                return self._open_detail_once(row_index, permit_no)
            except (PWTimeout, PWError) as exc:
                last = exc
                self._guard_quietly(f"detail row {row_index} (attempt {attempt + 1})")   # PortalBlocked still propagates
                if self._grid_lost():
                    raise DetailStateLost(f"results grid lost while opening row {row_index}") from exc
                time.sleep(2.0 * (2 ** attempt))
        raise HarvestError(f"detail panel for row {row_index} ({permit_no}) not available after {DETAIL_ATTEMPTS} attempts: "
                           f"{self.creds.scrub(str(last))[:160]}")

    def _grid_lost(self) -> bool:
        try:
            return "login.aspx" in self.page.url.lower() or self.page.locator(GRID).count() == 0
        except (PWTimeout, PWError):
            return True

    def _open_detail_once(self, row_index: int, permit_no: str | None) -> str:
        self.phase = "detail"
        page = self.page
        # no networkidle here: the targeted waits below (panel is for this permit, tabs/rows rendered) are the settle signal
        self._post(lambda: page.click(f"{GRID}_ctl00__{row_index} td:first-child"), f"open row {row_index}", settle=False)
        if permit_no:  # the panel's site-address link carries ActivityNo=<permit>; confirm it is the clicked permit
            try:
                page.wait_for_function("(no) => { const a = document.querySelector(\"[id$='hlSiteAddress']\"); "
                                       "return !!a && (a.getAttribute('onclick') || '').includes('ActivityNo=' + no); }",
                                       arg=permit_no, timeout=8000)
            except (PWTimeout, PWError):
                pass  # verification in the harvester records a mismatch if the panel is stale
        # tabs and grids render client-side shortly after the postback: wait until each tab's
        # "(N)" count equals the number of rows actually rendered (best effort; parser flags a gap)
        try:
            page.wait_for_function("""() => {
                const root = document.getElementById('divSearchDetails'); if (!root) return false;
                const labs = [...root.querySelectorAll('.rtsTxt')].map(e => e.textContent.trim());
                if (labs.length < 5) return false;
                const want = (re) => { const l = labs.find(x => re.test(x)); const m = l && l.match(/\\((\\d+)\\)/); return m ? parseInt(m[1], 10) : 0; };
                const have = (frag) => root.querySelectorAll(`tr[id*='${frag}_ctl00__']`).length;
                return have('rgContactInfo') >= want(/^Contacts/) && have('rgInspectionInfo') >= want(/^Inspections/); }""", timeout=20000)
        except (PWTimeout, PWError):
            pass
        # the one read that used to be unprotected: a short timeout so a missing panel fails fast and is retried
        html = page.inner_html("#divSearchDetails", timeout=20000)   # tab strip + every pane (Permit/Site/Contacts/Fees/Inspections/Conditions)
        return self.creds.scrub(html)
