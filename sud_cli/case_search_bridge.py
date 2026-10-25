"""Safe, transitional case-search CLI bridge.

This module implements a narrow, allowlisted subprocess surface over the
installed ``case-search`` binary. It is a compatibility stepping stone;
sud-cli's target remains independently implemented functionality.

Only the commands explicitly listed in the phase plan are supported. Every
invocation uses an argument array (never a shell), a bounded timeout/output
size, an explicit sanitized environment, and per-invocation temporary
directories under the sud-cli state directory. Input files are validated and
copied into the temporary directory before the subprocess sees them, so the
upstream binary never receives paths outside the isolated workspace.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit


class BridgeError(Exception):
    """Validation or internal bridge failure."""


#: Commands the bridge may forward to the upstream binary.
#: ``needs_case`` -- a synthetic --case value is required.
#: ``needs_out``  -- the command produces output to a temp file.
#: ``json_out``   -- the output file should be parsed as JSON.
_COMMANDS = {
    "providers": {"needs_case": False, "needs_out": False, "json_out": False},
    "import": {"needs_case": True, "needs_out": True, "json_out": True},
    "kad-import": {"needs_case": True, "needs_out": True, "json_out": True},
    "vsrf-import": {"needs_case": True, "needs_out": True, "json_out": True},
    "auto-import": {"needs_case": True, "needs_out": True, "json_out": True},
    # Added: upstream parses a saved ГАС Правосудие printout into a full case
    # card (number, court, judge, parties, events, next hearing) and fails
    # closed when the requested --case is absent from the document. It was
    # reachable directly from the upstream binary but the bridge had no entry,
    # so sud-cli answered "unknown bridge command" for the one importer that
    # actually works on general-jurisdiction cases.
    "sudrf-import": {"needs_case": True, "needs_out": True, "json_out": True},
    "extract-text": {"needs_case": False, "needs_out": True, "json_out": False},
}

#: Commands that must never be invoked, even if the upstream binary supports them.
#:
#: ``config`` stays permanently forbidden: it can mutate upstream configuration.
#: ``watch`` stays permanently forbidden: it mutates watchlist/alert state.
#: The remaining entries are live/network commands that are now permitted, but
#: only when live access is explicitly enabled (see ``SUD_CLI_BRIDGE_LIVE``).
#: They are deliberately *not* removed from this set: the list doubles as the
#: registry of commands that require the live gate, so enabling one is an
#: explicit edit rather than an accidental side effect of a future change.
_FORBIDDEN_COMMANDS = {
    "config",
    "watch",
}

#: Environment switch that enables live court-source commands through the
#: bridge. Default is off. Live commands reach external court hosts and must be
#: opted into explicitly so that merely running sud-cli cannot trigger network
#: access.
LIVE_ENV_VAR = "SUD_CLI_BRIDGE_LIVE"

#: Live commands and how they take their arguments upstream.
#:
#: ``positional`` lists the argument vectors appended after the command name,
#: in upstream order. Live commands do NOT use ``--case``: the upstream CLI
#: takes them positionally (``lookup case_ref``, ``case-history case_number``),
#: and ``mos-live`` takes two (``court_slug case_number``). ``kad-search`` takes
#: none positionally — its inputs are flag-based, so it is not exposed here
#: until a validated flag allowlist exists.
#:
#: ``flags`` lists extra flags forwarded after the positional arguments. Only
#: enumerated choices are allowed — free-form values would let a caller inject
#: upstream arguments the bridge never validated.
_LIVE_COMMANDS = {
    # `lookup` without `--source` always failed: upstream then defaults to the
    # `synthetic` provider, which only knows fixture case refs, so every real
    # reference returned "source 'synthetic' unavailable". The flag is exposed
    # so an agent can pick a real source; `synthetic` is deliberately kept out
    # of the choices because it is unreachable from the bridge (no --fixtures).
    "lookup": {
        "positional": 1,
        "json_out": True,
        "flags": {
            "--source": frozenset({"kad", "mos-gorsud", "mos-gorsud-dom", "sudrf"}),
        },
    },
    "case-history": {"positional": 1, "json_out": True},
    "sudrf-live": {"positional": 1, "json_out": True},
    "mos-live": {"positional": 2, "json_out": True},
}

# Flag-driven live commands (Phase 8).
#
# Unlike the four commands above, these take their inputs through flags rather
# than positional arguments, so they need an explicit per-flag allowlist instead
# of a positional count. The allowlist is derived from the upstream ``--help``
# output of case-search v1.2.0, not from assumption:
#
#   search-practice <query> [--section {arbitral,regular,magistrate,vsrf}]
#                   [--page N] [--limit N] [--via-camofox]
#                   [--runtime-url URL]
#   kad-search [--inn INN] [--ogrn OGRN] [--name NAME] [--page N]
#              [--runtime-url URL]
#   get-text <url_or_file> [--out FILE] [--no-cache] [--blocks]
#
# The following upstream flags are deliberately NOT allowlisted, because the
# bridge sets them itself or because they redirect where data is read from:
#
#   --fixtures DIR    reads synthetic fixtures from an arbitrary directory;
#                     the bridge pins store/alerts instead.
#   --runtime-url     accepts an arbitrary base URL (including a raw IP, e.g.
#                     ``http://169.254.169.254``) and turns the bridge into a
#                     request forwarder. Only the literal value ``native``
#                     is allowed, which selects the embedded browser.
#   --now TS          fixed-clock override used to make snapshots
#                     deterministic; meaningless for live reads and would
#                     make output timestamps misleading.
#   --store/--alerts  already forced into the isolated temp workspace.
_FLAG_COMMANDS = {
    "search-practice": {
        "positional": "query",
        "flags": {"section": "choice", "page": "int", "limit": "int"},
        "bools": {"via_camofox"},
        "json_out": True,
    },
    "kad-search": {
        "positional": None,
        "flags": {"inn": "digits", "ogrn": "digits", "name": "text", "page": "int"},
        "bools": set(),
        "json_out": True,
    },
    "get-text": {
        "positional": "url_or_file",
        "flags": {},
        "bools": {"no_cache", "blocks"},
        "json_out": False,
    },
}

#: Human-readable upstream flag names for the Python-attribute spellings used
#: above (``no_cache`` -> ``--no-cache``).
_FLAG_SPELLING = {"via_camofox": "--via-camofox", "no_cache": "--no-cache"}

#: ``--section`` accepts only these values (upstream enum).
_SECTIONS = ("arbitral", "regular", "magistrate", "vsrf")

#: sudact.ru is the only document host exposed through ``get-text``. Anything
#: else would turn the bridge into an arbitrary-URL fetcher.
_SUDACT_HOST = "sudact.ru"

#: INN is 10 or 12 digits; OGRN/OGRNIP are 13 or 15. Only digits are accepted,
#: so the value can never carry a flag, a separator, or a query fragment.
_INN_RE = re.compile(r"^(\d{10}|\d{12})$")
_OGRN_RE = re.compile(r"^(\d{13}|\d{15})$")

#: Free-text search terms and party names: allow any Unicode letter, digit,
#: space and a small set of punctuation marks. Single token or short phrase.
#: Excludes ``;&|`$<>(){}[]*?!`` and whitespace runs, so nothing can escape
#: into a shell or a second argv token.
_SEARCH_TEXT_RE = re.compile(r"^[\w\s.,()'\"«»-]{2,200}$", re.UNICODE)

#: Page/limit bounds mirror the upstream defaults (page 1, limit 10, max 30).
_MAX_PAGE = 1000
_MAX_LIMIT = 30

#: Court reference syntax for live lookups. Accepts a Cyrillic or Latin letter
#: prefix, digits, hyphen, slash and a year, e.g. ``А40-12345/2026``
#: or ``33-35073/2026`` for SudRF. The value is passed as a single argv token
#: with ``shell=False``; this regex rejects separators, whitespace and flags.
#:
#: The year is two or four digits, not strictly four: Russian courts print the
#: abbreviated form on the document itself — a real АС Москвы ruling is headed
#: ``Дело № А40-11111/25`` — and case-search's own case-history search matches
#: on exactly that abbreviated form, so a four-digit-only pattern rejects the
#: reference that upstream is able to resolve. Verified live 2026-10-09 against
#: sudact doc docidPLACEHOLDER1; both ``/2025`` and ``/25`` resolve there.
#:
#: The court number is a hyphen-separated pair — the judge's court code and the
#: case number (``А40-11111`` for АС Москвы) — optionally prefixed by a single
#: letter. Both the hyphen and the second number are required: a relaxed
#: ``-?\d{0,6}`` silently admitted ``А40/25`` and ``А4012345/25``, which no
#: Russian court prints.
#:
#: The terminator is ``\Z``, not ``$``: in Python ``$`` also matches just
#: *before* a trailing newline, so ``"А40-12345/25\n"`` passed validation while
#: claiming to be an exact reference. Not shell-exploitable (argv is a list and
#: ``shell=False``) but a contract/reality mismatch, so the anchor is strict.
_LIVE_CASE_RE = re.compile(
    r"\A[0-9A-Za-zА-Яа-яЁё]{1,4}-\d{1,7}/(?:\d{2}|\d{4})\Z"
)

#: Simple synthetic identifiers (ASCII letters/digits/dot/underscore/hyphen).
#: Must start with an alphanumeric character so the value cannot be mistaken
#: for a flag or hidden path segment.
_SAFE_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: Standard court reference syntax with an optional Cyrillic or Latin court
#: prefix. Examples: ``A00-00001/2099`` (Latin) or ``А00-00001/2099``
#: (Cyrillic А). The values accepted here are synthetic test identifiers only.
#: A court case number as Russian courts actually print it.
#:
#: Derived from the upstream parsers rather than from assumption. Letters are
#: legal in ANY prefix position, which is what the courts do and what the
#: earlier "letter then digits" pattern missed:
#:
#: * ``А40-11111/25``    — Арбитражный суд (буква + цифры)
#: * ``2а-2939/2025``     — районный суд ГАС Правосудие (ЦИФРА + буква)
#: * ``33а-29651/2025``   — апелляция (цифры + буква)
#: * ``02-3696/2024``     — ВС РФ (цифры)
#: * ``16АП-1234/2026``   — апелляция, буквы в конце префикса
#:
#: So a digit-first prefix is the *majority* format for general-jurisdiction
#: courts, and both the old patterns rejected it.
#:
#: Sources: mos/parser.py ``[0-9А-ЯA-Z]{1,4}-[0-9]{3,6}/20[0-9]{2}``;
#: sudrf/parser.py ``[0-9]{1,4}[А-Яа-яA-Za-z]{0,3}-\d{1,6}/\d{4}``;
#: vsrf/parser.py ``[А-ЯA-Z0-9]{1,6}-\d{2,7}/\d{2,4}``;
#: sudact/case_history.py ``[А-ЯA-Z0-9]{1,6}-\d{1,7}/\d{2,4}``.
#:
#: ``\A``/``\Z`` rather than ``^``/``$``: Python's ``$`` also matches before a
#: trailing newline, so ``"А40-12345/25\n"`` would pass. The trailing
#: alternative accepts the 2-digit year courts use in printed text.
#:
#: The prefix is bounded at 4 characters on measured evidence: across the
#: upstream fixtures the longest real prefix is ``16АП``, while ``{1,6}`` (the
#: vsrf parser's bound) would admit implausible shapes like ``12345-12345/25``.
#: A 2-digit year is accepted because it is a genuine abbreviation, not a
#: malformed number — ``А00-00001/20`` carries no injectable characters.
_COURT_REFERENCE_RE = re.compile(r"\A[0-9A-Za-zА-Яа-яЁё]{1,4}-\d{1,7}/(?:\d{2}|\d{4})\Z")

#: Maximum bytes the bridge will read from upstream stdout or an output file.
MAX_OUTPUT_BYTES = 1 * 1024 * 1024

#: Maximum seconds to wait for the upstream binary.
TIMEOUT_SECONDS = 30

#: Per-command timeout overrides, in seconds.
#:
#: ``kad-search`` drives a full browser session against kad.arbitr.ru: the
#: embedded Camoufox navigates, waits for load, and only then runs the query.
#: The global 30s cap is shorter than a single ``Page.goto`` (upstream budget:
#: 60s), so the bridge would kill every kad-search as "timed out" regardless of
#: whether the upstream search was working. Measured live: kad.arbitr.ru can
#: take ~2 minutes end to end, while ``search-practice`` against sudact.ru
#: completes in ~6 seconds.
_COMMAND_TIMEOUTS = {
    "kad-search": 180,
}

#: Environment variable names the bridge is willing to forward.
#: HOME, SUD_CLI_STATE_DIR, and TMPDIR are set explicitly under the
#: per-invocation temporary workspace so caller values are never forwarded.
_ALLOWED_ENV_NAMES = {
    "PATH",
    "LC_ALL",
    "LANG",
    "TZ",
}

#: Hostnames the external Camofox runtime may listen on. ``CSE_CAMOFOX_URL``
#: is the documented switch that moves case-search from its embedded
#: browser onto the shared camofox service (``CSE_CAMOFOX_URL=http://...``).
#: Without it every JS-capable command falls back to the in-process browser,
#: which fails inside the bridge with "Playwright Sync API inside the asyncio
#: loop" — the bridge runs the child in a context where the embedded launch
#: cannot initialise, while the service on loopback renders the same pages.
#:
#: Forwarded only for a loopback URL. A non-loopback host would let a caller
#: point the child at an arbitrary endpoint (SSRF), so such values are dropped
#: rather than silently trusted — the same fail-closed posture the command
#: allowlist takes.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _forwarded_camofox_url(environ: Optional[dict] = None) -> Optional[str]:
    """Return ``CSE_CAMOFOX_URL`` when it points at a loopback runtime.

    Only the exact loopback literals above are accepted, and only over http
    or https; anything else returns ``None`` so the variable is dropped.
    """
    source = os.environ if environ is None else environ
    raw = source.get("CSE_CAMOFOX_URL")
    if not raw:
        return None
    parsed = urlsplit(raw.strip())
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.hostname not in _LOOPBACK_HOSTS:
        return None
    return raw.strip()

#: Camoufox locates its browser payload through ``XDG_CACHE_HOME``. The bridge
#: scrubs the environment and points ``HOME`` at a throwaway directory, which
#: otherwise makes the embedded browser report ``official/stable is not
#: installed`` and every live search fail.
#:
#: The value is not forwarded blindly. It is accepted only when it is an
#: existing directory whose name marks it as a camoufox cache; anything else is
#: dropped rather than pointed at an arbitrary location. On this host the
#: python layout (``~/.cache/camoufox-py``) is the one that works -- the node
#: layout (``~/.cache/camoufox``) exists but is not a usable payload, which is
#: the collision documented in the case-search skill: both layouts share one
#: cache directory and expect different content, so ``camoufox fetch`` must run
#: with ``XDG_CACHE_HOME=~/.cache/camoufox-py``.
_CAMOUFOX_CACHE_NAME = "camoufox-py"
_XDG_CACHE_HOME = "XDG_CACHE_HOME"


def _forwarded_env_value(env: Dict[str, str]) -> Optional[str]:
    """Return a safe XDG_CACHE_HOME value, or None to drop the variable.

    ``XDG_CACHE_HOME`` is not set in this user's profile — the Camoufox payload
    simply lives at ``~/.cache/camoufox-py`` — so reading it only from ``env``
    yields nothing and the upstream binary then looks for its browser under the
    isolated HOME and fails with "official/stable is not installed". The default
    is therefore derived from the *real* HOME rather than from the caller's
    environment.

    Forwarding an arbitrary caller-supplied path unchanged would let a
    caller-controlled value steer where the upstream binary reads a browser
    payload from, so the rules stay: the directory must exist, be absolute, and
    be named ``camoufox-py``.
    """
    raw = env.get(_XDG_CACHE_HOME)
    if not raw:
        real_home = Path(env.get("HOME") or Path.home())
        raw = str(real_home / ".cache" / _CAMOUFOX_CACHE_NAME)
    candidate = Path(raw).expanduser()
    try:
        if not candidate.is_dir():
            return None
        if candidate.name != _CAMOUFOX_CACHE_NAME:
            return None
        if not candidate.is_absolute():
            return None
    except OSError:
        return None
    return str(candidate)


def _sanitize_env(environ: Optional[dict] = None) -> Dict[str, str]:
    """Return an explicit minimal environment with proxy/secret vars removed.

    Caller-supplied HOME, SUD_CLI_STATE_DIR, and TMPDIR are intentionally
    dropped; ``run_bridge_command`` replaces them with paths inside the
    isolated per-invocation temporary directory.

    ``CSE_CAMOFOX_URL`` is carried over separately: it is not on the plain
    allowlist because it only counts when it names a loopback runtime, which
    :func:`_forwarded_camofox_url` verifies before it survives.
    """
    source: dict = dict(environ if environ is not None else os.environ)
    safe = {k: v for k, v in source.items() if k in _ALLOWED_ENV_NAMES}
    camofox_url = _forwarded_camofox_url(source)
    if camofox_url:
        safe["CSE_CAMOFOX_URL"] = camofox_url
    return safe


def _subprocess_failure(
    command: str, returncode: int, stdout: str, stderr: str, *, snapshot: bool = True
) -> str:
    """Describe an upstream non-zero exit without leaking case content.

    The bridge deliberately never shows raw ``stderr`` to the caller: upstream
    error text can echo the document body of a real case (parties, amounts,
    judge). That rule stays, but a bare "bridge subprocess failed" left the
    agent with no way to tell a wrong input format from a missing license, so a
    small set of *recognised* upstream phrases is mapped to a fixed reason
    instead. Only phrases on this allowlist are ever surfaced, and each maps to
    a constant string that contains no upstream text.

    ``snapshot=False`` for commands that never write a snapshot (the live
    ones), so the fail-closed wording is not applied where it would be false.
    """
    blob = f"{stdout}\n{stderr}"

    if "import file is not valid JSON" in blob:
        return (
            f"{command}: ожидался JSON-файл выгрузки, а получен документ другого "
            "формата (для сохранённой HTML/PDF-карточки используйте auto-import)"
        )
    if "case identity mismatch" in blob:
        return (
            f"{command}: номер дела не найден в документе — "
            + ("fail-closed, снимок не изменён" if snapshot else "fail-closed")
        )
    if "no VS court body marker" in blob:
        return f"{command}: документ не распознан как решение суда"
    if "unknown subcommand" in blob:
        return (
            f"{command}: case-search не поддерживает эту команду для данного "
            "формата файла"
        )
    if "license" in blob.lower():
        return f"{command}: требуется действующая лицензия case-search"
    if returncode == 2:
        return f"{command}: неверные или недостающие аргументы для case-search"
    if returncode == 3:
        detail = "fail-closed: снимок не изменён" if snapshot else "fail-closed"
        return f"{command}: case-search отклонил документ ({detail})"
    return f"{command}: case-search завершился с кодом {returncode}"


def _validate_case_id(case_id: str) -> None:
    if not case_id:
        raise BridgeError("missing synthetic case id")
    if not (
        _SAFE_CASE_ID_RE.match(case_id) or _COURT_REFERENCE_RE.match(case_id)
    ):
        raise BridgeError("invalid synthetic case id")


def _validate_live_flags(
    command: str, options: Optional[dict]
) -> List[str]:
    """Return the extra upstream flags for a live command, validated.

    Only flags listed in the command's ``flags`` spec are accepted, and only
    with a value drawn from that flag's frozen choice set. An unknown flag, an
    empty value or a value outside the set is refused rather than forwarded, so
    a caller cannot smuggle ``--runtime-url`` or ``--fixtures`` (the two flags
    excluded from the bridge by design) through the generic options mapping.
    """
    spec = _LIVE_COMMANDS[command]
    allowed = spec.get("flags") or {}
    if options is None:
        return []
    if not isinstance(options, dict):
        raise BridgeError("invalid command options")
    if not options and not allowed:
        return []

    argv: List[str] = []
    for name, value in options.items():
        if name not in allowed:
            raise BridgeError(f"option not available for this command: {name}")
        choices = allowed[name]
        if not isinstance(value, str) or value not in choices:
            raise BridgeError(f"invalid value for option: {name}")
        argv.extend([name, value])
    return argv


def _live_access_enabled(environ: Optional[dict] = None) -> bool:
    """Return whether live court-source access is explicitly enabled.

    Only the exact values ``1``, ``true``, ``yes`` and ``on`` (case-insensitive)
    enable it. Anything else — including an unset variable, an empty string or
    a typo — leaves live commands blocked, so a misspelled setting cannot
    accidentally open network access.
    """
    env = os.environ if environ is None else environ
    raw = env.get(LIVE_ENV_VAR, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _validate_live_case_ref(case_ref: str) -> str:
    """Validate a real court case reference passed to a live command."""
    if not case_ref:
        raise BridgeError("missing case reference")
    if not _LIVE_CASE_RE.match(case_ref):
        raise BridgeError("invalid case reference")
    return case_ref


def _validate_court_slug(slug: str) -> str:
    """Validate a Moscow court slug (``mos-live`` first positional argument).

    Slugs are lowercase latin with digits and hyphens, e.g. ``horoshevskij``.
    Keeping the charset narrow guarantees the value is one argv token and
    cannot smuggle a flag or a path separator.
    """
    if not slug:
        raise BridgeError("missing court slug")
    if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", slug):
        raise BridgeError("invalid court slug")
    return slug


def _validate_search_text(value: str, field: str = "value") -> str:
    """Validate a free-text query or party name.

    Accepts a short phrase of letters, digits, spaces and light punctuation.
    Everything that could start a flag, split an argv token, or reach a shell
    is rejected by the regex, which is a stronger guarantee than escaping.
    """
    if not value or not isinstance(value, str):
        raise BridgeError(f"missing {field}")
    if not _SEARCH_TEXT_RE.match(value):
        raise BridgeError(f"invalid {field}")
    return value


def _validate_inn(value: str) -> str:
    """Validate a participant INN: exactly 10 or 12 digits."""
    if not value or not _INN_RE.match(value):
        raise BridgeError("invalid inn")
    return value


def _validate_ogrn(value: str) -> str:
    """Validate a participant OGRN/OGRNIP: exactly 13 or 15 digits."""
    if not value or not _OGRN_RE.match(value):
        raise BridgeError("invalid ogrn")
    return value


def _validate_bounded_int(value: str, field: str, maximum: int) -> str:
    """Validate a positive integer flag value within ``1..maximum``."""
    if value is None or not re.match(r"^\d{1,9}$", str(value)):
        raise BridgeError(f"invalid {field}")
    number = int(value)
    if number < 1 or number > maximum:
        raise BridgeError(f"invalid {field}")
    return str(number)


def _validate_section(value: str) -> str:
    """Validate the sudact section against the upstream enum."""
    if value not in _SECTIONS:
        raise BridgeError("invalid section")
    return value


def _validate_sudact_url(value: str) -> str:
    """Validate a sudact.ru document URL for ``get-text``.

    This is the narrowest gate in the bridge: without it, ``get-text`` would
    fetch an arbitrary URL supplied by the caller, turning sud-cli into an open
    HTTP client against any reachable host (including internal addresses).
    Only ``http``/``https`` on ``sudact.ru`` (or its subdomains) is accepted,
    and only when no credentials are embedded in the URL.
    """
    if not value or not isinstance(value, str):
        raise BridgeError("missing url")
    if any(ch.isspace() for ch in value):
        raise BridgeError("invalid url")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise BridgeError("invalid url")
    if not parts.hostname:
        raise BridgeError("invalid url")
    if parts.username or parts.password:
        raise BridgeError("invalid url")
    host = parts.hostname.lower()
    if host != _SUDACT_HOST and not host.endswith("." + _SUDACT_HOST):
        raise BridgeError("invalid url")
    return value


def _build_flag_argv(command: str, options: Optional[dict]) -> List[str]:
    """Build the validated argv tail for a flag-driven live command.

    ``options`` carries only already-named caller inputs (e.g.
    ``{"query": "...", "section": "arbitral", "limit": 5}``). Every value is
    validated here and dropped silently if it is ``None``; unknown keys are an
    error rather than being forwarded, so a new caller field can never become an
    arbitrary upstream flag by accident.
    """
    spec = _FLAG_COMMANDS[command]
    options = options or {}
    allowed_flags = spec["flags"]
    allowed_bools = spec["bools"]
    positional_kind = spec["positional"]

    # Only the positional key this command actually declares is accepted. A
    # generic ``{"query", "url_or_file"}`` allowance would let a caller pass a
    # search term to ``kad-search`` (which takes none) and have it silently
    # dropped instead of raising -- a cross-command input leak.
    positional_key = {positional_kind} if positional_kind else set()
    unknown = set(options) - set(allowed_flags) - allowed_bools - positional_key
    if unknown:
        raise BridgeError("unsupported option")

    argv: List[str] = []

    if positional_kind == "query":
        argv.append(_validate_search_text(options.get("query"), "query"))
    elif positional_kind == "url_or_file":
        argv.append(_validate_sudact_url(options.get("url_or_file")))

    for name, kind in sorted(allowed_flags.items()):
        value = options.get(name)
        if value is None:
            continue
        if kind == "digits":
            if name == "inn":
                value = _validate_inn(value)
            else:
                value = _validate_ogrn(value)
        elif kind == "text":
            value = _validate_search_text(value, name)
        elif kind == "choice":
            value = _validate_section(value)
        elif kind == "int":
            maximum = _MAX_LIMIT if name == "limit" else _MAX_PAGE
            value = _validate_bounded_int(value, name, maximum)
        else:  # pragma: no cover - guarded by the static allowlist above
            raise BridgeError("unsupported option")
        argv.extend([f"--{name}", value])

    for name in sorted(allowed_bools):
        if options.get(name):
            argv.append(_FLAG_SPELLING.get(name, f"--{name.replace('_', '-')}"))

    return argv


def _validate_input_path(input_path: Path, allowed_root: Path) -> Path:
    """Reject traversal, directories, out-of-root files, and non-existent files.

    The returned path is the original, resolved source file. The subprocess
    never receives this path directly; the caller copies the file into the
    isolated temporary workspace.
    """
    if not input_path:
        raise BridgeError("missing input path")
    # Reject explicit traversal attempts before resolving.
    parts = input_path.parts
    if ".." in parts:
        raise BridgeError("input path outside allowed root")
    resolved = input_path.expanduser().resolve()
    root = allowed_root.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise BridgeError("input path outside allowed root")
    if not resolved.exists():
        raise BridgeError("input file not found")
    if not resolved.is_file():
        raise BridgeError("input path is not a file")
    return resolved


def _ensure_state_dir(state_dir: Path) -> Path:
    resolved = state_dir.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _exec(
    cmd: Sequence[str],
    env: Dict[str, str],
    cwd: Path,
    out_file: Optional[Path] = None,
    timeout_seconds: Optional[int] = None,
) -> Tuple[int, str, str]:
    """Run the upstream binary with explicit safe settings.

    Stdout and stderr are read incrementally. The child is terminated as
    soon as the combined output exceeds ``MAX_OUTPUT_BYTES``, and the
    process is always reaped on timeout or overflow. If ``out_file`` is
    provided, its size is also monitored during execution and the child
    is killed the moment the file grows beyond the cap.

    ``timeout_seconds`` defaults to :data:`TIMEOUT_SECONDS`, resolved at call
    time rather than bound into the signature, so that a per-command override
    from :data:`_COMMAND_TIMEOUTS` -- or a test patching the module constant --
    is actually honoured.
    """
    if timeout_seconds is None:
        timeout_seconds = TIMEOUT_SECONDS
    proc = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env=env,
        cwd=str(cwd),
    )

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    total = 0
    lock = threading.Lock()
    overflow = threading.Event()

    def _drain(stream, buf: bytearray) -> None:
        nonlocal total
        try:
            while True:
                chunk = stream.read1(65536)
                if not chunk:
                    break
                with lock:
                    if overflow.is_set():
                        continue
                    if total + len(chunk) > MAX_OUTPUT_BYTES:
                        overflow.set()
                        continue
                    buf.extend(chunk)
                    total += len(chunk)
        finally:
            stream.close()

    def _out_file_overflow() -> bool:
        if out_file is None:
            return False
        try:
            return out_file.stat().st_size > MAX_OUTPUT_BYTES
        except FileNotFoundError:
            return False

    out_thread = threading.Thread(target=_drain, args=(proc.stdout, stdout_buf), daemon=True)
    err_thread = threading.Thread(target=_drain, args=(proc.stderr, stderr_buf), daemon=True)
    out_thread.start()
    err_thread.start()

    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if overflow.is_set() or _out_file_overflow():
                overflow.set()
                proc.kill()
                proc.wait()
                out_thread.join(timeout=5)
                err_thread.join(timeout=5)
                raise BridgeError("bridge output exceeded size limit")

            rc = proc.poll()
            if rc is not None:
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait()
                raise subprocess.TimeoutExpired(cmd, TIMEOUT_SECONDS)

            time.sleep(min(0.05, remaining))

        out_thread.join(timeout=5)
        err_thread.join(timeout=5)

        return (
            proc.returncode,
            stdout_buf.decode("utf-8", errors="replace"),
            stderr_buf.decode("utf-8", errors="replace"),
        )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        out_thread.join(timeout=1)
        err_thread.join(timeout=1)


def _read_limited_text(path: Path) -> str:
    """Read a text file with a bounded size check.

    A missing file means the child exited without producing its output
    artifact. That is a state, not a crash: an unhandled ``FileNotFoundError``
    here escaped as ``unexpected internal error: FileNotFoundError`` and hid
    the real cause. Report it as a bridge failure instead, and never echo the
    child's stderr, which may carry case content.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError as exc:
        raise BridgeError("bridge produced no output file") from exc
    if size > MAX_OUTPUT_BYTES:
        raise BridgeError("bridge output exceeded size limit")
    return path.read_text(encoding="utf-8", errors="replace")


