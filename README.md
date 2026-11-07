# sud-cli

`sud-cli` is a standalone command-line tool for importing and searching synthetic
court records, native court case/event timelines, NPA-style document fixtures,
and **bounded official-publication metadata**. This repository contains the
Phase 1 offline foundation, the Phase 2 bounded live metadata adapter, the
Phase 3 native offline court case/event timeline slice, the Phase 4 native
offline court search capability, and the Phase 5 KAD status registry.

**Offline core, live by default.** The offline foundation (import, local
search, court/NPA timelines, fees, deadlines, interest, verification probes)
never touches the network. Live sources — `kad`, `case-history`, `sudrf-live`,
`mos-live`, `practice`, live `extract-text`, `npa download`, ЕФРСБ — are a
**core part of the product** and work by default (owner decision 2026-11-07):
there is no environment gate. When the network or proxy is unreachable the
adapter returns a typed error naming the cause and touches nothing else —
fail-closed by fact, not by configuration. Live responses are HTML with
CAPTCHA gates and are best-effort by design; the tool does **not** provide
consolidated/current law text, article segmentation, effective/repeal status,
future editions, full-text comparison, or guaranteed live court coverage.

## Scope

- **Offline foundation.** Local JSON import, local search, and NPA fixture
  comparison use synthetic data only.
- **Native court timeline (Phase 3).** `sud-cli court import` and
  `sud-cli court case` work with a versioned `cases`/`events` schema and
  synthetic fixtures only. No live court portal is contacted.
- **Native court search (Phase 4).** `sud-cli court search` performs local
  free-text search over imported `cases`/`events` with optional date bounds,
  bounded result limits, and deterministic ordering. No live sources are
  contacted; full document bodies are never returned.
- **Phase 5 KAD status.** `kad-arbitr-ru` is registered as a live provider
  with status `available`: the KAD/arbitr.ru search adapter ships as core
  functionality and requires a browser runtime (camofox) for the HTML portal.
  The KAD/arbitr.ru response contract is HTML with hidden count fields and a
  CAPTCHA gate (a direct probe returned HTTP 451); the parser is
  best-effort, not a parity claim. When the runtime or network is
  unavailable, `sud-cli kad` fails closed with an explicit error naming the
  cause.
- **Bounded live metadata (Phase 2).** `sud-cli law search` performs one
  user-invoked HTTPS GET to the documented official publication endpoint and
  returns metadata only.
- **Synthetic data only.** No real case identifiers, credentials, or legal
  documents in fixtures or tests.
- **Fail-closed.** `fees` and `deadlines` исполняются строго по утверждённой
  спецификации `legal_rules.json`; вызовы вне спецификации
  отклоняются с UNSUPPORTED/MISSING статусами
  shape; errors return non-zero with explicit messages instead of empty-success
  output.
- **Isolated storage.** All local state lives in a sud-cli-owned directory
  (default `~/.local/share/sud-cli`, override with `SUD_CLI_STATE_DIR` or
  `--state-dir`).

## Live sources (core functionality)

sud-cli is an **offline-first** tool: all core offline features (import,
court timelines, court/NPA search, fees, deadlines, interest, verification
probes) work entirely locally and never touch the network. Live adapters are
**part of the product, not opt-in extras** (owner decision 2026-11-07): they
run by default, and there is no environment gate to enable them.

- **Network egress.** Live commands reach court hosts over HTTPS, honoring
  the standard `HTTPS_PROXY`/`HTTP_PROXY` variables when set — a user
  option (in RU deployments a local RU-proxy; in the the project deploy Amnezia
  intercepts the traffic transparently). When the network or proxy is
  unreachable, the adapter fails closed with a typed error naming the cause;
  no empty-success answer is ever returned.
- **Browser runtime.** Portal adapters that render HTML (KAD/arbitr.ru,
  sudact.ru, ГАС Правосудие, mos-gov) still require a browser runtime, as
  before: either an external camofox service URL (`camofox_url` in the
  engine config, env `CW_CAMOFOX_URL`) or the embedded native browser
  (`browser_native.py`, which needs the camofox system libraries). Without
  a reachable runtime the adapter reports an explicit error.
