# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""Fail-closed сетевой выход."""
import os
import socket
import sqlite3
import threading
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional


class EgressBlockedError(Exception):
    """Сетевой выход заблокирован."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _allowed(host: str, allowed_hosts: set) -> bool:
    for allowed in allowed_hosts:
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def guarded_get(url, *, allowed_hosts: set, proxy_env: str, timeout=15, require_proxy: bool = True) -> bytes:
    """Выполняет GET с fail-closed проверками.

    require_proxy=True (дефолт): без proxy_env в окружении — EgressBlockedError.
    require_proxy=False: прямые соединения разрешены (host allow-list + DNS-pin активны);
    для контуров без RU-egress-требования. Явный выбор вызывающей стороны — в коде видно.
    """
    if require_proxy and not os.environ.get(proxy_env):
        raise EgressBlockedError("no_proxy_configured")
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if not host:
        raise EgressBlockedError("host_not_allowed")
    if not _allowed(host, allowed_hosts):
        raise EgressBlockedError("host_not_allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    # DNS-консистентность внутри IPv4-семейства: gethostbyname (только v4)
    # должен совпасть с одним из v4-адресов getaddrinfo. Сравнение через
    # двойной резолв НЕ используем — CDN round-robin легитимно ротирует IP;
    # TLS проверяет хост на connect. Расхождение методов = poisoning/сплит.
    ghbn_ip = None
    gai_v4 = set()
    try:
        ghbn_ip = socket.gethostbyname(host)
    except Exception:
        ghbn_ip = None
    try:
        for fam, _, _, _, sockaddr in socket.getaddrinfo(host, port):
            ip = sockaddr[0]
            if ":" not in ip:
                gai_v4.add(ip)
    except Exception:
        pass
    if not gai_v4 and ghbn_ip is None:
        raise EgressBlockedError("dns_pin_mismatch")
    if gai_v4 and ghbn_ip is not None and ghbn_ip not in gai_v4:
        raise EgressBlockedError("dns_pin_mismatch")
    if ghbn_ip is None and not gai_v4:
        raise EgressBlockedError("dns_pin_mismatch")
    proxy_url = os.environ.get(proxy_env, "").strip() if require_proxy else ""
    if require_proxy and not proxy_url:
        raise EgressBlockedError("no_proxy_configured")
    req = urllib.request.Request(url, headers={"Host": host})
    if proxy_url:
        # трафик реально идёт через прокси (RU-egress: mode=proxy_only)
        parsed_scheme = parsed.scheme or "http"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({parsed_scheme: proxy_url})
        )
        with opener.open(req, timeout=timeout) as resp:
            return resp.read()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class Breaker:
    """Простой circuit breaker (in-memory + optional sqlite)."""

    def __init__(self, threshold: int = 5, reset_seconds: int = 60, db_path: Optional[str] = None, name: str = "default", **kwargs):
        self.threshold = kwargs.pop("failure_threshold", kwargs.pop("max_failures", kwargs.pop("threshold", threshold)))
        self.reset_seconds = kwargs.pop("recovery_timeout", kwargs.pop("reset_timeout", kwargs.pop("cooldown", kwargs.pop("reset_seconds", reset_seconds))))
        self.db_path = kwargs.pop("storage_path", kwargs.pop("path", kwargs.pop("db", kwargs.pop("db_path", db_path))))
        self.name = kwargs.pop("name", name)
        self._failures = 0
        self._opened_at = None
        self._lock = threading.Lock()

    def _storage_ok(self) -> bool:
        if not self.db_path:
            return True
        try:
            conn = sqlite3.connect(self.db_path, timeout=1)
            conn.close()
            return True
        except (sqlite3.Error, OSError):
            return False

    def allow(self) -> bool:
        if not self._storage_ok():
            return False  # fail-closed
        with self._lock:
            if self._opened_at is not None:
                import time
                if time.time() - self._opened_at < self.reset_seconds:
                    return False
                self._opened_at = None
                self._failures = 0
            return True

    def allow_request(self) -> bool:
        return self.allow()

    def can_execute(self) -> bool:
        return self.allow()

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                import time
                self._opened_at = time.time()

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def is_open(self) -> bool:
        return not self.allow()

    def call(self, fn: Callable, *args, **kwargs) -> Any:
        if not self.allow():
            raise EgressBlockedError("host_not_allowed")
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result
