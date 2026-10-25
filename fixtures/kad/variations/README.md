# KAD synthetic instance-tab variations

**SYNTHETIC — NOT live captures.**

These HTML fixtures are hand-built regressions for KAD parser instance-tab
hardening. They mimic the structural classes of the live first-instance card
(`fixtures/kad-saved-card-А40-308035-2025.html`) but carry plausibly different
wrapper ordering, instance labels, and case-number patterns.

Purpose:
- Exercise appeal / cassation / supervision tab parsing without network or
  browser.
- Verify that instance detection derives from case-number patterns **and**
  page context.
- Keep fail-closed validation: missing critical fields still raise.

Do not use these fixtures as evidence of live KAD markup. They are testing
artifacts only.

Files:
- `kad-variation-appeal-16AP-1234-2026.html` — synthetic appeal tab
  (16АП-1234/2026, 16 Арбитражный апелляционный суд).
- `kad-variation-cassation-02-3696-2026.html` — synthetic cassation tab
  (02-3696/2026(02-1234/2025), Арбитражный суд Волго-Вятского округа).
- `kad-variation-supervision-VAS-12345-2026.html` — synthetic supervision tab
  (ВАС-12345/2026, Высший Арбитражный Суд РФ — historical label).

Generated for: `tests/test_kad_variations.py`.