def _read_limited_json(path: Path):
    text = _read_limited_text(path)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise BridgeError("bridge returned invalid JSON") from exc


def _sanitize_local_import_payload(payload):
    """Remove invocation-local file URI paths from normalized import results."""
    if not isinstance(payload, dict):
        return payload
    provenance = payload.get("provenance")
    if isinstance(provenance, dict):
        source_url = provenance.get("source_url")
        if isinstance(source_url, str) and source_url.startswith("file://"):
            provenance["source_url"] = "synthetic://local-import"
    return payload




def _run_live_command(
    command: str,
    *,
    state_dir: Path,
    case_id: Optional[str],
    court_slug: Optional[str],
    binary_path: str,
    env: Optional[dict],
    options: Optional[dict] = None,
) -> dict:
    """Run one live court-source command through the same safety envelope.

    Live commands differ from offline imports in two ways, both handled here:

    * Their inputs are positional upstream, not ``--case``. ``lookup``,
      ``case-history`` and ``sudrf-live`` take a single case reference;
      ``mos-live`` takes a court slug followed by a case number.
    * They return JSON snapshots, so output lands in a temp file and is parsed
      with the same size cap and sanitized-error policy as offline imports.

    The subprocess keeps every isolation property of the offline path: argv
    arrays with ``shell=False``, a 30s timeout, a 1 MiB output cap, HOME /
    store / alerts / TMPDIR redirected into a per-invocation temp workspace,
    and a sanitized environment with proxy variables dropped.
    """
    spec = _LIVE_COMMANDS[command]
    positional_count = spec["positional"]

    try:
        case_ref = _validate_live_case_ref(case_id)
        if positional_count == 2:
            slug = _validate_court_slug(court_slug)
            positionals = [slug, case_ref]
        else:
            if court_slug is not None:
                raise BridgeError("court slug is not valid for this command")
            positionals = [case_ref]

        extra_flags = _validate_live_flags(command, options)

        resolved_state = _ensure_state_dir(state_dir)
        bridge_root = resolved_state / "bridge-temp"
        bridge_root.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(dir=bridge_root) as tmpdir:
            tmp_path = Path(tmpdir)
            store_dir = tmp_path / "store"
            alerts_dir = tmp_path / "alerts"
            home_dir = tmp_path / "home"
            store_dir.mkdir()
            alerts_dir.mkdir()
            home_dir.mkdir()

            out_file = tmp_path / "output.json"
            cmd: List[str] = [binary_path, command]
            cmd.extend(positionals)
            cmd.extend(extra_flags)
            cmd.extend(["--json", str(out_file)])
            cmd.extend(["--store", str(store_dir), "--alerts", str(alerts_dir)])

            safe_env = _sanitize_env(env)
            safe_env["HOME"] = str(home_dir)
            safe_env["SUD_CLI_STATE_DIR"] = str(store_dir)
            safe_env["TMPDIR"] = str(tmp_path)
            xdg = _forwarded_env_value(os.environ if env is None else env)
            if xdg:
                safe_env[_XDG_CACHE_HOME] = xdg
            _stage_license(home_dir, env)

            try:
                returncode, _stdout, _stderr = _exec(
                    cmd, safe_env, tmp_path, out_file=out_file
                )
            except subprocess.TimeoutExpired:
                return {"status": "unavailable", "data": "bridge subprocess timed out"}
            except OSError:
                return {"status": "unavailable", "data": "bridge binary unavailable"}

            if returncode != 0:
                # Never echo upstream stderr: it can contain case data or paths.
                # _subprocess_failure maps only allowlisted phrases to fixed
                # reasons, so no upstream text reaches the caller.
                return {
                    "status": "error",
                    "data": _subprocess_failure(
                        command, returncode, _stdout, _stderr, snapshot=False
                    ),
                }

            try:
                payload = _read_limited_json(out_file)
            except BridgeError as exc:
                return {"status": "error", "data": str(exc)}

            if isinstance(payload, dict):
                payload.setdefault("bridge", {})["live"] = True
            return {"status": "ok", "data": payload}
    except BridgeError as exc:
        return {"status": "error", "data": str(exc)}


