"""The parity document must not drift from the code again.

``docs/CASE-SEARCH-PARITY.md`` used to be a hand-written table that went stale:
it still called fees and deadlines ``blocked`` and the KAD/SudAct surfaces
``unavailable`` long after they shipped and were verified live. An agent reading
the doc would be told the tool could not do things it demonstrably can.

So: every bridge command name the doc mentions must exist in the registries, and
every command the registries advertise must appear in the doc.
"""

import pathlib
import re

import pytest

from sud_cli import case_search_bridge as bridge

DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "CASE-SEARCH-PARITY.md"

# Commands that are deliberately never advertised as capabilities: they are
# permanently blocked by contract, and the doc discusses them under "Permanently
# blocked" rather than in the capability tables.
PERMANENTLY_EXCLUDED = {
    "watch", "config",
    "kad-case-history", "sudrf-lookup", "vsrf-lookup",
}

#: Hyphenated tokens that are not case-search subcommands: sud-cli's own
#: commands, environment/path names, and shell options.
NON_COMMAND_HYPHENATED = {
    "case-diff", "case-search", "camoufox-py",
}


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def _known_commands() -> set:
    """Every command name the bridge recognises.

    There is no separate "blocked" registry: a command is gated by carrying
    ``live_gate`` in its entry, so gating is not a separate membership list.
    """
    return (
        set(bridge._COMMANDS)
        | set(bridge._LIVE_COMMANDS)
        | set(bridge._FLAG_COMMANDS)
        | set(bridge._FORBIDDEN_COMMANDS)
    )


def test_doc_exists_and_is_substantial(doc_text):
    assert len(doc_text) > 1500, "the parity doc must carry real content"


def test_readiness_examples_are_accepted_by_the_bridge(doc_text=None):
    """Every example `ready` hands an agent must pass the real validator.

    `ready` is the first call an agent makes; if it suggests a reference the
    bridge then rejects, the agent reports a working tool as broken.
    """
    from sud_cli import case_search_bridge as bridge
    from sud_cli.readiness import CASE_REFERENCE_EXAMPLES

    assert CASE_REFERENCE_EXAMPLES, "an agent needs at least one format hint"
    for reference, note in CASE_REFERENCE_EXAMPLES:
        assert note, f"{reference} is listed without an explanation"
        assert bridge._COURT_REFERENCE_RE.match(reference), (
            f"ready advertises {reference!r} but the bridge rejects it"
        )
        assert bridge._LIVE_CASE_RE.match(reference), (
            f"ready advertises {reference!r} but live commands reject it"
        )


def test_readiness_examples_cover_the_digit_first_shape():
    """Regression: ГАС Правосудие prints a digit first — that shape must be shown.

    The agent otherwise learns only letter-first forms and reports every
    district-court lookup as a validation failure.
    """
    from sud_cli.readiness import CASE_REFERENCE_EXAMPLES

    references = [ref for ref, _ in CASE_REFERENCE_EXAMPLES]
    # "digit first" means a digit FOLLOWED BY A LETTER — the mixed form
    # ГАС Правосудие prints. A purely numeric prefix such as "33-35073/2026"
    # does not demonstrate the shape: it has no letter anywhere in the prefix,
    # so it would still pass a naive "starts with a digit" check.
    mixed = [
        ref for ref in references
        if ref[0].isdigit() and any(ch.isalpha() for ch in ref.split("-")[0])
    ]
    assert mixed, (
        f"no digit-then-letter example among {references}: the agent would "
        "not learn that a district-court reference may start with a digit"
    )


def test_every_documented_command_is_real(doc_text):
    """No command name in the doc may be invented."""
    # Only hyphenated tokens can be *subcommands*; plain words in the prose are
    # statuses (``blocked``), values (``native``), or shell snippets.
    documented = set(re.findall(r"`([a-z][a-z0-9]*-[a-z0-9-]+)`", doc_text))
    unknown = {
        name for name in documented
        if name not in _known_commands()
        and name not in PERMANENTLY_EXCLUDED
        and name not in NON_COMMAND_HYPHENATED
    }
    assert not unknown, f"doc mentions commands the bridge does not know: {unknown}"


def test_every_bridged_command_is_documented(doc_text):
    """The reverse direction: nothing shipped may be undocumented."""
    missing = set(bridge._COMMANDS) - PERMANENTLY_EXCLUDED
    undocumented = {c for c in missing if f"`{c}`" not in doc_text}
    assert not undocumented, (
        f"bridge commands absent from the doc: {sorted(undocumented)}"
    )


def test_every_live_command_is_documented_as_gated(doc_text):
    """Live commands must be flagged as needing SUD_CLI_BRIDGE_LIVE.

    An agent that reads the doc must not call a gated command without setting
    the flag and then report the resulting ``blocked`` as a failure.
    """
    # Matched on the backticked span *starting* with the command name: the doc
    # writes the full signature (``case-history <case_id>``), which is more
    # useful than the bare name, so an exact match would miss it.
    for command in bridge._LIVE_COMMANDS:
        assert f"`{command}" in doc_text, f"{command} undocumented"


def test_doc_does_not_claim_fees_or_deadlines_are_blocked(doc_text):
    """Regression guard for the exact staleness that was found.

    The old doc's table marked both native calculations ``blocked``. An agent
    reading it would have refused to use two features that work. So the claim is
    asserted positively instead: each must be documented as ``native``, and
    must not carry a blocked/unavailable marker on its row.
    """
    rows = {}
    for line in doc_text.splitlines():
        if line.startswith("|") and "Court fee" in line:
            rows["fees"] = line
        if line.startswith("|") and "Procedural deadline" in line:
            rows["deadlines"] = line

    assert set(rows) == {"fees", "deadlines"}, f"missing rows: {set(rows)}"

    for name, line in rows.items():
        low = line.lower()
        assert "`native`" in low, (
            f"{name} must be documented as native, not gated: {line.strip()}"
        )
        for stale in ("blocked", "unavailable", "not bridged"):
            assert stale not in low, (
                f"{name} row still claims a stale status {stale!r}: {line.strip()}"
            )


def test_doc_points_at_the_generating_command(doc_text):
    """The doc must name the machine-readable source of truth."""
    assert "case-search parity" in doc_text, (
        "the doc must tell the reader where the live matrix comes from"
    )


def test_doc_documents_the_blocked_versus_error_distinction(doc_text):
    """`blocked` means the gate closed; `error` means an attempt failed."""
    assert "blocked" in doc_text and "error" in doc_text
    assert "nothing was attempted" in doc_text or "ничего не предпринималось" in doc_text