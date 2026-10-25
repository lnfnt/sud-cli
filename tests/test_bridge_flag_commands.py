"""Tests for the Phase 8 flag-driven bridge commands.

Covers ``search-practice``, ``kad-search`` and ``get-text``: the per-flag
allowlist, the value validators, the live gate, argv construction, and the
isolation/error-sanitization contract that the commands inherit from the
offline path.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sud_cli.case_search_bridge import (
    BridgeError,
    _build_flag_argv,
    _COMMAND_TIMEOUTS,
    _FLAG_COMMANDS,
    _forwarded_env_value,
    _forwarded_camofox_url,
    run_bridge_command,
    LIVE_ENV_VAR,
    TIMEOUT_SECONDS,
)


# --------------------------------------------------------------------------
# argv construction: happy paths
# --------------------------------------------------------------------------


def test_search_practice_query_is_positional_first():
    argv = _build_flag_argv("search-practice", {"query": "взыскание неустойки"})
    assert argv[0] == "взыскание неустойки"
    assert "--section" not in argv


def test_search_practice_renders_every_allowlisted_flag():
    argv = _build_flag_argv(
        "search-practice",
        {
            "query": "неустойка",
            "section": "arbitral",
            "page": 2,
            "limit": 5,
            "via_camofox": True,
        },
    )
    assert argv[0] == "неустойка"
    assert "--section" in argv and "arbitral" in argv
    assert "--page" in argv and "2" in argv
    assert "--limit" in argv and "5" in argv
    assert "--via-camofox" in argv


def test_search_practice_false_bool_is_omitted():
    argv = _build_flag_argv("search-practice", {"query": "неустойка", "via_camofox": False})
    assert "--via-camofox" not in argv


def test_search_practice_none_flags_are_omitted():
    argv = _build_flag_argv(
        "search-practice", {"query": "неустойка", "section": None, "page": None, "limit": None}
    )
    assert argv == ["неустойка"]


def test_kad_search_inn_is_flag_valued():
    argv = _build_flag_argv("kad-search", {"inn": "7707083893"})
    assert argv == ["--inn", "7707083893"]


def test_kad_search_accepts_several_participant_criteria():
    argv = _build_flag_argv(
        "kad-search", {"name": "ООО Ромашка", "ogrn": "1027700132195", "page": 3}
    )
    assert "--name" in argv and "ООО Ромашка" in argv
    assert "--ogrn" in argv and "1027700132195" in argv
    assert "--page" in argv and "3" in argv


def test_kad_search_has_no_positional_argument():
    # Upstream takes no positional for kad-search: the bridge must not invent
    # one. Every emitted token must belong to an allowlisted flag.
    argv = _build_flag_argv("kad-search", {"inn": "7707083893"})
    positionals = [a for a in argv if not a.startswith("--")]
    # Only the flag *value* is a bare token; it directly follows --inn.
    assert positionals == ["7707083893"]
    assert argv[argv.index("--inn") + 1] == "7707083893"


def test_positional_key_from_another_command_is_rejected():
    """A query must not be silently dropped by kad-search, which takes none."""
    with pytest.raises(BridgeError):
        _build_flag_argv("kad-search", {"inn": "7707083893", "query": "неустойка"})
    with pytest.raises(BridgeError):
        _build_flag_argv("search-practice", {"query": "ок", "url_or_file": "https://sudact.ru/x/"})
    with pytest.raises(BridgeError):
        _build_flag_argv("get-text", {"url_or_file": "https://sudact.ru/x/", "query": "ок"})


def test_get_text_url_is_positional_and_bools_rendered():
    argv = _build_flag_argv(
        "get-text", {"url_or_file": "https://sudact.ru/arbitral/doc/xyz/", "blocks": True}
    )
    assert argv[0] == "https://sudact.ru/arbitral/doc/xyz/"
    assert "--blocks" in argv
    assert "--no-cache" not in argv


def test_get_text_no_cache_spelling_is_upstream_flag():
    argv = _build_flag_argv(
        "get-text", {"url_or_file": "https://sudact.ru/arbitral/doc/xyz/", "no_cache": True}
    )
    assert "--no-cache" in argv
    assert "--no_cache" not in argv


def test_flag_argv_never_contains_forbidden_destination_flags():
    """--fixtures / --runtime-url / --now must not be reachable from options."""
    for command in _FLAG_COMMANDS:
        for option in ("fixtures", "runtime_url", "now", "store", "alerts"):
            with pytest.raises(BridgeError):
                _build_flag_argv(command, {"query": "ок", "url_or_file": "https://sudact.ru/x/"} | {option: "/etc"})


# --------------------------------------------------------------------------
# value validators: every one of these must be rejected
# --------------------------------------------------------------------------


@pytest.mark.parametrize("inn", ["abc", "7707083893; rm -rf /", "770708389", "77070838931", "", "77 7083893"])
def test_inn_must_be_10_or_12_digits(inn):
    with pytest.raises(BridgeError):
        _build_flag_argv("kad-search", {"inn": inn})


@pytest.mark.parametrize("ogrn", ["abc", "102770013219", "10277001321951", "1027700132195; ls"])
def test_ogrn_must_be_13_or_15_digits(ogrn):
    with pytest.raises(BridgeError):
        _build_flag_argv("kad-search", {"ogrn": ogrn})


def test_inn_of_exactly_10_and_12_digits_is_accepted():
    assert _build_flag_argv("kad-search", {"inn": "1234567890"})
    assert _build_flag_argv("kad-search", {"inn": "123456789012"})


@pytest.mark.parametrize(
    "query",
    [
        "x",  # too short
        "тест; rm -rf /",  # shell metacharacter
        "тест && whoami",
        "тест | cat /etc/passwd",
        "тест\n--store /etc",
        "$(whoami)",
        "`id`",
        "--store /etc",
        "тест" * 200,  # over the length cap
    ],
)
def test_search_query_rejects_injection_attempts(query):
    with pytest.raises(BridgeError):
        _build_flag_argv("search-practice", {"query": query})


def test_search_query_accepts_normal_cyrillic_phrase():
    argv = _build_flag_argv("search-practice", {"query": "взыскание неустойки по договору"})
    assert argv[0] == "взыскание неустойки по договору"


def test_party_name_rejects_injection_but_accepts_legal_form():
    with pytest.raises(BridgeError):
        _build_flag_argv("kad-search", {"name": "ООО Ромашка; drop table"})
    assert _build_flag_argv("kad-search", {"name": "ООО «Ромашка»"})


@pytest.mark.parametrize("section", ["arbitral", "regular", "magistrate", "vsrf"])
def test_section_accepts_upstream_enum(section):
    assert _build_flag_argv("search-practice", {"query": "ок", "section": section})


@pytest.mark.parametrize("section", ["../../etc", "arbitral; ls", "", "ARBITRAL "])
def test_section_rejects_non_enum_values(section):
    with pytest.raises(BridgeError):
        _build_flag_argv("search-practice", {"query": "ок", "section": section})


@pytest.mark.parametrize("page", ["0", "-1", "1001", "abc", "1e3", ""])
def test_page_bounds_are_enforced(page):
    with pytest.raises(BridgeError):
        _build_flag_argv("search-practice", {"query": "ок", "page": page})


@pytest.mark.parametrize("limit", ["0", "31", "-5", "abc", ""])
def test_limit_bounds_mirror_upstream_max_of_30(limit):
    with pytest.raises(BridgeError):
        _build_flag_argv("search-practice", {"query": "ок", "limit": limit})


def test_limit_of_exactly_30_is_accepted():
    assert _build_flag_argv("search-practice", {"query": "ок", "limit": "30"})


# --------------------------------------------------------------------------
# get-text host restriction: the SSRF gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://127.0.0.1:9377/",
        "http://localhost/",
        "file:///etc/passwd",
        "https://evil.example.com/sudact",
        "https://sudact.ru.evil.com/x",  # suffix confusion
        "https://notsudact.ru/x",
        "https://user:password@sudact.ru/x",  # embedded credentials
        "https://sudact.ru@evil.com/x",  # userinfo confusion
        "ftp://sudact.ru/x",
        "",
        "https://sudact.ru/x\nhttps://evil.com",
    ],
)
def test_get_text_rejects_everything_outside_sudact(url):
    with pytest.raises(BridgeError):
        _build_flag_argv("get-text", {"url_or_file": url})


@pytest.mark.parametrize(
    "url",
    [
        "https://sudact.ru/arbitral/doc/abc/",
        "http://sudact.ru/regular/doc/abc/",
        "https://www.sudact.ru/arbitral/doc/abc/",  # subdomain
    ],
)
def test_get_text_accepts_sudact_hosts(url):
    assert _build_flag_argv("get-text", {"url_or_file": url})[0] == url


def test_get_text_rejects_url_that_merely_contains_sudact():
    with pytest.raises(BridgeError):
        _build_flag_argv("get-text", {"url_or_file": "https://evil.com/?next=https://sudact.ru/x"})


# --------------------------------------------------------------------------
# live gate: no subprocess without SUD_CLI_BRIDGE_LIVE
# --------------------------------------------------------------------------


@pytest.mark.parametrize("command", sorted(_FLAG_COMMANDS))
def test_flag_commands_are_blocked_without_the_live_gate(command, tmp_path):
    result = run_bridge_command(
        command,
        state_dir=tmp_path,
        binary_path="/nonexistent/case-search",
        env={},
        options={"query": "неустойка", "url_or_file": "https://sudact.ru/x/"},
    )
    assert result["status"] == "blocked"
    assert LIVE_ENV_VAR in result["data"]


@pytest.mark.parametrize("command", sorted(_FLAG_COMMANDS))
def test_flag_commands_reach_validation_when_gate_is_open(command, tmp_path):
    """With the gate open, invalid input is rejected before any spawn."""
    result = run_bridge_command(
        command,
        state_dir=tmp_path,
        binary_path="/nonexistent/case-search",
        env={LIVE_ENV_VAR: "1"},
        options={"query": "x; rm -rf /", "url_or_file": "http://169.254.169.254/"},
    )
    assert result["status"] == "error"
    assert "invalid" in result["data"] or "unsupported" in result["data"]


def test_unknown_option_is_refused_not_forwarded(tmp_path):
    result = run_bridge_command(
        "search-practice",
        state_dir=tmp_path,
        binary_path="/nonexistent/case-search",
        env={LIVE_ENV_VAR: "1"},
        options={"query": "ок", "runtime_url": "http://169.254.169.254"},
    )
    assert result["status"] == "error"
    assert result["data"] == "unsupported option"


# --------------------------------------------------------------------------
# end-to-end execution contract with a fake binary
# --------------------------------------------------------------------------


def _fake_binary(tmp_path: Path, body: str, exit_code: int = 0, stderr_text: str = ""):
    """Create a fake case-search that records argv and writes ``body``."""
    argv_log = tmp_path / "argv.log"
    script = tmp_path / "fake_cw.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"argv = {argv_log.as_posix()!r}\n"
        "with open(argv, 'w') as fh:\n"
        "    json.dump(sys.argv[1:], fh)\n"
        "out = None\n"
        "args = sys.argv[1:]\n"
        "for i, a in enumerate(args):\n"
        "    if a in ('--json', '--out') and i + 1 < len(args):\n"
        "        out = args[i + 1]\n"
        "if out:\n"
        "    with open(out, 'w') as fh:\n"
        f"        fh.write({body!r})\n"
        f"sys.stderr.write({stderr_text!r})\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, argv_log


def _run_with_fake(tmp_path, command, options, body, exit_code=0, stderr_text=""):
    script, argv_log = _fake_binary(tmp_path, body, exit_code, stderr_text)
    env = dict(os.environ)
    env[LIVE_ENV_VAR] = "1"
    # ``binary_path`` is argv[0] with shell=False, so the fake must be directly
    # executable (shebang) rather than "python script.py" with an embedded space.
    result = run_bridge_command(
        command,
        state_dir=tmp_path / "state",
        binary_path=str(script),
        env=env,
        options=options,
    )
    argv = json.loads(argv_log.read_text()) if argv_log.exists() else None
    return result, argv


def test_search_practice_executes_and_returns_json(tmp_path):
    payload = json.dumps({"query": "неустойка", "count": 2, "results": [{"title": "Р��шение"}]})
    result, argv = _run_with_fake(tmp_path, "search-practice", {"query": "неустойка", "limit": 5}, payload)
    assert result["status"] == "ok"
    assert result["data"]["count"] == 2
    assert result["data"]["bridge"]["live"] is True
    assert argv[0] == "search-practice"
    assert argv[1] == "неустойка"
    assert "--limit" in argv and "5" in argv


def test_get_text_executes_and_returns_text(tmp_path):
    result, argv = _run_with_fake(
        tmp_path, "get-text", {"url_or_file": "https://sudact.ru/arbitral/doc/x/"}, "  ТЕКСТ РЕШЕНИЯ  "
    )
    assert result["status"] == "ok"
    assert result["data"] == "ТЕКСТ РЕШЕНИЯ"
    assert argv[0] == "get-text"
    assert argv[1] == "https://sudact.ru/arbitral/doc/x/"


def test_kad_search_executes_and_flags_are_in_argv(tmp_path):
    payload = json.dumps({"status": "ok", "extensions": {"count": 25, "hits": []}})
    result, argv = _run_with_fake(tmp_path, "kad-search", {"inn": "7707083893"}, payload)
    assert result["status"] == "ok"
    assert argv[0] == "kad-search"
    assert "--inn" in argv and "7707083893" in argv
    # The output path is injected by the bridge, never by the caller.
    assert "--json" in argv
    assert not any(a == "--fixtures" for a in argv)


def test_upstream_stderr_is_never_echoed(tmp_path):
    secret = "CASE А40-99999/2026 SECRET PARTY NAME"
    result, _ = _run_with_fake(
        tmp_path, "search-practice", {"query": "ок"}, "{}", exit_code=1, stderr_text=secret
    )
    assert result["status"] == "error"
    assert secret not in json.dumps(result, ensure_ascii=False)
    # A fixed constant, not a rephrasing of upstream stderr: the allowlist in
    # _subprocess_failure decides, so case content can never reach the caller.
    assert result["data"] == "search-practice: case-search завершился с кодом 1"


def test_output_paths_are_inside_the_isolated_workspace(tmp_path):
    payload = json.dumps({"count": 0})
    result, argv = _run_with_fake(tmp_path, "search-practice", {"query": "ок"}, payload)
    state_root = (tmp_path / "state").resolve()
    for token in argv:
        if token.endswith((".json", ".txt")):
            assert Path(token).resolve().is_relative_to(state_root)


def test_store_and_alerts_are_pinned_to_isolated_temp(tmp_path):
    payload = json.dumps({"count": 0})
    _result, argv = _run_with_fake(tmp_path, "search-practice", {"query": "ок"}, payload)
    assert "--store" in argv and "--alerts" in argv
    store = argv[argv.index("--store") + 1]
    alerts = argv[argv.index("--alerts") + 1]
    assert "/bridge-temp/" in store and "/bridge-temp/" in alerts
    assert str(tmp_path) in store


def test_isolated_workspace_is_cleaned_up_after_run(tmp_path):
    payload = json.dumps({"count": 0})
    _result, _argv = _run_with_fake(tmp_path, "search-practice", {"query": "ок"}, payload)
    leftovers = list((tmp_path / "state" / "bridge-temp").glob("*"))
    assert leftovers == []


def test_proxy_and_secret_env_vars_are_not_forwarded(tmp_path):
    payload = json.dumps({"count": 0})
    script, _ = _fake_binary(tmp_path, payload)
    full_env = dict(os.environ)
    full_env.update(
        {
            LIVE_ENV_VAR: "1",
            "HTTPS_PROXY": "http://attacker:8080",
            "HTTP_PROXY": "http://attacker:8080",
            "SUD_SECRET": "top-secret",
        }
    )
    result = run_bridge_command(
        "search-practice",
        state_dir=tmp_path / "state",
        binary_path=str(script),
        env=full_env,
        options={"query": "ок"},
    )
    assert result["status"] == "ok"


def test_missing_binary_is_reported_as_unavailable(tmp_path):
    env = dict(os.environ)
    env[LIVE_ENV_VAR] = "1"
    result = run_bridge_command(
        "search-practice",
        state_dir=tmp_path / "state",
        binary_path="/nonexistent/definitely-not-here",
        env=env,
        options={"query": "ок"},
    )
    assert result["status"] == "unavailable"


#: Probe script template for environment-spying tests. case-search is
#: required to write the path given by ``--json``/``--out``; a probe that ignores
#: it makes the bridge report a missing output file, which is a failure of the
#: probe rather than of the bridge.
_PROBE_TMPL = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
out = None
for i, a in enumerate(args):
    if a in ('--json', '--out') and i + 1 < len(args):
        out = args[i + 1]
with open(__PROBE_PATH__, 'w') as fh:
    json.dump(__EXPR__, fh)
if out:
    with open(out, 'w') as fh:
        fh.write('{}')
sys.exit(0)
"""

