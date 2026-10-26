# Nassau County FOIL request drafts: NOT SENT

Status: **DRAFTS ONLY.** Nothing in this folder has been sent. The operator reviews, fills the `[REQUESTER ...]` placeholders, confirms each submission channel, and sends them.

Legal basis: NY Public Officers Law Article 6 (FOIL), §§ 84–90. An agency must acknowledge a request within 5 business days (§ 89(3)(a)). Records kept electronically must be provided electronically when the requester asks and it is practicable (§ 87(5)).

Why these requests exist: discovery (see `../county-discovery-nassau.md`) found **no anonymously harvestable bulk permit source** for any of Nassau's 69 permit authorities. Each one is behind an anonymous-listing block (OpenGov), a login (eTRAKiT, SmartGov), a reCAPTCHA (iWorQ), or paper. FOIL is the records-first or api-first route recorded for every blocked or manual-only row in `../nassau-sources.yaml`.

| File | Recipient (street addresses are from general knowledge; verify) | Parcels covered (NYS centroid MUNI_NAME) | Channel (to confirm before sending) |
|---|---|---|---|
| [01-town-of-hempstead.md](01-town-of-hempstead.md) | Town of Hempstead, Records Access Officer (Town Clerk) / Dept. of Buildings | 157,220 unincorporated | Town Clerk FOIL channel on hempsteadny.gov |
| [02-town-of-north-hempstead.md](02-town-of-north-hempstead.md) | Town of North Hempstead, Dept. of Buildings | 32,245 unincorporated | OpenGov record type "FOIL Request - Building Department" (verified in tenant metadata) |
| [03-town-of-oyster-bay.md](03-town-of-oyster-bay.md) | Town of Oyster Bay, Records Access Officer / Planning & Development, Building Division | 81,210 unincorporated | Town Clerk FOIL channel on oysterbaytown.com |
| [04-city-of-glen-cove.md](04-city-of-glen-cove.md) | City of Glen Cove, Records Access Officer / Building Dept. | 7,980 | City Clerk |
| [05-city-of-long-beach.md](05-city-of-long-beach.md) | City of Long Beach, Records Access Officer / Building Dept. | 9,850 | City Clerk |
| [06-villages.md](06-villages.md) | One template for each village Records Access Officer (village clerk), with a table of the 13 largest villages | 82,748 (13 largest); all 64 villages 134,925 | Village clerk email or office |
| [07-nassau-department-of-assessment.md](07-nassau-department-of-assessment.md) | Nassau County Department of Assessment | all ~426k | County FOIL channel |

Request 07 is not about permits. It asks for the roll's residential inventory (year built) and sales history. Those fields are not in any open bulk source; NSI `med_yr_blt` is only a census-block median.

Common field list (permits): permit/application number; permit type and sub-type; work description; Section-Block-Lot or tax-map number; site address; application date; issue date; final inspection / certificate of completion date; closed date; current status; expiration date; contractor business name; contractor Nassau County Consumer Affairs home-improvement licence number; declared job value. Priority: re-roof / roofing / roof-covering permits first, then all building permits.

Privacy: ask only for permit-level public records. Do not request homeowner phone numbers or e-mails. Applicants' personal contact data can be redacted under § 87(2)(b).
