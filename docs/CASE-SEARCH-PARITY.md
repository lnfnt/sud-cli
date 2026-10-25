# sud-cli ↔ case-search capability parity

**Status:** this file is a *narrative* view. The machine-readable source of truth is
`sud_cli case-search parity`, which builds its rows from the bridge registries
(`_COMMANDS`, `_LIVE_COMMANDS`, `_BLOCKED_COMMANDS`, `_FORBIDDEN_COMMANDS`) rather than
from a hand-written list. The previous version of this document had drifted out of
sync with the code — it still reported fees and deadlines as `blocked` and the KAD
and SudAct surfaces as `unavailable` after they had shipped and been verified live.

Regenerate the table with:

```bash
python3 -m sud_cli --json case-search parity
```

`sud_cli_status`:

| value | meaning |
|---|---|
| `bridge` | forwarded to the case-search binary through the allowlisted bridge |
| `native` | implemented in sud-cli itself; no case-search involvement |
| `blocked` | present upstream but permanently excluded by the bridge contract |

`live_gate`: the command reaches a court website and therefore requires
`SUD_CLI_BRIDGE_LIVE=1`. Without it the bridge returns `status: "blocked"` — meaning
the gate is closed and **nothing was attempted** — which is deliberately distinct from
`status: "error"`, meaning an attempt was made and failed.

## Verified bridge surfaces

| Capability | case-search command | Gate | Verification |
|---|---|---|---|
| Provider status discovery | `providers` | no | live, read-only |
| HTML case import | `import` | no | live, local saved file |
| ГАС Правосудие case import | `sudrf-import` | no | live, local saved file |
| KAD JSON case import | `kad-import` | no | live, needs KAD JSON **export**, not HTML |
| VSRF PDF case import | `vsrf-import` | no | local PDF only |
| Auto-detected file import | `auto-import` | no | live, infers format from content |
| Document text extraction | `extract-text` | no | live, local document |
| Judicial practice search (sudact.ru) | `search-practice` | **yes** | live, returns documents |
| Live KAD / arbitr.ru search | `kad-search --inn` | **yes** | live, returns organizations |
| Full decision text by URL | `get-text <url>` | **yes** | live |
| Case history across linked instances | `case-history <case_id>` | **yes** | live, needs `CSE_CAMOFOX_URL` |
| Single case lookup | `lookup <case_id> --source` | **yes** | upstream returns `NOT_IMPLEMENTED` |
| Live ГАС Правосудие card | `sudrf-live <case_id>` | **yes** | live, needs `CSE_CAMOFOX_URL` |
| Live Moscow court card | `mos-live <slug> <case_id>` | **yes** | live, needs `CSE_CAMOFOX_URL` |

## Native sud-cli capabilities

These need no case-search binary and no live gate. Status is `native` for all.

| Capability | Command | Status | Notes |
|---|---|---|---|
| Court fee calculation | `fees` | `native` | from `legal_rules.json` v1.1.0; fail-closed, never invents a rate |
| Procedural deadline calculation | `deadlines` | `native` | production calendar built into the rules file |
| Readiness self-check | `ready` | `native` | configuration only; contacts no court source |
| Case version diff | `case-diff <old> <new>` | `native` | offline over two saved snapshots |
| Official publication metadata | `law search` | `native` | bounded metadata adapter, no case-search dependency |
| Local NPA fixtures | `npa` | `native` | offline |

## Permanently blocked

`watch` (watchlists) and `config` are **never** invoked or exposed through the bridge.
They are not "not yet implemented" — they are excluded by contract.

`kad-case-history`, `sudrf-lookup` and `vsrf-lookup` are also blocked: they are
proxy-dependent or credential-dependent, and this bridge uses neither.

`lookup` is bridged but every real source returns `NOT_IMPLEMENTED` from
case-search itself. That is an upstream gap, not a sud-cli defect, and the
implementation rights are not ours.

## Case number formats

The bridge accepts what Russian courts actually print, derived from the upstream
parsers rather than from assumption — letters are legal in *any* prefix position:

- `А40-11111/25`, `А40-11111/2025` — arbitration, 2- or 4-digit year
- `2а-2939/2025` — ГАС Правосудие, digit first
- `16АП-12345/2025` — appellate, letter suffix
- `33а-29651/2025` — mixed Cyrillic

## Bridge limits

- Explicit subcommand allowlist with argument arrays and `shell=False`; unlisted
  commands are rejected before process launch.
- Inputs must resolve under the sud-cli-owned state directory and are copied to a
  per-call temporary directory. Store and alerts are redirected there.
- The environment is allowlisted; proxy variables are **not** forwarded.
  `CSE_CAMOFOX_URL` is forwarded only when it resolves to a loopback host over
  http/https — anything else is refused, so it cannot be used as an SSRF pivot.
- `XDG_CACHE_HOME` is forwarded only when the path exists and is named `camoufox-py`.
- Output size and execution time are bounded.
- **Upstream stderr and stdout are never surfaced.** A non-zero exit is reported as a
  fixed Russian sentence plus the exit code; upstream text may contain case content.
- `--runtime-url`, `--fixtures` and `--now` are not in any allowlist: they would allow
  arbitrary endpoint access, arbitrary directory reads and time spoofing.
- Errors from case-search never reach the operator.

## Verification

```bash
python3 -m pytest -q                 # full suite
python3 -m compileall -q sud_cli tests
python3 tools/recalc_manifest.py     # MANIFEST hash/size check
python3 -m sud_cli ready             # agent-facing readiness gate
```

The bridge was exercised live against the installed binary: 23 commands across the
offline, live-gated and blocked paths, including real saved ГАС Правосудие and KAD
documents. Mutation testing confirmed the new tests fail when the corresponding fix
is reverted.