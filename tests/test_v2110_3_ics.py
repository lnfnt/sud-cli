"""W6a §3 (S-28): ICS-экспорт сроков (RFC 5545) — самописный разворот конверта.

Тест не доверяет парсеру на слово: файл разворачивается (CRLF + fold
continuation) и разбирается по свойствам. Проверяются ровно требования ТЗ:
UID=det(case,key), DTSTART 09:00 Europe/Moscow, DTEND +1ч, SUMMARY/
DESCRIPTION, VALARM только на confirmed, экранирование ; , \\, пустой список
= валидный пустой VCALENDAR, стабильность и идемпотентность вывода.
"""
import json
from datetime import date, timedelta

from sud_cli.calendar_ics import build_ics, event_uid
from tests.conftest import run_cli

FIX_TODAY = date(2026, 10, 24)


def _state(case="case-a", key="as_appeal_decision", due="2026-11-24",
           state="computed", basis=None, evidence=None, confirmed_by=None, sid=1):
    return {
        "id": sid, "case_id": case, "deadline_key": key, "due_at": due,
        "state": state,
        "basis": basis if basis is not None else {"event_date": "2026-10-24",
                                                  "method": "вручение", "evidence": "test"},
        "evidence_note": evidence, "confirmed_by": confirmed_by,
    }


def _unfold(raw: bytes):
    text = raw.decode("utf-8")
    lines = []
    for line in text.split("\r\n"):
        if line[:1] in (" ", "\t"):
            assert lines, "continuation без родительской строки"
            lines[-1] += line[1:]
        elif line:
            lines.append(line)
    return lines


def _props(lines, name):
    out = []
    prefix = name + ":"
    for ln in lines:
        if ln.startswith(prefix):
            out.append(ln[len(prefix):])
    return out


def test_ics_structure_and_window():
    text, events = build_ics([_state(), _state(key="soyu_appeal_decision",
                                               due="2026-12-20", sid=2)],
                             today=FIX_TODAY, days=60)
    lines = _unfold(text.encode("utf-8"))
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-1] == "END:VCALENDAR"
    assert _props(lines, "VERSION") == ["2.0"]
    assert _props(lines, "METHOD") == ["PUBLISH"]
    assert _props(lines, "CALSCALE") == ["GREGORIAN"]
    assert lines.count("BEGIN:VEVENT") == lines.count("END:VEVENT") == 2
    assert len(_props(lines, "UID")) == 2 == len(events)


def test_ics_events_properties_tz_and_summary():
    text, events = build_ics([_state()], today=FIX_TODAY, days=60)
    lines = _unfold(text.encode("utf-8"))
    uid = _props(lines, "UID")
    assert uid == [event_uid("case-a", "as_appeal_decision")]
    assert uid[0].endswith("@sud-cli")
    starts = _props(lines, "DTSTART;TZID=Europe/Moscow")
    assert starts == ["20261124T090000"], starts
    ends = _props(lines, "DTEND;TZID=Europe/Moscow")
    assert ends == ["20261124T100000"], "DTEND = DTSTART + 1 час"
    assert _props(lines, "SUMMARY") == [r"deadline: as_appeal_decision case-a"]
    desc = _props(lines, "DESCRIPTION")[0]
    assert "basis:" in desc and "test" in desc
    assert any(l.startswith("BEGIN:VTIMEZONE") for l in lines)


def test_ics_valarm_only_for_confirmed():
    text, events = build_ics([
        _state(state="confirmed", confirmed_by="оператор", sid=1),
        _state(key="soyu_cassation", due="2026-12-01", state="computed", sid=2),
    ], today=FIX_TODAY, days=60)
    lines = _unfold(text.encode("utf-8"))
    assert lines.count("BEGIN:VALARM") == 1
    assert "TRIGGER:-P1D" in lines
    by_uid = {}
    current = None
    for ln in lines:
        if ln.startswith("UID:"):
            current = ln[4:]
        elif ln == "BEGIN:VALARM" and current:
            by_uid[current] = True
    assert by_uid[event_uid("case-a", "as_appeal_decision")] is True
    assert event_uid("case-a", "soyu_cassation") not in by_uid


