"""Case numbers as Russian courts print them, and leak-free upstream diagnostics.

The regression these guard: the bridge required a *letter before digits*, so
``2а-2939/2025`` — the ГАС Правосудие form used by district and appeal courts —
was rejected by both case-number patterns. Every general-jurisdiction case was
unreachable. The patterns are now derived from the upstream parsers.
"""

import re

import pytest

from sud_cli import case_search_bridge as bridge
from sud_cli.case_search_bridge import _subprocess_failure


# Formats taken from case-search parsers and live court output.
REAL_CASE_NUMBERS = [
    "А40-11111/25",  # Арбитражный суд, 2-digit year
    "А40-11111/2025",  # Арбитражный суд, 4-digit year
    "2а-2939/2025",  # районный суд ГАС Правосудие: DIGIT first
    "33а-29651/2025",  # апелляция: digits then letter
    "02-3696/2024",  # ВС РФ: digits only
    "А56-25678/2025",
    "а40-11111/2025",  # lowercase Cyrillic
    "А41-69570/2025",
    "16АП-1234/2026",  # letters at the end of the prefix
    "А00-00001/20",  # 2-digit year
]

REJECTED = [
    "",
    "А40-11111/",  # no year
    "/2025",  # no prefix
    "А40-11111/2",  # 1-digit year
    "А40-11111/25 extra",
    "А40-11111/25\x00",
    "А40-11111/25\n",  # Python `$` would match before this newline
    "А40-1/25; rm -rf /",
    "../../etc/passwd",
    "А40/25",  # prefix with no court number
    # The prefix is capped at 4 characters: 5+ is not a court prefix, and the
    # vsrf parser's looser {1,6} must not leak into the bridge.
    "12345-12345/25",
    "А4012345/25",
    "А" * 40 + "-1/25",  # over-long prefix
    "А40-11111/2500000",  # over-long year
]


@pytest.mark.parametrize("case_id", REAL_CASE_NUMBERS)
def test_accepts_formats_courts_actually_print(case_id):
    """Both patterns must accept every real format, digit-first prefixes included."""
    assert bridge._LIVE_CASE_RE.match(case_id)
    assert bridge._COURT_REFERENCE_RE.match(case_id)
    bridge._validate_case_id(case_id)  # must not raise


@pytest.mark.parametrize("case_id", REJECTED)
def test_rejects_malformed_or_hostile(case_id):
    assert not bridge._LIVE_CASE_RE.match(case_id)
    assert not bridge._COURT_REFERENCE_RE.match(case_id)


def test_gas_digit_first_format_is_not_rejected():
    """The specific regression: digit-first was rejected by both patterns."""
    case_id = "2а-2939/2025"
    assert not re.match(r"^[A-Za-zА-Яа-яЁё]\d+-\d+/\d{4}$", case_id), (
        "guard pattern changed — this test no longer proves the regression"
    )
    assert bridge._COURT_REFERENCE_RE.match(case_id)


def test_trailing_newline_is_rejected():
    """`$` matches before a trailing newline; the anchor must be \\Z."""
    assert not bridge._COURT_REFERENCE_RE.match("А40-11111/25\n")
    assert not bridge._LIVE_CASE_RE.match("А40-11111/25\n")


def test_sudrf_import_is_a_known_command():
    """The one importer that works on general-jurisdiction cases was missing."""
    assert "sudrf-import" in bridge._COMMANDS
    spec = bridge._COMMANDS["sudrf-import"]
    assert spec["needs_case"] and spec["needs_out"] and spec["json_out"]


def test_diagnostics_name_the_reason():
    """A bare 'bridge subprocess failed' told the agent nothing actionable."""
    msg = _subprocess_failure(
        "kad-import", 3, "import file is not valid JSON: Expecting value", ""
    )
    assert "JSON" in msg
    assert "auto-import" in msg, "should point at the command that does work"

    mismatch = _subprocess_failure("sudrf-import", 3, "case identity mismatch", "")
    assert "номер дела не найден" in mismatch
    assert "fail-closed" in mismatch


def test_diagnostics_never_echo_upstream_text():
    """Upstream stderr can echo case content, so raw text must not surface."""
    stdout = (
        "status: error (malformed payload from 'kad-import': import file is not "
        "valid JSON: Expecting value: line 1 column 1 (char 0))\n"
        "snapshot: none fabricated (previous baseline preserved)"
    )
    stderr = "21:01:56 case_search.cli DEBUG command=kad-import"
    msg = _subprocess_failure("kad-import", 3, stdout, stderr)

    for fragment in (
        "not valid JSON",
        "Expecting value",
        "line 1 column 1",
        "DEBUG",
        "malformed payload",
        "snapshot: none fabricated",
    ):
        assert fragment not in msg, f"upstream text leaked: {fragment!r}"


