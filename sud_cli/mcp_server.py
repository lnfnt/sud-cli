"""sud-cli MCP stdio server: JSON-RPC 2.0 tool surface.

v2.2.0 (боевая рецензия colleague-legal, A2A 001a16046ebe, 21.10):
- типизированные inputSchema у всех инструментов (properties/required/enum/
  default) вместо единого {"type":"object"} — агент видит сигнатуры;
- additionalProperties=false: неизвестный параметр — явная ошибка, а не
  молчаливое игнорирование (fail-closed);
- выведены недостающие тулзы: case_history, payment, deferral,
  efrsb_companies/persons/publications/managers, sudrf_live, mos_live
  (через case-search bridge; без SUD_CLI_BRIDGE_LIVE возвращают
  status=blocked и не порождают дочерних процессов).

v2.3.0 (письмо A2A colleague-legal 001a1608, 21.10):
- sud_cli_case_diff (первичное по письму, вариант «в»): offline-дифф двух
  сохранённых snapshots одного дела (sud_cli.case_diff.diff_case_snapshots,
  тот же код, что в CLI 'case-search case-diff');
- sud_cli_npa_compare (аддитивно, вариант «а»): сравнение двух редакций
  сохранённого НПА-фикстура (sud_cli.db.compare_npa_editions);
- доменные ошибки DiffError/SearchError конвертируются в ValueError внутри
  _call: handle_request ловит только (KeyError, TypeError, ValueError,
  OSError), поэтому без конверсии serve_stdio падал бы вместо того, чтобы
  вернуть клиенту isError=True с текстом ошибки.

v2.3.1 (боевой аудит colleague-legal, ТЗ task-brief, письмо A2A 001a1609):
- sud_cli_fees: claim_amount объявлен number (integer терял копейки), база
  расчёта доходит до движка как Decimal; имя вида пошлины принимается и как
  fee_kind (имя движка), и как kind (CLI --kind) — required через anyOf;
- CLI-алиасы объявлены в схемах там, где вызов по CLI-имени падал: fees
  applicant, limitation type/years/months, interest start_date/end_date,
  case_history case_id, efrsb_persons name;
- два имени с разными значениями — явная ошибка, а не молчаливый выбор одной
  стороны (_pick);
- enum'ы приведены к значениям, которые действительно принимает движок:
  юрисдикции пошлин (arbitration|general_jurisdiction|kas, алиасы
  arbitral/soyu распознаёт хендлер), секции sudact, applicant, виды пошлины;
- serverInfo.version больше не константа, а sud_cli.__version__ — раньше
  версия сервера расходилась с пакетом молча.

v2.11.0 (task-briefa §1, S-80): MCP/CLI паритет + права + идемпотентность:
- каждый инструмент несёт поле permission: read|write (источник — WRITE_TOOLS,
  единый для всех 45); write-тулы требуют явный confirm: true (без него
  status=needs_confirmation, запись не производится) и принимают
  optional idempotency_key (реестр state-dir/idempotency.json, окно 10 мин,
  повтор = первый результат + idempotent_replay=true в теле);
- +10 офлайн-утилитных тулов паритета (anchor/confirm/waive/list/audit/
  autofill, calendar agenda/ics, reminders due, case-search alerts);
  ни один сетевой live-тул в этой волне не добавлен;
- mcp_exposed_commands()/TOOL_CLI_PATH — карта паритета против дерева CLI,
  live-тулы помечены LIVE_GATED_TOOLS и не входят в обязательный список.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import __version__


DATE = {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "description": "ISO date YYYY-MM-DD"}

#: Виды пошлины из legal_rules.json (engine-имена; CLI вызывает их как --kind).
FEE_KINDS = ["property_claim", "court_order", "non_property_claim", "appeal",
             "cassation_first", "cassation_supreme", "bankruptcy_petition",
             "bankruptcy_creditor_claim"]

#: Юрисдикции: реальные ключи legal_rules.json. CLI знает только их; схема до
#: v2.3.1 рекламировала arbitral/soyu/constitutional, которых в движке нет.
FEE_JURISDICTIONS = ["arbitration", "general_jurisdiction", "kas"]

#: Алиасы юрисдикции для вызова по MCP (обратная совместимость со старым enum).
JURISDICTION_ALIASES = {
    "arbitral": "arbitration", "as": "arbitration",
    "soyu": "general_jurisdiction", "general": "general_jurisdiction",
    "kas": "kas",
}

#: Канонические секции sudact (normalize_section); 'general' движок не знает.
SUDACT_SECTIONS = ["arbitral", "regular", "magistrate", "vsrf"]

TOOLS: list[dict[str, Any]] = [
    {"name": "sud_cli_limitation", "description": "Calculate a civil limitation period.",
     "schema": {"type": "object", "properties": {
         "start": {**DATE, "description": "Начало течения срока"},
         "period_type": {"type": "string", "default": "general", "description": "Тип срока (general/special/custom_years и др.); CLI-алиас: type"},
         "type": {"type": "string", "description": "CLI-алиас period_type (--type)"},
         "claim_notice_days": {"type": "integer", "default": 0, "description": "Дни досудебного порядка (+30)"},
         "check_date": {**DATE, "description": "Дата проверки (default сегодня)"},
         "custom_years": {"type": "integer", "default": 0, "description": "Лет (period_type=custom_years); CLI-алиас: years"},
         "custom_months": {"type": "integer", "default": 0, "description": "Месяцев; CLI-алиас: months"},
         "years": {"type": "integer", "description": "CLI-алиас custom_years (--years)"},
         "months": {"type": "integer", "description": "CLI-алиас custom_months (--months)"}},
         "required": ["start"], "additionalProperties": False}},
    {"name": "sud_cli_deadlines", "description": "Calculate a procedural deadline.",
     "schema": {"type": "object", "properties": {
          "deadline_key": {"type": "string", "description": "Ключ из утверждённой матрицы сроков"},
          "event_date": {**DATE}, "region": {"type": "string"}, "tz": {"type": "string"},
          "case_id": {"type": "string"},
          "deadlines_command": {"type": "string"},
          "confirm": {"type": "boolean"}, "by": {"type": "string"},
          "evidence": {"type": "string"}, "norm": {"type": "string"},
          "waive": {"type": "boolean"}},
          "required": ["deadline_key"], "additionalProperties": False}},
    {"name": "sud_cli_deadlines_matrix", "description": "Deadline matrix (control points) for a case.",
     "schema": {"type": "object", "properties": {
          "case_id": {"type": "string", "description": "Case id; CLI-алиас: id"},
          "id": {"type": "string", "description": "CLI-алиас case_id"},
          "instance": {"type": "string", "enum": ["apk", "gpk", "kas"], "description": "Override инстанции"},
          "as_of": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "description": "Дата as-of (не влияет на расчёт)"},
          "missing": {"type": "boolean", "description": "Только точки needs_anchor"}},
         "anyOf": [{"required": ["case_id"]}, {"required": ["id"]}],
         "additionalProperties": False}},

    {"name": "sud_cli_fees", "description": "Calculate a state fee.",
     "schema": {"type": "object", "properties": {
         "jurisdiction": {"type": "string", "enum": FEE_JURISDICTIONS,
                          "description": "Юрисдикция (CLI --jurisdiction); хендлер принимает также алиасы arbitral/as→arbitration, soyu/general→general_jurisdiction"},
         "fee_kind": {"type": "string", "enum": FEE_KINDS, "description": "Вид пошлины из правил 259-ФЗ (имя движка); CLI-алиас: kind"},
         "kind": {"type": "string", "enum": FEE_KINDS, "description": "CLI-алиас fee_kind (--kind)"},
         "applicant_type": {"type": "string", "enum": ["individual", "entity"], "description": "Обязателен для фиксированных пошлин: физлицо/организация; CLI-алиас: applicant"},
         "applicant": {"type": "string", "enum": ["individual", "entity"], "description": "CLI-алиас applicant_type (--applicant)"},
         "claim_amount": {"type": "number", "description": "Цена иска в рублях; копейки сохраняются (Decimal), округление п. 6 ст. 52 НК РФ — только на итоге"},
         "claim_class": {"type": "string"},
         "as_of": {"type": "string", "format": "date",
                   "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
                   "description": "Тариф на дату (ISO YYYY-MM-DD): 2015-01-01..2024-09-08 — редакция 221-ФЗ, с 2024-09-09 — действующая 259-ФЗ; раньше 2015-01-01 — действующий тариф с warning в meta. Без поля считается по действующему тарифу (прежний контракт)"},
         },
         "required": ["jurisdiction"],
         "anyOf": [{"required": ["fee_kind"]}, {"required": ["kind"]}],
         "additionalProperties": False}},
    {"name": "sud_cli_interest", "description": "Calculate statutory interest.",
     "schema": {"type": "object", "properties": {
         "amount": {"type": "number", "description": "Остаток долга (проценты на просрочку)"},
         "start": {**DATE, "description": "Начало периода; CLI-алиас: start_date"},
         "end": {**DATE, "description": "Конец периода; CLI-алиас: end_date"},
         "start_date": {**DATE, "description": "CLI-алиас start (--from/--start)"},
         "end_date": {**DATE, "description": "CLI-алиас end (--to/--end)"},
         "article": {"type": "string", "enum": ["395", "317.1"], "default": "395"},
         "payments": {"type": "array", "description": "Частичные погашения [{date, amount}]"}},
         "required": ["amount"],
         "anyOf": [{"required": ["start", "end"]}, {"required": ["start_date", "end_date"]}],
         "additionalProperties": False}},
    {"name": "sud_cli_practice_search", "description": "Search judicial practice (sudact).",
     "schema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "section": {"type": "string", "enum": SUDACT_SECTIONS, "default": "arbitral"},
         "page": {"type": "integer", "default": 1}, "limit": {"type": "integer", "default": 10}},
         "required": ["query"], "additionalProperties": False}},
    {"name": "sud_cli_kad_search", "description": "Search KAD by participant.",
     "schema": {"type": "object", "properties": {
         "inn": {"type": "string"}, "ogrn": {"type": "string"}, "name": {"type": "string"},
         "page": {"type": "integer", "default": 1}},
         "anyOf": [{"required": ["inn"]}, {"required": ["ogrn"]}, {"required": ["name"]}],
         "additionalProperties": False}},
    {"name": "sud_cli_timeline_audit", "description": "Detect procedural timeline anomalies.",
     "schema": {"type": "object", "properties": {
         "events": {"type": "array", "items": {"type": "object"},
                    "description": "Хронология: [{timestamp (ISO, обязателен), action|doc_type, id, ...}] — движок читает event['timestamp']"}},
         "required": ["events"], "additionalProperties": False}},
    {"name": "sud_cli_npa_as_of", "description": "Get a historical legal norm edition.",
     "schema": {"type": "object", "properties": {
         "act": {"type": "string", "description": "Акт: gk_rf | uk_rf | nk_rf (ключи data/npa_historical_editions.json)"},
         "article": {"type": "string"}, "date": {**DATE}},
         "required": ["act", "article", "date"], "additionalProperties": False}},
    {"name": "sud_cli_verify_quote", "description": "Verify a quoted fragment against npa bundle / case events / text file (verdict exact|fuzzy|not_found|edition_unavailable; правило «тексты только из реестра» — без «ok на глаз»).",
     "schema": {"type": "object", "properties": {
         "text": {"type": "string", "description": "Цитируемый фрагмент (не пустой)"},
         "source": {"type": "string", "enum": ["npa", "case", "file"]},
         "act": {"type": "string", "description": "source=npa: ключ бандла (gk_rf|uk_rf|nk_rf)"},
         "article": {"type": "string"}, "date": {**DATE},
         "case_id": {"type": "string", "description": "source=case: id дела в локальном реестре"},
         "path": {"type": "string", "description": "source=file: plain-text файл"},
         "max_fuzzy": {"type": "number", "minimum": 0, "maximum": 1, "default": 0,
                       "description": "0 = fuzzy выключен; порог ratio >= 1-max_fuzzy"}},
         "required": ["text", "source"], "additionalProperties": False}},
    {"name": "sud_cli_case_history", "description": "Case fate: all instances of a case number via sudact.",
     "schema": {"type": "object", "properties": {
         "case_number": {"type": "string", "description": "Номер дела; CLI-алиас: case_id"},
         "case_id": {"type": "string", "description": "CLI-алиас case_number (позиционный аргумент case-history)"},
         "section": {"type": "string", "enum": SUDACT_SECTIONS, "default": "arbitral"},
         "max_related": {"type": "integer", "default": 5}, "limit": {"type": "integer", "default": 10}},
         "anyOf": [{"required": ["case_number"]}, {"required": ["case_id"]}],
         "additionalProperties": False}},
    {"name": "sud_cli_payment", "description": "Payment requisites and purpose for a state fee.",
     "schema": {"type": "object", "properties": {
         "jurisdiction": {"type": "string"}, "amount": {"type": "integer"},
         "court": {"type": "string"}, "defendant": {"type": "string"}},
         "required": ["jurisdiction", "amount", "court", "defendant"], "additionalProperties": False}},
    {"name": "sud_cli_deferral", "description": "Draft a motion to defer the state fee.",
     "schema": {"type": "object", "properties": {
         "applicant": {"type": "string"}, "court": {"type": "string"},
         "amount": {"type": "integer"}, "reason": {"type": "string"}},
         "required": ["applicant", "court", "amount", "reason"], "additionalProperties": False}},
    {"name": "sud_cli_fssp_check_plan", "description": "FSSP BDIP manual-check plan: URL, params, checklist (human solves captcha; no network here).",
     "schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["fiz", "ur"]},
         "name": {"type": "string"}, "inn": {"type": "string"}, "ogrn": {"type": "string"},
         "birth_date": {"type": "string"}, "region": {"type": "string"}},
         "required": ["kind"], "additionalProperties": False}},
    {"name": "sud_cli_fssp_record", "description": "Record a manual FSSP check result into case evidence registry (provenance: who/when/result).",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string"}, "subject": {"type": "string"},
         "result": {"type": "string", "enum": ["none", "found", "inconclusive"]},
         "checked_by": {"type": "string"}, "notes": {"type": "string"},
         "file": {"type": "string", "description": "path to screenshot/PDF evidence"}},
         "required": ["case_id", "subject", "result", "checked_by"], "additionalProperties": False}},
    {"name": "sud_cli_fssp_stats", "description": "Official FSSP BDIP aggregate statistics (public CSV, reference data).",
     "schema": {"type": "object", "properties": {"region": {"type": "string"}},
                "additionalProperties": False}},
    {"name": "sud_cli_efrsb_link", "description": "EFRSB manual-check plan for humans (act 25.10): valid search link + steps; KAD stays the automated primary path. No network.",
     "schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["ul", "ip", "fl"]},
         "name": {"type": "string"}, "inn": {"type": "string"}, "ogrn": {"type": "string"}},
         "required": ["kind"], "additionalProperties": False}},
    {"name": "sud_cli_plenum_search", "description": "Search the VS/VAS rulings corpus (plenum/obzor paragraphs with sha256-verified quotes).",
     "schema": {"type": "object", "properties": {"text": {"type": "string"}},
                "required": ["text"], "additionalProperties": False}},
    {"name": "sud_cli_license_info", "description": "AGPL section 13 interface: license, build mode, source retrieval, build_flags (no network).",
     "schema": {"type": "object", "properties": {
         "with_build_flags": {"type": "boolean", "default": True,
                           "description": "включить HMAC-build_flag метки сборки (пусто без SUD_BUILD_SECRET)"}},
         "additionalProperties": False}},
    {"name": "sud_cli_egrul_search", "description": "EGRYUL/EGRIP open counterparty check via egrul.nalog.ru (FNS public registry).",
     "schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "ИНН, ОГРН или наименование"},
         "name_eq": {"type": "boolean", "description": "точное соответствие наименования", "default": False},
         "rows_hint": {"type": "integer", "default": 1}}, "additionalProperties": False}},
    {"name": "sud_cli_efrsb_companies", "description": "EFRSB bankruptcy search for legal entities/IP.",
     "schema": {"type": "object", "properties": {
         "inn": {"type": "string"}, "ogrn": {"type": "string"}, "name": {"type": "string"},
         "limit": {"type": "integer", "default": 5}}, "additionalProperties": False}},
    {"name": "sud_cli_efrsb_persons", "description": "EFRSB bankruptcy search for individuals.",
     "schema": {"type": "object", "properties": {
         "full_name": {"type": "string", "description": "ФИО; CLI-алиас: name"},
         "name": {"type": "string", "description": "CLI-алиас full_name (--name)"},
         "birth_date": {**DATE}, "limit": {"type": "integer", "default": 5}},
         "anyOf": [{"required": ["full_name"]}, {"required": ["name"]}],
         "additionalProperties": False}},
    {"name": "sud_cli_efrsb_publications", "description": "EFRSB publications for a bankruptcy case.",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string"}, "limit": {"type": "integer", "default": 10}},
         "required": ["case_id"], "additionalProperties": False}},
    {"name": "sud_cli_efrsb_managers", "description": "EFRSB arbitration managers lookup.",
     "schema": {"type": "object", "properties": {
         "search": {"type": "string"}, "inn": {"type": "string"}, "sro": {"type": "string"},
         "limit": {"type": "integer", "default": 5}}, "additionalProperties": False}},
    {"name": "sud_cli_sudrf_live", "description": "Look up a GAS Pravosudie case (bridge; requires SUD_CLI_BRIDGE_LIVE).",
     "schema": {"type": "object", "properties": {"case_id": {"type": "string"}},
         "required": ["case_id"], "additionalProperties": False}},
    {"name": "sud_cli_mos_live", "description": "Look up a Moscow court case (bridge; requires SUD_CLI_BRIDGE_LIVE).",
     "schema": {"type": "object", "properties": {"case_id": {"type": "string"}, "court_slug": {"type": "string"}},
         "required": ["case_id"], "additionalProperties": False}},
    {"name": "sud_cli_case_diff", "description": "Diff two saved snapshots of the same case (offline, no court contact).",
     "schema": {"type": "object", "properties": {
         "old": {"type": "string", "description": "Path to earlier snapshot JSON"},
         "new": {"type": "string", "description": "Path to later snapshot JSON"},
         "old_snapshot": {"type": "string"}, "new_snapshot": {"type": "string"}},
         "anyOf": [{"required": ["old", "new"]}, {"required": ["old_snapshot", "new_snapshot"]}],
         "additionalProperties": False}},
    {"name": "sud_cli_npa_compare", "description": "Compare two editions of a saved NPA fixture.",
     "schema": {"type": "object", "properties": {
         "doc_id": {"type": "string"}, "edition1": {"type": "string"}, "edition2": {"type": "string"}},
         "required": ["doc_id", "edition1", "edition2"], "additionalProperties": False}},
    {"name": "sud_cli_case_new", "description": "Create a case in the local registry.",
     "schema": {"type": "object", "properties": {
         "number": {"type": "string"}, "case_number": {"type": "string"},
         "court": {"type": "string"},
         "plaintiff": {"type": "string"}, "defendant": {"type": "string"},
         "owner": {"type": "string"}, "status": {"type": "string"}},
         "anyOf": [{"required": ["number"]}, {"required": ["case_number"]}],
         "required": ["court", "plaintiff", "defendant"], "additionalProperties": False}},
    {"name": "sud_cli_case_set", "description": "Update case fields.",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string"}, "id": {"type": "string"},
         "status": {"type": "string"}, "owner": {"type": "string"}, "closed_at": {"type": "string"}},
         "anyOf": [{"required": ["case_id"]}, {"required": ["id"]}],
         "additionalProperties": False}},
    {"name": "sud_cli_case_list", "description": "List cases with filters.",
     "schema": {"type": "object", "properties": {
         "status": {"type": "string"}, "owner": {"type": "string"},
         "court": {"type": "string"}, "party": {"type": "string"}},
         "additionalProperties": False}},
    {"name": "sud_cli_case_card", "description": "Aggregate case card.",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string"}, "id": {"type": "string"}},
         "anyOf": [{"required": ["case_id"]}, {"required": ["id"]}],
         "additionalProperties": False}},
    {"name": "sud_cli_case_amount_add", "description": "Add an amount to a case.",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string"}, "id": {"type": "string"},
         "kind": {"type": "string"},
         "amount": {"type": ["number", "string"]}, "note": {"type": "string"}},
         "anyOf": [{"required": ["case_id"]}, {"required": ["id"]}],
         "required": ["kind", "amount"], "additionalProperties": False}},
    {"name": "sud_cli_case_doc_add", "description": "Add a document reference to a case.",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string"}, "id": {"type": "string"},
         "path": {"type": "string"}, "title": {"type": "string"}},
         "anyOf": [{"required": ["case_id"]}, {"required": ["id"]}],
         "required": ["path"], "additionalProperties": False}},
    {"name": "sud_cli_case_note_add", "description": "Add a note to a case.",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string"}, "id": {"type": "string"},
         "text": {"type": "string"}, "author": {"type": "string"}},
         "anyOf": [{"required": ["case_id"]}, {"required": ["id"]}],
         "required": ["text"], "additionalProperties": False}},
    {"name": "sud_cli_conflict_check", "description": "Conflict-of-interest heuristic over the local case registry (name-exact).",
     "schema": {"type": "object", "properties": {
         "plaintiff": {"type": "string"}, "defendant": {"type": "string"},
         "court": {"type": "string"}, "case_number": {"type": "string"},
         "case_id": {"type": "string"}, "id": {"type": "string"},
         "parties": {"type": "array", "items": {"type": "string"}}},
         "additionalProperties": False}},
    {"name": "sud_cli_jurisdiction_check", "description": "Jurisdiction venue helper over VERIFIED rules.",
     "schema": {"type": "object", "properties": {
         "code": {"type": "string", "enum": ["apk", "gpk", "kas"]},
         "claim": {"type": "string"},
         "party_org": {"type": "boolean"}, "party_phys": {"type": "boolean"},
         "unknown_rf_location": {"type": "boolean"}, "immovable": {"type": "boolean"},
         "corporate": {"type": "boolean"}, "bankruptcy": {"type": "boolean"},
         "ip_rights": {"type": "boolean"}, "contract_forum": {"type": "boolean"},
         "arbitration_clause": {"type": "boolean"}, "multiple_defendants": {"type": "boolean"},
         "foreign_element": {"type": "boolean"}, "consumer": {"type": "boolean"},
         "alimony": {"type": "boolean"}},
         "required": ["code", "claim"], "additionalProperties": False}},
    {"name": "sud_cli_fee_exemption", "description": "Fee exemption check over VERIFIED category sample (333.35-37).",
     "schema": {"type": "object", "properties": {
         "jurisdiction": {"type": "string", "enum": ["apk", "gpk", "kas", "arbitration", "general_jurisdiction"]},
         "category": {"type": "string"}},
         "required": ["jurisdiction", "category"], "additionalProperties": False}},
    {"name": "sud_cli_fee_refund", "description": "Fee refund per 333.40 VERIFIED grounds with share/conditions.",
     "schema": {"type": "object", "properties": {
         "paid": {"type": "string"}, "ground": {"type": "string"},
         "claim_admitted": {"type": "boolean"}, "plaintiff_default": {"type": "boolean"},
         "transferred_to_arbitration": {"type": "boolean"}},
         "required": ["paid", "ground"], "additionalProperties": False}},
    {"name": "sud_cli_fee_indexation", "description": "Indexation frame (183/208/189.1) or compute with operator CPI.",
     "schema": {"type": "object", "properties": {
         "code": {"type": "string", "enum": ["apk", "gpk", "kas"]},
         "amount": {"type": "string"}, "cpi_start": {"type": "string"}, "cpi_end": {"type": "string"}},
         "required": ["code"], "additionalProperties": False}},
    {"name": "sud_cli_pretrial_calc", "description": "Pretrial response deadline calc.",
     "schema": {"type": "object", "properties": {
         "code": {"type": "string", "enum": ["apk", "zozpp", "osago"]},
         "sent": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"}},
         "required": ["code", "sent"], "additionalProperties": False}},
    {"name": "sud_cli_pretrial_consequences", "description": "Consequences of noncompliance with pretrial order.",
     "schema": {"type": "object", "properties": {
         "code": {"type": "string", "enum": ["apk", "gpk", "kas"]},
         "stage": {"type": "string", "enum": ["filing", "trial", "movement"]}},
         "required": ["code", "stage"], "additionalProperties": False}},

    # ---------------- W6a §1/§3/§4/§5: паритет недостающих утилитных команд ----
    # Live-сетевые команды в этой волне НЕ экспонированы: новые тулзы только
    # локальные (state-dir + rules), сеть запрещена контрактом task-briefa.
    {"name": "sud_cli_deadlines_anchor", "description": "Anchor deadline start date with evidence (write: state=computed).",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string"},
         "key": {"type": "string", "description": "Ключ из утверждённой матрицы сроков"},
         "event_date": {**DATE},
         "method": {"type": "string", "enum": ["вручение", "публикация", "почта", "иное"]},
         "evidence": {"type": "string"},
         "by": {"type": "string", "description": "Актор (по умолчанию 'mcp')"}},
         "required": ["case_id", "key", "event_date", "method", "evidence"],
         "additionalProperties": False}},
    {"name": "sud_cli_deadlines_confirm", "description": "Confirm (or waive) a deadline state (write).",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string"},
         "key": {"type": "string"},
         "by": {"type": "string"},
         "evidence": {"type": "string"},
         "waive": {"type": "boolean", "default": False},
         "norm": {"type": "string", "description": "Норма-ссылка (обязательна при waive)"}},
         "required": ["case_id", "key", "by"],
         "additionalProperties": False}},
    {"name": "sud_cli_deadlines_waive", "description": "Waive a deadline with norm reference (write).",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string"},
         "key": {"type": "string"},
         "by": {"type": "string"},
         "norm": {"type": "string"},
         "evidence": {"type": "string"}},
         "required": ["case_id", "key", "by", "norm"],
         "additionalProperties": False}},
    {"name": "sud_cli_deadlines_list", "description": "Deadline states history (optionally per case).",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string", "description": "Идентификатор дела; без него — все состояния"}},
         "additionalProperties": False}},
    {"name": "sud_cli_deadlines_audit", "description": "Deadline edit audit log for a case.",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string"}},
         "required": ["case_id"], "additionalProperties": False}},
    {"name": "sud_cli_deadlines_autofill", "description": "Autofill deadline points from act-text markers (write; computed+needs_confirm only, never confirmed).",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string"},
         "event": {"type": "string", "description": "Обработать только этот event_id"}},
         "required": ["case_id"], "additionalProperties": False}},
    {"name": "sud_cli_calendar_agenda", "description": "Unified agenda of deadlines and hearings.",
     "schema": {"type": "object", "properties": {
         "days": {"type": "integer", "default": 14, "minimum": 1, "maximum": 365}},
         "additionalProperties": False}},
    {"name": "sud_cli_calendar_ics", "description": "RFC 5545 VCALENDAR of computed/confirmed deadlines (text only; no file writes).",
     "schema": {"type": "object", "properties": {
         "days": {"type": "integer", "default": 60, "minimum": 1, "maximum": 3650}},
         "additionalProperties": False}},
    {"name": "sud_cli_reminders_due", "description": "Deadlines in window + ready bus-free a2a strings (sud never calls the bus).",
     "schema": {"type": "object", "properties": {
         "days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 90}},
         "additionalProperties": False}},
    {"name": "sud_cli_case_search_alerts", "description": "Local case-search alerts -> bus-free a2a strings (offline artifacts only).",
     "schema": {"type": "object", "properties": {
         "days": {"type": "integer", "default": 7, "minimum": 1, "maximum": 90},
         "alerts_dir": {"type": "string", "description": "Каталог alert-артефактов (по умолчанию — локальные пути)"}},
         "additionalProperties": False}},
    # the wave §4 (S-70): структурный скелет формы = чистое чтение реестра + строка;
    # без сайд-эффектов => permission read (в WRITE_TOOLS не попадает).
    {"name": "sud_cli_form_new", "description": "Структурный скелет процессуальной формы по делу (md|json, плейсхолдеры, без нормативных цитат; read).",
     "schema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": ["vosstanovlenie_sroka", "otsrochka", "appeal_structure"]},
         "case_id": {"type": "string", "description": "Дело из локального реестра"},
         "format": {"type": "string", "enum": ["md", "json"], "default": "md"}},
         "required": ["kind", "case_id"], "additionalProperties": False}},
    # ---------------- the wave §1..§5: портфель, данные, ПДн (12 тулов) ----------------
    # Порядок read|write — по матрице ТЗ §5: только изменение состояния или
    # запись на диск получает write+confirm; счёт/чтение остаётся read.
    {"name": "sud_cli_deadlines_chain", "description": "Цепочка инстанций дела по ключам VERIFIED матрицы (read; без якоря — null+needs_anchor, дат не выдумываем).",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string"},
         "instance": {"type": "string", "enum": ["apk", "gpk", "kas"],
                      "description": "Override инстанции дела"}},
         "required": ["case_id"], "additionalProperties": False}},
    {"name": "sud_cli_case_rep_add", "description": "Добавить представителя дела (write; полномочия не проверяются — hint о ручной проверке в ответе).",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string"},
         "side": {"type": "string", "enum": ["plaintiff", "defendant"]},
         "name": {"type": "string"},
         "authority_note": {"type": "string"},
         "power_docs": {"type": "string", "description": "Свободное описание доверенности"}},
         "required": ["case_id", "side", "name"], "additionalProperties": False}},
    {"name": "sud_cli_case_rep_list", "description": "Список представителей дела (read; пусто = []).",
     "schema": {"type": "object", "properties": {"case_id": {"type": "string"}},
                "required": ["case_id"], "additionalProperties": False}},
    {"name": "sud_cli_calendar_conflicts", "description": "Пересечения слушаний (фикс. набор event_type) и сроков по разным делам (read; справочно).",
     "schema": {"type": "object", "properties": {
         "days": {"type": "integer", "default": 30, "minimum": 1, "maximum": 3650}},
         "additionalProperties": False}},
    {"name": "sud_cli_report_portfolio", "description": "Сводка портфеля: дела по статусам, near/overdue, суммы Decimal, представители (read; без выдуманных KPI).",
     "schema": {"type": "object", "properties": {
         "as_of": {"type": "string", "description": "Дата отсчёта YYYY-MM-DD"},
         "days": {"type": "integer", "default": 30, "minimum": 1, "maximum": 3650}},
         "additionalProperties": False}},
    {"name": "sud_cli_report_case", "description": "Развёртка дела: секции card + сводка матрицы + evidence verify (read; offline-пересчёт локальных path).",
     "schema": {"type": "object", "properties": {"case_id": {"type": "string"}},
                "required": ["case_id"], "additionalProperties": False}},
    {"name": "sud_cli_metrics", "description": "Метрики state-dir: счётчики из таблиц состояния, last backup из meta (read; тот же блок, что в doctor).",
     "schema": {"type": "object", "properties": {
         "near_days": {"type": "integer", "default": 30, "minimum": 1, "maximum": 3650,
                       "description": "Горизонт near для счётчика сроков"}},
         "additionalProperties": False}},
    {"name": "sud_cli_backup_create", "description": "Снять snapshot store.db (write: файл 0600 + sha256 sidecar на диске state-dir).",
     "schema": {"type": "object", "properties": {
         "out": {"type": "string", "description": "Путь снимка (по умолчанию state-dir/backups)"}},
         "additionalProperties": False}},
    {"name": "sud_cli_backup_verify", "description": "Проверить снимок: integrity_check + quick_check + таблицы схемы + sidecar (read).",
     "schema": {"type": "object", "properties": {"file": {"type": "string"}},
                "required": ["file"], "additionalProperties": False}},
    {"name": "sud_cli_backup_restore", "description": "Перезаписать store.db из снимка (write+confirm: деструктив, без confirm — только превью).",
     "schema": {"type": "object", "properties": {"file": {"type": "string"}},
                "required": ["file"], "additionalProperties": False}},
    {"name": "sud_cli_case_export", "description": "Экспорт дела для субъекта/юриста: полный дамп секций + pdn_policy дословно из правил (read; без сокращений).",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string"},
         "format": {"type": "string", "enum": ["json", "md"], "default": "json"}},
         "required": ["case_id"], "additionalProperties": False}},
    {"name": "sud_cli_case_purge", "description": "Уничтожить данные дела: каскад таблиц + файлы, в audit только факт без ПДн (write+confirm).",
     "schema": {"type": "object", "properties": {
         "case_id": {"type": "string"},
         "remove_files": {"type": "boolean", "default": True}},
         "required": ["case_id"], "additionalProperties": False}},
]


# ---------------- W6a §1 (S-80): permission + идемпотентность ----------------
#: state-changing-тулы. Всё остальное — read. Поле permission дописывается
#: КАЖДОЙ схеме тула (включая исторические 35): один источник правды — этот
#: набор, а не 45 ручных строк, которые расходятся при первом же новом туле.
#: write-тулы дополнительно получают в properties поля confirm/idempotency_key:
#: additionalProperties:false обязывает объявлять всё, что диспатч принимает.
WRITE_TOOLS = frozenset({
    # v2.15.0 S-61: внесение результата ручной проверки меняет реестр дела.
    "sud_cli_fssp_record",
    "sud_cli_case_new", "sud_cli_case_set", "sud_cli_case_amount_add",
    "sud_cli_case_doc_add", "sud_cli_case_note_add",
    "sud_cli_deadlines_anchor", "sud_cli_deadlines_confirm",
    "sud_cli_deadlines_waive", "sud_cli_deadlines_autofill",
    # the wave: запись представителя, снимок на диск, перезапись БД, уничтожение
    # данных дела — всё это меняет состояние, значит write + confirm.
    "sud_cli_case_rep_add", "sud_cli_backup_create",
    "sud_cli_backup_restore", "sud_cli_case_purge",
})

CONFIRM_PROPERTY = {
    "type": "boolean",
    "description": "write-инструмент: без confirm=true вернёт status=needs_confirmation, запись не производится (W6a §1)",
}
IDEMPOTENCY_PROPERTY = {
    "type": "string",
    "description": "повтор с тем же ключом в окне 10 минут возвращает первый результат с флагом idempotent_replay=true (реестр state-dir/idempotency.json, 0600)",
}


def _apply_permission_fields() -> None:
    for t in TOOLS:
        t["permission"] = "write" if t["name"] in WRITE_TOOLS else "read"
        if t["permission"] == "write":
            props = t["schema"].setdefault("properties", {})
            props.setdefault("confirm", dict(CONFIRM_PROPERTY))
            props.setdefault("idempotency_key", dict(IDEMPOTENCY_PROPERTY))


_apply_permission_fields()


#: Паритет MCP↔CLI (S-80): инструмент отвечает позиционной команде CLI.
#: Тест сверяет белый список офлайн-утилит против дерева argparse, а также
#: то, что каждый тул имеет CLI-аналог (обратная строна паритета).
TOOL_CLI_PATH: dict[str, tuple] = {
    "sud_cli_limitation": ("limitation",),
    "sud_cli_deadlines": ("deadlines", "calculate"),
    "sud_cli_deadlines_matrix": ("deadlines", "matrix"),
    "sud_cli_deadlines_anchor": ("deadlines", "anchor"),
    "sud_cli_deadlines_confirm": ("deadlines", "confirm"),
    "sud_cli_deadlines_waive": ("deadlines", "waive"),
    "sud_cli_deadlines_list": ("deadlines", "list"),
    "sud_cli_deadlines_audit": ("deadlines", "audit"),
    "sud_cli_deadlines_autofill": ("deadlines", "autofill"),
    "sud_cli_fees": ("fees",),
    "sud_cli_interest": ("interest",),
    "sud_cli_practice_search": ("practice",),
    "sud_cli_kad_search": ("kad",),
    "sud_cli_timeline_audit": ("timeline-audit",),
    "sud_cli_npa_as_of": ("npa-as-of",),
    "sud_cli_verify_quote": ("verify", "quote"),
    "sud_cli_case_history": ("case-history",),
    "sud_cli_payment": ("payment",),
    "sud_cli_deferral": ("deferral",),
    "sud_cli_egrul_search": ("egrul",),
    "sud_cli_fssp_check_plan": ("fssp", "check"),
    "sud_cli_fssp_record": ("fssp", "record"),
    "sud_cli_fssp_stats": ("fssp", "stats"),
    "sud_cli_license_info": ("license",),
    "sud_cli_efrsb_link": ("efrsb", "link"),
    "sud_cli_plenum_search": ("plenum", "search"),
    "sud_cli_efrsb_companies": ("efrsb", "companies"),
    "sud_cli_efrsb_persons": ("efrsb", "persons"),
    "sud_cli_efrsb_publications": ("efrsb", "publications"),
    "sud_cli_efrsb_managers": ("efrsb", "managers"),
    "sud_cli_sudrf_live": ("sudrf-live",),
    "sud_cli_mos_live": ("mos-live",),
    "sud_cli_case_diff": ("case-search", "case-diff"),
    "sud_cli_npa_compare": ("npa", "compare"),
    "sud_cli_case_new": ("case", "new"),
    "sud_cli_case_set": ("case", "set"),
    "sud_cli_case_list": ("case", "list"),
    "sud_cli_case_card": ("case", "card"),
    "sud_cli_case_amount_add": ("case", "amount", "add"),
    "sud_cli_case_doc_add": ("case", "doc", "add"),
    "sud_cli_case_note_add": ("case", "note", "add"),
    "sud_cli_conflict_check": ("conflict", "check"),
    "sud_cli_jurisdiction_check": ("jurisdiction", "check"),
    "sud_cli_fee_exemption": ("fee-exemption",),
    "sud_cli_fee_refund": ("fee-refund",),
    "sud_cli_fee_indexation": ("fee-indexation",),
    "sud_cli_pretrial_calc": ("pretrial", "calc"),
    "sud_cli_pretrial_consequences": ("pretrial", "consequences"),
    "sud_cli_calendar_agenda": ("calendar", "agenda"),
    "sud_cli_calendar_ics": ("calendar", "ics"),
    "sud_cli_reminders_due": ("reminders", "due"),
    "sud_cli_case_search_alerts": ("case-search", "alerts"),
    "sud_cli_form_new": ("form", "new"),
    # the wave: каждый новый тул обязан иметь живую CLI-команду — паритет S-80.
    "sud_cli_deadlines_chain": ("deadlines", "chain"),
    "sud_cli_case_rep_add": ("case", "rep", "add"),
    "sud_cli_case_rep_list": ("case", "rep", "list"),
    "sud_cli_calendar_conflicts": ("calendar", "conflicts"),
    "sud_cli_report_portfolio": ("report", "portfolio"),
    "sud_cli_report_case": ("report", "case"),
    "sud_cli_metrics": ("metrics",),
    "sud_cli_backup_create": ("backup", "create"),
    "sud_cli_backup_verify": ("backup", "verify"),
    "sud_cli_backup_restore": ("backup", "restore"),
    "sud_cli_case_export": ("case", "export"),
    "sud_cli_case_purge": ("case", "purge"),
}

#: Сетевые live-команды (под гейтом SUD_CLI_BRIDGE_LIVE или native-live).
#: В новой волне ни один тул сюда не добавлен; существующие не тронуты.
LIVE_GATED_TOOLS = frozenset({
    "sud_cli_practice_search", "sud_cli_kad_search", "sud_cli_case_history",
    "sud_cli_efrsb_companies", "sud_cli_efrsb_persons",
    "sud_cli_efrsb_publications", "sud_cli_efrsb_managers",
    "sud_cli_sudrf_live", "sud_cli_mos_live",
})


def tool_permission(name: str) -> str | None:
    for t in TOOLS:
        if t["name"] == name:
            return t["permission"]
    return None


def mcp_exposed_commands() -> set:
    """CLI-пути команд, закрытые MCP-инструментами (для паритетного теста)."""
    return {TOOL_CLI_PATH[t["name"]] for t in TOOLS if t["name"] in TOOL_CLI_PATH}


def tool_list():
    return {"tools": [{"name": t["name"], "description": t["description"],
                       "inputSchema": t["schema"], "permission": t["permission"]}
                      for t in TOOLS]}


def _pick(values: dict, names, label: str):
    """Значение параметра под любым из допустимых имён (CLI-алиас ↔ имя движка).

    Оба имени принимаются, но молча выбрать одно при конфликте нельзя: агент,
    передавший `kind` и `fee_kind` с разными значениями, получил бы ответ по
    другому основанию, чем просил. Конфликт — явная ошибка.
    """
    found = {name: values.pop(name) for name in names if name in values}
    if not found:
        return None
    distinct = {repr(value) for value in found.values()}
    if len(distinct) > 1:
        raise ValueError(
            f"conflicting values for {label}: {sorted(found)} — передайте только одно имя"
        )
    return next(iter(found.values()))


def _claim_amount(value):
    """Цена иска из JSON в денежный тип.

    JSON `number` прибыл float'ом — его надо перевести в Decimal через str,
    иначе двоичная погрешность осядет в копейках. bool — не деньги.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("claim_amount must be a number of rubles, not a boolean")
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, int):
        amount = value
    elif isinstance(value, float):
        amount = Decimal(str(value))
    elif isinstance(value, str):
        from decimal import InvalidOperation
        try:
            amount = Decimal(value.strip())
        except InvalidOperation:
            raise ValueError(f"claim_amount must be a number of rubles, got {value!r}") from None
    else:
        raise ValueError(f"claim_amount must be a number of rubles, got {type(value).__name__}")
    if isinstance(amount, Decimal) and not amount.is_finite():
        raise ValueError(f"claim_amount must be finite, got {amount}")
    return amount


