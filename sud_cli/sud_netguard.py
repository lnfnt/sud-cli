"""
sud_netguard — общий защитный сетевой слой the project (egress guard + per-host breaker + spotlighting).

Возможности:
  1. Egress guard (fail-closed): запросы в loopback/приватные сети/link-local/
     облачную метадату (169.254.169.254) запрещены, кроме явного trust-списка.
     DNS-имена резолвятся и проверяется КАЖДЫЙ адрес (защита от DNS-rebinding).
  2. Per-host breaker: при 403/429/капче хост уходит в cooldown (по умолчанию 60 с),
     состояние крос-процессное (SQLite), чтобы CLI-запуски не долбили хост лавиной.
  3. Spotlighting: обёртка недоверенного контента в маркированные теги
     <external_untrusted_data> с экранированием попыток побега из тега.

Только stdlib (кроме ленивого import requests в guarded_get).
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import re
import socket
import sqlite3
import ssl
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple, Optional

NETGUARD_VERSION = "0.3.2"


class EgressBlockedError(RuntimeError):
    """Запрос в запрещённую сеть (fail-closed)."""


class BreakerOpenError(RuntimeError):
    """Хост в cooldown после 4xx/капчи; повтор позже."""

    def __init__(self, host: str, remaining: float, reason: str = ""):
        self.host = host
        self.remaining = remaining
        self.reason = reason
        super().__init__(
            f"breaker open for {host}: cooldown {remaining:.0f}s remaining"
            + (f" (last reason: {reason})" if reason else "")
        )


class _ValidatedTarget(NamedTuple):
    hostport: str
    scheme: str
    hostname: str
    port: int
    address: str
    authority: str


# --- Egress guard -----------------------------------------------------------

# Доверенные внутренние endpoint'ы (host или host:port). Расширяется через env
# SUD_NETGUARD_TRUSTED="host[:port],host[:port]".
_TRUSTED_DEFAULT: set[str] = set()  # trusted-эндпоинты — только явным env (audit 25.10)


def _trusted() -> set[str]:
    extra = os.environ.get("SUD_NETGUARD_TRUSTED", "")
    out = set(_TRUSTED_DEFAULT)
    for part in extra.split(","):
        part = part.strip().lower()
        if part:
            out.add(part)
    return out


def _is_denied_addr(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if addr.is_loopback or addr.is_private or addr.is_link_local:
        return True
    if addr.is_unspecified or addr.is_multicast or addr.is_reserved:
        return True
    # Облачная метадата и CGNAT-диапазоны — не наши цели.
    if addr.version == 4 and str(addr).startswith("169.254.169.254"):
        return True
    if addr.version == 4 and addr in ipaddress.ip_network("100.64.0.0/10"):
        return True
    return False


def _validated_target(url: str, *, resolve: bool = True) -> _ValidatedTarget:
    """Parse, validate every DNS answer, and retain one exact address to pin."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        explicit_port = parts.port
    except Exception as exc:  # noqa: BLE001
        raise EgressBlockedError(f"unparseable url: {url!r}") from exc
    if scheme not in ("http", "https"):
        raise EgressBlockedError(f"scheme not allowed: {scheme!r} ({url!r})")
    if not hostname or parts.username is not None or parts.password is not None:
        raise EgressBlockedError(f"invalid or credentialed hostname in url: {url!r}")

    host = hostname.lower().rstrip(".")
    port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise EgressBlockedError(f"bad port in url: {url!r}")
    hostport = f"{host}:{port}"
    trusted_hosts = _trusted()
    trusted = host in trusted_hosts or hostport in trusted_hosts

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_denied_addr(literal) and not trusted:
            raise EgressBlockedError(f"egress denied to {hostport} (non-public address)")
        return _ValidatedTarget(hostport, scheme, host, port, str(literal), parts.netloc)

    if not resolve:
        return _ValidatedTarget(hostport, scheme, host, port, "", parts.netloc)

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception as exc:  # noqa: BLE001
        raise EgressBlockedError(f"DNS failure for {host} (fail-closed)") from exc
    if not infos:
        raise EgressBlockedError(f"DNS returned no addresses for {host} (fail-closed)")

    addresses: list[str] = []
    for info in infos:
        try:
            family, _socktype, _proto, _canonname, sockaddr = info
            if family not in (socket.AF_INET, socket.AF_INET6) or not sockaddr:
                raise ValueError("unsupported address family or empty sockaddr")
            candidate = sockaddr[0]
            if not isinstance(candidate, str) or "%" in candidate:
                raise ValueError("malformed or scoped address")
            addr = ipaddress.ip_address(candidate)
            if (family == socket.AF_INET and addr.version != 4) or (
                family == socket.AF_INET6 and addr.version != 6
            ):
                raise ValueError("address family does not match answer")
        except Exception as exc:  # noqa: BLE001
            raise EgressBlockedError(f"malformed DNS answer for {host}") from exc
        if _is_denied_addr(addr) and not trusted:
            raise EgressBlockedError(f"egress denied: {host} resolves to non-public {addr}")
        addresses.append(str(addr))

    return _ValidatedTarget(hostport, scheme, host, port, addresses[0], parts.netloc)


