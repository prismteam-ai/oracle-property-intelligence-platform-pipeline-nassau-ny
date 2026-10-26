"""Synthetic-fixture tests (no real permits or people): run with  .venv/bin/python -m unittest discover -s tests"""
import unittest
from datetime import date

from pipeline import permit_classify as C
from pipeline import etrakit_parse as P

DETAIL_HTML = """
<ul><li><a><span class="rtsTxt">Permit Info</span></a></li><li><span class="rtsTxt">Site Info</span></li>
<li><span class="rtsTxt">Contacts (2)</span></li><li><span class="rtsTxt">Fees $0.00</span></li>
<li><span class="rtsTxt">Inspections(1)</span></li><li><span class="rtsTxt">Conditions (0)</span></li></ul>
<span id="cplMain_ctl07_lblPermitType">BUILDING</span><span id="cplMain_ctl07_lblPermitSubtype">RESIDENTIAL</span>
<span id="cplMain_ctl07_lblPermitDesc">RE-ROOF EXISTING HOUSE</span><span id="cplMain_ctl07_lblPermitStatus">OPEN</span>
<span id="cplMain_ctl07_lblPermitAppliedDate">3/16/2015</span><span id="cplMain_ctl07_lblPermitIssuedDate">3/20/2015</span>
<span id="cplMain_ctl07_lblPermitFinaledDate"></span><span id="cplMain_ctl07_lblPermitExpirationDate">3/20/2016</span>
<a id="cplMain_ctl08_hlSiteAddress" onclick="Javascript:openBingMap('../MS_vMap.aspx?Group=permit&amp;ActivityNo=X123', 685,520);">1 TEST ST&nbsp;</a>
<div class="column"><span id="x_lblParcelNumberLbl">PARID:</span> </div> <div class="column"> <a href="x">99-1-5</a> </div>
<tr class="rgRow" id="ctl00_cplMain_ctl09_rgContactInfo_ctl00__0"><td>OWNER</td><td>TEST OWNER</td><td>()-</td><td>&nbsp;</td><td>1 TEST ST</td><td>X, NY</td></tr>
<tr class="rgRow" id="ctl00_cplMain_ctl09_rgContactInfo_ctl00__1"><td>ROOFER</td><td>ACME ROOFING INC</td><td>()-</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td></tr>
<tr class="rgRow" id="ctl00_cplMain_ctl13_rgInspectionInfo_ctl00__0"><td>B-FINAL</td><td>COMPLETED</td><td><span>4/13/2017</span></td><td>&nbsp;</td><td>4/13/2017</td><td>&nbsp;</td></tr>
"""


class ParseTests(unittest.TestCase):
    def test_detail(self):
        d = P.parse_detail(DETAIL_HTML)
        self.assertEqual(d["permit_no_in_panel"], "X123")
        self.assertEqual(d["status"], "OPEN")
        self.assertEqual(d["applied_date"], "2015-03-16")
        self.assertEqual(len(d["contacts"]), 2)
        self.assertEqual(d["tab_counts"], {"contacts": 2, "inspections": 1, "conditions": 0})
        self.assertFalse(d["incomplete"])

    def test_missing_parid_does_not_scan_ahead(self):
        html = ('<div class="column"><span id="x_lblParcelNumberLbl">PARID:</span></div><div class="column"></div>'
                '<a href="y">Contact Type</a>')
        self.assertEqual(P.parse_detail(html)["site_parid"], "")

    def test_incomplete_detected(self):
        d = P.parse_detail(DETAIL_HTML.replace('rgContactInfo_ctl00__1', 'x'))
        self.assertTrue(d["incomplete"])