def _fees_arguments(args: dict) -> dict:
    """Нормализация вызова sud_cli_fees: алиасы имён + денежный тип базы."""
    from .legal_engine import parse_as_of

    values = dict(args)
    jurisdiction = _pick(values, ("jurisdiction",), "jurisdiction")
    if jurisdiction is None:
        raise ValueError("jurisdiction is required (arbitration|general_jurisdiction|kas)")
    key = str(jurisdiction).strip().lower()
    jurisdiction = JURISDICTION_ALIASES.get(key, key)
    fee_kind = _pick(values, ("fee_kind", "kind"), "fee_kind/kind")
    if fee_kind is None:
        raise ValueError(
            "fee_kind is required; accepted names: fee_kind (engine) or kind (CLI --kind)"
        )
    applicant_type = _pick(values, ("applicant_type", "applicant"), "applicant_type/applicant")
    claim_amount = _claim_amount(values.pop("claim_amount", None))
    claim_class = values.pop("claim_class", None)
    # v2.10.0 (W4d): дата-привязка тарифа. Значение отсутствует → None → прежний
    # контракт (действующая редакция, конверт без provenance). Неверная строка —
    # явная ошибка, а не молчаливый пересчёт «на сегодня».
    as_of = parse_as_of(values["as_of"]) if values.get("as_of") is not None else None
    values.pop("as_of", None)
    if values:
        raise TypeError(f"unknown fees arguments: {sorted(values)}")
    return {"jurisdiction": jurisdiction, "fee_kind": fee_kind,
            "applicant_type": applicant_type, "claim_amount": claim_amount,
            "claim_class": claim_class, "as_of": as_of}