def guard_url(url: str, *, resolve: bool = True) -> str:
    """Проверить URL и вернуть нормализованный host[:port]."""
    return _validated_target(url, resolve=resolve).hostport


# --- Per-host breaker (крос-процессный, SQLite) ------------------------------

_DEFAULT_DB = "netguard_breaker.sqlite3"

# Суффиксы хостов, к которым применяется breaker. Env:
# SUD_NETGUARD_BREAKER_SUFFIXES=".cian.ru,.avito.ru,sudrf.ru"
_SUFFIX_DEFAULT = (
    ".cian.ru", "cian.ru", ".avito.ru", "avito.ru",
    "sudrf.ru", "sudact.ru", ".sudact.ru",
    "rosreestr.gov.ru", ".rosreestr.gov.ru",
    "pravo.gov.ru", ".pravo.gov.ru",
    # v2.3.3 (review R7): efrsb_public дёргает breaker_trip для fedresurs, а хоста
    # не было в списке — per-host cooldown молча не работал.
    "bankrot.fedresurs.ru", ".bankrot.fedresurs.ru",
    "fssp.gov.ru", ".fssp.gov.ru", "api.fssp.gov.ru",
    "vsrf.ru", ".vsrf.ru",
    "mos-gorsud.ru", ".mos-gorsud.ru",
)


def _breaker_db() -> str:
    return os.environ.get("SUD_NETGUARD_DB", _DEFAULT_DB)


def _breaker_suffixes() -> tuple[str, ...]:
    extra = os.environ.get("SUD_NETGUARD_BREAKER_SUFFIXES", "")
    base = list(_SUFFIX_DEFAULT)
    for part in extra.split(","):
        part = part.strip().lower()
        if part and part not in base:
            base.append(part)
    return tuple(base)


def _host_key(hostport_or_host: str) -> str:
    """Выделить hostname из 'host', 'host:port' ИЛИ полного URL."""
    value = (hostport_or_host or "").strip()
    if "://" in value:
        from urllib.parse import urlsplit

        netloc = urlsplit(value).netloc
        value = netloc or value
    host = value.split("/")[0].split(":")[0].lower().rstrip(".")
    return host


def _breaker_conn() -> sqlite3.Connection:
    path = _breaker_db()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS breaker_state ("
        " host TEXT PRIMARY KEY, open_until REAL NOT NULL, reason TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS breaker_soft ("
        " host TEXT PRIMARY KEY, failures INTEGER NOT NULL, reason TEXT)"
    )
    return conn


def _applies_to(host: str) -> bool:
    host = _host_key(host)
    for suf in _breaker_suffixes():
        bare = suf.lstrip(".")
        if host == bare:
            return True
        if host.endswith("." + bare) or host.endswith("--" + bare):
            return True
    return False


def breaker_remaining(host: str) -> float:
    """Сколько секунд cooldown осталось у хоста (0 = можно ходить)."""
    if not _applies_to(host):
        return 0.0
    try:
        with _breaker_conn() as conn:
            row = conn.execute(
                "SELECT open_until, reason FROM breaker_state WHERE host = ?",
                (_host_key(host),),
            ).fetchone()
    except sqlite3.Error:
        return 0.0  # state-файл недоступен: не блокируем работу (breaker — оптимизация)
    if not row:
        return 0.0
    remaining = float(row[0]) - time.time()
    return remaining if remaining > 0 else 0.0


