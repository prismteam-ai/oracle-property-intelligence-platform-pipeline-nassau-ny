# FOIL draft: villages (one letter per village)

**Status: DRAFT, NOT SENT.** Send one copy per village, addressed to that village's Records Access Officer (usually the Village Clerk). Fill `[VILLAGE]` and the requester placeholders.

Villages are listed by parcel count (NYS centroid `MUNI_NAME`, 2025 roll). Portal/vendor status comes from discovery on 2026-10-25. Websites come from the Nassau GIS `Villages_Cities` layer. Clerk e-mail addresses were not collected: confirm each channel on the village website.

| Village | Parcels | Permit system found | Website |
|---|---:|---|---|
| Valley Stream | 11,240 | OpenGov (valleystreamny); anonymous listing 403 | https://www.vsvny.org/ |
| Freeport | 10,591 | OpenGov (freeportny); anonymous listing 403 | https://www.freeportny.gov/ |
| Hempstead | 9,254 | Granicus SmartGov; login required | https://www.villageofhempstead.org/ |
| Garden City | 7,792 | paper forms (web search) | https://www.gardencityny.net/ |
| Rockville Centre | 6,964 | paper / in person (web search); site Cloudflare-challenged | https://www.rvcny.gov/ |
| Lynbrook | 6,132 | none found (iWorQ parking only) | https://www.lynbrookvillage.net/ |
| Massapequa Park | 5,904 | unknown; site Cloudflare-challenged | https://masspk.com/ |
| Mineola | 5,710 | paper forms (web search) | https://www.mineola-ny.gov/ |
| Floral Park | 4,939 | iWorQ, reCAPTCHA-gated | https://fpvillage.org/ |
| Westbury | 4,741 | none found | https://www.villageofwestbury.org/ |
| Malverne | 3,263 | none found | https://www.malvernevillage.org/ |
| New Hyde Park | 3,190 | iWorQ, reCAPTCHA-gated | https://vnhp.org/ |
| Great Neck | 3,028 | none found | https://www.greatneckvillage.org/ |

The other 51 villages (each under ~3,000 parcels) use the same letter; their portal status is `needs-review` in `../nassau-sources.yaml`. Village of Hewlett Harbor (captcha-gated iWorQ portal) and Village of North Hills (OpenGov tenant, anonymous listing forbidden) are `blocked`: each has an identified portal, but neither allows a public records search through it, so send the same FOIL letter for these two as well, addressed to that village's Clerk/Records Access Officer per `records_request.recipient_office` in `../nassau-sources.yaml` -- do not rely on the blocked portal.

---

**To:** Records Access Officer, Village of [VILLAGE] (Village Clerk); copy: Building Department
**Subject:** FOIL request: building permit records (roofing first), electronic export
**Date:** [DATE]

Dear Records Access Officer:


Pursuant to the New York Freedom of Information Law, Public Officers Law §§ 84–90, I request copies of the following records maintained by your Building Department:

1. **Building permit records, roofing first.** For every building permit application and issued permit from **January 1, 2000 (or the earliest date held electronically) to the date of this request**, a record-level export with these fields, where they exist:
   - permit / application number; permit type and sub-type (including any re-roof, roofing, or roof-covering category); description of work;
   - Section-Block-Lot (tax map) number; site address;
   - application date; issue date; final inspection or certificate of completion date; closed date; current status; expiration date;
   - contractor business name and the contractor's Nassau County Department of Consumer Affairs home-improvement licence number as recorded on the permit;
   - declared job value / estimated cost.
2. If a full export is not practicable, please provide **roofing-related permits first** (any permit whose type, sub-type, or work description refers to roof, re-roof, roofing, or roof covering), and then the remaining building permits.
3. A **data dictionary** or code list for the permit type and status values, if one exists.

**Format.** Under § 87(5), please provide records kept electronically in electronic form: CSV or Excel export from the permit system, delivered by e-mail or download link. If records exist only on paper or microfilm for earlier years, please tell me the date ranges involved before copying anything.

**Redactions.** I do not seek homeowners' or applicants' personal phone numbers, personal e-mail addresses, or other information whose disclosure would be an unwarranted invasion of personal privacy (§ 87(2)(b)). Please redact those rather than withholding the records.

**Fees.** If fees will exceed $25, please tell me in advance. I understand that no fee may be charged for searching for or redacting electronic records except as permitted by § 87(1)(c).

Please acknowledge this request within five business days as required by § 89(3)(a), and give an approximate date when the records will be provided. If any part is denied, please cite the specific exemption and name the person to whom an appeal should be directed.

Thank you,

[REQUESTER NAME]
[REQUESTER ORGANIZATION]
[REQUESTER MAILING ADDRESS]
[REQUESTER E-MAIL] · [REQUESTER PHONE]