- **License check (diagnostic only).** The engine can validate an offline
  HMAC-signed key (`CW_LICENSE_KEY` env or `license_key` config) used by
  `doctor`/engine diagnostics; absence of a key never silently degrades
  core offline features.
- **Egress guard.** Direct-HTTP adapters (ЕФРСБ/bankrot.fedresurs.ru,
  pravo.gov.ru) go through `sud_cli/net_guard.py` — fail-closed egress
  with a per-host circuit breaker and a host allowlist.
- **Honesty limits.** Live portals serve HTML with CAPTCHA gates; parsers
  are best-effort and typed statuses (`ok`/`no_results`/`unavailable`/
  `blocked`/`error`) never claim parity. All automated tests use injected
  fake transports and make no network calls.

## Installation

From the project root in a normal user-owned Python environment:

```bash
python -m pip install -e .
```

No system-wide install, network access, or package publishing is required.

## Extended Legal & Financial Capabilities (Phase 2 additions)

- **Regional holidays & non-working days:** `--region <code_or_slug>` (e.g., `--region 16` for Tatarstan, `--region 02` for Bashkortostan) overlays regional laws onto the federal production calendar.
- **Court timezones:** `--tz <iana_name>` (default `Europe/Moscow`) calculates precise submission deadlines down to 23:59:59 local court time (ч. 6 ст. 114 АПК РФ).
- **Calendar verification:** `sud-cli calendar check` reports government transfer decree coverage and validity years.
- **Interest calculator:** `sud-cli interest --principal <amount> --start <YYYY-MM-DD> --end <YYYY-MM-DD> [--payments "YYYY-MM-DD:AMOUNT,..."] [--calc-fee]` calculates statutory interest under ст. 395 and 317.1 ГК РФ across changing Central Bank key rates and partial payment schedules, with end-to-end court fee pipeline support.
- **Bankruptcy fees (259-FZ):** `sud-cli fees --jurisdiction arbitration --kind {bankruptcy_petition,bankruptcy_creditor_claim} ...` covers entity (100,000 руб.), individual (10,000 руб.), and creditor claim progressive brackets under ст. 333.21 п. 1 пп. 5 НК РФ.
- **Ergonomic CLI:** `--json` can be supplied anywhere (at the start or end of any subcommand).
- **Payment credentials & KBK:** `sud-cli payment --jurisdiction {arbitration,soyu,kas} --court <court> --defendant <name> --amount <amount>` generates official KBK and payment purpose strings for accounting.
- **Fee deferral motions:** `sud-cli deferral --court <court> --amount <amount> --applicant <name> --reason <text>` generates procedural motions under ст. 333.41 НК РФ / ст. 102 АПК РФ with mandatory bank/tax cert checklists.

## Quick start

```bash
# Help
python -m sud_cli --help

# List providers and their status
python -m sud_cli providers

# Check the local state directory and database
python -m sud_cli doctor

# Import legacy synthetic court records
python -m sud_cli import court fixtures/synthetic/court_records.json

# Import native synthetic court case/event timelines
python -m sud_cli court import fixtures/synthetic/court_cases.json

# View a normalized case summary and deterministically ordered event timeline
python -m sud_cli court case SYNTH-CASE-0001

# Search imported native court cases and events
python -m sud_cli court search widget
python -m sud_cli court search hearing --since 2026-01-01 --until 2026-12-31 --limit 10

# Search imported legacy records
python -m sud_cli search court widget
python -m sud_cli search court "" --since 2026-01-01 --until 2026-12-31 --limit 10

# NPA fixture search and edition comparison
python -m sud_cli npa search widget
python -m sud_cli npa compare --id SYNTH-NPA-0001 --edition1 v1 --edition2 v2

# Official publication metadata only (Phase 2)
python -m sud_cli law search "synthetic query"
python -m sud_cli law search --number "12345"

# Machine-readable JSON mode
python -m sud_cli --json providers
python -m sud_cli --state-dir .my-state --json court import fixtures/synthetic/court_cases.json
```

## JSON output contract

In `--json` mode every response is a single JSON object:

```json
{
  "status": "ok" | "error" | "blocked",
  "data":   <payload>,
  "meta":   {"command": "...", "state_dir": "..."}
}
```