def breaker_trip(host: str, *, seconds: float = 60.0, reason: str = "") -> None:
    """Включить cooldown для хоста (жёсткий блок: 403/429/капча)."""
    if not _applies_to(host):
        return
    try:
        with _breaker_conn() as conn:
            conn.execute(
                "INSERT INTO breaker_state (host, open_until, reason) VALUES (?, ?, ?) "
                "ON CONFLICT(host) DO UPDATE SET open_until = excluded.open_until, "
                "reason = excluded.reason",
                (_host_key(host), time.time() + seconds, reason or "hard_block"),
            )
    except sqlite3.Error:
        pass


def breaker_soft_fail(host: str, *, threshold: int = 3,
                      cooldown: float = 60.0, reason: str = "") -> None:
    """Мягкий сигнал (challenge-маркер и т.п.): триггер после N повторов.

    Важно: trip выполняется ПОСЛЕ закрытия соединения состояния, иначе
    вложенное соединение к тому же файлу ловит database-is-locked.
    """
    if not _applies_to(host):
        return
    key = _host_key(host)
    n = 1
    try:
        with _breaker_conn() as conn:
            row = conn.execute(
                "SELECT failures FROM breaker_soft WHERE host = ?", (key,)
            ).fetchone()
            n = (int(row[0]) + 1) if row else 1
            if n >= threshold:
                conn.execute("DELETE FROM breaker_soft WHERE host = ?", (key,))
            else:
                conn.execute(
                    "INSERT INTO breaker_soft (host, failures, reason) VALUES (?, ?, ?) "
                    "ON CONFLICT(host) DO UPDATE SET failures = excluded.failures",
                    (key, n, reason or "soft"),
                )
    except sqlite3.Error:
        return
    if n >= threshold:
        breaker_trip(key, seconds=cooldown, reason=reason or f"soft_{n}")


def breaker_success(host: str) -> None:
    key = _host_key(host)
    try:
        with _breaker_conn() as conn:
            conn.execute("DELETE FROM breaker_soft WHERE host = ?", (key,))
    except sqlite3.Error:
        pass


def _check_target(target: _ValidatedTarget) -> None:
    remaining = breaker_remaining(target.hostport)
    if remaining > 0:
        raise BreakerOpenError(
            _host_key(target.hostport), remaining, reason=_last_reason(target.hostport)
        )


def check_before_fetch(url: str) -> str:
    """Полная пред-проверка: egress-guard + breaker. Возвращает hostport."""
    target = _validated_target(url)
    _check_target(target)
    return target.hostport


def _last_reason(host: str) -> str:
    try:
        with _breaker_conn() as conn:
            row = conn.execute(
                "SELECT reason FROM breaker_state WHERE host = ?", (_host_key(host),)
            ).fetchone()
        return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""


# --- Транспортные обёртки ----------------------------------------------------

_TRIP_STATUSES = {403, 429}


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, *, pinned_address: str, **kwargs: Any):
        self._pinned_address = pinned_address
        super().__init__(host, **kwargs)

    def connect(self) -> None:
        if self._tunnel_host:
            raise EgressBlockedError("HTTP proxy tunnels are unsupported")
        self.sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout, self.source_address
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, *, pinned_address: str, tls_hostname: str,
                 **kwargs: Any):
        self._pinned_address = pinned_address
        self._tls_hostname = tls_hostname
        super().__init__(host, **kwargs)

    def connect(self) -> None:
        if self._tunnel_host:
            raise EgressBlockedError("HTTPS proxy tunnels are unsupported")
        raw_sock = socket.create_connection(
            (self._pinned_address, self.port), self.timeout, self.source_address
        )
        # HTTPSConnection's verified context is retained; only the TCP target is pinned.
        self.sock = self._context.wrap_socket(
            raw_sock, server_hostname=self._tls_hostname
        )