def _mcp_state_dir():
    import os
    from pathlib import Path
    return Path(os.environ.get("SUD_CLI_STATE_DIR") or (Path.home() / ".local" / "share" / "sud-cli"))


def _call(name, args):
    if name == "sud_cli_efrsb_link":
        from . import efrsb_manual
        return efrsb_manual.link_plan(args["kind"], name=args.get("name"),
                                      inn=args.get("inn"), ogrn=args.get("ogrn"))
    if name == "sud_cli_plenum_search":
        from . import plenum_corpus as pc
        from .config import get_state_dir
        return pc.search(get_state_dir(), args["text"])
    if name == "sud_cli_license_info":
        from . import _wm
        from . import __version__ as _v
        payload = _wm.source_info(_v)
        payload["build_flags"] = _wm.build_flags(_v) if args.get("with_build_flags", True) else []
        return payload
    if name == "sud_cli_limitation":
        from .limitation import calculate_limitation
        values = dict(args)
        picked = _pick(values, ("period_type", "type"), "period_type/type")
        values["period_type"] = picked if picked is not None else "general"
        for engine_name, cli_name in (("custom_years", "years"), ("custom_months", "months")):
            chosen = _pick(values, (engine_name, cli_name), f"{engine_name}/{cli_name}")
            if chosen is not None:
                values[engine_name] = chosen
        values["start"] = date.fromisoformat(values["start"])
        if values.get("check_date"):
            values["check_date"] = date.fromisoformat(values["check_date"])
        return calculate_limitation(**values).to_dict()
    if name == "sud_cli_timeline_audit":
        from .timeline_audit import audit_timeline
        return audit_timeline(args.get("events", []))
    if name == "sud_cli_fee_exemption":
        from .fees_extra import fee_exemption_check
        return fee_exemption_check(load_rules(), jurisdiction=args["jurisdiction"], category_code=args["category"])
    if name == "sud_cli_fee_refund":
        from decimal import Decimal
        from .fees_extra import fee_refund
        return fee_refund(load_rules(), paid=Decimal(args["paid"]), ground=args["ground"],
                          claim_admitted=bool(args.get("claim_admitted")),
                          plaintiff_default=bool(args.get("plaintiff_default")),
                          transferred_to_arbitration=bool(args.get("transferred_to_arbitration")))
    if name == "sud_cli_fee_indexation":
        from decimal import Decimal
        from .fees_extra import indexation_compute, indexation_frame
        r = load_rules()
        if args.get("amount"):
            return indexation_compute(r, code=args["code"], amount=Decimal(args["amount"]),
                                      cpi_start=Decimal(args["cpi_start"]) if args.get("cpi_start") else None,
                                      cpi_end=Decimal(args["cpi_end"]) if args.get("cpi_end") else None)
        return indexation_frame(r, code=args["code"])
    if name == "sud_cli_npa_as_of":
        from .npa_as_of import lookup_as_of
        return lookup_as_of(args["act"], args["article"], args["date"])
    if name == "sud_cli_verify_quote":
        from .verify import verify_quote
        values = dict(args or {})
        return verify_quote(
            values["text"], values["source"],
            act=values.get("act"), article=values.get("article"), date=values.get("date"),
            case_id=values.get("case_id"), path=values.get("path"),
            state_dir=_mcp_state_dir(), max_fuzzy=values.get("max_fuzzy", 0.0))
    if name == "sud_cli_deadlines":
        from .legal_deadlines import calculate_deadline
        allowed = {"deadline_key", "event_date", "region", "tz"}
        values = {k: v for k, v in (args or {}).items() if k in allowed}
        if values.get("event_date"):
            values["event_date"] = date.fromisoformat(values["event_date"])
        return calculate_deadline(**values).to_dict()
    if name == "sud_cli_deadlines_matrix":
        from .deadline_matrix import build_matrix
        from .legal_engine import load_rules
        values = dict(args or {})
        case_id = _pick(values, ("case_id", "id"), "case_id/id")
        if case_id is None:
            raise ValueError("case_id is required")
        instance_override = values.pop("instance", None)
        as_of = values.pop("as_of", None)
        missing = bool(values.pop("missing", False))
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        state_dir = _mcp_state_dir()
        instance, points = build_matrix(state_dir, case_id, instance_override=instance_override, as_of=as_of)
        if missing:
            points = [p for p in points if p.get("needs_anchor")]
        rules = load_rules()
        calc_date = date.today().isoformat()
        disclaimer = rules["mandatory_disclaimer"].format(calc_date=calc_date)
        payload = {
            "case_id": case_id,
            "instance": instance,
            "as_of": as_of,
            "points": points,
            "matrix": points,
            "items": points,
            "deadlines": points,
            "entries": points,
            "disclaimer": disclaimer,
            "clean_room": rules.get("clean_room"),
        }
        return payload
    if name == "sud_cli_fees":
        from .legal_engine import STATUS_OK, calculate_fee, load_rules, validate_claim_class
        # v2.3.1: вызов проходит через нормализацию — CLI-алиасы (kind, applicant),
        # алиасы юрисдикции и цена иска в денежном типе (Decimal с копейками).
        rules = load_rules()
        fa = _fees_arguments(args)
        cc = fa.get("claim_class")
        if cc is not None:
            # v2.3.3 (review R2): тот же guard класса, что CLI; без него
            # claim_class="criminal" молча проходил в расчёт через MCP.
            cr = validate_claim_class(cc, rules, fa.get("fee_kind"))
            if cr.status != STATUS_OK:
                return cr.to_dict()
        return calculate_fee(rules=rules, **fa).to_dict()
    if name == "sud_cli_interest":
        from .interest import calculate_interest
        values = dict(args)
        # start/start_date и end/end_date — одно и то же разными именами (CLI
        # десты start_date/end_date). Конфликт значений молча проигрывает одной
        # стороне, поэтому тоже явная ошибка.
        start_value = _pick(values, ("start_date", "start"), "start/start_date")
        end_value = _pick(values, ("end_date", "end"), "end/end_date")
        if start_value is None or end_value is None:
            raise ValueError("start and end are required (или CLI-алиасы start_date/end_date)")
        values["start"] = date.fromisoformat(start_value)
        values["end"] = date.fromisoformat(end_value)
        return calculate_interest(**values).to_dict()
    if name in {"sud_cli_practice_search", "sud_cli_kad_search", "sud_cli_case_history"}:
        # v2.3.2 (sa-1 F-05): живые поиски под тем же гейтом SUD_CLI_BRIDGE_LIVE,
        # что и sudrf_live/mos_live — без него никаких исходящих запросов в суды.
        # v2.3.3 (review R1): строго {1,true,yes,on} как в bridge — "0"/"off" раньше
        # ОТКРЫВАЛИ live-доступ (truthy-строка). Единый контракт обоих входов.
        if os.environ.get("SUD_CLI_BRIDGE_LIVE", "").strip().lower() not in {"1", "true", "yes", "on"}:
            return {"status": "blocked", "reason": "live_access_disabled",
                    "hint": "Исходящие запросы к sudact включаются переменной SUD_CLI_BRIDGE_LIVE."}
    if name == "sud_cli_practice_search":
        from .case_search.public_api import search_practice
        return search_practice(**args)
    if name == "sud_cli_kad_search":
        from .case_search.public_api import kad_search
        return kad_search(**args)
    if name == "sud_cli_case_history":
        from .case_search.public_api import case_history
        values = dict(args)
        # CLI отдаёт номер дела позиционным аргументом (dest case_id); MCP-схема
        # исторически звала его case_number. Оба имени принимаются, конфликт — ошибка.
        chosen = _pick(values, ("case_number", "case_id"), "case_number/case_id")
        if chosen is None:
            raise ValueError("case_number is required; accepted names: case_number or case_id")
        values["case_number"] = chosen
        return case_history(**values)
    if name == "sud_cli_payment":
        from .payment import payment_details
        return payment_details(**args)
    if name == "sud_cli_deferral":
        from .deferral import generate_deferral
        return generate_deferral(**args)
    if name == "sud_cli_fssp_check_plan":
        from .fssp_manual import check_plan
        values = dict(args or {})
        kind = values.pop("kind", None)
        if kind is None:
            raise ValueError("kind is required")
        kwargs = {}
        for k in ("name", "inn", "ogrn", "birth_date", "region"):
            if k in values:
                kwargs[k] = values.pop(k)
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        return check_plan(kind, **kwargs)
    if name == "sud_cli_fssp_record":
        from .db import add_case_evidence, get_connection, init_db
        from .fssp_manual import record_payload
        values = dict(args or {})
        case_id = values.pop("case_id", None)
        subject = values.pop("subject", None)
        result_v = values.pop("result", None)
        checked_by = values.pop("checked_by", None)
        if not (case_id and subject and result_v and checked_by):
            raise ValueError("case_id, subject, result, checked_by are required")
        payload = record_payload(subject=subject, result=result_v,
                                 checked_by=checked_by,
                                 notes=values.pop("notes", None),
                                 evidence_file=values.pop("file", None))
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        state_dir = _mcp_state_dir()
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            return add_case_evidence(conn, case_id, title=payload["title"],
                                     path=payload["evidence_file"], note=payload["note"])
        finally:
            conn.close()
    if name == "sud_cli_fssp_stats":
        from .fssp_stats import statistics
        values = dict(args or {})
        region = values.pop("region", None)
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        return statistics(region)
    if name == "sud_cli_efrsb_companies":
        from .efrsb_public import search_companies
        return search_companies(**args)
    if name == "sud_cli_efrsb_persons":
        from .efrsb_public import search_persons
        values = dict(args)
        # CLI-dest --name против имени движка full_name.
        chosen = _pick(values, ("full_name", "name"), "full_name/name")
        if chosen is None:
            raise ValueError("full_name is required; accepted names: full_name or name")
        values["full_name"] = chosen
        return search_persons(**values)
    if name == "sud_cli_efrsb_publications":
        from .efrsb_public import case_publications
        return case_publications(**args)
    if name == "sud_cli_efrsb_managers":
        from .efrsb_public import search_managers
        return search_managers(**args)
    if name in {"sud_cli_sudrf_live", "sud_cli_mos_live"}:
        from .case_search_bridge import run_bridge_command
        state_dir = Path(os.environ.get("SUD_CLI_STATE_DIR") or Path.home() / ".local" / "share" / "sud-cli")
        kwargs = {"case_id": args["case_id"]}
        if name == "sud_cli_mos_live":
            kwargs["court_slug"] = args.get("court_slug")
        return run_bridge_command("mos-live" if name.endswith("mos_live") else "sudrf-live",
                                  state_dir=state_dir, **kwargs)
    if name == "sud_cli_case_diff":
        from .case_diff import DiffError, diff_case_snapshots
        old = args.get("old") or args.get("old_snapshot")
        new = args.get("new") or args.get("new_snapshot")
        if not old:
            raise ValueError("old snapshot path required")
        if not new:
            raise ValueError("new snapshot path required")
        try:
            return diff_case_snapshots(Path(old), Path(new))
        except DiffError as exc:
            # DiffError outside the handle_request catch set: convert, or the
            # serve_stdio loop dies instead of returning isError=True.
            raise ValueError(str(exc)) from exc
    if name == "sud_cli_npa_compare":
        from .db import SearchError, compare_npa_editions, get_connection, init_db
        state_dir = Path(os.environ.get("SUD_CLI_STATE_DIR") or Path.home() / ".local" / "share" / "sud-cli")
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            return compare_npa_editions(conn, args["doc_id"], args["edition1"], args["edition2"])
        except SearchError as exc:
            raise ValueError(str(exc)) from exc
        finally:
            conn.close()
    if name == "sud_cli_case_new":
        from .db import LEGAL_CASE_STATUSES, create_case, get_connection, init_db
        state_dir = _mcp_state_dir()
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            values = dict(args)
            number = _pick(values, ("number", "case_number"), "number/case_number")
            if number is None:
                raise ValueError("number is required")
            status = values.get("status")
            if status is not None and status not in LEGAL_CASE_STATUSES:
                raise ValueError("invalid_status")
            res = create_case(
                conn,
                number,
                values.get("court"),
                values.get("plaintiff"),
                values.get("defendant"),
                owner=values.get("owner"),
                status=status,
            )
            if res.get("duplicate"):
                return {"status": "duplicate_window", "case_id": res["case_id"]}
            return {"case_id": res["case_id"], "case_number": res.get("case_number"),
                    "court": res.get("court"), "status": res.get("status"),
                    "owner": res.get("owner")}
        finally:
            conn.close()
    if name == "sud_cli_case_set":
        from .db import LEGAL_CASE_STATUSES, get_connection, init_db, set_case
        values = dict(args)
        case_id = _pick(values, ("case_id", "id"), "case_id/id")
        if case_id is None:
            raise ValueError("case_id is required")
        fields = {}
        for k in ("status", "owner", "closed_at"):
            if k in values and values.get(k) is not None:
                fields[k] = values.pop(k)
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        if "status" in fields and fields["status"] not in LEGAL_CASE_STATUSES:
            raise ValueError("invalid_status")
        state_dir = _mcp_state_dir()
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            updated = set_case(conn, case_id, **fields)
        finally:
            conn.close()
        if updated is None:
            raise ValueError(f"case not found: {case_id}")
        return updated
    if name == "sud_cli_case_list":
        from .db import get_connection, init_db, list_cases
        state_dir = _mcp_state_dir()
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            return list_cases(
                conn,
                status=args.get("status"),
                owner=args.get("owner"),
                court=args.get("court"),
                party=args.get("party"),
            )
        finally:
            conn.close()
    if name == "sud_cli_case_card":
        from .db import get_case_card, get_connection, init_db
        values = dict(args)
        case_id = _pick(values, ("case_id", "id"), "case_id/id")
        if case_id is None:
            raise ValueError("case_id is required")
        state_dir = _mcp_state_dir()
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            card = get_case_card(conn, case_id)
        finally:
            conn.close()
        if card is None:
            raise ValueError(f"case not found: {case_id}")
        return card
    if name == "sud_cli_case_amount_add":
        from .db import InvalidAmountError, add_case_amount, get_connection, init_db
        values = dict(args)
        case_id = _pick(values, ("case_id", "id"), "case_id/id")
        if case_id is None:
            raise ValueError("case_id is required")
        state_dir = _mcp_state_dir()
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            try:
                res = add_case_amount(
                    conn, case_id, values.get("kind"), values.get("amount"), note=values.get("note")
                )
            except InvalidAmountError as exc:
                raise ValueError("invalid_amount") from exc
        finally:
            conn.close()
        if res is None:
            raise ValueError(f"case not found: {case_id}")
        return res
    if name == "sud_cli_case_doc_add":
        from .db import DocNotFoundError, add_case_document, get_connection, init_db
        values = dict(args)
        case_id = _pick(values, ("case_id", "id"), "case_id/id")
        if case_id is None:
            raise ValueError("case_id is required")
        state_dir = _mcp_state_dir()
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            try:
                res = add_case_document(conn, case_id, values.get("path"), title=values.get("title"))
            except DocNotFoundError as exc:
                raise ValueError("doc_not_found") from exc
        finally:
            conn.close()
        if res is None:
            raise ValueError(f"case not found: {case_id}")
        return res
    if name == "sud_cli_case_note_add":
        from .db import add_case_note, get_connection, init_db
        values = dict(args)
        case_id = _pick(values, ("case_id", "id"), "case_id/id")
        if case_id is None:
            raise ValueError("case_id is required")
        state_dir = _mcp_state_dir()
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            res = add_case_note(conn, case_id, values.get("text"), author=values.get("author"))
        finally:
            conn.close()
        if res is None:
            raise ValueError(f"case not found: {case_id}")
        return res
    if name == "sud_cli_conflict_check":
        from .conflict import check as conflict_check
        from .db import get_case, get_connection, init_db, list_cases

        values = dict(args)
        case_id = _pick(values, ("case_id", "id"), "case_id/id")
        state_dir = _mcp_state_dir()
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            if case_id is not None:
                case = get_case(conn, case_id)
                if case is None:
                    raise ValueError(f"case not found: {case_id}")
                portfolio = [c for c in list_cases(conn) if c.get("case_id") != case_id]
                candidate = {
                    "parties": [],
                    "case_id": case_id,
                    "court": case.get("court"),
                    "case_number": case.get("case_number"),
                }
                for key in ("plaintiff", "defendant"):
                    val = case.get(key)
                    if val:
                        candidate["parties"].append(val)
            else:
                portfolio = list_cases(conn)
                candidate = {
                    "parties": list(values.pop("parties", []) or []),
                    "court": values.pop("court", None),
                    "case_number": values.pop("case_number", None),
                }
                for key in ("plaintiff", "defendant"):
                    if key in values and values.get(key) is not None:
                        candidate["parties"].append(values.pop(key))
                if values:
                    raise ValueError(f"unknown fields: {sorted(values)}")
            hits = conflict_check(portfolio, candidate)
        finally:
            conn.close()
        return {
            "clean": len(hits) == 0,
            "hits": hits,
            "method": "name_exact",
            "coverage": "local registry names only",
            "disclaimer": (
                "Отсутствие совпадений не означает отсутствие конфликта: покрытие — "
                "только имена локального реестра; внешние стороны и аффилированные лица "
                "не охвачены. Решение остаётся за человеком (boundary IX-3)."
            ),
        }
    if name == "sud_cli_jurisdiction_check":
        from .jurisdiction import check_jurisdiction
        return check_jurisdiction(
            code=args.get("code"),
            claim=args.get("claim"),
            party_org=bool(args.get("party_org", False)),
            party_phys=bool(args.get("party_phys", False)),
            unknown_rf_location=bool(args.get("unknown_rf_location", False)),
            immovable=bool(args.get("immovable", False)),
            corporate=bool(args.get("corporate", False)),
            bankruptcy=bool(args.get("bankruptcy", False)),
            ip_rights=bool(args.get("ip_rights", False)),
            contract_forum=bool(args.get("contract_forum", False)),
            arbitration_clause=bool(args.get("arbitration_clause", False)),
            multiple_defendants=bool(args.get("multiple_defendants", False)),
            foreign_element=bool(args.get("foreign_element", False)),
            consumer=bool(args.get("consumer", False)),
            alimony=bool(args.get("alimony", False)),
        )
    if name == "sud_cli_pretrial_calc":
        from .pretrial import calc_pretrial
        return calc_pretrial(code=args.get("code"), sent=args.get("sent"))
    if name == "sud_cli_pretrial_consequences":
        from .pretrial import consequences
        return consequences(code=args.get("code"), stage=args.get("stage"))
    # ---------------- W6a §1/§3/§4/§5: диспатч новых офлайн-утилит -------------
    if name in {"sud_cli_deadlines_anchor", "sud_cli_deadlines_confirm",
                "sud_cli_deadlines_waive", "sud_cli_deadlines_autofill"}:
        from .db import (
            anchor_deadline_state,
            confirm_deadline_state,
            get_connection,
            init_db,
            waive_deadline_state,
        )
        values = dict(args or {})
        state_dir = _mcp_state_dir()
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            case_id = values.pop("case_id", None)
            if case_id is None:
                raise ValueError("case_id is required")
            if name == "sud_cli_deadlines_anchor":
                key = values.pop("key", None)
                event_date = values.pop("event_date", None)
                method = values.pop("method", None)
                evidence = values.pop("evidence", None)
                actor = values.pop("by", None) or "mcp"
                if values:
                    raise ValueError(f"unknown fields: {sorted(values)}")
                if not (key and event_date and method and evidence):
                    raise ValueError("key, event_date, method, evidence are required")
                return anchor_deadline_state(conn, state_dir, case_id, key,
                                             event_date, method, evidence, actor=actor)
            if name == "sud_cli_deadlines_confirm":
                key = values.pop("key", None)
                by = values.pop("by", None)
                evidence = values.pop("evidence", None)
                waive = bool(values.pop("waive", False))
                norm = values.pop("norm", None)
                if values:
                    raise ValueError(f"unknown fields: {sorted(values)}")
                if not (key and by):
                    raise ValueError("key and by are required")
                if waive and not norm:
                    raise ValueError("norm required for waive")
                return confirm_deadline_state(conn, state_dir, case_id, key, by,
                                              evidence=evidence, waive=waive, norm=norm)
            if name == "sud_cli_deadlines_waive":
                key = values.pop("key", None)
                by = values.pop("by", None)
                norm = values.pop("norm", None)
                evidence = values.pop("evidence", None)
                if values:
                    raise ValueError(f"unknown fields: {sorted(values)}")
                if not (key and by and norm):
                    raise ValueError("key, by and norm are required")
                return waive_deadline_state(conn, state_dir, case_id, key, by, norm,
                                            evidence=evidence)
            event = values.pop("event", None)
            if values:
                raise ValueError(f"unknown fields: {sorted(values)}")
            from .autofill import autofill_case
            return autofill_case(conn, state_dir, case_id, event_id=event)
        finally:
            conn.close()
    if name == "sud_cli_deadlines_list":
        from .db import get_connection, init_db, list_all_deadline_states, list_deadline_states
        state_dir = _mcp_state_dir()
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            case_id = (args or {}).get("case_id")
            if case_id is not None:
                return list_deadline_states(conn, case_id)
            return list_all_deadline_states(conn)
        finally:
            conn.close()
    if name == "sud_cli_deadlines_audit":
        from .db import init_db, read_deadline_audit
        state_dir = _mcp_state_dir()
        init_db(state_dir)
        return read_deadline_audit(state_dir, args["case_id"])
    if name == "sud_cli_calendar_agenda":
        from .agenda import agenda_payload
        days = int((args or {}).get("days", 14))
        if not 1 <= days <= 365:
            raise ValueError("days must be within 1..365")
        return agenda_payload(_mcp_state_dir(), days=days)
    if name == "sud_cli_calendar_ics":
        from .calendar_ics import ics_for_state_dir
        days = int((args or {}).get("days", 60))
        if not 1 <= days <= 3650:
            raise ValueError("days must be within 1..3650")
        text, events = ics_for_state_dir(_mcp_state_dir(), days=days)
        return {"days": days, "count": len(events), "events": events, "ics": text}
    if name == "sud_cli_reminders_due":
        from .db import get_connection, init_db, list_all_deadline_states
        from .reminders import BUS_FREE_NOTE, due_items, generate_mail_commands
        days = int((args or {}).get("days", 7))
        if not 1 <= days <= 90:
            raise ValueError("days must be within 1..90")
        state_dir = _mcp_state_dir()
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            states = list_all_deadline_states(conn)
        finally:
            conn.close()
        today = date.today()
        items = due_items(states, today, days)
        return {
            "days": days,
            "today": today.isoformat(),
            "window_to": (today + timedelta(days=days)).isoformat(),
            "items": items,
            "commands": generate_mail_commands(items, days),
            "bus_free": BUS_FREE_NOTE,
        }
    if name == "sud_cli_case_search_alerts":
        from . import court_alerts
        values = dict(args or {})
        days = int(values.pop("days", 7))
        if not 1 <= days <= 90:
            raise ValueError("days must be within 1..90")
        alerts_dir = values.pop("alerts_dir", None)
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        if alerts_dir is None:
            candidates = court_alerts.alerts_search_dirs(_mcp_state_dir())
            alerts_dir = next((d for d in candidates if d.is_dir()), candidates[0])
        return court_alerts.alerts_payload(alerts_dir, date.today(), days)
    if name == "sud_cli_form_new":
        # the wave §4: read-тул — только чтение реестра + детерминированный скелет.
        from .db import get_case, get_connection, init_db
        from .forms import render_form
        values = dict(args or {})
        case_id = _pick(values, ("case_id", "id"), "case_id/id")
        kind = values.pop("kind", None)
        fmt = values.pop("format", "md")
        values.pop("case_id", None)
        values.pop("id", None)
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        if case_id is None:
            raise ValueError("case_id is required")
        state_dir = _mcp_state_dir()
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            case = get_case(conn, case_id)
        finally:
            conn.close()
        if case is None:
            raise ValueError(f"case not found: {case_id}")
        return render_form(kind, case, fmt=fmt)
    # ---------------- the wave §1..§5 ----------------
    # Каждый тул вызывает ТОТ ЖЕ движок, что и CLI-команда из TOOL_CLI_PATH:
    # вторая копия логики в MCP расходится с оригиналом при первой же правке.
    if name == "sud_cli_deadlines_chain":
        from .chain import build_chain
        values = dict(args or {})
        case_id = _pick(values, ("case_id", "id"), "case_id/id")
        instance = values.pop("instance", None)
        values.pop("case_id", None)
        values.pop("id", None)
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        if case_id is None:
            raise ValueError("case_id is required")
        return build_chain(_mcp_state_dir(), case_id, instance_override=instance)
    if name == "sud_cli_case_rep_add":
        from .db import add_case_representative, get_connection, init_db
        values = dict(args or {})
        case_id = values.pop("case_id", None)
        side = values.pop("side", None)
        rep_name = values.pop("name", None)
        authority_note = values.pop("authority_note", None)
        power_docs = values.pop("power_docs", None)
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        if not (case_id and side and rep_name):
            raise ValueError("case_id, side and name are required")
        state_dir = _mcp_state_dir()
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            row = add_case_representative(conn, case_id, side=side, name=rep_name,
                                          authority_note=authority_note,
                                          power_docs=power_docs)
        finally:
            conn.close()
        if row is None:
            raise ValueError(f"case not found: {case_id}")
        from .db import REPRESENTATIVE_HINT
        return {**row, "hint": REPRESENTATIVE_HINT}
    if name == "sud_cli_case_rep_list":
        from .db import REPRESENTATIVE_HINT, get_connection, init_db
        from .db import list_case_representatives
        values = dict(args or {})
        case_id = _pick(values, ("case_id", "id"), "case_id/id")
        values.pop("case_id", None)
        values.pop("id", None)
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        if case_id is None:
            raise ValueError("case_id is required")
        state_dir = _mcp_state_dir()
        init_db(state_dir)
        conn = get_connection(state_dir)
        try:
            rows = list_case_representatives(conn, case_id)
        finally:
            conn.close()
        return {"case_id": case_id, "items": rows, "hint": REPRESENTATIVE_HINT}
    if name == "sud_cli_calendar_conflicts":
        from .conflicts import conflicts_payload
        values = dict(args or {})
        days = int(values.pop("days", 30))
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        if not 1 <= days <= 3650:
            raise ValueError("days must be within 1..3650")
        return conflicts_payload(_mcp_state_dir(), days=days)
    if name == "sud_cli_report_portfolio":
        from .reports import portfolio_payload
        values = dict(args or {})
        as_of = values.pop("as_of", None)
        days = int(values.pop("days", 30))
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        if not 1 <= days <= 3650:
            raise ValueError("days must be within 1..3650")
        return portfolio_payload(_mcp_state_dir(), as_of=as_of, days=days)
    if name == "sud_cli_report_case":
        from .reports import case_payload
        values = dict(args or {})
        case_id = _pick(values, ("case_id", "id"), "case_id/id")
        values.pop("case_id", None)
        values.pop("id", None)
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        if case_id is None:
            raise ValueError("case_id is required")
        payload = case_payload(_mcp_state_dir(), case_id)
        if payload is None:
            raise ValueError(f"case not found: {case_id}")
        return payload
    if name == "sud_cli_metrics":
        from .metrics import metrics_payload
        values = dict(args or {})
        near_days = int(values.pop("near_days", 30))
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        if not 1 <= near_days <= 3650:
            raise ValueError("near_days must be within 1..3650")
        return metrics_payload(_mcp_state_dir(), near_days=near_days)
    if name == "sud_cli_backup_create":
        from pathlib import Path as _Path

        from .backup import create_backup
        values = dict(args or {})
        out = values.pop("out", None)
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        payload = create_backup(_mcp_state_dir(), out=_Path(out) if out else None)
        payload["disclaimer"] = (
            "Снимок содержит ПДн реестра: файл и sidecar записаны правами 0600."
        )
        return payload
    if name == "sud_cli_backup_verify":
        from .backup import verify_backup
        values = dict(args or {})
        file = values.pop("file", None)
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        if not file:
            raise ValueError("file is required")
        payload = verify_backup(file)
        payload["ok"] = bool(payload["integrity_ok"] and payload["quick_ok"])
        return payload
    if name == "sud_cli_backup_restore":
        from .backup import restore_backup
        values = dict(args or {})
        file = values.pop("file", None)
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        if not file:
            raise ValueError("file is required")
        # confirm уже снят гейтом _dispatch_gated: сюда доходит только
        # подтверждённый вызов, поэтому confirmed=True обоснован.
        return restore_backup(_mcp_state_dir(), file, confirmed=True)
    if name == "sud_cli_case_export":
        from .pdn import export_case, render_export_md
        values = dict(args or {})
        case_id = _pick(values, ("case_id", "id"), "case_id/id")
        fmt = values.pop("format", "json")
        values.pop("case_id", None)
        values.pop("id", None)
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        if case_id is None:
            raise ValueError("case_id is required")
        payload = export_case(_mcp_state_dir(), case_id, fmt=fmt)
        if payload is None:
            raise ValueError(f"case not found: {case_id}")
        if fmt == "md":
            payload["markdown"] = render_export_md(payload)
        return payload
    if name == "sud_cli_case_purge":
        from .pdn import purge_apply
        values = dict(args or {})
        case_id = _pick(values, ("case_id", "id"), "case_id/id")
        remove_files = bool(values.pop("remove_files", True))
        values.pop("case_id", None)
        values.pop("id", None)
        if values:
            raise ValueError(f"unknown fields: {sorted(values)}")
        if case_id is None:
            raise ValueError("case_id is required")
        return purge_apply(_mcp_state_dir(), case_id, confirmed=True,
                           actor="mcp", remove_files=remove_files)