def _probe(probe_path, expression):
    """Render a probe script that records an environment fact and exits 0."""
    return (
        _PROBE_TMPL
        .replace("__PROBE_PATH__", repr(probe_path))
        .replace("__EXPR__", expression)
    )


# --------------------------------------------------------------------------
# XDG_CACHE_HOME forwarding (Camoufox payload discovery)
# --------------------------------------------------------------------------


def test_xdg_cache_home_is_forwarded_when_it_is_the_camoufox_payload(tmp_path):
    cache = tmp_path / "camoufox-py"
    cache.mkdir()
    assert _forwarded_env_value({"XDG_CACHE_HOME": str(cache)}) == str(cache)


def test_xdg_cache_home_is_dropped_for_a_non_camoufox_directory(tmp_path):
    """Forwarding an arbitrary path would steer where the payload is read from."""
    other = tmp_path / "somewhere-else"
    other.mkdir()
    assert _forwarded_env_value({"XDG_CACHE_HOME": str(other)}) is None


def test_xdg_cache_home_is_dropped_when_the_directory_does_not_exist(tmp_path):
    assert _forwarded_env_value({"XDG_CACHE_HOME": str(tmp_path / "nope")}) is None


def test_xdg_cache_home_falls_back_to_the_real_home_payload():
    """XDG_CACHE_HOME is unset on this host, so the payload must still be found.

    Dropping the variable left the upstream binary searching the isolated HOME
    and failing with "official/stable is not installed", which is why every live
    search silently stopped working. The fallback is derived from the real HOME,
    so an empty or absent variable resolves to ~/.cache/camoufox-py.
    """
    resolved = _forwarded_env_value({})
    assert resolved == str(Path.home() / ".cache" / "camoufox-py")

    resolved_empty = _forwarded_env_value({"XDG_CACHE_HOME": ""})
    assert resolved_empty == str(Path.home() / ".cache" / "camoufox-py")