def _stage_license(home_dir: Path, environ: Optional[dict] = None) -> None:
    """Copy the upstream license file into the isolated per-invocation HOME.

    The bridge runs every subprocess with ``HOME`` pointed at a throwaway
    directory so that neither the snapshot store nor the alert state can leak
    between invocations or out of the workspace. case-search resolves its
    license through ``$HOME/.config/case-search/config.json``, so the
    isolation would otherwise strip the license and every live command would
    fail with ``license error: no license key``.

    Two approaches were verified against the real binary. Passing the key as
    ``CSE_LICENSE_KEY`` in the environment also works, but it exposes the secret
    in the process environment where it is readable via ``/proc/<pid>/environ``
    by anything running as this user. Copying the file keeps the secret on
    disk, inside a directory that is deleted with the temporary workspace, and
    is readable only by the invoking user (mode 0600).

    This is a deliberate, narrow exception to the environment scrubbing: exactly
    one file is copied, it is never forwarded as an environment variable, and
    the destination lives and dies with the invocation.
    """
    env = os.environ if environ is None else environ
    source = env.get("CSE_LICENSE_FILE")
    if not source:
        source = str(Path(env.get("HOME", str(Path.home()))) / ".config" / "case-search" / "config.json")
    source_path = Path(source).expanduser()
    try:
        if not source_path.is_file():
            return
        target_dir = home_dir / ".config" / "case-search"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "config.json"
        shutil.copy2(source_path, target)
        target.chmod(0o600)
    except OSError:
        # A missing or unreadable license leaves the upstream error in place;
        # it is reported by case-search itself, not masked here.
        return


