"""Pure parsers for Oyster Bay eTRAKiT (CentralSquare) permit search and detail HTML.

Inputs are the saved panel HTML (``cplMain_UpdatePanel1`` for the grid and
``cplMain_UpdatePanelDetail`` for the detail), so the parsers can be re-run offline on raw
captures. Everything visible is extracted; contact fields stay raw (no identity resolution).
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime


def text(s: str | None) -> str:
    if s is None:
        return ""
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def iso_date(s: str | None) -> str | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _id_text(html: str, suffix: str) -> str:
    m = re.search(r'<(span|a)\b[^>]*\bid="[^"]*%s"[^>]*>(.*?)</\1>' % re.escape(suffix), html, re.S)
    return text(m.group(2)) if m else ""


def _money(s: str | None) -> float | None:
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", s or "")
    return float(m.group(1).replace(",", "")) if m else None


def parse_grid(html: str) -> dict:
    """Search-result grid: rows (permit_no, sbl, record_id) and the count message."""
    rows = []
    for m in re.finditer(r'<tr[^>]*class="rg(?:Alt)?Row[^"]*"[^>]*id="[^"]*rgSearchRslts_ctl00__(\d+)"[^>]*>(.*?)</tr>', html, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", m.group(2), re.S)
        if len(tds) >= 2:
            rows.append({"row_index": int(m.group(1)), "permit_no": text(tds[0]), "sbl": text(tds[1]),
                         "record_id": text(tds[2]) if len(tds) > 2 else ""})
    flat = text(html)
    returned = total = None
    m = re.search(r"returned\s+([\d,]+)\s+of\s+([\d,]+)\s+total\s+records", flat, re.I)
    if m:
        returned, total = int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
    else:
        m = re.search(r"returned\s+([\d,]+)\s+(?:total\s+)?records?", flat, re.I)
        if m:
            returned = total = int(m.group(1).replace(",", ""))
    pages = None
    m = re.search(r"page\s+(\d+)\s+of\s+(\d+)", flat, re.I)
    page_no = None
    if m:
        page_no, pages = int(m.group(1)), int(m.group(2))
    return {"rows": rows, "returned": returned, "total": total, "page": page_no, "pages": pages,
            "no_records": bool(re.search(r"no records|0 records|did not return", flat, re.I)) and not rows}


def parse_detail(html: str) -> dict:
    """Detail panel: Permit Info, Site Info, Contacts, Fees, Inspections, Conditions."""
    site_link = re.search(r'id="[^"]*hlSiteAddress"[^>]*onclick="[^"]*ActivityNo=([^&\'"]+)', html)
    # the value sits in the very next column div; a permit with no PARID has an empty column (never scan ahead)
    parid = re.search(r'PARID:</span>\s*</div>\s*<div class="column">(.*?)</div>', html, re.S)
    tabs = {text(m.group(1)).split(" (")[0].split(" $")[0]: text(m.group(1)) for m in re.finditer(r'<span class="rtsTxt">(.*?)</span>', html, re.S)}
    out = {
        "permit_no_in_panel": _html.unescape(site_link.group(1)) if site_link else None,
        "permit_type": _id_text(html, "lblPermitType"),
        "permit_subtype": _id_text(html, "lblPermitSubtype"),
        "job_description": _id_text(html, "lblPermitDesc"),
        "status": _id_text(html, "lblPermitStatus"),
        "applied_date": iso_date(_id_text(html, "lblPermitAppliedDate")),
        "approved_date": iso_date(_id_text(html, "lblPermitApprovedDate")),
        "issued_date": iso_date(_id_text(html, "lblPermitIssuedDate")),
        "finaled_date": iso_date(_id_text(html, "lblPermitFinaledDate")),
        "expiration_date": iso_date(_id_text(html, "lblPermitExpirationDate")),
        "site_address": _id_text(html, "hlSiteAddress"),
        "site_city_state_zip": _id_text(html, "lblSiteCityStateZip"),
        "property_type": _id_text(html, "lblPropertyType"),
        "site_parid": text(parid.group(1)) if parid else "",
        "site_subdivision": _id_text(html, "lblSiteSubdivision"),
        "site_section": _id_text(html, "lblSiteTract"),
        "site_block": _id_text(html, "lblSiteBlock"),
        "site_lot": _id_text(html, "lblSiteLotNo"),
        "site_village": _id_text(html, "lblSectionTwpRng"),
        "lot_size_sf": _id_text(html, "lblSiteLotSqFt"),
        "tab_labels": sorted(tabs.values()),
    }
    # contacts
    contacts = []
    for m in re.finditer(r'<tr[^>]*id="[^"]*rgContactInfo_ctl00__(\d+)"[^>]*>(.*?)</tr>', html, re.S):
        tds = [text(t) for t in re.findall(r"<td[^>]*>(.*?)</td>", m.group(2), re.S)]
        tds += [""] * (6 - len(tds))
        contacts.append({"seq": int(m.group(1)), "contact_type": tds[0], "name": tds[1], "phone": tds[2],
                         "email": tds[3], "address": tds[4], "city_state_zip": tds[5]})
    out["contacts"] = contacts
    # fees
    fees = []
    for m in re.finditer(r'<tr[^>]*id="[^"]*rgFeeDetails_ctl00__(\d+)"[^>]*>(.*?)</tr>', html, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", m.group(2), re.S)
        if len(tds) >= 3:
            fees.append({"seq": int(m.group(1)), "description": text(tds[0]), "amount": _money(text(tds[1])), "paid_date": iso_date(text(tds[2]))})
    out["fees"] = fees
    flat = text(re.search(r'id="[^"]*lblTotalFees".*?</table>', html, re.S).group(0)) if re.search(r'id="[^"]*lblTotalFees"', html) else ""
    out["fees_charged"] = _money(re.sub(r".*Charged:", "", flat.split("Balance Due")[0])) if "Charged:" in flat else None
    out["fees_balance_due"] = _money(flat.split("Balance Due:")[1]) if "Balance Due:" in flat else None
    # inspections
    insps = []
    for m in re.finditer(r'<tr[^>]*id="[^"]*rgInspectionInfo_ctl00__(\d+)"[^>]*>(.*?)</tr>', html, re.S):
        tds = [text(t) for t in re.findall(r"<td[^>]*>(.*?)</td>", m.group(2), re.S)]
        tds += [""] * (6 - len(tds))
        insps.append({"seq": int(m.group(1)), "type": tds[0], "result": tds[1], "scheduled_date": iso_date(tds[2]),
                      "completed_date": iso_date(tds[4])})
    out["inspections"] = insps
    out["conditions_none"] = "no conditions on this record" in text(html).lower()
    counts = {}
    for lab in tabs.values():
        m = re.match(r"(Contacts|Inspections|Conditions)\s*\((\d+)\)", lab)
        if m:
            counts[m.group(1).lower()] = int(m.group(2))
    out["tab_counts"] = counts
    out["incomplete"] = (not tabs) or counts.get("contacts", 0) != len(contacts) or counts.get("inspections", 0) != len(insps)
    return out
