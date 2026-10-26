"""Playwright timeouts in the detail path must never escape open_detail / _capture (no browser or portal needed)."""
import types
import unittest
from unittest import mock

from playwright.sync_api import TimeoutError as PWTimeout

from pipeline import etrakit
from pipeline.etrakit import DetailStateLost, EtrakitClient, HarvestError


class FakeLocator:
    def __init__(self, n): self.n = n
    def count(self): return self.n


class FakePage:
    """inner_html fails ``fail_times`` times with a Playwright timeout, then returns the panel."""
    def __init__(self, fail_times=0, grid_present=True, url="https://portal/Search/permit.aspx"):
        self.fail_times, self.grid_present, self.url, self.reads = fail_times, grid_present, url, 0
    def wait_for_function(self, *a, **k): return None
    def inner_text(self, sel): return "Permit search"
    def locator(self, sel): return FakeLocator(1 if (sel == etrakit.GRID and self.grid_present) else 0)
    def inner_html(self, sel, timeout=None):
        self.reads += 1
        if self.reads <= self.fail_times:
            raise PWTimeout('Page.inner_html: Timeout 60000ms exceeded.\nCall log:\n  waiting for locator("#divSearchDetails")')
        return "<div>panel</div>"


def make_client(page):
    c = EtrakitClient(creds=types.SimpleNamespace(scrub=lambda s: s))
    c.page = page
    c.clicks = 0
    def post(*a, **k): c.clicks += 1
    c._post = post
    return c


class OpenDetailResilience(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(etrakit.time, "sleep", lambda s: None)
        p.start(); self.addCleanup(p.stop)

    def test_retries_then_succeeds(self):
        page = FakePage(fail_times=2); c = make_client(page)
        self.assertEqual(c.open_detail(0, "P1"), "<div>panel</div>")
        self.assertEqual(c.clicks, 3)          # the row was re-clicked for each attempt

    def test_gives_up_with_harvest_error_not_timeout(self):
        page = FakePage(fail_times=99); c = make_client(page)
        with self.assertRaises(HarvestError) as cm:
            c.open_detail(0, "P1")
        self.assertNotIsInstance(cm.exception, DetailStateLost)
        self.assertEqual(page.reads, etrakit.DETAIL_ATTEMPTS)

    def test_grid_lost_is_reported_for_recovery(self):
        page = FakePage(fail_times=99, grid_present=False); c = make_client(page)
        with self.assertRaises(DetailStateLost):
            c.open_detail(0, "P1")
        self.assertEqual(page.reads, 1)        # no point retrying without a grid

    def test_login_bounce_counts_as_lost_grid(self):
        page = FakePage(fail_times=99, url="https://portal/login.aspx?rd=x"); c = make_client(page)
        with self.assertRaises(DetailStateLost):
            c.open_detail(0, "P1")


class CaptureMovesOn(unittest.TestCase):
    def _harvester(self, exc):
        from pipeline.etrakit_harvest import Harvester
        client = mock.Mock(); client.open_detail.side_effect = exc
        h = Harvester.__new__(Harvester)
        h.client, h.run_id, h.log = client, "r", lambda m: None
        h.stop, h.budget, h.captured_this_run = False, None, 0
        h.totals = {"permits": 0, "blocks_complete": 0, "queries": 0, "failures": 0}
        return h, client

    def test_failed_permit_is_logged_and_skipped(self):
        h, client = self._harvester(HarvestError("detail panel not available"))
        st = {"block_key": "1-1-", "failures": [], "permits": {}}
        h._capture(st, {}, {"row_index": 0, "permit_no": "P9", "sbl": "", "record_id": ""}, "1-1-", "BEGINS WITH", 2)
        self.assertEqual([f["permit_no"] for f in st["failures"]], ["P9"])
        self.assertEqual(h.totals["failures"], 1)
        client.search.assert_not_called()

    def test_lost_grid_reruns_search_and_returns_to_page(self):
        h, client = self._harvester(DetailStateLost("grid gone"))
        st = {"block_key": "1-1-", "failures": [], "permits": {}}
        h._capture(st, {}, {"row_index": 0, "permit_no": "P9", "sbl": "", "record_id": ""}, "1-1-", "BEGINS WITH", 3)
        client.search.assert_called_once()
        self.assertEqual(client.next_page.call_count, 2)    # page 3 = two next-page steps from page 1


if __name__ == "__main__":
    unittest.main()