def test_xdg_cache_home_fallback_follows_an_explicit_home(tmp_path):
    """An explicit HOME is honoured, but the payload must still be camoufox-py."""
    assert _forwarded_env_value({"HOME": str(tmp_path)}) is None


def test_xdg_cache_home_rejects_a_caller_supplied_non_payload_path(tmp_path):
    """The safety rule survives the fallback: a bad explicit value is still dropped."""
    other = tmp_path / "somewhere-else"
    other.mkdir()
    assert _forwarded_env_value({"XDG_CACHE_HOME": str(other), "HOME": str(tmp_path)}) is None


def test_node_layout_cache_is_not_accepted(tmp_path):
    """camoufox and camoufox-py are different payloads sharing one name space."""
    node_layout = tmp_path / "camoufox"
    node_layout.mkdir()
    assert _forwarded_env_value({"XDG_CACHE_HOME": str(node_layout)}) is None


def test_xdg_cache_home_reaches_the_subprocess(tmp_path):
    """End-to-end: a valid camoufox cache is visible to the spawned command."""
    import sud_cli.case_search_bridge as bridge_mod

    cache = tmp_path / "camoufox-py"
    cache.mkdir()
    seen = tmp_path / "env.json"
    script = tmp_path / "envprobe.py"
    script.write_text(
        _probe(str(seen), "os.environ.get('XDG_CACHE_HOME')"),
        encoding="utf-8",
    )
    script.chmod(0o755)
    env = dict(os.environ)
    env[LIVE_ENV_VAR] = "1"
    env["XDG_CACHE_HOME"] = str(cache)
    run_bridge_command(
        "search-practice",
        state_dir=tmp_path / "state",
        binary_path=str(script),
        env=env,
        options={"query": "ок"},
    )
    assert json.loads(seen.read_text()) == str(cache)