Search results additionally include `limit`, `total`, and `truncated` fields.

## Import contract

### Native court cases and events (Phase 3)

Top-level object must contain a `cases` array. Each case must have:

- `case_id` (string) — synthetic stable case identifier.
- `case_number` (string)
- `court` (string)
- `source_name` (string) — logical source label.
- `source_record_id` (string) — immutable source identifier.

Optional case fields: `parties`, `fetched_at` (ISO-8601), `source_url`
(`http://`, `https://`, or `file://` only, no path traversal).

Each case may contain an `events` array. Every event must have:

- `event_id` (string)
- `event_type` (string)
- `source_name` (string)
- `source_record_id` (string)

Optional event fields: `event_date` (YYYY-MM-DD), `doc_text`, `fetched_at`,
`source_url`.

### Court records (legacy Phase 1)

Top-level object must contain a `records` array. Each record must have:

- `record_id` (string) — deterministic key for idempotency.
- `case_number` (string)
- `court` (string)
- `event_date` (YYYY-MM-DD)
- `event_type` (string)

Optional fields: `parties` (`{"plaintiff": ..., "defendant": ...}`), `doc_text`.

Legacy records are also mirrored into the native `cases`/`events` tables so the
`court case` command can display them.

### NPA documents

Top-level object must contain a `documents` array. Each document must have:

- `doc_id` (string)
- `edition` (string)
- `title` (string)

Optional fields: `doc_date` (YYYY-MM-DD), `body`.

### Duplicate policy

Imports are atomic and deterministic. See `docs/IMPORT-POLICY.md` for the full
Phase 3 court import policy. In short:

- Existing cases are **never overwritten**; the incoming case and all of its
  events are skipped.
- Existing events are **never overwritten**.
- Duplicate `case_id` or `event_id` values repeated within the same input batch
  are skipped: the **first occurrence wins**.
- `inserted + skipped == total_cases` and
  `events_inserted + events_skipped == total_events` always hold.

## Реестр дел (v2.4.0)

Центральный объект «дело» в локальной БД: статусы по legal-v1
(`new`/`review`/`filed`/`pending`/`decided`/`appealed`/`closed`/`unknown`),
владелец, суммы, документы (реестр ссылок), заметки.

Примеры:

```
sud-cli case new --number "А40-1/2026" --court "АС г. Москвы" --plaintiff "Истец" --defendant "Ответчик" --owner "Иванов" --status new
sud-cli case set <id> --status filed --owner "Петров" --closed-at 2026-12-01
sud-cli case amount add <id> --kind "иск" --amount "1234,56"
sud-cli case amount list <id>
sud-cli case doc add <id> --path /tmp/doc.pdf --title "Иск"
sud-cli case doc list <id>
sud-cli case note add <id> --text "заметка" --author "Иванов"
sud-cli case note list <id>
sud-cli case note supersede <note-id> --text "уточнение"
sud-cli case list --status filed --owner "Петров" --court "АС" --party "Истец"
sud-cli case card <id>
```

## Conflict gate (v2.4.1)

Вспомогательный детект пересечений имён сторон в локальном реестре (name-exact,
без выдуманных связей):

```
sud-cli conflict check --plaintiff "Иванов" --defendant "Петров" [--court Z] [--json]
sud-cli conflict case <case_id>
```

Результат: `{clean, hits, method, coverage, disclaimer}` с `method="name_exact"` и
`coverage="local registry names only"`. Отсутствие совпадений не означает
отсутствие конфликта: покрытие — только имена локального реестра, внешние
стороны и аффилированные лица не охвачены. Это вспомогательный детект, не
этическое заключение — решение за человеком (boundary IX-3).

## Jurisdiction gate (v2.6.0)

Процессуальная навигация по VERIFIED правилам (`sud_cli/jurisdiction_rules.json`,
legal 24.10). Инструмент ≠ юридический совет (boundary IX-3); тексты норм без
`legal_text` подтягиваются runtime через `npa_as_of`:

```bash
sud-cli jurisdiction check --code apk|gpk|kas --claim <kind> [--immovable] [--corporate] [--bankruptcy] [--ip-rights] [--contract-forum] [--arbitration-clause] [--multiple-defendants] [--foreign-element] [--consumer] [--alimony] [--unknown-rf-location] [--json]
```