def test_ics_escaping_and_folding_and_crlf():
    weird = _state(case='case;мыло, "x"\nend', key="soyu_appeal_ruling",
                   due="2026-11-10",
                   evidence="вручение; копия, том 2\\лист 7 — очень длинное "
                            "доказательство, которое гарантированно расползается "
                            "за предельные 75 октетов и обязано фолдиться без разрыва букв")
    text, events = build_ics([weird], today=FIX_TODAY, days=60)
    raw = text.encode("utf-8")
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")
    for ln in text.split("\r\n"):
        assert len(ln.encode("utf-8")) <= 75, ln
    lines = _unfold(raw)
    summary = _props(lines, "SUMMARY")[0]
    assert r"\;" in summary and r"\," in summary, "символы ; , экранированы"
    desc = _props(lines, "DESCRIPTION")[0]
    assert r"\\лист" in desc
    # разворот восстановил исходную строку целиком (фолдинг не потерял данных)
    assert "75 октетов" in desc.replace("\n", "")


def test_ics_empty_is_valid_and_waived_excluded():
    text, events = build_ics([], today=FIX_TODAY, days=60)
    lines = _unfold(text.encode("utf-8"))
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-1] == "END:VCALENDAR"
    assert events == [] and "BEGIN:VEVENT" not in lines
    # waived / без due_at не экспортируются
    skipped = [_state(state="waived", sid=5),
               _state(key="kas_cassation", due=None, sid=6)]
    text, events = build_ics(skipped, today=FIX_TODAY, days=60)
    assert events == []


def test_ics_uid_stable_and_output_idempotent():
    states = [_state(), _state(key="soyu_appeal_decision", due="2026-12-20", sid=2)]
    t1, e1 = build_ics(states, today=FIX_TODAY, days=60)
    t2, e2 = build_ics(list(reversed(states)), today=FIX_TODAY, days=60)
    assert t1 == t2, "порядок входа не должен менять вывод (DTSTAMP детерминирован)"
    uids = [e["uid"] for e in e1]
    assert sorted(uids) == sorted([event_uid("case-a", "as_appeal_decision"),
                                   event_uid("case-a", "soyu_appeal_decision")])


def test_ics_cli_end_to_end_with_out(tmp_path):
    env = {"SUD_CLI_STATE_DIR": str(tmp_path)}
    code, out, err = run_cli(["--json", "case", "new", "--number", "S28-1/2026",
                              "--court", "АС г. Москвы", "--plaintiff", "П",
                              "--defendant", "Д"], env=env)
    assert code == 0, err
    case_id = json.loads(out)["data"]["case_id"]
    ev = (date.today() - timedelta(days=1)).isoformat()
    code, out, err = run_cli(["--json", "deadlines", "anchor", case_id, "--key",
                              "as_court_order_cancel", "--event-date", ev,
                              "--method", "почта", "--evidence", "конверт"], env=env)
    assert code == 0, err
    code, out, err = run_cli(["--json", "deadlines", "confirm", case_id, "--key",
                              "as_court_order_cancel", "--by", "оператор"], env=env)
    assert code == 0, err
    out_file = tmp_path / "out" / "cases.ics"
    code, out, err = run_cli(["--json", "calendar", "ics", "--days", "60",
                              "--out", str(out_file)], env=env)
    assert code == 0, err
    data = json.loads(out)["data"]
    assert data["count"] == 1
    assert out_file.read_bytes() == data["ics"].encode("utf-8")
    lines = _unfold(out_file.read_bytes())
    assert "BEGIN:VALARM" in lines, "confirmed-срок обязан нести напоминание"