def test_xdg_fallback_reaches_the_subprocess_without_an_explicit_value(tmp_path):
    """End-to-end for the fallback: no XDG_CACHE_HOME set, payload still found.

    This is the case that actually broke live search — the subprocess ran with
    the isolated HOME, looked for its browser under it, and failed with
    "official/stable is not installed". A caller-supplied value alone was
    already covered, so without this test the whole fallback could be deleted.
    """
    seen = tmp_path / "env.json"
    script = tmp_path / "envprobe.py"
    script.write_text(_probe(str(seen), "os.environ.get('XDG_CACHE_HOME')"), encoding="utf-8")
    script.chmod(0o755)
    env = dict(os.environ)
    env[LIVE_ENV_VAR] = "1"
    env.pop("XDG_CACHE_HOME", None)
    run_bridge_command(
        "search-practice",
        state_dir=tmp_path / "state",
        binary_path=str(script),
        env=env,
        options={"query": "ок"},
    )
    assert json.loads(seen.read_text()) == str(Path.home() / ".cache" / "camoufox-py")


def test_search_practice_defaults_to_camofox(monkeypatch):
    """sudact.ru serves a "Поиск не доступен" stub to plain HTTP clients.

    Without the browser the command fails with captcha_or_waf, so the flag
    defaults on. Verified at the parser boundary so no network is involved.
    """
    import sud_cli.cli as cli_mod

    captured = {}

    def fake_run(args, command, options, label):
        captured["command"] = command
        captured["options"] = options
        return 0

    monkeypatch.setattr(cli_mod, "_run_flag_command_cli", fake_run)
    cli_mod.main(["--json", "case-search", "search-practice", "договор"])

    assert captured["command"] == "search-practice"
    assert captured["options"]["via_camofox"] is True