Конверт: `{candidates:[{court_level, venue_basis, confidence, legal_text|fetch_hint}], manual_review:[{reason, norm}], warnings, disclaimer}`.

## Pretrial (v2.6.0)

Претензионный порядок по VERIFIED правилам:

```bash
sud-cli pretrial calc --code apk|zozpp|osago --sent YYYY-MM-DD [--json]
sud-cli pretrial consequences --code apk|gpk|kas --stage filing|trial|movement [--json]
```

`calc` возвращает срок ответа и дату возникновения права на иск (`sent+days`),
OSAGO учитывает исключение нерабочих праздников через `holidays_rf`. `consequences`
возвращает нормы возврата/оставления из правил (verbatim ссылок). Хартия транспортных
уставов `NOT_SERIALIZED` → `manual_review`.

## Schema migration

The SQLite database is versioned. `init_db` automatically upgrades an existing
Phase 1 schema (`v1`) to the Phase 3 schema (`v2`) inside a single transaction:

- New `cases` and `events` tables are created.
- Existing `records` rows are copied into the native case/event model with
  source provenance set to `local-json-court-migrated`.
- If any migration step fails, the transaction is rolled back and the database
  stays at its previous version.
- Unknown future schema versions fail closed.

## Official publication metadata (Phase 2)

The `sud-cli law search` command performs a single user-invoked HTTPS GET to the
documented endpoint `https://publication.pravo.gov.ru/api/Documents`. It returns
only publication metadata:

- e-publication number (`eo_number`)
- title/name
- document number
- signed date (`documentDate`)
- publication date (`publishDateShort`)
- source endpoint and retrieval timestamp
- pagination summary

This is **publication metadata only**. It is not consolidated or current law text,
it is not a legal-status or effective-date determination, and it performs no legal
analysis. Each invocation makes at most one request, uses `PageSize=10`/`Index=1`,
times out after 5 seconds, and rejects responses larger than 1 MiB or any non-JSON
content. The host and path are allowlisted. Redirects are refused before any
additional request is made; cross-host redirects are therefore rejected at the
transport layer. Exact number search sends the documented `NumberSearchType=0`
value (not the unsupported string `Equal`). No cookies, credentials, or
authentication are sent.

All automated tests use an injected fake transport and make no network calls.

## Provider adapter contract

`sud_cli/court_source.py` defines the transport-agnostic adapter interface that
future live court sources will implement. Typed statuses are:

- `ok`
- `no_results`
- `unavailable`
- `blocked`
- `error`

Phase 3 ships the contract and the offline `LocalJsonCourtSource`. Live
adapters for specific portals (KAD, sudact.ru, ГАС Правосудие, mos-gov,
ЕФРСБ) are implemented as core functionality and run by default — they need
network egress (standard `HTTPS_PROXY`/`HTTP_PROXY` when set) and report
typed statuses, never claiming parity with the portal UI. See
`docs/COURT-SOURCE-CONTRACT.md`.

## Limits and safety

- Maximum records/documents per import: 1,000.
- Maximum cases per native court import: 1,000.
- Maximum events per native court import: 1,000.
- Maximum text length per field: 1,000,000 characters.
- Maximum fixture file size: 10 MiB.
- Maximum native court search limit: 100.
- Maximum legacy/NPA search limit: 100.
- Native court search returns event metadata and a bounded 200-character snippet; full event document bodies are never returned.
- Official publication query length limit: 200 characters.
- Official publication timeout: 5 seconds; response cap: 1 MiB; one GET per invocation.
- Imports are atomic: validation failures roll back the entire transaction.
- Duplicate keys are skipped, not replaced, and reported in the `skipped` count.
- Path traversal (`..`) is rejected.
- Directory inputs are rejected.

## Verification — Закон Zero (v2.9.0)

Two probes close S-52/S-54. Both are offline by construction (bundled NPA
editions, the local events table, local files) and both refuse to promote
"looks right" into a confirmed fact.

`verify quote` — a fragment is confirmed only against an explicit source:

