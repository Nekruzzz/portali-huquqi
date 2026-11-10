---
inclusion: always
---
# Product: Portali huquqi (portali-huquqi.tj)

A rebuild of the Internet portal of legal information of the Republic of Tajikistan. It replaces the
2018 site at http://www.portali-huquqi.tj (PHPMaker 2018, http only), which is operated by the National
Centre of Legislation under the President (ММҚ); the Ministry of Justice holds the domain.

## Legal basis (do not remove features these require)
- Law No. 1414 of 30.05.2017 "On normative legal acts", Articles 34–39.
- Procedure for maintaining the portal, Government Resolution No. 617 of 29.12.2018 (amended 2020, 2024).
- RIA Instruction, Government Resolution No. 355 of 31.07.2018.

Required: three registers (plans, drafts with regulatory impact assessment, adopted acts); public
consultation of at least 30 calendar days; registration of state bodies, legal entities and individuals
(Annexes 1–3); developer and authorised-body (admin) roles; free search, viewing, download and print;
Tajik and Russian.

## Users
- Citizens and businesses: read acts, follow drafts, send proposals tied to an article.
- Developers (ministries): post plans and drafts, answer every proposal.
- Authorised body (admin): approves accounts, writes conclusions, maintains the acts register.

## Migration from the old site (in progress)
- Old site: ≈23,950 acts in Tajik and ≈26,240 in Russian (newest dated 28.11.2023), 121 plans, 84 drafts.
- Every old address must keep working: `/publicadliya/view_qonunhoview.php?asosi_id=N`,
  `/publicria/view_rnaqshaview.php?reestr_id=N`, `/publicria/view_rloihaview.php?reestr_id=N` and the old
  list/search pages redirect (301) to the new pages via `documents.legacy_id`.
- Data comes in through `flask --app app import-legacy FILE.jsonl` (format in `portali-huquqi/legacy_import.py`).
  The source will be an export of the old database (structure not received yet); `tools/fetch_old_portal.py`
  copies the public pages as a fallback and may only be run at full scale with the operator's permission.

## Principles
- Tajik is the state language: the default and the fallback when a Russian text is missing.
- Everything public works without login and without JavaScript, on a 390 px phone, with keyboard only.
- Demo content is clearly marked "Намуна / Демо". The real acts in the demo (Laws 1414, Resolutions 355,
  617, 98) must stay accurate; their old-site numbers are 19892, 21259, 21650, 19650.
