"""Roofing classification and open/close derivations for Oyster Bay eTRAKiT permits.

Pure functions (no I/O). Every roofing call carries its basis and confidence; nothing here
resolves a contractor's legal identity (names and roles are raw portal text).

Observed in the pilot (25 permits, block 46-334-): Job Description was empty on every permit, so
description text alone would miss real roofing work. The classifier therefore also uses permit
type/subtype, contact roles, contact (company) names, and fee / inspection codes.
"""
from __future__ import annotations

import re
from datetime import date

# --------------------------------------------------------------------------- roofing
STRONG_DESC = re.compile(r"\bre-?roof(?:ing)?\b|\broofing\b|\bshingl(?:e|es|ing)\b|\btear[- ]?off\b|"
                         r"\broof\s*(?:replacement|replace|repair|covering|install\w*|over|overlay|membrane)\b|\bnew\s+roof\b", re.I)
WEAK_DESC = re.compile(r"\broofs?\b", re.I)
NOT_REROOF_CONTEXT = re.compile(r"\bsolar\b|\bphotovolt\w*\b|\bpv\b|\bantenna\b|\bdormer\b|\baddition\b|\bdeck\b|\bskylight\b|\bvents?\b|\bventilat\w*\b|\bgutter\b|\bchimney\b|"
                                r"roof\s*top\s*(?:unit|hvac|a/?c)|\brtu\b", re.I)
ROLE_ROOFER = re.compile(r"roof", re.I)
NAME_ROOF = re.compile(r"\broof(?:ing|er|ers)?\b|\bshingle", re.I)
CODE_ROOF = re.compile(r"roof", re.I)          # fee codes and inspection types, e.g. "B-ROOF"
CODE_EXTERIOR = re.compile(r"^B-[RC]EXT", re.I)  # residential/commercial *exterior* work fee codes: may include roofing or siding
CONFIDENCE_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}
NONOWNER_ROLES = ("OWNER", "APPLICANT")


def classify_roofing(detail: dict) -> dict:
    """Return {is_roofing, confidence, bases:[{basis, confidence, evidence}], work_type}.

    ``is_roofing`` is true for medium or high confidence; low is recorded but not counted."""
    bases: list[dict] = []
    desc = (detail.get("job_description") or "").strip()
    if desc:
        strong = STRONG_DESC.search(desc)
        weak = WEAK_DESC.search(desc)
        if strong and not NOT_REROOF_CONTEXT.search(desc):
            bases.append({"basis": "job_description_roof_keyword", "confidence": "high", "evidence": desc[:160]})
        elif strong or weak:
            ctx = NOT_REROOF_CONTEXT.search(desc)
            bases.append({"basis": "job_description_roof_keyword_ambiguous", "confidence": "low" if ctx else "medium",
                          "evidence": desc[:160]})
    ptxt = f"{detail.get('permit_type', '')} {detail.get('permit_subtype', '')}"
    if ROLE_ROOFER.search(ptxt):
        bases.append({"basis": "permit_type_roof", "confidence": "high", "evidence": ptxt.strip()})
    for c in detail.get("contacts") or []:
        role, name = (c.get("contact_type") or ""), (c.get("name") or "")
        if ROLE_ROOFER.search(role):
            bases.append({"basis": "contact_role_roofer", "confidence": "medium", "evidence": f"{role}: {name}"[:160]})
        elif NAME_ROOF.search(name) and role.upper() not in NONOWNER_ROLES[:1]:
            # the applicant may be the roofing company; an owner named like a roofing firm is not evidence
            conf = "medium" if role.upper() in ("CONTRACTOR", "APPLICANT") else "low"
            bases.append({"basis": "contractor_name_contains_roof", "confidence": conf, "evidence": f"{role}: {name}"[:160]})
    for f in detail.get("fees") or []:
        if CODE_ROOF.search(f.get("description") or ""):
            bases.append({"basis": "fee_code_roof", "confidence": "medium", "evidence": f["description"]})
    for f in detail.get("fees") or []:
        if CODE_EXTERIOR.search(f.get("description") or ""):
            bases.append({"basis": "fee_code_exterior_work", "confidence": "low", "evidence": f["description"]})
            break
    for i in detail.get("inspections") or []:
        if CODE_ROOF.search(i.get("type") or ""):
            bases.append({"basis": "inspection_type_roof", "confidence": "medium", "evidence": i["type"]})
    best = max((CONFIDENCE_RANK[b["confidence"]] for b in bases), default=0)
    conf = {v: k for k, v in CONFIDENCE_RANK.items()}[best]
    return {"is_roofing": best >= CONFIDENCE_RANK["medium"], "confidence": conf, "bases": bases,
            "basis_summary": ",".join(sorted({b["basis"] for b in bases}))}