def _assert_request_matches(req: Any, target: _ValidatedTarget) -> None:
    from urllib.parse import urlsplit

    if req.has_proxy():
        raise EgressBlockedError("proxy requests are unsupported by the pinned transport")
    try:
        parts = urlsplit(req.full_url)
        request_port = parts.port if parts.port is not None else (
            443 if parts.scheme.lower() == "https" else 80
        )
    except Exception as exc:  # noqa: BLE001
        raise EgressBlockedError("malformed request reached pinned transport") from exc
    request_host = (parts.hostname or "").lower().rstrip(".")
    if (parts.scheme.lower() != target.scheme or request_host != target.hostname
            or request_port != target.port):
        raise EgressBlockedError("request target changed after DNS validation")


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, target: _ValidatedTarget):
        super().__init__()
        self._target = target

    def http_open(self, req: Any):
        _assert_request_matches(req, self._target)
        factory = lambda host, **kwargs: _PinnedHTTPConnection(
            host, pinned_address=self._target.address, **kwargs
        )
        return self.do_open(factory, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, target: _ValidatedTarget):
        self._target = target
        context = ssl.create_default_context()
        super().__init__(context=context)

    def https_open(self, req: Any):
        _assert_request_matches(req, self._target)
        factory = lambda host, **kwargs: _PinnedHTTPSConnection(
            host,
            pinned_address=self._target.address,
            tls_hostname=self._target.hostname,
            **kwargs,
        )
        return self.do_open(factory, req, context=self._context)


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str):
        raise EgressBlockedError("redirects are disabled by the pinned transport")


def _origin_headers(headers: Optional[dict], authority: str) -> dict:
    result = {key: value for key, value in (headers or {}).items()
              if key.lower() != "host"}
    result["Host"] = authority
    return result


def _pinned_open(target: _ValidatedTarget, url: str, timeout: float,
                 headers: Optional[dict]):
    req = urllib.request.Request(url, headers=_origin_headers(headers, target.authority))
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _PinnedHTTPHandler(target),
        _PinnedHTTPSHandler(target),
        _RejectRedirectHandler(),
    )
    return opener.open(req, timeout=timeout)


def _requests_url_matches(url: str, target: _ValidatedTarget) -> None:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        port = parts.port if parts.port is not None else (
            443 if parts.scheme.lower() == "https" else 80
        )
    except Exception as exc:  # noqa: BLE001
        raise EgressBlockedError("malformed request reached pinned requests transport") from exc
    if ((parts.scheme or "").lower() != target.scheme
            or (parts.hostname or "").lower().rstrip(".") != target.hostname
            or port != target.port):
        raise EgressBlockedError("request target changed after DNS validation")