def test_no_camofox_opts_out(monkeypatch):
    import sud_cli.cli as cli_mod

    captured = {}

    def fake_run(args, command, options, label):
        captured["options"] = options
        return 0

    monkeypatch.setattr(cli_mod, "_run_flag_command_cli", fake_run)
    cli_mod.main(["--json", "case-search", "search-practice", "договор", "--no-camofox"])

    assert captured["options"]["via_camofox"] is False


def test_xdg_fallback_reaches_live_command_subprocess(tmp_path):
    """The positional live-command path must forward the fallback too.

    The flag path and the live path each have their own copy of the env
    assembly; patching only one leaves the other silently broken, so both are
    covered here.
    """
    import sud_cli.case_search_bridge as bridge_mod

    seen = tmp_path / "env.json"
    script = tmp_path / "envprobe.py"
    script.write_text(
        _probe(str(seen), "os.environ.get('XDG_CACHE_HOME')"),
        encoding="utf-8",
    )
    script.chmod(0o755)
    env = dict(os.environ)
    env[LIVE_ENV_VAR] = "1"
    env.pop("XDG_CACHE_HOME", None)
    bridge_mod.run_bridge_command(
        "lookup",
        state_dir=tmp_path / "state",
        case_id="А40-12345/2026",
        binary_path=str(script),
        env=env,
    )
    assert json.loads(seen.read_text()) == str(Path.home() / ".cache" / "camoufox-py")


