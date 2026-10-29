# Current UI screenshots

These are read-only screenshots of real local sandbox cases using synthetic ERP-Bench data. New slides and videos were cancelled by the user and are excluded from this delivery. Original decks, videos and historical evidence remain unchanged.

The A screenshots use final source `bf880f2f1a8881809a6da2d89bcd3ba6724f4f81` and the completed [A-final acceptance](../../evidence/2026-10-28-next-phase/A-final/report.json). The authorized acceptance driver approved the case before capture. Browser capture only viewed saved facts and asked case questions, with no approve/select/reprepare or ERP mutation requests and no console errors. The screenshots do not depict a human browser approval or a public deployment upgrade.

| View | Chinese | English |
| --- | --- | --- |
| Full scope and direct associations | [Overview](A-overview.png) | [Overview](A-overview-en.png) |
| Actual distinct candidate and margin basis | [Candidate](A-margin-and-scope.png) | [Candidate](A-candidates-en.png) |
| Policy checks | [Checks](A-checks.png) | [Checks](A-checks-en.png) |
| Case question and answer | [Answer](A-qa-zh.png) | [Answer](A-qa-en.png) |
| Saved execution evidence | [Receipt](A-receipt.png) | [Receipt](A-receipt-en.png) |

A records 76.75% new-spend margin, 2 business-document actions and 7 verified execution steps, with 51/51 original rules and 100/100. Its full planning scope is 5 orders / 50 units, directly linked demand is 1 order / 13 units, and failed supply is 1 purchase order / 3 units. These quantities describe different scopes.

## Desktop and narrow layout checks

The `layout-*` screenshots test the final CSS using the earlier real A/B persisted cases. The UI source bytes match `bf880f2`, while those business results come from the preceding `74a990a` acceptance. They demonstrate layout correctness, not a new final-revision B execution. B-final execution evidence is recorded separately in the [release record](../../NEXT_PHASE_RELEASE.md).

| Case / language | 1440 px | 390 px |
| --- | --- | --- |
| A Chinese | [Desktop](layout-A-zh-1440.png) | [Narrow](layout-A-zh-390.png) |
| A English | [Desktop](layout-A-en-1440.png) | [Narrow](layout-A-en-390.png) |
| B Chinese | [Desktop](layout-B-zh-1440.png) | [Narrow](layout-B-zh-390.png) |
| B English | [Desktop](layout-B-en-1440.png) | [Narrow](layout-B-en-390.png) |

All eight combinations have no policy-label/value overlap or horizontal overflow. [Exact source identities and screenshot hashes](screenshots-manifest.json) and the [frontend validation record](../../evidence/2026-10-28-next-phase/validation/frontend.json) separate these checks from mock-API UI regressions and from ERP acceptance.
