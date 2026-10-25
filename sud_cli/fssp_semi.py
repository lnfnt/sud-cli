"""S-61 полу-автомат ФССП (решение проекта 25.10, юридическая проверка запрошена отдельно).

Полу-автомат: скрипт делает ВСЁ, кроме капчи — открывает страницу БДИП через
общий loopback-браузер (Camofox runtime), заполняет форму, нажимает «Найти».
Если сайт выставил капчу — делает скриншот, отдаёт его человеку; человек
сообщает код, `fssp assist --resume` вводит его и забирает результат.
Капчу решает человек, не сервис-Recognizer и не брутфорс.

Ограничения честности:
- селекторы эвристические (страница недоступна для калибровки из-за лимита
  подключений ФССП к нашему exit IP); при нераспознанной странице — статус
  page_changed со снимком, ручной контур (fssp check) остаётся запасным;
- rate-limit: не более одного start на сессию; 503/429/'лимит подключений'
  -> breaker_trip(fssp.gov.ru) и честная ошибка limited;
- runtime обязан быть loopback (SSRF-политика, как в мосте case-search).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

FSSP_URL = "https://fssp.gov.ru/iss/ip/"
DEFAULT_RUNTIME = "http://127.0.0.1:9377"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_TTL_S = 30 * 60


class FsspAssistError(Exception):
    pass


def _runtime_url() -> str:
    raw = os.environ.get("CSE_FSSP_RUNTIME", DEFAULT_RUNTIME).strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or parts.hostname not in _LOOPBACK:
        raise FsspAssistError("CSE_FSSP_RUNTIME must be a loopback http(s) URL")
    return raw.rstrip("/")


def _api(method: str, path: str, *, body: Optional[dict] = None,
         query: Optional[dict] = None, timeout: int = 60, binary: bool = False):
    import urllib.error
    import urllib.request

    base = _runtime_url()
    qs = ("?" + "&".join(f"{k}={v}" for k, v in (query or {}).items())) if query else ""
    url = f"{base}{path}{qs}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(12_000_000)
    except urllib.error.HTTPError as exc:
        detail = exc.read(300).decode("utf-8", "replace") if binary is False else ""
        raise FsspAssistError(f"runtime http {exc.code}: {detail[:160]}") from exc
    except urllib.error.URLError as exc:
        raise FsspAssistError(f"runtime unreachable: {str(exc.reason)[:80]}") from exc
    if binary:
        return raw
    try:
        return json.loads(raw.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        return {"raw": raw.decode("utf-8", "replace")[:400]}


def _state_dir(state_dir: Path) -> Path:
    d = Path(state_dir) / "fssp_assist"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(sess: dict) -> None:
    d = _state_dir(Path(sess["state_dir"]))
    (d / f"{sess['session_id']}.json").write_text(
        json.dumps(sess, ensure_ascii=False), encoding="utf-8")


def _load(session_id: str, state_dir: Path) -> dict:
    p = _state_dir(state_dir) / f"{session_id}.json"
    if not p.is_file():
        raise FsspAssistError(f"session not found: {session_id}")
    sess = json.loads(p.read_text(encoding="utf-8"))
    if time.time() - sess.get("created_at", 0) > _TTL_S:
        raise FsspAssistError("session expired (30 min); start fresh or use manual contour")
    return sess


def _breaker_trip(url: str) -> None:
    try:
        from sud_cli.sud_netguard import breaker_trip
        breaker_trip(url, reason="fssp_assist_limited")
    except Exception:
        pass


_LIMIT_RE = re.compile(r"превысили лимит|попробуйте позже", re.I)
_CAPTCHA_RE = re.compile(r"captcha|капч|смарт-?каптча|не робот", re.I)
_RESULT_WORDS = re.compile(r"найдено|результат|исполнительное производство|отсутству", re.I)


_FORM_MARK = re.compile(r"Наименование предприятия-должника|Фамилия", re.I)
_RESULT_MARK = re.compile(
    r"исполнительн\w* производств\w* (?:не )?(?:найдено|отсутству)"
    r"|найдено (?:исполнительных )?производств"
    r"|^.*\bдолжник\b", re.I | re.M)
_CAPTCHA_MARK = re.compile(r"captcha|каптч|не робот|robot check|challenge|подтвердите,? что", re.I)


def _detect_page(text: str, *, pre: bool = False) -> str:
    body = text or ""
    if _LIMIT_RE.search(body):
        return "limited"
    if _CAPTCHA_MARK.search(body):
        return "captcha"
    if not pre and _RESULT_MARK.search(body):
        return "result"
    if _FORM_MARK.search(body):
        return "form"
    return "unknown"


def _close_tab(user_id: str, tab_id: str) -> None:
    """Закрыть вкладку (пул не должен копить мёртвые сессии)."""
    try:
        _api("DELETE", f"/tabs/{tab_id}", query={"userId": user_id})
    except Exception:
        pass


def start(state_dir: Path, *, kind: str, name: Optional[str] = None,
          inn: Optional[str] = None, birth_date: Optional[str] = None) -> dict:
    """Открыть БДИП и заполнить форму; капчу — не решает."""
    kind = (kind or "").strip().lower()
    if kind not in ("fiz", "ur"):
        raise FsspAssistError("kind must be fiz|ur")
    if not (name or inn):
        raise FsspAssistError("provide --name or --inn")
    sid = secrets.token_hex(6)
    user_id = f"sud-fssp-{sid}"
    tab = _api("POST", "/tabs", body={"userId": user_id, "sessionKey": sid, "url": FSSP_URL})
    tab_id = tab.get("tabId") or (tab.get("tab") or {}).get("tabId")
    if not tab_id:
        raise FsspAssistError(f"runtime did not return tabId: {str(tab)[:120]}")
    _api("POST", f"/tabs/{tab_id}/wait", body={"userId": user_id, "timeout": 15000})
    snap = _api("GET", f"/tabs/{tab_id}/snapshot", query={"userId": user_id}, timeout=60)
    text = snap.get("snapshot") or snap.get("text") or snap.get("raw") or json.dumps(snap, ensure_ascii=False)
    state = _detect_page(text, pre=True)
    sess = {"session_id": sid, "tab_id": tab_id, "user_id": user_id,
            "state_dir": str(state_dir), "kind": kind, "name": name, "inn": inn,
            "birth_date": birth_date, "created_at": time.time(), "page_state": state}
    if state == "limited":
        _breaker_trip(FSSP_URL)
        sess["status"] = "limited"
        sess["hint"] = ("ФССП ограничила подключения с exit IP; хост уведён в cooldown. "
                        "Повторите позже или используйте ручной контур: fssp check.")
        _close_tab(user_id, tab_id)
        _save(sess)
        return sess
    if state in ("form", "unknown"):
        try:
            filled = _fill_form(user_id, tab_id, kind=kind, name=name, inn=inn,
                                birth_date=birth_date)
            sess["fill"] = filled
            if filled.get("submitted"):
                _api("POST", f"/tabs/{tab_id}/wait", body={"userId": user_id, "timeout": 15000})
                snap2 = _api("GET", f"/tabs/{tab_id}/snapshot", query={"userId": user_id})
                t2 = snap2.get("snapshot") or snap2.get("text") or ""
                state = _detect_page(t2, pre=False)
                sess["page_state"] = state
                sess["page_text"] = t2[:4000]
        except FsspAssistError as exc:
            sess["page_state"] = "page_changed"
            sess["hint"] = f"автозаполнение не удалось: {exc}; используйте fssp check"
            _close_tab(user_id, tab_id)
    if sess["page_state"] == "captcha":
        png = _state_dir(Path(state_dir)) / f"{sid}.png"
        raw = _api("GET", f"/tabs/{tab_id}/screenshot", query={"userId": user_id, "fullPage": "true"},
                   timeout=60, binary=True)
        png.write_bytes(raw if isinstance(raw, (bytes, bytearray)) else b'')
        sess["status"] = "waiting_human_captcha"
        sess["captcha_image"] = str(png)
        sess["instruction"] = ("Решите капчу на скриншоте, пришлите код: "
                               f"sud-cli fssp assist --resume {sid} --code <КОД>")
    elif sess["page_state"] == "result":
        sess["status"] = "done"
        sess["results_text"] = sess.get("page_text", "")
    else:
        sess["status"] = sess.get("page_state", "unknown")
    _save(sess)
    return sess


def _fill_form(user_id: str, tab_id: str, *, kind: str, name: Optional[str],
               inn: Optional[str], birth_date: Optional[str]) -> dict:
    """Заполнение формы БДИП по калиброванным подписям (a11y-снапшот 25.10)."""
    script = r"""((kind, name, inn, bd) => {
      const vis = el => el && el.offsetParent !== null;
      const labOf = el => {
        let n = el.previousElementSibling;
        let txt = '';
        while (n && !txt) { txt = (n.innerText || n.textContent || '').trim(); n = n.previousElementSibling; }
        return txt + ' ' + (el.placeholder || '') + ' ' + (el.name || '') + ' ' + (el.id || '');
      };
      const setVal = (el, v) => {
        const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
        if (desc && desc.set) desc.set.call(el, v); else el.value = v;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
      };
      const inputs = [...document.querySelectorAll('input')].filter(e => vis(e) && (e.type === 'text' || !e.type));
      const byLabel = re => inputs.find(e => re.test(labOf(e)));
      const steps = [];
      let done = 0;
      const put = (re, val, tag) => {
        if (!val) return;
        const el = byLabel(re);
        if (el) { setVal(el, val); steps.push(tag); done++; }
      };
      if (kind === 'fiz') {
        const parts = (name || '').split(/\s+/);
        put(/Фамилия/i, parts[0], 'last');
        put(/Имя(?! и отчество)/i, parts[1], 'first');
        put(/Отчество/i, parts[2], 'middle');
        if (bd) put(/Дата рождения/i, bd, 'birth');
      } else {
        put(/Наименование предприятия-должника/i, name, 'ur-name');
        put(/ИНН юридического лица/i, inn, 'ur-inn');
      }
      const btns = [...document.querySelectorAll('button, input[type=submit]')].filter(vis);
      const btn = btns.find(b => /найт/i.test((b.innerText || b.value || '').trim()));
      let submitted = false;
      if (btn && done) { btn.click(); submitted = true; steps.push('submit'); }
      return {steps, filled: done, submitted};
    })"""
    body = {"userId": user_id,
            "expression": f"{script}({json.dumps(kind)}, {json.dumps(name)}, {json.dumps(inn)}, {json.dumps(birth_date)})",
            "timeout": 15000}
    out = _api("POST", f"/tabs/{tab_id}/evaluate", body=body)
    res = out.get("result") if isinstance(out, dict) else None
    if not isinstance(res, dict):
        raise FsspAssistError(f"evaluate returned {str(out)[:120]}")
    if not res.get("submitted"):
        raise FsspAssistError(f"форма не распознана (заполнено полей: {res.get('filled')}); используйте fssp check")
    return res


def resume(state_dir: Path, session_id: str, code: str) -> dict:
    """Ввести человеческий ответ капчи и забрать результат."""
    sess = _load(session_id, state_dir)
    if not code or not str(code).strip():
        raise FsspAssistError("code required (what the human read from the captcha)")
    user_id, tab_id = sess["user_id"], sess["tab_id"]
    _api("POST", f"/tabs/{tab_id}/type", body={
        "userId": user_id,
        "selector": "input[name*='captch' i], input[id*='captch' i], input[name*='capcha' i], textarea[name*='captch' i]",
        "text": str(code).strip(),
    })
    _api("POST", f"/tabs/{tab_id}/click", body={
        "userId": user_id, "selector": "button, input[type=submit]"})
    _api("POST", f"/tabs/{tab_id}/wait", body={"userId": user_id, "timeout": 20000})
    snap = _api("GET", f"/tabs/{tab_id}/snapshot", query={"userId": user_id})
    text = snap.get("text") or snap.get("snapshot") or json.dumps(snap, ensure_ascii=False)
    state = _detect_page(text)
    sess["page_state"] = state
    sess["resumed_at"] = time.time()
    if state == "result":
        sess["status"] = "done"
        sess["results_text"] = text[:4000]
        sess["verdict"] = ("none" if re.search(r"не найдено|отсутству", text, re.I) else "found")
        _close_tab(user_id, tab_id)
    elif state == "limited":
        _breaker_trip(FSSP_URL)
        sess["status"] = "limited"
        _close_tab(user_id, tab_id)
    else:
        sess["status"] = f"need_retry ({state})"
    _save(sess)
    return sess