def _tool_schema(name: str) -> dict | None:
    for t in TOOLS:
        if t["name"] == name:
            return t["schema"]
    return None


def _dispatch_gated(name, args) -> Any:
    """W6a §1: права + идемпотентность поверх диспатча write-инструментов.

    needs_confirmation — это ДАННЫЕ (status), не ошибка транспорта: запись не
    произведена, клиент получает явный сигнал повторить с confirm: true.
    Идемпотентный повтор внутри окна (10 мин) возвращает ПЕРВЫЙ результат с
    флагом idempotent_replay=true в теле — молча не дублирует и не ругается.

    Fail-closed на неизвестном поле сохранён и до подтверждения: контракт
    additionalProperties=false и пин v2.4.0 (case_set отклоняет foo) не
    смещаются гейтом confirm — иначе write-тул становился терпимее read.
    """
    if tool_permission(name) != "write":
        return _call(name, args)
    values = dict(args or {})
    confirm = values.pop("confirm", None)
    idem_key = values.pop("idempotency_key", None)
    schema = _tool_schema(name) or {}
    allowed = set((schema.get("properties") or {}).keys())
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    if confirm is not True:
        return {
            "status": "needs_confirmation",
            "permission": "write",
            "tool": name,
            "detail": "write-инструмент требует явного confirm: true — запись не произведена",
        }
    from . import idempotency
    state_dir = _mcp_state_dir()
    if idem_key is not None and str(idem_key):
        hit = idempotency.lookup(state_dir, name, str(idem_key))
        if hit is not None:
            replay = dict(hit)
            replay["idempotent_replay"] = True
            return replay
    value = _call(name, values)
    if idem_key is not None and str(idem_key) and isinstance(value, dict):
        idempotency.record(state_dir, name, str(idem_key), value)
    return value


