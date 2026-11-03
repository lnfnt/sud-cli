# sud-cli court-source provider contract

This document defines the transport-agnostic adapter interface that live court
sources will implement in future phases. Phase 3 ships **only** the contract and
offline synthetic fixtures; no live provider is implemented.

## Status taxonomy

A court-source adapter must report one of the following typed statuses:

| Status | Meaning |
|--------|---------|
| `ok` | The adapter returned data. The result may still contain zero items; use pagination metadata to distinguish empty pages. |
| `no_results` | The source is reachable and healthy, but the query matched no cases or events. |
| `unavailable` | The source is temporarily unreachable, rate-limited, or not yet configured. |
| `blocked` | The source is deliberately disabled by policy (e.g. pending legal gate or access contract). |
| `error` | An unexpected failure occurred (transport, parsing, schema mismatch, DB failure, etc.). |

Statuses are plain strings; callers compare against the constants in
`sud_cli.court_source`.

## Result shape

`CourtProviderResult` is a frozen dataclass with these fields:

- `status` — one of the statuses above.
- `items` — list of normalized case or event dictionaries. Empty when status is
  not `ok`.
- `source` — human-readable source label (e.g. `"local-synthetic-json"`).
- `source_url` — optional URL of the source endpoint or fixture.
- `fetched_at` — optional ISO-8601 timestamp of when the data was obtained.
- `pagination` — dictionary with `page`, `page_size`, `total`, `pages_total`,
  `limit`. Always present; totals are `-1` when unknown.
- `error_message` — present and non-empty for `unavailable`, `blocked`, and
  `error` statuses.

## Adapter protocol

```python
class CourtSource(Protocol):
    def fetch_case(self, case_id: str, **kwargs) -> CourtProviderResult: ...
    def search_cases(self, query: str, page: int = 1, page_size: int = 10,
                     **kwargs) -> CourtProviderResult: ...
```

Adapters must:

- Return only metadata and event timelines; never invent or fabricate records.
- Bound every request (timeout, response size, page size, result count).
- Distinguish `no_results` from `unavailable` and `error`.
- Include explicit source provenance on every result.
- Fail closed on malformed schemas, transport errors, and DB failures.

## Phase 3 implementation

- `LocalJsonCourtSource` is the only concrete adapter. It reads local synthetic
  JSON files and returns `ok` or `error` results.
- KAD, SudAct, SudRF, VS RF, and all other live court
  adapters remain `unavailable` until each has its own access contract,
  synthetic tests, timeout/rate policy, and implementation brief.
