# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""S3-11: timeline_audit — толерантность к битым timestamp, доследственная
проверка не ложный CRITICAL, позитивные сценарии."""
from __future__ import annotations

from sud_cli.timeline_audit import audit_timeline

VUD = {"id": "e1", "timestamp": "2024-03-01T10:00:00", "action": "вуд", "doc_type": "vud"}


def _codes(payload):
    return [a["code"] for a in payload["anomalies"]]


def test_broken_timestamp_does_not_crash_audit():
    events = [VUD, {"id": "e2", "timestamp": "not-a-date", "action": "обыск"}]
    payload = audit_timeline(events)
    assert payload["status"] == "ok"
    assert "TIMESTAMP_INVALID" in _codes(payload)


def test_missing_timestamp_does_not_crash_audit():
    payload = audit_timeline([VUD, {"id": "e2", "action": "обыск"}])
    assert payload["status"] == "ok"
    assert "TIMESTAMP_INVALID" in _codes(payload)


def test_pre_initiation_search_still_flagged():
    events = [VUD, {"id": "e2", "timestamp": "2024-03-01T09:00:00",
                    "action": "обыск", "doc_type": "search"}]
    payload = audit_timeline(events)
    assert "PRE_INITIATION_ACTION" in _codes(payload)


def test_predosledstvennaya_expertise_not_flagged():
    # экспертиза в рамках доследственной проверки до ВУД — законна (ст. 144 УПК)
    events = [VUD, {"id": "e2", "timestamp": "2024-03-01T09:00:00",
                    "action": "экспертиза при доследственной проверке"}]
    payload = audit_timeline(events)
    assert "PRE_INITIATION_ACTION" not in _codes(payload)


def test_detention_protocol_delay_detected():
    events = [
        {"id": "d1", "timestamp": "2024-03-01T10:00:00", "action": "фактическое задержание",
         "participant": "Иванов"},
        {"id": "d2", "timestamp": "2024-03-01T14:30:00", "action": "протокол задержания",
         "participant": "Иванов"},
    ]
    payload = audit_timeline(events)
    assert "DETENTION_PROTOCOL_DELAY" in _codes(payload)


def test_clean_timeline_no_anomalies():
    events = [VUD, {"id": "e2", "timestamp": "2024-03-02T10:00:00", "action": "обыск"}]
    payload = audit_timeline(events)
    assert payload["anomalies_count"] == 0


def test_custody_chain_broken_detected():
    events = [
        {"id": "c1", "timestamp": "2024-03-01T10:00:00", "action": "seizure",
         "evidence_id": " knife", "custodian": "A"},
        {"id": "c2", "timestamp": "2024-03-02T10:00:00", "action": "transfer",
         "evidence_id": " knife", "custodian": "B"},
    ]
    payload = audit_timeline(events)
    assert "CHAIN_OF_CUSTODY_BROKEN" in _codes(payload)
