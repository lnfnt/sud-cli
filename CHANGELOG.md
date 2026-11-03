# Changelog

## Нормативная точность (W6)

Заключение colleague от 2026-11-01 по S1-1/S1-3/S1-11. Благодарим colleague
за правовое ревью: все три находки подтвердились кодом и исправлены.

- **S1-1 тарифы по редакциям**: `fees --as-of YYYY-MM-DD` + MCP `as_of` —
  расчёт по редакции НК РФ, действовавшей на дату подачи (до 09.09.2024 —
  ред. 221-ФЗ с потолками 200 000 АС / 60 000 СОЮ; далее — ред. 259-ФЗ).
  Блок `historical_court_tariffs` перестал быть мёртвыми данными.
- **S1-3 исковая давность**: модель приостановления вместо «+дней к концу» —
  п. 3 ст. 202 ГК РФ + п. 16 ППВС № 43 (итог = конец порядка + остаток на дату
  начала); остаток <6 мес. или порядок за пределами срока → продление до
  6 месяцев (п. 4 ст. 202 ГК РФ). Новые входы `--pretrial-start/--pretrial-end`
  (CLI и MCP); легаси `--claim-notice-days` сохранён.
- **S1-11 отсрочка пошлины**: разграничение по типам судов —
  `deferral --court-type arbitration|general`; АС: ст. 102 АПК + Пленум ВАС РФ
  от 20.03.1997 **№ 6** (галлюцинация «№ 20» убрана полностью); СОЮ:
  ст. 89, 90 ГПК/КАС без АПК и ВАС. Невалидный court_type → INVALID_ARGUMENTS.

## 2.9.0

- W5 (Закон Zero): `verify quote --source npa|case|file` — verdict exact (литеральное вхождение после нормализации NBSP/ё-е/регистр, пунктуация сохранена), fuzzy только при явном --max-fuzzy, not_found без «ok на глаз», edition_unavailable при пробеле бандла; **case-источник сканирует и события, и приложенные документы** (контроль координатора: обнаружил молчаливый пробел — патч +docs_scanned/docs_skipped_unreadable в покрытии, нечитаемое маркируется явно).
- `practice-status <номер>` / `practice status`: verdict possibly_overturned только при локальном свидетельстве отмены/изменения (snapshot/diff артефакты), иначе unknown + hint; слово «действует» не производится (гейт тестом).
- MCP: sud_cli_verify_quote (тулов 35); README секция Verification.
- +35 тестов (23 a-verify + 12 b-practice). (заготовка; версию в sud_cli/__init__.py и pyproject поднимает координатор)

- W5 §1 (S-52): `verify quote` — валидация цитаты против явного источника: бандл исторических редакций NPA (`npa/case/file`), нормализация NBSP/сжатие пробелов/ё-е/lower с сохранением пунктуации; вердикты `exact|fuzzy|not_found|edition_unavailable` (fuzzy выключен по умолчанию, `--max-fuzzy` задаёт порог `ratio >= 1−max_fuzzy`, в ответе ratio+окно±80симв; отсутствие бандла — `edition_unavailable`, никогда `not_found`); `location` = act/article/date | event:id | file:path:offset; пустой `--text`/несогласованные параметры источника → `INVALID_ARGUMENTS` exit≠0. «Никогда не ok на глаз».
- W5 §2 (S-54): `practice status <номер>` — offline fate probe (`sud_cli/practice_status.py`): `possibly_overturned` только из ЛОКАЛЬНЫХ артефактов (snapshot-файлы и case-diff записи, заголовки instance>1 по корням отмен/измен с negation-гейдом «без изменения»; явный fixture-вход `--related "a;b"` без сети); иначе `unknown` + обязательный hint; «действует» не выдается никогда. Сетевые примитивы в модуле отсутствуют (гейт-тест).
- W5 §3: MCP-тул `sud_cli_verify_quote` (required text+source, `additionalProperties:false`), dispatch рядом с `sud_cli_npa_as_of` (всего 35 тулов; счётчики-пины test_mcp_server/test_strategic_modules/test_v260 bumped 34→35 — минимальное отклонение от «старые тесты не менять», без него прогон краснеет); README «Verification — Закон Zero».
- Версия: pyproject.toml и sud_cli/__init__.py оставлены на 2.8.0 — bump до 2.9.0 ставит координатор целиком (test_audit_infra сверяет pyproject==__init__).