```bash
sud-cli verify quote --text "хищение чужого имущества" \
    --source npa --act uk_rf --article 159 --date 2015-11-20
sud-cli verify quote --text "принятии заявления" --source case --case-id VC-1
sud-cli verify quote --text "про мошенничество" --source file --path note.txt
```

- Verdicts: `exact` — literal substring after normalization (NBSP→space,
  whitespace collapsed, ё→е, case-folded; **punctuation is never stripped**);
  `fuzzy` — only when `--max-fuzzy` > 0 and difflib ratio ≥ 1−max-fuzzy, the
  answer carries `ratio`, `threshold` and a ±80-char snippet window;
  `not_found` — the honest default; `edition_unavailable` — the requested act/
  article/date is not in the offline bundle (an NPA gap is never disguised as
  "the quote does not exist").
- Every answer includes `source`, `matched_fragment` and `location`
  (`act/article/date` | `event:<id>` | `file:<path>:<offset>`).
- Empty `--text` and missing source parameters exit non-zero with
  `INVALID_ARGUMENTS`. MCP tool: `sud_cli_verify_quote` (strict schema).

`practice status` — an act/case fate probe that NEVER says «действует»:

```bash
sud-cli practice status А40-99/2026 --store ~/.local/share/sud-cli/practice-snapshots
sud-cli practice status А40-99/2026 --related "А40-100/2026 постановление отменено;А40-101/2026 решение изменено"
```

- `possibly_overturned` only when a local snapshot/case-diff
  record or an explicit `--related` fixture title carries an overturn/
  modification marker (отмен/измен roots, negations like "оставлено без
  изменения" are excluded); the answer cites the evidence file and title.
- Anything else is `unknown` with a mandatory hint: freshness checks belong
  to a Camofox/egress run or manual review — sud does not assert a norm's
  lifecycle status without a source. No network is reachable from
  `sud_cli/practice_status.py` (gate-tested).

## Blocked commands

```bash
python -m sud_cli fees          # exit 1, BLOCKED / NOT_IMPLEMENTED
python -m sud_cli fees --json   # exit 1, JSON blocked contract
python -m sud_cli deadlines 2026-01-01 --appeal  # exit 1, blocked
```

These commands perform no calculation, no date arithmetic, no holiday logic, and never shell out.

## Development

```bash
python -m pytest -q
python -m compileall -q .
```

The full test suite currently passes 1275 tests. This is an isolated development worktree; no installation or deployment has been performed.

## Roadmap

See `ROADMAP.md` for live court adapters, official NPA source discovery, commercial metadata access, and unblocked calculators.

## Anti-Laches (v2.7.0)

Подтверждение сроков, фиксация начала срока с доказательством, журнал правок и единый календарь:

```bash
# расчёт с привязкой к делу и сразу подтверждение
sud-cli deadlines calculate <case_id> as_appeal_decision --event-date 2026-10-01 --confirm --by "Иванов"

# подтвердить существующий срок
sud-cli deadlines confirm <case_id> --key as_appeal_decision --by "Иванов" --evidence "вручение 12.10 заказное 156..."

# зафиксировать начало срока
sud-cli deadlines anchor <case_id> --key as_appeal_decision --event-date 2026-10-01 --method вручение --evidence "заказное письмо №156"

# waiver с нормой-ссылкой (не валидируется, печатается как есть)
sud-cli deadlines waive <case_id> --key as_appeal_decision --by "Иванов" --norm "ст. 117 АПК РФ"

# журнал правок
sud-cli deadlines audit <case_id>

# единый календарь
sud-cli calendar agenda --days 14
```

```bash
# матрица контрольных точек дела (без выдуманных баз-дат)
sud-cli deadlines matrix <case_id>
sud-cli deadlines matrix <case_id> --instance apk
sud-cli deadlines matrix <case_id> --missing

# деньги: льготы/возврат/индексация (VERIFIED pack)
sud-cli fee-exemption --jurisdiction apk --category arbitration_disabled_1_2
sud-cli fee-refund --paid 50000 --ground settlement_first_instance
sud-cli fee-indexation --code apk --amount 100000 --cpi-start 1.0 --cpi-end 1.084

# напоминания bus-free (sud не вызывает a2a сам)
sud-cli reminders due --days 7
```
