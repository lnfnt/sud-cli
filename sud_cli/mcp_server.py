# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 lnfnt <lnfnt@proton.me>

"""sud-cli MCP stdio server: JSON-RPC 2.0 tool surface.

v2.2.0 (боевая рецензия colleague, A2A 00000000000000000000000000000001, 21.10):
- типизированные inputSchema у всех инструментов (properties/required/enum/
  default) вместо единого {"type":"object"} — агент видит сигнатуры;
- additionalProperties=false: неизвестный параметр — явная ошибка, а не
  молчаливое игнорирование (fail-closed);
- выведены недостающие тулзы: case_history, payment, deferral,
  efrsb_companies/persons/publications/managers, sudrf_live, mos_live
  (через court-watcher bridge; без SUD_CLI_BRIDGE_LIVE возвращают
  status=blocked и не порождают дочерних процессов).

v2.3.0 (письмо A2A colleague 001a1608, 21.10):
- sud_cli_case_diff (первичное по письму, вариант «в»): offline-дифф двух
  сохранённых snapshots одного дела (sud_cli.case_diff.diff_case_snapshots,
  тот же код, что в CLI 'court-watcher case-diff');
- sud_cli_npa_compare (аддитивно, вариант «а»): сравнение двух редакций
  сохранённого НПА-фикстура (sud_cli.db.compare_npa_editions);
- доменные ошибки DiffError/SearchError конвертируются в ValueError внутри
  _call: handle_request ловит только (KeyError, TypeError, ValueError,
  OSError), поэтому без конверсии serve_stdio падал бы вместо того, чтобы
  вернуть клиенту isError=True с текстом ошибки.

v2.3.1 (боевой аудит colleague, ТЗ TASK_SUDCLI_V231, письмо A2A 001a1609):
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
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import __version__

from .drafter import TEMPLATES as DRAFT_TYPES


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
         "period_type": {"type": "string", "default": "general",
                         "enum": ["general", "contract_work", "carriage",
                                  "voidable_deal", "custom_years", "custom_months"],
                         "description": "Тип срока; CLI-алиас: type"},
         "type": {"type": "string", "description": "CLI-алиас period_type (--type)"},
         "claim_notice_days": {"type": "integer", "default": 0, "description": "Дни досудебного порядка (+30) — легаси-модель; новые вызовы: pretrial_start/pretrial_end"},
         "pretrial_start": {**DATE, "description": "Дата начала досудебного порядка; W6/S1-3: приостановление срока (п. 3 ст. 202 ГК РФ); только вместе с pretrial_end"},
         "pretrial_end": {**DATE, "description": "Дата окончания досудебного порядка; только вместе с pretrial_start"},
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
         "as_of": {**DATE, "description": "Дата подачи заявления YYYY-MM-DD; тарифы из редакции, действовавшей на дату (S1-13; до 2024-09-08 — ред. 221-ФЗ)"}},
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
    {"name": "sud_cli_verify_quote", "description": "Verify a quoted fragment against npa bundle / case events / text file (verdict exact|fuzzy|not_found|edition_unavailable; Закон Zero — без «ok на глаз»).",
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
         "jurisdiction": {"type": "string"}, "amount": {"type": "number"},
         "court": {"type": "string"}, "defendant": {"type": "string"}},
         "required": ["jurisdiction", "amount", "court", "defendant"], "additionalProperties": False}},
    {"name": "sud_cli_deferral", "description": "Draft a motion to defer the state fee.",
     "schema": {"type": "object", "properties": {
         "applicant": {"type": "string"}, "court": {"type": "string"},
         "amount": {"type": "integer"}, "reason": {"type": "string"},
         "court_type": {"type": "string", "enum": ["arbitration", "general"],
                        "default": "arbitration",
                        "description": "Тип суда (W6/S1-11): arbitration — АПК+Пленум ВАС №6; general — ГПК/КАС без АПК/ВАС"}},
         "required": ["applicant", "court", "amount", "reason"], "additionalProperties": False}},
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
    {"name": "sud_cli_npa_download", "description": "Download the full official text of an NPA/court act into local npa storage (pravo.gov.ru by publication number, or a direct official URL); extracts the text layer and records sha256.",
     "schema": {"type": "object", "properties": {
          "eo_number": {"type": "string", "description": "pravo.gov.ru publication number (e.g. 0001202509240023)"},
          "url": {"type": "string", "description": "Direct official URL (pravo.gov.ru / vsrf.ru)"},
          "source": {"type": "string", "enum": ["pravo", "vsrf", "url"]},
          "extract_text": {"type": "boolean", "default": True}},
          "anyOf": [{"required": ["eo_number"]}, {"required": ["url"]}],
          "additionalProperties": False}},
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
    {"name": "sud_cli_draft", "description": "Draft a procedural court document (7 types: claim_arbitration, claim_soyu, objections, petition_expert, petition_interim, petition_deferral, appeal); Law Zero: missing required fields blocks assembly (missing_required_fields). S3-2: MCP возвращает markdown-контент; запись в файл — только CLI.",
     "schema": {"type": "object", "properties": {
          "doc_type": {"type": "string", "enum": sorted(DRAFT_TYPES), "description": "Тип процессуального документа"},
          "court": {"type": "string"}, "plaintiff": {"type": "string"}, "defendant": {"type": "string"},
          "claim_amount": {"type": "number", "description": "Цена иска в рублях (Decimal, копейки сохраняются)"},
          "basis": {"type": "string", "description": "Фабула/основание (для petition_deferral — причина отсрочки)"},
          "petitum": {"type": "string", "description": "Просительная часть"},
          "case_number": {"type": "string"}, "case_id": {"type": "string", "description": "Дело из локального реестра: подтянуть суд/стороны/номер"},
          "pretrial": {"type": "string", "description": "Сведения о соблюдении претензионного порядка"},
          "measure": {"type": "string", "description": "Испрашиваемая мера обеспечения (petition_interim)"},
          "questions": {"type": "string", "description": "Вопросы эксперту (petition_expert)"},
          "experts": {"type": "string", "description": "Кандидатуры экспертных учреждений (petition_expert)"},
          "counter_security": {"type": "string", "description": "Встречное обеспечение (petition_interim)"},
          "interest_start": {**DATE, "description": "Начало периода процентов ст. 395 ГК РФ (требует claim_amount)"},
          "interest_end": {**DATE, "description": "Конец периода процентов ст. 395 ГК РФ"},
          "applicant_type": {"type": "string", "enum": ["individual", "entity"], "description": "Заявитель для тарифа пошлины (default entity)"}},
          "required": ["doc_type"], "additionalProperties": False}},
]


def tool_list():
    return {"tools": [{"name": t["name"], "description": t["description"], "inputSchema": t["schema"]} for t in TOOLS]}


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
    as_of = values.pop("as_of", None)
    if values:
        raise TypeError(f"unknown fees arguments: {sorted(values)}")
    return {"jurisdiction": jurisdiction, "fee_kind": fee_kind,
            "applicant_type": applicant_type, "claim_amount": claim_amount,
            "claim_class": claim_class, "as_of": as_of}


def _mcp_state_dir():
    # S3-19: раньше дублировал resolution без expanduser()/resolve() — при
    # SUD_CLI_STATE_DIR="~/x" или относительном пути CLI и MCP работали с
    # РАЗНЫМИ базами. Делегируем каноническому резолверу config.
    from .config import get_state_dir
    return get_state_dir()


def _call(name, args):
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
        for k in ("pretrial_start", "pretrial_end"):
            if values.get(k):
                values[k] = date.fromisoformat(values[k])
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
        # S3-2: MCP-агент не получает оракул чтения произвольных файлов —
        # source=file скоупится корнем (state_dir по умолчанию, переопределяется
        # SUD_CLI_VERIFY_FILE_ROOT).
        file_root = os.environ.get("SUD_CLI_VERIFY_FILE_ROOT") or _mcp_state_dir()
        return verify_quote(
            values["text"], values["source"],
            act=values.get("act"), article=values.get("article"), date=values.get("date"),
            case_id=values.get("case_id"), path=values.get("path"),
            state_dir=_mcp_state_dir(), max_fuzzy=values.get("max_fuzzy", 0.0),
            file_root=file_root)
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
            # v2.3.3 (панель glm R2): тот же guard класса, что CLI; без него
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
        # v2.3.3 (панель glm R1): строго {1,true,yes,on} как в bridge — "0"/"off" раньше
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
        from .court_watcher_bridge import run_bridge_command
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
    if name == "sud_cli_npa_download":
        LIVE_MSG = "live-источники отключены (S2-1..S2-5: egress обходится)"
        if os.environ.get("SUD_CLI_LIVE_OK", "").strip().lower() not in {"1", "true", "yes", "on"}:
            return {"ok": False, "status": "blocked", "error": LIVE_MSG,
                    "meta": {"command": "npa download"}}
        from .npa_downloader import NpaDownloadError, download_npa
        try:
            payload = download_npa(
                eo_number=args.get("eo_number"),
                url=args.get("url"),
                source=args.get("source", "pravo"),
                out_dir=_mcp_state_dir() / "npa",
                extract_text=bool(args.get("extract_text", True)),
            )
        except NpaDownloadError as exc:
            return {"ok": False, "error": f"DOWNLOAD_FAILED: {exc}",
                    "meta": {"command": "npa download"}}
        if isinstance(payload, dict):
            payload.setdefault("warning", "live-источники включены через SUD_CLI_LIVE_OK=1 (S2-1..S2-5: egress обходится)")
            if isinstance(payload.get("meta"), dict):
                payload["meta"].setdefault("warning", payload["warning"])
        return payload
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
    # S3-1: agent-driven update disabled (RCE via arbitrary pip target) —
    # инструмент намеренно исключён из TOOLS; гейт handle_request отвечает
    # -32602 раньше, чем сюда попадут. Хендлер не существует.
    if name == "sud_cli_draft":
        from .drafter import draft_document, load_case_fields
        # S3-2: MCP-контур не получает примитива записи файлов — out/format
        # исключены из схемы (гейт отвечает -32602); здесь — defense in depth.
        if "out" in (args or {}) or "format" in (args or {}):
            raise ValueError("out/format are CLI-only; MCP returns markdown content")
        values = dict(args or {})
        doc_type = values.pop("doc_type", None)
        if doc_type is None:
            raise ValueError("doc_type is required")
        if doc_type not in DRAFT_TYPES:
            raise ValueError(
                f"unknown doc_type: {doc_type!r}; допустимые типы: {', '.join(DRAFT_TYPES)}"
            )
        values["claim_amount"] = _claim_amount(values.pop("claim_amount", None))
        doc_format = values.pop("format", None)
        # case_id реестра -> суд/стороны/номер; явные аргументы вызова значат то
        # же самое и остаются приоритетными (load_case_fields кладёт их первыми).
        registry = load_case_fields(values.pop("case_id", None), None, _mcp_state_dir())
        return draft_document(doc_type, doc_format=doc_format, **{**registry, **values})


def _validate_schema(tool: dict, args: dict) -> str | None:
    """S3-3: серверная валидация inputSchema до диспетчеризации.

    Гейт структурный: required и additionalProperties=false — обязательны
    для всех тулов. Семантика (enum с легаси-алиасами, строковые числа
    ради копеек, unknown kind → статус ручной проверки) остаётся у
    хендлеров — это их документированный контракт (см. описания схем).
    bool отклоняется везде: JSON true не бывает ни строкой, ни числом.
    """
    schema = tool.get("schema", {})
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            return f"missing required argument: {key}"
    if schema.get("additionalProperties") is False:
        for key in args:
            if key not in props:
                return f"unknown argument: {key}"
    for key, value in args.items():
        if isinstance(value, bool):
            spec = props.get(key)
            if isinstance(spec, dict) and spec.get("type") in (
                    "string", "integer", "number"):
                return f"argument {key!r}: expected {spec['type']}, got bool"
    return None


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
        params = request.get("params", {})
        tool_name = params.get("name", "")
        # S3-4: неизвестный инструмент — JSON-RPC -32602 (Invalid params), а
        # не молчаливый isError=False с текстом "null".
        if tool_name not in {t["name"] for t in TOOLS}:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602,
                              "message": f"Unknown tool: {tool_name!r}"}}
        # S3-3: контрактная валидация аргументов по inputSchema до _call.
        schema_error = _validate_schema(next(t for t in TOOLS if t["name"] == tool_name),
                                        params.get("arguments", {}))
        if schema_error:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": schema_error}}
        try:
            value = _call(tool_name, params.get("arguments", {}))
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
        # v2.3.3 (панель beam R1): не-JSON строка убивала serve_stdio целиком.
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