def guarded_get(url: str, *, timeout: float = 30.0, headers: Optional[dict] = None,
                stream: bool = False, session: Any = None,
                challenge_marker: str = "") -> Any:
    """GET with a requests-compatible response over a DNS-pinned connection.

    The connection pool is keyed by the validated address, while the HTTP Host,
    TLS SNI, and certificate hostname remain the original URL hostname. Redirects
    are returned to the caller and never followed automatically.
    """
    try:
        import requests
        from requests.adapters import HTTPAdapter
        from requests.cookies import extract_cookies_to_jar
        from requests.sessions import dispatch_hook
    except ImportError as exc:  # noqa: BLE001
        raise EgressBlockedError("requests is required for guarded_get") from exc

    target = _validated_target(url)
    _check_target(target)

    owns_session = session is None
    session_close_attempted = False
    created_adapters = []
    if owns_session:
        session = requests.Session.__new__(requests.Session)
        original_mount = session.mount

        def track_mount(prefix, adapter):
            if all(existing is not adapter for existing in created_adapters):
                created_adapters.append(adapter)
            return original_mount(prefix, adapter)

        session.mount = track_mount
        try:
            requests.Session.__init__(session)
        except BaseException:
            for adapter in created_adapters:
                try:
                    adapter.close()
                except BaseException:
                    pass
            raise
        finally:
            session.__dict__.pop("mount", None)
    elif type(session) is not requests.Session:
        raise EgressBlockedError("custom requests sessions are unsupported by the pinned transport")

    def close_owned_session():
        nonlocal session_close_attempted
        if not owns_session or session_close_attempted:
            return
        session_close_attempted = True

        attempted_adapters = []
        wrapped_adapters = []
        for owned_adapter in created_adapters:
            original_closer = owned_adapter.close
            had_instance_closer = "close" in owned_adapter.__dict__
            previous_instance_closer = owned_adapter.__dict__.get("close")

            def track_close(adapter=owned_adapter, closer=original_closer):
                if any(attempted is adapter for attempted in attempted_adapters):
                    return None
                attempted_adapters.append(adapter)
                return closer()

            owned_adapter.close = track_close
            wrapped_adapters.append((
                owned_adapter, original_closer,
                had_instance_closer, previous_instance_closer,
            ))

        session_error = None
        cleanup_error = None
        try:
            session.close()
        except BaseException as exc:
            session_error = exc
        finally:
            for owned_adapter, _, had_instance_closer, previous_instance_closer in wrapped_adapters:
                try:
                    if had_instance_closer:
                        owned_adapter.close = previous_instance_closer
                    else:
                        del owned_adapter.close
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc

        for owned_adapter, original_closer, _, _ in wrapped_adapters:
            if any(attempted is owned_adapter for attempted in attempted_adapters):
                continue
            try:
                original_closer()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc

        if session_error is not None:
            raise session_error
        if cleanup_error is not None:
            raise cleanup_error

    # We intentionally do not use any session adapter: only its standard request
    # preparation is reused, so cookies/auth/headers survive without granting a
    # custom adapter control of the socket destination.
    try:
        if any(type(adapter) is not HTTPAdapter for adapter in session.adapters.values()):
            raise EgressBlockedError("custom requests adapters are unsupported by the pinned transport")

        request = requests.Request("GET", url, headers=headers)
        prepared = session.prepare_request(request)
        _requests_url_matches(prepared.url, target)
        prepared.headers["Host"] = target.authority

        settings = session.merge_environment_settings(
            prepared.url, {}, stream=stream, verify=session.verify, cert=session.cert
        )
        if settings.get("proxies"):
            raise EgressBlockedError("proxy requests are unsupported by the pinned transport")
    except BaseException:
        if owns_session:
            try:
                close_owned_session()
            except BaseException:
                pass
        raise

    class _PinnedRequestsAdapter(HTTPAdapter):
        def __init__(self):
            try:
                super().__init__()
            except BaseException:
                try:
                    self.close()
                except BaseException:
                    pass
                raise

        def _pool_for(self, request_or_url: Any, verify: Any, cert: Any):
            request_url = getattr(request_or_url, "url", request_or_url)
            _requests_url_matches(request_url, target)
            if not hasattr(self, "build_connection_pool_key_attributes"):
                pool_kwargs = {}
                if target.scheme == "https":
                    pool_kwargs.update(
                        assert_hostname=target.hostname,
                        server_hostname=target.hostname,
                    )
                return self.poolmanager.connection_from_host(
                    host=target.address,
                    port=target.port,
                    scheme=target.scheme,
                    pool_kwargs=pool_kwargs,
                )

            request_for_key = request_or_url
            if not hasattr(request_for_key, "url"):
                request_for_key = type("PreparedURL", (), {"url": request_url})()
            host_params, pool_kwargs = self.build_connection_pool_key_attributes(
                request_for_key, verify, cert
            )
            host_params = dict(host_params)
            pool_kwargs = dict(pool_kwargs)
            host_params["host"] = target.address
            if target.scheme == "https":
                # urllib3 connects to the pool host (the IP), but uses these
                # explicit names for SNI and certificate hostname verification.
                pool_kwargs["assert_hostname"] = target.hostname
                pool_kwargs["server_hostname"] = target.hostname
            return self.poolmanager.connection_from_host(
                **host_params, pool_kwargs=pool_kwargs
            )

        def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
            if proxies:
                raise EgressBlockedError("proxy requests are unsupported by the pinned transport")
            return self._pool_for(request, verify, cert)

        def get_connection(self, url, proxies=None):
            if proxies:
                raise EgressBlockedError("proxy requests are unsupported by the pinned transport")
            return self._pool_for(url, self.config.get("verify", True), None)

    adapter = None
    response = None
    response_close_installed = False
    owned_responses = []
    owned_raws = []
    cleaned_raws = []

    def remember_raw(raw):
        if raw is not None and all(existing is not raw for existing in owned_raws):
            owned_raws.append(raw)

    def remember_response_raw(candidate):
        try:
            raw = getattr(candidate, "raw", None)
        except BaseException:
            return
        remember_raw(raw)

    def cleanup_raw_resource(raw):
        """Best-effort raw cleanup, returning its first introspection/close error."""
        if raw is None:
            return None
        if any(cleaned is raw for cleaned in cleaned_raws):
            return None
        cleaned_raws.append(raw)
        try:
            raw_closer = getattr(raw, "close", None)
        except BaseException as exc:
            return exc
        if not callable(raw_closer):
            return None
        first_error = None
        try:
            raw_closed = bool(getattr(raw, "closed", False))
        except BaseException as exc:
            first_error = exc
            raw_closed = False
        if not raw_closed:
            try:
                raw_closer()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        return first_error

    def cleanup_raw_response(owned_response):
        try:
            raw = getattr(owned_response, "raw", None)
        except BaseException as exc:
            return exc
        return cleanup_raw_resource(raw)

    def remember_response(candidate):
        if candidate is not None and all(
            existing is not candidate for existing in owned_responses
        ):
            owned_responses.append(candidate)

    def track_response_hook(hook):
        def tracked_hook(hook_response, **kwargs):
            remember_response(hook_response)
            remember_response_raw(hook_response)
            try:
                result = hook(hook_response, **kwargs)
            finally:
                remember_response_raw(hook_response)
            if result is not None:
                remember_response(result)
                remember_response_raw(result)
            return result

        return tracked_hook

    try:
        adapter = _PinnedRequestsAdapter()
        response = adapter.send(
            prepared,
            stream=stream,
            timeout=timeout,
            verify=settings["verify"],
            cert=settings["cert"],
            proxies={},
        )
        remember_response(response)
        remember_response_raw(response)
        extract_cookies_to_jar(session.cookies, prepared, response.raw)

        # Apply host cooldowns to the actual transport response before user hooks can
        # replace it or raise. Inspecting a requested challenge marker may buffer a
        # streaming body, but requests keeps that content available to the caller.
        if response.status_code in _TRIP_STATUSES:
            breaker_trip(url, reason=f"http_{response.status_code}")
        elif challenge_marker and challenge_marker.casefold() in response.text.casefold():
            breaker_soft_fail(target.hostport, reason="challenge_marker")
        else:
            breaker_success(target.hostport)

        hooks = prepared.hooks
        response_hooks = hooks.get("response") if hooks else None
        if response_hooks:
            if hasattr(response_hooks, "__call__"):
                response_hooks = [response_hooks]
            hooks = dict(hooks)
            hooks["response"] = [
                track_response_hook(hook) for hook in response_hooks
            ]
        response = dispatch_hook(
            "response", hooks, response,
            stream=stream, timeout=timeout, verify=settings["verify"],
            cert=settings["cert"], proxies={},
        )
        remember_response(response)
        remember_response_raw(response)
        if not stream:
            response.content
        response.history = []
        response._netguard_adapter = adapter

        # Retain the original close methods before wrapping the final response.
        # Every response stays usable until the caller closes this final response.
        close_order = [response] + [
            owned for owned in owned_responses if owned is not response
        ]
        response_closers = [owned.close for owned in close_order]
        response_closed = False
        response_close_lock = threading.Lock()
        response_close_complete = threading.Event()
        response_close_error = None

        def close_response():
            nonlocal response_closed, response_close_error
            is_cleanup_owner = False
            wait_for_cleanup = False
            with response_close_lock:
                if response_closed:
                    wait_for_cleanup = not response_close_complete.is_set()
                else:
                    response_closed = True
                    is_cleanup_owner = True
            if not is_cleanup_owner:
                # In-flight waiters observe the first error; later closes are idempotent.
                if wait_for_cleanup:
                    response_close_complete.wait()
                    with response_close_lock:
                        completed_error = response_close_error
                    if completed_error is not None:
                        raise completed_error
                return

            first_error = None
            try:
                for owned_response, response_closer in zip(close_order, response_closers):
                    is_base_close = False
                    try:
                        is_base_close = (
                            getattr(response_closer, "__func__", None)
                            is requests.Response.close
                        )
                    except BaseException:
                        pass
                    skip_base_close = False
                    if is_base_close:
                        try:
                            response_raw = getattr(owned_response, "raw", None)
                        except BaseException:
                            response_raw = None
                        skip_base_close = response_raw is not None and any(
                            cleaned is response_raw for cleaned in cleaned_raws
                        )
                    if not skip_base_close:
                        try:
                            response_closer()
                        except BaseException as exc:
                            if first_error is None:
                                first_error = exc
                    raw_error = cleanup_raw_response(owned_response)
                    if first_error is None and raw_error is not None:
                        first_error = raw_error
                for raw in owned_raws:
                    raw_error = cleanup_raw_resource(raw)
                    if first_error is None and raw_error is not None:
                        first_error = raw_error
                try:
                    adapter.close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            finally:
                with response_close_lock:
                    response_close_error = first_error
                response_close_complete.set()
            if first_error is not None:
                raise first_error

        response.close = close_response
        response_close_installed = True
        if owns_session:
            close_owned_session()
        return response
    except BaseException:
        if response_close_installed:
            try:
                response.close()
            except BaseException:
                pass
        else:
            for owned_response in owned_responses:
                try:
                    owned_response.close()
                except BaseException:
                    pass
                cleanup_raw_response(owned_response)
            for raw in owned_raws:
                cleanup_raw_resource(raw)
            if adapter is not None:
                try:
                    adapter.close()
                except BaseException:
                    pass
        if owns_session and not session_close_attempted:
            try:
                close_owned_session()
            except BaseException:
                pass
        raise