def handle_request(request):
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "sud-cli", "version": __version__}}
    elif method == "notifications/initialized":
        return None
    elif method == "tools/list":
        result = tool_list()
    elif method == "tools/call":
        try:
            params = request.get("params", {})
            # W6a §1: права (needs_confirmation) и идемпотентность write-тулов
            # применяются ДО движка — диспатч ниже знает только предметную логику.
            value = _dispatch_gated(params["name"], params.get("arguments", {}))
            if hasattr(value, "to_dict"):  # v2.3.2 (sa-1 F-05): ApiOutcome -> JSON, не repr
                value = value.to_dict()
            result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}], "isError": False}
        except (KeyError, TypeError, ValueError, OSError) as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except Exception as exc:  # v2.3.2 (sa-1 F-06): доменные ошибки -> isError, stdio не умирает
            result = {"content": [{"type": "text", "text": f"unexpected: {type(exc).__name__}: {exc}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve_stdio(input_stream=None, output_stream=None):
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    for line in input_stream:
        if not line.strip():
            continue
        # v2.3.3 (review R1): не-JSON строка убивала serve_stdio целиком.
        try:
            frame = json.loads(line)
        except json.JSONDecodeError as exc:
            response = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": f"Parse error: {exc}"}}
        else:
            response = handle_request(frame)
        if response is not None:
            output_stream.write(json.dumps(response, ensure_ascii=False) + "\n")
            output_stream.flush()