# --------------------------------------------------------------------------- status
CLOSED_STATUSES = {"CLOSED", "FINALED", "FINAL", "COMPLETE", "COMPLETED", "CO ISSUED", "CERTIFICATE ISSUED", "APPROVED FINAL"}
VOID_STATUSES = {"VOID", "VOIDED", "CANCELLED", "CANCELED", "WITHDRAWN", "DENIED", "REJECTED", "REFUSED"}
OPEN_STATUSES = {"OPEN", "ISSUED", "APPROVED", "APPLIED", "PENDING", "IN REVIEW", "EXPIRED", "ACTIVE", "ON HOLD", "UNDER REVIEW"}
PLACEHOLDER_YEARS = {1900, 1901}   # a '1900-03-10' applied date is a database placeholder, not a real date
FINAL_INSPECTION = re.compile(r"final", re.I)
DONE_RESULTS = re.compile(r"complete|pass|approved|satisf", re.I)


def valid_date(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        y = int(iso[:4])
    except ValueError:
        return None
    return None if y in PLACEHOLDER_YEARS or y < 1850 or y > 2100 else iso


def derive_status(detail: dict, as_of: date) -> dict:
    """is_open / years_open / past_expiration with their bases.

    - ``status_class``: closed | void | open | unknown (unknown statuses are treated as open and flagged);
    - ``close_date``: Finaled Date, else the latest completed *final* inspection, else None (the portal shows no
      Finaled Date on CLOSED permits, so the inspection date is the best close signal);
    - ``years_open``: for open permits only, from the issue date (else the applied date, labelled)."""
    status = (detail.get("status") or "").strip().upper()
    if status in CLOSED_STATUSES:
        cls, known = "closed", True
    elif status in VOID_STATUSES:
        cls, known = "void", True
    elif status in OPEN_STATUSES:
        cls, known = "open", True
    else:
        cls, known = ("open" if status else "unknown"), False
    fin = valid_date(detail.get("finaled_date"))
    close_date, close_basis = (fin, "finaled_date") if fin else (None, None)
    if not close_date:
        done = [i["completed_date"] for i in detail.get("inspections") or []
                if i.get("completed_date") and FINAL_INSPECTION.search(i.get("type") or "") and DONE_RESULTS.search(i.get("result") or "")]
        done = [d for d in done if valid_date(d)]
        if done:
            close_date, close_basis = max(done), "last_completed_final_inspection"
    if fin and cls == "open":
        cls = "closed"       # finaled date contradicts an open status
    is_open = cls in ("open", "unknown")
    issued, applied = valid_date(detail.get("issued_date")), valid_date(detail.get("applied_date"))
    open_since, open_basis = (issued, "issued_date") if issued else ((applied, "applied_date_not_issued") if applied else (None, None))
    years_open = None
    if is_open and open_since:
        y, m, d = (int(x) for x in open_since.split("-"))
        years_open = round((as_of - date(y, m, d)).days / 365.25, 2)
    exp = valid_date(detail.get("expiration_date"))
    past_exp = bool(is_open and exp and exp < as_of.isoformat())
    return {"status_class": cls, "status_known": known, "is_open": is_open, "close_date": close_date, "close_date_basis": close_basis,
            "open_since": open_since, "open_since_basis": open_basis, "years_open": years_open,
            "expiration_date_valid": exp, "past_expiration": past_exp,
            "applied_date_placeholder": bool(detail.get("applied_date") and not applied)}


# --------------------------------------------------------------------------- SBL
SBL_RX = re.compile(r"^\s*(\d+)-([A-Za-z0-9]+)-(\d+)(.*)$")


def lot_key(sbl: str | None) -> tuple[str | None, str]:
    """eTRAKiT 'section-block-lot' (e.g. '46-334-11') -> our roll_keys.lot_key ('46-334-11') plus any suffix text."""
    m = SBL_RX.match(sbl or "")
    if not m:
        return None, ""
    sec, blk, lot, rest = m.groups()
    blk = str(int(blk)) if blk.isdigit() else blk.upper()
    return f"{int(sec)}-{blk}-{int(lot)}", rest.strip()
