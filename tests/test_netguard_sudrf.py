"""Интеграционный тест netguard в sud-cli (sudrf/live.py).

Без сети: если хост sudrf.ru в cooldown, fetch_listing обязан упасть
SudrfLiveError, НЕ доходя до HTTP. Запуск:
  python3 tests/test_netguard_sudrf.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

fd, db = tempfile.mkstemp(suffix=".sqlite3")
os.close(fd)
os.unlink(db)
os.environ["SUD_NETGUARD_DB"] = db


def test_fetch_listing_blocked_by_breaker():
    from sud_cli import sud_netguard as ng
    from sud_cli.case_search.sudrf import live

    host = "momsudy--mms.sudrf.ru"
    ng.breaker_trip(host, seconds=120.0, reason="http_403")
    try:
        live.fetch_listing(host, use_cache=False, timeout=5)
    except live.SudrfLiveError as exc:
        assert "cooldown" in str(exc).lower(), str(exc)
    else:
        raise AssertionError("fetch_listing must fail while breaker is open")


def test_http_get_egress_fails_closed():
    """Сквозной egress-guard: приватный адрес блокируется ДО сети."""
    from sud_cli.sud_netguard import EgressBlockedError
    from sud_cli.case_search.sudrf import live

    try:
        live._http_get("http://192.168.13.13/", timeout=1)
    except EgressBlockedError:
        pass
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"expected EgressBlockedError, got {exc!r}")
    else:
        raise AssertionError("egress guard must deny private nets in _http_get")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {exc!r}")
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)


def test_mos_http_get_egress_fails_closed():
    from sud_cli import sud_netguard as ng
    from sud_cli.case_search.mos import live as mos_live
    try:
        mos_live._http_get("http://192.168.13.13/", timeout=1)
    except mos_live.MosLiveError as exc:
        assert "netguard blocked" in str(exc)
    else:
        raise AssertionError("mos _http_get must fail closed on private nets")


def test_vsrf_post_egress_fails_closed():
    from sud_cli import sud_netguard as ng
    from sud_cli.case_search.vsrf import live as vsrf_live
    client = vsrf_live.VsrfClient(base_url="http://192.168.13.13/")
    try:
        client._http_post("http://192.168.13.13/login", {"u": "x"})
    except vsrf_live.VsrfLiveError as exc:
        assert "netguard blocked" in str(exc)
    else:
        raise AssertionError("vsrf _http_post must fail closed on private nets")


def test_breaker_suffixes_cover_court_hosts():
    from sud_cli import sud_netguard as ng
    assert ng._applies_to("vkod.vsrf.ru")
    assert ng._applies_to("mos-gorsud.ru")
    assert ng._applies_to("momsudy--mms.sudrf.ru")