def test_rejected_xdg_value_is_not_reaching_the_subprocess(tmp_path):
    import sud_cli.case_search_bridge as bridge_mod

    other = tmp_path / "not-camoufox"
    other.mkdir()
    seen = tmp_path / "env2.json"
    script = tmp_path / "envprobe2.py"
    script.write_text(
        _probe(str(seen), "os.environ.get('XDG_CACHE_HOME')"),
        encoding="utf-8",
    )
    script.chmod(0o755)
    env = dict(os.environ)
    env[LIVE_ENV_VAR] = "1"
    env["XDG_CACHE_HOME"] = str(other)
    run_bridge_command(
        "search-practice",
        state_dir=tmp_path / "state",
        binary_path=str(script),
        env=env,
        options={"query": "ок"},
    )
    assert json.loads(seen.read_text()) is None


# --------------------------------------------------------------------------
# per-command timeout
# --------------------------------------------------------------------------


def test_kad_search_has_a_longer_timeout_than_the_global_cap():
    """kad.arbitr.ru needs minutes; the global cap would kill it every time."""
    assert _COMMAND_TIMEOUTS["kad-search"] > TIMEOUT_SECONDS


def test_fast_commands_keep_the_global_timeout():
    assert "search-practice" not in _COMMAND_TIMEOUTS
    assert "get-text" not in _COMMAND_TIMEOUTS