def test_live_commands_do_not_claim_a_snapshot():
    """Live commands write no snapshot, so the wording would be false."""
    imp = _subprocess_failure("sudrf-import", 3, "", "", snapshot=True)
    live = _subprocess_failure("sudrf-live", 3, "", "", snapshot=False)
    assert "снимок не изменён" in imp
    assert "снимок" not in live

    imp_mismatch = _subprocess_failure(
        "sudrf-import", 3, "case identity mismatch", "", snapshot=True
    )
    live_mismatch = _subprocess_failure(
        "lookup", 3, "case identity mismatch", "", snapshot=False
    )
    assert "снимок" in imp_mismatch
    assert "снимок" not in live_mismatch


def test_unknown_exit_code_reports_the_code_without_inventing_a_cause():
    msg = _subprocess_failure("mos-live", 9, "x", "y")
    assert "с кодом 9" in msg
    assert "fail-closed" not in msg, "must not guess a reason it cannot see"


# --- parity matrix -------------------------------------------------------
#
# It used to be a hand-written list that had drifted into lying in both
# directions: fees and deadlines were advertised as "blocked" when both work,
# and kad-search and the live lookups as "unavailable" when both are
# reachable behind SUD_CLI_BRIDGE_LIVE. An agent reading that matrix would
# skip capabilities it has, or wait for ones it cannot get.

PARITY_STALE = {"Court fee calculation", "Procedural deadline calculation"}


def _parity_by_capability():
    return {r["capability"]: r for r in bridge.get_parity_matrix()}


def test_parity_does_not_advertise_working_features_as_blocked():
    rows = _parity_by_capability()
    for capability in PARITY_STALE:
        assert rows[capability]["sud_cli_status"] == "native", (
            f"{capability} works but the matrix says otherwise"
        )


def test_parity_covers_every_bridge_command():
    """A command cannot be added to the bridge and stay hidden from the matrix."""
    rows = bridge.get_parity_matrix()
    advertised = " ".join(r["case_search_command"] for r in rows)
    for command in (
        list(bridge._COMMANDS) + list(bridge._FLAG_COMMANDS) + list(bridge._LIVE_COMMANDS)
    ):
        if command == "providers":
            continue
        assert command in advertised, f"{command} is not in the parity matrix"


def test_parity_marks_live_commands_behind_the_gate():
    rows = bridge.get_parity_matrix()
    live = {c for c in bridge._LIVE_COMMANDS}
    for row in rows:
        parts = row["case_search_command"].split()
        if len(parts) > 1 and parts[1] in live:
            assert row["live_gate"] is True, row["capability"]


def test_parity_marks_network_flag_commands_behind_the_gate():
    """search-practice / kad-search / get-text reach external hosts.

    They are not in _LIVE_COMMANDS, but they still go through the live gate:
    marking them ``live_gate: False`` would tell an agent they work offline.
    """
    rows = bridge.get_parity_matrix()
    network_flag = {"search-practice", "kad-search", "get-text"}
    for row in rows:
        parts = row["case_search_command"].split()
        if len(parts) > 1 and parts[1] in network_flag:
            assert row["live_gate"] is True, row["capability"]


def test_parity_marks_local_importers_as_not_gated():
    """Offline importers must NOT claim a gate that does not apply to them."""
    rows = bridge.get_parity_matrix()
    offline = {"import", "sudrf-import", "kad-import", "vsrf-import", "auto-import",
               "extract-text", "providers"}
    for row in rows:
        parts = row["case_search_command"].split()
        if len(parts) > 1 and parts[1] in offline:
            assert row["live_gate"] is False, row["capability"]


def test_parity_does_not_advertise_forbidden_commands():
    """``watch`` is a substring of ``kad-search``, so match whole tokens."""
    for row in bridge.get_parity_matrix():
        parts = row["case_search_command"].split()
        for forbidden in bridge._FORBIDDEN_COMMANDS:
            assert forbidden not in parts, row["capability"]


def test_parity_lists_the_gas_importer():
    rows = _parity_by_capability()
    assert "ГАС Правосудие case import" in rows
    assert rows["ГАС Правосудие case import"]["sud_cli_status"] == "bridge"