# sud-cli court import policy

This policy governs all native court-case/event imports into the sud-cli-owned
local database. It applies to `sud-cli court import <local-synthetic-json>` and
to the legacy `sud-cli import court <local-synthetic-json>` path.

## Scope

- Only synthetic fixtures created for testing and development are accepted.
- No live court portal data, real case identifiers, natural persons, or legal
  documents may be imported.
- All imports are local-file, offline operations.

## Identity and provenance

Every imported case and event carries source provenance:

- `source_name` — the logical source label (e.g. `local-synthetic-json`).
- `source_record_id` — the immutable identifier assigned by that source.
- `fetched_at` — ISO-8601 timestamp when the source record was obtained.
- `source_url` — optional source location; validated if present.
- `imported_at` — ISO-8601 timestamp when the record was written into the
  sud-cli database.

Case identity is determined by `case_id`. Event identity is determined by
`event_id`. Both are stable, source-scoped identifiers.

## De-duplication and update rules

1. **First occurrence wins within a batch.** If a fixture repeats the same
   `case_id` or `event_id` more than once, the first occurrence is inserted and
   later occurrences are counted as `skipped`.
2. **Existing cases are never overwritten.** If `case_id` already exists in the
   database, the incoming case and **all of its events** are skipped. Stored
   case fields are preserved unchanged.
3. **Existing events are never overwritten.** If `event_id` already exists, the
   incoming event is skipped.
4. **Safe updates only.** Because existing records are skipped rather than
   merged, there is no ambiguity about which field values survive an import.
   Partial or conditional updates are not supported.
5. **Atomicity.** The entire import is one SQLite transaction. If any record
   fails validation, the whole import is rolled back and no changes are saved.

## Result contract

Import results report:

- `kind`: `"court-case"` for native case/event imports.
- `inserted`: number of cases inserted.
- `skipped`: number of cases skipped because they already exist.
- `events_inserted`: number of events inserted.
- `events_skipped`: number of events skipped because they already exist.
- `total_cases`: number of cases in the input.
- `total_events`: number of events in the input.

`inserted + skipped == total_cases` and
`events_inserted + events_skipped == total_events` always hold.

## URL validation

If a `source_url` is supplied, it must use one of these schemes:

- `http://` or `https://` for a documented public endpoint.
- `file://` for a local synthetic fixture.

The URL is rejected if it contains path traversal (`..`), a non-printable
control character, or an unsupported scheme.

## Limits

- Maximum cases per import: 1,000.
- Maximum events per import: 1,000.
- Maximum text length per field: 1,000,000 characters.
- Maximum fixture file size: 10 MiB.
