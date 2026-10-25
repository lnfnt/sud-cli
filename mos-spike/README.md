# mos-gorsud bounded spike

## Goal
Проверить read-only доступность официального open-data/API контура без real case data и без изменения parser/colleague-legal.

## Commands

```text
GET https://data.mos-gorsud.ru/api/opendata/json
GET https://data.mos-gorsud.ru/glossary
GET https://mos-gorsud.ru/mgs/services/cases
```

Headers used:

```text
User-Agent: the project-Case-Search-research/0.1
Accept: application/json,text/html;q=0.9
```

## Output

```text
https://data.mos-gorsud.ru/api/opendata/json
  HTTP 200
  Content-Type application/json
  prefix bytes 16623
  returned an open-data catalog array with identifier/title/passport fields

https://data.mos-gorsud.ru/glossary
  timeout at 20s

https://mos-gorsud.ru/mgs/services/cases
  timeout at 20s
```

Full bounded output:

```text
results.json
```

## Interpretation

The official open-data catalog is reachable and should be treated as a metadata/discovery source first. The catalog endpoint is not itself a case lookup endpoint. The interactive cases endpoint timed out in this probe, so no endpoint was inferred and no live adapter was written.

## Status

```text
open-data catalog discovery: PASS
case lookup endpoint: OPEN / requires browser/API contract investigation
production adapter: QUARANTINE
real case data: NOT USED
```

## Safety

- No colleague-legal access.
- No case number or PII sent.
- No cookies/auth/credentials used.
- No production files changed.
- No cron/Telegram integration.