def guarded_urlopen(url: str, *, timeout: float = 30.0,
                    headers: Optional[dict] = None) -> bytes:
    """Pinned urllib fetch returning body bytes; redirects fail closed."""
    target = _validated_target(url)
    _check_target(target)
    try:
        with _pinned_open(target, url, timeout, headers) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in _TRIP_STATUSES:
            breaker_trip(url, reason=f"http_{exc.code}")
        raise
    breaker_success(target.hostport)
    return body


def guarded_open(url: str, *, timeout: float = 30.0, headers: Optional[dict] = None):
    """Pinned urllib fetch returning a streaming response; redirects fail closed."""
    target = _validated_target(url)
    _check_target(target)
    try:
        return _pinned_open(target, url, timeout, headers)
    except urllib.error.HTTPError as exc:
        if exc.code in _TRIP_STATUSES:
            breaker_trip(url, reason=f"http_{exc.code}")
        raise


# --- Spotlighting -------------------------------------------------------------

_MARKER_OPEN = "<external_untrusted_data source='{source}' type='{data_type}'>"
_MARKER_CLOSE = "</external_untrusted_data>"
_ESCAPE_RE = re.compile(r"</\s*external_untrusted_data\s*>", re.IGNORECASE)


def spotlight(raw: str, *, source: str = "web", data_type: str = "text") -> str:
    """Обернуть недоверенный контент в маркированные теги.

    Все попытки преждевременно закрыть тег экранируются, чтобы контент
    не мог «выйти» из песочницы разметки и притвориться инструкцией.
    """
    sanitized = _ESCAPE_RE.sub("&lt;/external_untrusted_data&gt;", raw or "")
    return (
        _MARKER_OPEN.format(source=source, data_type=data_type)
        + sanitized
        + _MARKER_CLOSE
    )


_MARKED_PREFIX_RE = re.compile(
    r"^<external_untrusted_data source='[^']*' type='[^']*'>"
)


def is_spotlighted(text: str) -> bool:
    """True, если текст уже обёрнут в spotlight-маркер."""
    return bool(text) and bool(_MARKED_PREFIX_RE.match(text.lstrip()))


def strip_spotlight(text: str) -> str:
    """Снять spotlight-маркеры (для human-facing рендера). Безопасно на чистом тексте."""
    if not text:
        return text
    body = text
    lead = ""
    while body and body[0].isspace():
        lead += body[0]
        body = body[1:]
    body = _MARKED_PREFIX_RE.sub("", body, count=1)
    body = body.replace(_MARKER_CLOSE, "")
    return lead + body
