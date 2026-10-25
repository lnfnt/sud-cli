# sud-cli

![license](https://img.shields.io/badge/license-AGPL--3.0-orange) ![python](https://img.shields.io/badge/python-3.10%2B-blue) ![release](https://img.shields.io/badge/release-v0.1.0-informational) ![MCP](https://img.shields.io/badge/MCP-65%20tools-brightgreen) ![artifacts](https://img.shields.io/badge/artifacts-GPG--signed-green)


<p align="center"><img src="assets/poster.jpg" width="820" alt="sud-cli poster"></p>

CLI-инструмент для юридической работы с российскими судами: поиск практики и карточек дел,
процессуальные сроки, госпошлины, реестр доказательств, НПА. Offline-first: ядро работает без
сети, live-адаптеры ограничены документированными источниками. Встроенный MCP-сервер — 65
инструментов.

**Русский** · [<img src="assets/flags/gb.svg" height="16" align="top" alt="EN"> English](README.en.md)

## Установка

```bash
pip install git+https://github.com/lnfnt/sud-cli.git
sud-cli --help
```

На PyPI пакет не опубликован — установка только из репозитория.
Локальный checkout: `python3 -m venv .venv && . .venv/bin/activate && pip install .`
Python >= 3.10, рантайм-зависимостей нет.

## Возможности

- **Поиск практики и дел** — единый запрос по официальным источникам (sudact,
  publication.pravo.gov.ru, картотека арбитражных дел), карточки и таймлайны (КАД), подбор
  практики по номеру дела и предмету спора.
- **Карточка контрагента (ЕГРЮЛ/ЕГРИП)** — открытые реестры ФНС (ст. 6 129-ФЗ), без капчи и
  защищённых эндпоинтов.
- **Проверка банкротства** — основной путь через картотеку арбитражных дел; перепроверка ЕФРСБ
  ручным контуром с фиксацией provenance (`efrsb link` / `efrsb record`).
- **ФССП, полу-автомат** — `fssp assist`: инструмент открывает банк данных, заполняет форму и
  отправляет запрос; капчу решает человек (скриншот → код). Результат с provenance попадает в
  реестр доказательств. Ручной контур `fssp check` доступен всегда.
- **Сроки и пошлины** — расчёт по ГПК/АПК/КАС РФ с производственным календарём; тарифы
  госпошлин в проверяемых JSON-файлах правил.
- **Досье дела** — документы и версии, представители, доказательства, сроки, черновики;
  экспорт; идемпотентный purge строго внутри state-директории.
- **Корпус постановлений ВС/ВАС** — `plenum list | plenum search`.
- **MCP-сервер** — `sud-cli mcp`, 65 инструментов для агентских рантаймов.
- **§13 AGPL** — `sud-cli license`: лицензия, provenance сборки, способ получить исходники.

## Команды (Chain · Reps · Conflicts · Reports · Backup · Metrics · Purge)

```
sud-cli deadlines chain А40-11111/25            # цепочка процессуальных сроков (Chain)
sud-cli case rep add | case rep list            # представители по делу (Reps)
sud-cli calendar conflicts                      # конфликты слушаний (Conflicts)
sud-cli report portfolio / report case          # отчёты (Reports)
sud-cli backup create | backup verify | backup restore   # бэкап состояния (Backup)
sud-cli case export / case purge / metrics      # экспорт, purge, метрики (Metrics, Purge)
sud-cli fssp assist --kind ur --inn 7704361225  # ФССП, полу-автомат
sud-cli efrsb link --kind ul --inn 7704361225   # ЕФРСБ, ручной контур
sud-cli plenum list | plenum search ТЕКСТ       # корпус ВС/ВАС
sud-cli mcp                                     # MCP-сервер (65 tools)
sud-cli license                                 # §13 AGPL
```

## Разделы реестра дела

| Раздел | Содержание |
|---|---|
| **Evidence** | доказательства с provenance: кто, когда, метод, приложенный файл |
| **DocHistory** | версии процессуальных документов дела |
| **Checklist** | чек-листы из файлов правил |
| **Forms** | шаблоны и заполнение процессуальных форм |

## Ограничения

- Автоподача не реализована и не планируется: документ в внешние системы отправляет человек.
- Практика ищётся по формальным признакам; оценку перспектив даёт юрист, не инструмент.
- Это не юридическая консультация: расчёт сроков, форматирование, поиск.

## Сетевая политика

Live-адаптеры работают через ваш прокси (`SUD_EGRESS_PROXY`) с fail-closed DNS-pinning;
хост с ответом «лимит подключений» уходит в cooldown автоматически. Обход капчи автоматикой
запрещён конструкцией (см. ФССП/ЕФРСБ).

## Проверка релиза

<details>
<summary>Контрольные суммы и GPG</summary>

```bash
sha256sum -c SHA256SUMS
gpg --import release_pub.asc
gpg --verify sud_cli-0.1.0.tar.gz.asc   # fingerprint F4492D47B75F12D025400C19291E4ACACA58902A
```
</details>

## Вклад и лицензия

AGPL-3.0-only; вклад требует DCO sign-off и права перелицензирования владельцем
(CONTRIBUTING.md). Уязвимости — приватно (SECURITY.md).

Автор и правообладатель: [lnfnt](mailto:lnfnt@proton.me).

Автор и правообладатель: [lnfnt](mailto:lnfnt@proton.me).