def test_kad_search_timeout_override_is_actually_passed_to_exec(tmp_path):
    """The override must reach _exec, not just exist in a dict."""
    import sud_cli.case_search_bridge as bridge_mod

    observed = {}
    original_exec = bridge_mod._exec

    def spy(cmd, env, cwd, out_file=None, timeout_seconds=None):
        observed["command"] = cmd[1] if len(cmd) > 1 else None
        observed["timeout"] = timeout_seconds
        return 1, "", ""

    bridge_mod._exec = spy
    try:
        env = dict(os.environ)
        env[LIVE_ENV_VAR] = "1"
        run_bridge_command(
            "kad-search",
            state_dir=tmp_path / "state",
            binary_path="/fake/case-search",
            env=env,
            options={"inn": "7707083893"},
        )
    finally:
        bridge_mod._exec = original_exec

    assert observed["command"] == "kad-search"
    assert observed["timeout"] == _COMMAND_TIMEOUTS["kad-search"]


def test_search_practice_uses_the_global_timeout(tmp_path):
    import sud_cli.case_search_bridge as bridge_mod

    observed = {}
    original_exec = bridge_mod._exec

    def spy(cmd, env, cwd, out_file=None, timeout_seconds=None):
        observed["command"] = cmd[1] if len(cmd) > 1 else None
        observed["timeout"] = timeout_seconds
        return 1, "", ""

    bridge_mod._exec = spy
    try:
        env = dict(os.environ)
        env[LIVE_ENV_VAR] = "1"
        run_bridge_command(
            "search-practice",
            state_dir=tmp_path / "state",
            binary_path="/fake/case-search",
            env=env,
            options={"query": "ок"},
        )
    finally:
        bridge_mod._exec = original_exec

    assert observed["timeout"] == TIMEOUT_SECONDS


# --------------------------------------------------------------------------
# license staging
# --------------------------------------------------------------------------