class ClassifyTests(unittest.TestCase):
    def test_description_strong(self):
        r = C.classify_roofing({"job_description": "TEAR OFF AND REROOF WITH ARCHITECTURAL SHINGLES"})
        self.assertTrue(r["is_roofing"]); self.assertEqual(r["confidence"], "high")

    def test_solar_roof_is_low(self):
        r = C.classify_roofing({"job_description": "SOLAR PANELS ON ROOF"})
        self.assertFalse(r["is_roofing"]); self.assertEqual(r["confidence"], "low")

    def test_empty_description_uses_roles_and_names(self):
        d = P.parse_detail(DETAIL_HTML.replace("RE-ROOF EXISTING HOUSE", ""))
        r = C.classify_roofing(d)
        self.assertTrue(r["is_roofing"]); self.assertEqual(r["confidence"], "medium")
        self.assertIn("contact_role_roofer", r["basis_summary"])

    def test_owner_named_roofing_not_evidence(self):
        r = C.classify_roofing({"contacts": [{"contact_type": "OWNER", "name": "ROOFING HOLDINGS LLC"}]})
        self.assertFalse(r["is_roofing"]); self.assertEqual(r["confidence"], "none")

    def test_exterior_fee_code_is_low_only(self):
        r = C.classify_roofing({"fees": [{"description": "B-REXT1"}]})
        self.assertFalse(r["is_roofing"]); self.assertEqual(r["confidence"], "low"); self.assertIn("fee_code_exterior_work", r["basis_summary"])

    def test_plumbing_not_roofing(self):
        r = C.classify_roofing({"permit_type": "BUILDING", "contacts": [{"contact_type": "PLUMBER", "name": "ACME PLUMBING"}],
                                "fees": [{"description": "B-WTR HTR"}], "inspections": [{"type": "P-FINAL"}]})
        self.assertEqual(r["confidence"], "none")


class StatusTests(unittest.TestCase):
    AS_OF = date(2026, 10, 25)

    def test_open_issued(self):
        s = C.derive_status({"status": "OPEN", "issued_date": "2015-03-20", "expiration_date": "2016-03-20", "inspections": []}, self.AS_OF)
        self.assertTrue(s["is_open"]); self.assertTrue(s["past_expiration"]); self.assertAlmostEqual(s["years_open"], 11.6, 1)
        self.assertEqual(s["open_since_basis"], "issued_date")

    def test_closed_uses_final_inspection(self):
        s = C.derive_status({"status": "CLOSED", "issued_date": "2015-03-16", "inspections": [
            {"type": "P-FINAL", "result": "COMPLETED", "completed_date": "2017-04-13"}]}, self.AS_OF)
        self.assertFalse(s["is_open"]); self.assertEqual(s["close_date"], "2017-04-13"); self.assertIsNone(s["years_open"])

    def test_placeholder_applied_date(self):
        s = C.derive_status({"status": "OPEN", "applied_date": "1900-03-10", "inspections": []}, self.AS_OF)
        self.assertTrue(s["is_open"]); self.assertIsNone(s["years_open"]); self.assertTrue(s["applied_date_placeholder"])

    def test_unissued_application_labelled(self):
        s = C.derive_status({"status": "OPEN", "applied_date": "1994-06-22", "inspections": []}, self.AS_OF)
        self.assertEqual(s["open_since_basis"], "applied_date_not_issued"); self.assertGreater(s["years_open"], 32)

    def test_unknown_status_flagged(self):
        s = C.derive_status({"status": "SUSPENDED", "issued_date": "2020-01-01", "inspections": []}, self.AS_OF)
        self.assertFalse(s["status_known"]); self.assertTrue(s["is_open"])


class SblTests(unittest.TestCase):
    def test_lot_key(self):
        self.assertEqual(C.lot_key("46-334-11 ")[0], "46-334-11")
        self.assertEqual(C.lot_key("11-D-5")[0], "11-D-5")
        self.assertEqual(C.lot_key("01-0334-0011")[0], "1-334-11")
        self.assertIsNone(C.lot_key("garbage")[0])


if __name__ == "__main__":
    unittest.main()