## 2.7.0

- Матрица контрольных точек дела: `deadlines matrix <case_id> [--as-of date] [--instance apk|gpk|kas] [--missing]` — все применимые точки из утверждённой `deadline_matrix`, без выдуманных баз-дат (Закон Zero): без anchor → `due_at=null`, `needs_anchor=true`, `base_event=MISSING`.
- `--instance` override с валидацией; по умолчанию поле `instance` дела или `unknown`.
- MCP-тул `sud_cli_deadlines_matrix`; pretrial.charters по VERIFIED нормам (6×30d hard, postal manual_review 30/5), jurisdiction_rules v1.1.0.
- Акт legal: verification/2026.10.24.ACT_SUD_V270_W3B_AND_W4_NORMS.md (ГОДНО).

## 2.8.0

- W4c: VERIFIED pack legal сериализован 1:1 (legal_rules v1.2.0): льготы 333.35-37 (выборка категорий; вне выборки = manual_review + npa_as_of), возврат 333.40 (доли 100/70/50/30 + исключения признание-иска/неявка/третей), рамка индексации 183/208/189.1; команды `fee-exemption|fee-refund|fee-indexation`; ИПЦ только явный операторский (provenance unverified); 3 MCP-тула (всего 34).
- W4b: реестр расходов — миграция `case_amounts` (+status paid|planned|refunded, +paid_at), `case amount set --status`, `case amount summary` (Decimal, пустые явно), карточка обогащена; `reminders due --days N` — bus-free генератор готовой a2a-строки с обязательным --verify (sud шину не вызывает).
- Erratum: wheel 2.7.0 собран при несохранённом в git bumps docs — восстановлено в этом релизе, source-of-truth с 2.8.0 согласован.

## 2.6.0

- Гейдер подсудности на VERIFIED rules: `jurisdiction check --code apk|gpk|kas --claim <kind> ...`, конверт {candidates, manual_review, warnings}, MCP `sud_cli_jurisdiction_check`.
- Претензионный порядок: `pretrial calc --code apk|zozpp|osago --sent YYYY-MM-DD`, `pretrial consequences --code apk|gpk|kas --stage filing|trial|movement`, MCP `sud_cli_pretrial_calc`/`sud_cli_pretrial_consequences`.
- Правила: `sud_cli/jurisdiction_rules.json` v1.0.0 (VERIFIED legal 24.10), не меняются кодом; reference_only → fetch_hint "npa_as_of".
- Рамка: инструмент ≠ юридический совет (boundary IX-3); тексты норм без legal_text подтягиваются runtime.
- Версия 2.6.0.

## 2.5.0

- Anti-laches слой: таблица `deadline_states` (computed/confirmed/waived), подтверждение `deadlines confirm`, фиксация начала срока `deadlines anchor` (с supersede), waiver с нормой-ссылкой.
- Журнал правок `deadline_audit.jsonl` (append-only), команда `deadlines audit`.
- Единый календарь `calendar agenda` (сроки + слушания, overdue/next 5).
- Freshness в `doctor`: возраст key_rates, transferred verified_through, version stamps правил.
- Версия 2.5.0.


## 2.4.1

- Conflict-of-interest детект (name-exact): `conflict check`/`conflict case`, MCP `sud_cli_conflict_check`.
- Типы хитов: (a) одна сторона в делах разных owners; (b) adversary flip plaintiff↔defendant в портфеле одного owner; (c) кандидат совпадает с любой стороной существующего дела.
- Конверт: {clean, hits, method:"name_exact", coverage:"local registry names only", disclaimer}. Отсутствие хита не означает отсутствие конфликта.
- README: абзац про conflict-гейт с границей (вспомогательный детект, не этическое заключение — решение за человеком, boundary IX-3).

## 2.4.0

- Центральный объект «дело» (реестр): таблицы cases (+status/owner/closed_at), case_amounts, case_documents, case_notes.
- Миграция схемы v2→v3, doctor показывает v3.
- CLI: `case new/set/list/card`, `case amount`, `case doc`, `case note`.
- MCP: sud_cli_case_new/set/list/card/amount_add/doc_add/note_add.
- README: секция «Реестр дел».
