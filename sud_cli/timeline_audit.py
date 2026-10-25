"""Deterministic checks for procedural case timelines."""

from collections import defaultdict
from datetime import datetime


def _time(event):
    return datetime.fromisoformat(str(event["timestamp"]).replace("Z", "+00:00"))


def _anomaly(code, severity, title, detail, basis, ids):
    return {"code": code, "severity": severity, "title": title, "detail": detail,
            "legal_basis": basis, "event_ids": list(dict.fromkeys(ids))}


def audit_timeline(events):
    events = list(events)
    ordered = sorted(events, key=_time)
    anomalies = []
    initiation = next((e for e in ordered if e.get("action", "").lower() in {
        "initiation", "case_initiated", "возбуждение уголовного дела", "вуд"
    } or e.get("doc_type", "").lower() in {"vud", "initiation"}), None)
    if initiation:
        for event in ordered:
            action = f"{event.get('action', '')} {event.get('doc_type', '')}".lower()
            if _time(event) < _time(initiation) and any(word in action for word in
                    ("search", "обыск", "interrogation", "допрос", "seizure", "выемка", "экспертиз")):
                if not any(word in action for word in ("inspection", "осмотр места происшествия")):
                    anomalies.append(_anomaly("PRE_INITIATION_ACTION", "CRITICAL",
                        "Следственное действие до возбуждения дела",
                        f"{event.get('action', event.get('doc_type', ''))} проведено до ВУД",
                        "ст. 144, 146 УПК РФ", [event.get("id", ""), initiation.get("id", "")]))

    delivered = {}
    for event in ordered:
        action = f"{event.get('action', '')} {event.get('doc_type', '')}".lower()
        if any(word in action for word in ("доставлен", "delivered", "фактическое задержание")):
            delivered[event.get("participant", event.get("suspect", ""))] = event
        if "протокол задержания" in action or "detention_protocol" in action:
            key = event.get("participant", event.get("suspect", ""))
            prior = delivered.get(key)
            if prior and (_time(event) - _time(prior)).total_seconds() > 3 * 3600:
                anomalies.append(_anomaly("DETENTION_PROTOCOL_DELAY", "HIGH",
                    "Протокол задержания составлен с опозданием",
                    "между доставлением и протоколом прошло более 3 часов",
                    "ч. 1 ст. 92 УПК РФ", [prior.get("id", ""), event.get("id", "")]))

    witness_events = defaultdict(list)
    for event in events:
        for witness in event.get("witnesses", []) or []:
            witness_events[str(witness)].append(event)
        address = event.get("location") or event.get("object_address")
        for witness in event.get("witness_details", []) or []:
            if address and witness.get("address") == address:
                anomalies.append(_anomaly("REPEATED_OR_BIASED_WITNESS", "HIGH",
                    "Понятой может быть заинтересованным",
                    "адрес понятого совпадает с адресом объекта",
                    "ст. 60 УПК РФ", [event.get("id", "")]))
    for witness, rows in witness_events.items():
        if len(rows) > 1:
            anomalies.append(_anomaly("REPEATED_OR_BIASED_WITNESS", "MEDIUM",
                "Повторное участие понятого",
                f"лицо {witness} указано в {len(rows)} процессуальных событиях",
                "ст. 60 УПК РФ", [row.get("id", "") for row in rows]))

    custody = defaultdict(list)
    for event in ordered:
        evidence = event.get("evidence_id")
        if evidence:
            custody[evidence].append(event)
    for evidence, rows in custody.items():
        for previous, current in zip(rows, rows[1:]):
            if previous.get("custodian") and current.get("custodian") and previous["custodian"] != current["custodian"]:
                documented = current.get("transfer_document") or current.get("transfer_doc_id") or current.get("document_id")
                if not documented:
                    anomalies.append(_anomaly("CHAIN_OF_CUSTODY_BROKEN", "CRITICAL",
                        "Разрыв цепочки хранения вещдока",
                        f"{evidence}: {previous['custodian']} передал без документа {current['custodian']}",
                        "ст. 81, 82 УПК РФ", [previous.get("id", ""), current.get("id", "")]))
    return {"status": "ok", "anomalies_count": len(anomalies), "anomalies": anomalies}
