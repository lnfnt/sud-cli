# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S3-14/S1-15: importing TOCTOU/symlink/RecursionError; S1-15 мелкие пины."""
from __future__ import annotations

import json

import pytest

from sud_cli.importing import ImportError as FixtureImportError
from sud_cli.importing import _load_json, _validate_path


def test_symlink_rejected(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(FixtureImportError, match="symbolic links"):
        _validate_path(link)


def test_size_limit_enforced_on_actual_bytes(tmp_path):
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (11 * 1024 * 1024))  # > MAX_FILE_BYTES (10 MiB)
    # stat до чтения больше не используется; лимит enforced по read_bytes
    with pytest.raises(FixtureImportError, match="exceeds maximum size"):
        _load_json(big)


def test_deeply_nested_json_raises_typed_error(tmp_path, monkeypatch):
    # Детерминированно: RecursionError из парсера приводится к typed ImportError.
    import sud_cli.importing as importing

    def _boom(text):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(importing.json, "loads", _boom)
    f = tmp_path / "deep.json"
    f.write_text("[]", encoding="utf-8")
    with pytest.raises(FixtureImportError, match="malformed JSON"):
        _load_json(f)


def test_valid_json_still_loads(tmp_path):
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert _load_json(ok) == {"a": 1}


def test_interest_before_coverage_first_fails_visible():
    # S1-15: расчёт до coverage_first не extrapolates молча — rate_on бросает
    # ValueError, движок возвращает MISSING_REQUIRED_PARAM с предупреждением.
    import datetime
    from sud_cli.interest import RATES_META, calculate_interest
    first = RATES_META.get("coverage_first")
    if not first:
        pytest.skip("no coverage_first in meta")
    y, m, d = (int(x) for x in first.split("-"))
    calc = calculate_interest(1000, datetime.date(y - 1, m, d),
                              datetime.date(y - 1, m, min(d + 1, 28)), "395")
    assert calc.status == "MISSING_REQUIRED_PARAM"
    assert any("coverage" in str(w) for w in getattr(calc, "rates_warnings", []))


def test_rules_loader_accepts_corrected_spelling(tmp_path):
    from sud_cli.legal_engine import load_rules
    rules = {
        "version": "test", "tariff_version": "test", "legal_status": "test",
        "jurisdictions": {"arbitration": {}},
    }
    f = tmp_path / "rules.json"
    f.write_text(json.dumps(rules), encoding="utf-8")
    loaded = load_rules(path=f)
    assert "jisdictions" in loaded and loaded["jisdictions"] == {"arbitration": {}}