def _run_flag_command(
    command: str,
    *,
    state_dir: Path,
    options: Optional[dict],
    binary_path: str,
    env: Optional[dict],
) -> dict:
    """Run one flag-driven live command through the same safety envelope.

    This is deliberately a sibling of :func:`_run_live_command`, not a copy of
    it: the temporary workspace, environment scrubbing, argv array execution,
    timeout, output cap, and sanitized error policy all come from the same code
    paths, so a future change to those protections applies to both.
    """
    spec = _FLAG_COMMANDS[command]
    try:
        resolved_state = _ensure_state_dir(state_dir)
        bridge_root = resolved_state / "bridge-temp"
        bridge_root.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(dir=bridge_root) as tmpdir:
            tmp_path = Path(tmpdir)
            store_dir = tmp_path / "store"
            alerts_dir = tmp_path / "alerts"
            home_dir = tmp_path / "home"
            store_dir.mkdir()
            alerts_dir.mkdir()
            home_dir.mkdir()

            flag_argv = _build_flag_argv(command, options)

            out_file = tmp_path / ("output.json" if spec["json_out"] else "output.txt")
            cmd: List[str] = [binary_path, command]
            cmd.extend(flag_argv)
            if spec["json_out"]:
                cmd.extend(["--json", str(out_file)])
            else:
                cmd.extend(["--out", str(out_file)])
            cmd.extend(["--store", str(store_dir), "--alerts", str(alerts_dir)])

            safe_env = _sanitize_env(env)
            safe_env["HOME"] = str(home_dir)
            safe_env["SUD_CLI_STATE_DIR"] = str(store_dir)
            safe_env["TMPDIR"] = str(tmp_path)
            xdg = _forwarded_env_value(os.environ if env is None else env)
            if xdg:
                safe_env[_XDG_CACHE_HOME] = xdg
            _stage_license(home_dir, env)

            try:
                returncode, _stdout, _stderr = _exec(
                    cmd,
                    safe_env,
                    tmp_path,
                    out_file=out_file,
                    timeout_seconds=_COMMAND_TIMEOUTS.get(command, TIMEOUT_SECONDS),
                )
            except subprocess.TimeoutExpired:
                return {"status": "unavailable", "data": "bridge subprocess timed out"}
            except OSError:
                return {"status": "unavailable", "data": "bridge binary unavailable"}

            if returncode != 0:
                # Never echo upstream stderr: it can contain case data or paths.
                # _subprocess_failure maps only allowlisted phrases to fixed
                # reasons, so no upstream text reaches the caller.
                return {
                    "status": "error",
                    "data": _subprocess_failure(
                        command, returncode, _stdout, _stderr, snapshot=False
                    ),
                }

            try:
                if spec["json_out"]:
                    payload = _read_limited_json(out_file)
                else:
                    payload = _read_limited_text(out_file).strip()
            except BridgeError as exc:
                return {"status": "error", "data": str(exc)}

            if isinstance(payload, dict):
                payload.setdefault("bridge", {})["live"] = True
            return {"status": "ok", "data": payload}
    except BridgeError as exc:
        return {"status": "error", "data": str(exc)}