def test_license_file_is_copied_into_the_isolated_home(tmp_path):
    """case-search resolves its license under $HOME, which the bridge resets."""
    import sud_cli.case_search_bridge as bridge_mod

    seen_home = tmp_path / "home.json"
    script = tmp_path / "licprobe.py"
    script.write_text(
        _probe(
            str(seen_home),
            "(lambda lic: {'exists': os.path.isfile(lic), "
            "'mode': oct(os.stat(lic).st_mode & 0o777) if os.path.isfile(lic) else None})"
            "(os.path.join(os.environ['HOME'], '.config', 'case-search', 'config.json'))",
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    real_home = tmp_path / "realhome"
    (real_home / ".config" / "case-search").mkdir(parents=True)
    (real_home / ".config" / "case-search" / "config.json").write_text('{"license_key": "secret"}')

    env = dict(os.environ)
    env[LIVE_ENV_VAR] = "1"
    env["HOME"] = str(real_home)
    env["CSE_LICENSE_FILE"] = str(real_home / ".config" / "case-search" / "config.json")

    run_bridge_command(
        "search-practice",
        state_dir=tmp_path / "state",
        binary_path=str(script),
        env=env,
        options={"query": "ок"},
    )
    data = json.loads(seen_home.read_text())
    assert data["exists"] is True
    assert data["mode"] == "0o600"


def test_license_is_never_forwarded_as_an_environment_variable(tmp_path):
    """A key in the environment is readable via /proc/<pid>/environ."""
    import sud_cli.case_search_bridge as bridge_mod

    seen = tmp_path / "envkeys.json"
    script = tmp_path / "keyprobe.py"
    script.write_text(
        _probe(
            str(seen),
            "{k: v for k, v in os.environ.items() if 'CSE_' in k or 'LICEN' in k.upper()}",
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    real_home = tmp_path / "realhome2"
    (real_home / ".config" / "case-search").mkdir(parents=True)
    (real_home / ".config" / "case-search" / "config.json").write_text('{"license_key": "supersecret"}')

    env = dict(os.environ)
    # Hermetic: the assertion below is "no secret was forwarded", so any
    # CSE_* var inherited from the developer's shell must not widen it.
    for name in list(env):
        if name.startswith("CSE_"):
            del env[name]
    env[LIVE_ENV_VAR] = "1"
    env["HOME"] = str(real_home)
    env["CSE_LICENSE_FILE"] = str(real_home / ".config" / "case-search" / "config.json")
    env["CSE_LICENSE_KEY"] = "supersecret"

    run_bridge_command(
        "search-practice",
        state_dir=tmp_path / "state",
        binary_path=str(script),
        env=env,
        options={"query": "ок"},
    )
    # CSE_CAMOFOX_URL is a deliberately forwarded, non-secret runtime switch;
    # nothing license-bearing may reach the child.
    forwarded = json.loads(seen.read_text())
    assert set(forwarded) <= {"CSE_CAMOFOX_URL"}
    assert "CSE_LICENSE_KEY" not in forwarded
    assert "CSE_LICENSE_FILE" not in forwarded


def test_missing_license_file_does_not_crash_the_bridge(tmp_path):
    import sud_cli.case_search_bridge as bridge_mod

    home_dir = tmp_path / "emptyhome"
    home_dir.mkdir()
    env = {"HOME": str(home_dir), "CSE_LICENSE_FILE": str(tmp_path / "does-not-exist.json")}
    bridge_mod._stage_license(home_dir, env)  # must not raise


# --------------------------------------------------------------------------
# CSE_CAMOFOX_URL forwarding: loopback-only (SSRF boundary)
#
# The embedded browser cannot initialise inside the bridge's child context
# ("Playwright Sync API inside the asyncio loop"), which breaks every
# JS-capable command. Routing to the loopback camofox service fixes it, so the
# variable has to survive the environment scrub — but only for a loopback host.
# --------------------------------------------------------------------------

_LOOPBACK_URLS = (
    "http://127.0.0.1:9377",
    "https://127.0.0.1:9377",
    "http://localhost:9377",
    "http://[::1]:9377",
)

_NON_LOOPBACK_URLS = (
    # SSRF: point the child at an arbitrary endpoint
    "http://evil.com:3128",
    "https://attacker.example/collect",
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    # wildcard bind address — reaches whatever listens on the host, not loopback
    "http://0.0.0.0:9377",
    "http://[::]:9377",
    # userinfo confusion — real host is evil.com
    "http://127.0.0.1:9377@evil.com",
    "http://***@evil.com",
    # wrong scheme
    "file:///etc/passwd",
    "ftp://127.0.0.1",
    "javascript:alert(1)",
    "",
)


def test_camofox_url_is_forwarded_for_loopback_hosts():
    for url in _LOOPBACK_URLS:
        assert _forwarded_camofox_url({"CSE_CAMOFOX_URL": url}) == url


def test_camofox_url_is_dropped_for_non_loopback_hosts():
    for url in _NON_LOOPBACK_URLS:
        assert _forwarded_camofox_url({"CSE_CAMOFOX_URL": url}) is None, url


def test_camofox_url_absent_yields_none():
    assert _forwarded_camofox_url({}) is None


def test_camofox_url_empty_or_blank_yields_none():
    assert _forwarded_camofox_url({"CSE_CAMOFOX_URL": ""}) is None
    assert _forwarded_camofox_url({"CSE_CAMOFOX_URL": "   "}) is None


def test_camofox_url_without_scheme_is_dropped():
    """``127.0.0.1:9377`` parses as scheme=127.0.0.1 — must not be forwarded."""
    assert _forwarded_camofox_url({"CSE_CAMOFOX_URL": "127.0.0.1:9377"}) is None
    assert _forwarded_camofox_url({"CSE_CAMOFOX_URL": "localhost:9377/x"}) is None


def test_sanitize_env_carries_loopback_camofox_url():
    from sud_cli.case_search_bridge import _sanitize_env

    clean = _sanitize_env({"PATH": "/usr/bin", "CSE_CAMOFOX_URL": "http://127.0.0.1:9377"})
    assert clean["CSE_CAMOFOX_URL"] == "http://127.0.0.1:9377"
    assert "PATH" in clean


def test_sanitize_env_drops_non_loopback_camofox_url():
    from sud_cli.case_search_bridge import _sanitize_env

    clean = _sanitize_env({"PATH": "/usr/bin", "CSE_CAMOFOX_URL": "http://evil.com:3128"})
    assert "CSE_CAMOFOX_URL" not in clean


def test_sanitize_env_still_drops_secrets_and_proxies_with_camofox_set():
    """Allowlisting CSE_CAMOFOX_URL must not weaken the scrub of other vars."""
    from sud_cli.case_search_bridge import _sanitize_env

    dirty = {
        "PATH": "/usr/bin",
        "CSE_CAMOFOX_URL": "http://127.0.0.1:9377",
        "CSE_LICENSE_KEY": "SUPERSECRET",
        "HTTP_PROXY": "http://attacker:3128",
        "OPENROUTER_API_KEY": "sk-secret",
    }
    clean = _sanitize_env(dirty)
    assert clean == {"PATH": "/usr/bin", "CSE_CAMOFOX_URL": "http://127.0.0.1:9377"}