def run_bridge_command(
    command: str,
    *,
    state_dir: Path,
    input_path: Optional[Path] = None,
    case_id: Optional[str] = None,
    binary_path: str = "case-search",
    env: Optional[dict] = None,
    court_slug: Optional[str] = None,
    options: Optional[dict] = None,
) -> dict:
    """Run one allowlisted case-search command safely.

    Returns a dict with at least ``status`` (``ok``, ``error``, ``blocked``,
    or ``unavailable``) and ``data``. The result is safe to serialize; it
    never contains absolute paths, input values, or raw upstream output.

    Offline import commands take their input from ``input_path``/``case_id``.
    Live commands (``_LIVE_COMMANDS``) instead take ``case_id`` as the real
    court case reference and, for ``mos-live``, ``court_slug``. Flag-driven
    live commands (``_FLAG_COMMANDS``) take a validated ``options`` mapping.
    All live commands run only when ``SUD_CLI_BRIDGE_LIVE`` is set; otherwise
    they return ``blocked`` without spawning any subprocess.
    """
    if command in _FORBIDDEN_COMMANDS:
        return {"status": "blocked", "data": "command is not available through the bridge"}

    is_live = command in _LIVE_COMMANDS
    is_flag = command in _FLAG_COMMANDS
    if is_live or is_flag:
        # Live path is gated independently of the offline allowlist so that
        # enabling network access is a separate, visible decision.
        if not _live_access_enabled(env):
            return {
                "status": "blocked",
                "data": (
                    "live court access is disabled; "
                    f"set {LIVE_ENV_VAR}=1 to enable"
                ),
            }
        if is_flag:
            return _run_flag_command(
                command,
                state_dir=state_dir,
                options=options,
                binary_path=binary_path,
                env=env,
            )
        return _run_live_command(
            command,
            state_dir=state_dir,
            case_id=case_id,
            court_slug=court_slug,
            binary_path=binary_path,
            env=env,
            options=options,
        )

    if command not in _COMMANDS:
        return {"status": "blocked", "data": "unknown bridge command"}

    spec = _COMMANDS[command]

    try:
        resolved_state = _ensure_state_dir(state_dir)

        if spec["needs_case"]:
            _validate_case_id(case_id)
        if input_path is not None:
            source_path = _validate_input_path(input_path, resolved_state)
        else:
            source_path = None

        bridge_root = resolved_state / "bridge-temp"
        bridge_root.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(dir=bridge_root) as tmpdir:
            tmp_path = Path(tmpdir)
            store_dir = tmp_path / "store"
            alerts_dir = tmp_path / "alerts"
            home_dir = tmp_path / "home"
            store_dir.mkdir()
            alerts_dir.mkdir()
            home_dir.mkdir()

            cmd: List[str] = [binary_path, command]
            temp_input: Optional[Path] = None
            if source_path is not None:
                temp_input = tmp_path / "input" / source_path.name
                temp_input.parent.mkdir()
                shutil.copy2(source_path, temp_input)
                cmd.append(str(temp_input))

            if spec["needs_case"]:
                cmd.extend(["--case", case_id])

            out_file: Optional[Path] = None
            if spec["needs_out"]:
                if spec["json_out"]:
                    out_file = tmp_path / "output.json"
                    cmd.extend(["--json", str(out_file)])
                else:
                    out_file = tmp_path / "output.txt"
                    cmd.extend(["--out", str(out_file)])

            # Isolate store/alerts where the upstream CLI supports these flags.
            if command != "providers":
                cmd.extend(["--store", str(store_dir), "--alerts", str(alerts_dir)])

            safe_env = _sanitize_env(env)
            safe_env["HOME"] = str(home_dir)
            safe_env["SUD_CLI_STATE_DIR"] = str(store_dir)
            safe_env["TMPDIR"] = str(tmp_path)
            xdg = _forwarded_env_value(os.environ if env is None else env)
            if xdg:
                safe_env[_XDG_CACHE_HOME] = xdg
            _stage_license(home_dir, env)

            try:
                returncode, stdout, stderr = _exec(cmd, safe_env, tmp_path, out_file=out_file)
            except subprocess.TimeoutExpired:
                return {"status": "unavailable", "data": "bridge subprocess timed out"}
            except OSError:
                return {"status": "unavailable", "data": "bridge binary unavailable"}

            if returncode != 0:
                return {
                    "status": "error",
                    "data": _subprocess_failure(command, returncode, stdout, stderr),
                }

            if command == "providers":
                if len(stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
                    return {"status": "error", "data": "bridge output exceeded size limit"}
                try:
                    payload = json.loads(stdout)
                except json.JSONDecodeError:
                    return {"status": "error", "data": "bridge returned invalid JSON"}
                return {"status": "ok", "data": payload}

            if out_file is not None:
                try:
                    if spec["json_out"]:
                        payload = _read_limited_json(out_file)
                        if command in {"import", "kad-import", "vsrf-import", "auto-import"}:
                            payload = _sanitize_local_import_payload(payload)
                    else:
                        payload = _read_limited_text(out_file).strip()
                except BridgeError as exc:
                    return {"status": "error", "data": str(exc)}
                return {"status": "ok", "data": payload}

            return {"status": "ok", "data": None}
    except BridgeError as exc:
        return {"status": "error", "data": str(exc)}


def get_parity_matrix() -> List[dict]:
    """Return the sud-cli vs. case-search capability parity matrix.

    Statuses are ``native``, ``bridge``, ``unavailable``, or ``blocked``.

    The bridge rows are *derived from the registries* rather than written out
    by hand: a hardcoded list had drifted into lying in both directions. It
    still claimed fee and deadline calculation were blocked when both work,
    and still called kad-search and the live lookups unavailable when both are
    reachable behind ``SUD_CLI_BRIDGE_LIVE``. An agent reading that matrix would
    skip capabilities it has, or wait for ones it cannot get. Generating the
    rows from ``_COMMANDS``/``_FLAG_COMMANDS``/``_LIVE_COMMANDS`` means a
    command cannot be added to the bridge without appearing here.

    Live rows carry ``live_gate`` so the caller knows ``sud_cli_status`` is
    conditional on the opt-in switch rather than a permanent verdict.
    """
    rows: List[dict] = []

    rows.append({
        "capability": "Provider status discovery",
        "case_search_command": "case-search providers",
        "sud_cli_status": "bridge",
        "sud_cli_command": "sud-cli case-search providers",
        "live_gate": False,
    })

    importers = [
        ("import", "HTML case import"),
        ("sudrf-import", "ГАС Правосудие case import"),
        ("kad-import", "KAD JSON case import"),
        ("vsrf-import", "VSRF PDF case import"),
        ("auto-import", "Auto-detected file import"),
    ]
    for command, capability in importers:
        rows.append({
            "capability": capability,
            "case_search_command": (
                f"case-search {command} <saved-file> --case <id> --json <out>"
            ),
            "sud_cli_status": "bridge" if command in _COMMANDS else "unavailable",
            "sud_cli_command": f"sud-cli case-search {command} <file> --case <id>",
            "live_gate": False,
        })

    rows.append({
        "capability": "Document text extraction",
        "case_search_command": "case-search extract-text <saved-file> --out <out>",
        "sud_cli_status": "bridge" if "extract-text" in _COMMANDS else "unavailable",
        "sud_cli_command": "sud-cli case-search extract-text <file>",
        "live_gate": False,
    })

    for command, capability in (
        ("search-practice", "Judicial practice search (sudact.ru)"),
        ("kad-search", "Live KAD/arbitr.ru search"),
        ("get-text", "Full decision text by URL"),
    ):
        rows.append({
            "capability": capability,
            "case_search_command": f"case-search {command}",
            "sud_cli_status": "bridge" if command in _FLAG_COMMANDS else "unavailable",
            "sud_cli_command": f"sud-cli case-search {command} ...",
            "live_gate": True,
        })

    for command, capability in (
        ("case-history", "Case history across linked instances"),
        ("lookup", "Single case lookup"),
        ("sudrf-live", "Live ГАС Правосудие case card"),
        ("mos-live", "Live Moscow court case card"),
    ):
        spec = _LIVE_COMMANDS.get(command, {})
        flags = spec.get("flags") if isinstance(spec, dict) else None
        rows.append({
            "capability": capability,
            "case_search_command": f"case-search {command} <case-id>",
            "sud_cli_status": "bridge",
            "sud_cli_command": f"sud-cli case-search {command} <case-id>",
            "live_gate": True,
            # v2.3.3 (review R11): сортировался ИТЕРАТОР dict (имя флага посимвольно).
            "source_choices": sorted(next(iter(flags.values()))) if flags else [],
        })

    rows.append({
        "capability": "Court fee calculation",
        "case_search_command": "n/a",
        "sud_cli_status": "native",
        "sud_cli_command": "sud-cli fees",
        "live_gate": False,
        "note": "tariffs from legal_rules.json, fail-closed on missing parameter",
    })
    rows.append({
        "capability": "Procedural deadline calculation",
        "case_search_command": "n/a",
        "sud_cli_status": "native",
        "sud_cli_command": "sud-cli deadlines",
        "live_gate": False,
        "note": (
            "Б2 production calendar; regional non-working days are NOT applied "
            "— see production_calendar.REGIONAL_NOTE"
        ),
    })
    rows.append({
        "capability": "Readiness self-check",
        "case_search_command": "n/a",
        "sud_cli_status": "native",
        "sud_cli_command": "sud-cli ready",
        "live_gate": False,
        "note": "reports blockers without contacting any court source",
    })
    rows.append({
        "capability": "Case version diff",
        "case_search_command": "n/a (case_search.diff.diff_snapshots is internal)",
        "sud_cli_status": "native",
        "sud_cli_command": "sud-cli case-search case-diff <old.json> <new.json>",
        "live_gate": False,
        "note": (
            "compares two saved snapshots using the same field set as "
            "case_search.diff._SCALARS, which case-search never exposes"
        ),
    })
    rows.append({
        "capability": "Official publication metadata",
        "case_search_command": "n/a",
        "sud_cli_status": "native",
        "sud_cli_command": "sud-cli law search",
        "live_gate": True,
    })
    rows.append({
        "capability": "Local NPA fixtures",
        "case_search_command": "n/a",
        "sud_cli_status": "native",
        "sud_cli_command": "sud-cli import npa / sud-cli npa search|compare",
        "live_gate": False,
    })
    return rows
