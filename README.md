# plugin-ecosystem

Plugin ecosystem core with reusable plugin architecture.

## Текущее состояние: Заход 4 — HTTP, авторизация и MCP

Репозиторий содержит готовый навык проектирования экосистемы, контракт плагина,
архитектуру, генератор домена и адаптеры регистрации OpenAI function calling / MCP.
Добавлено рабочее ядро вызовов в одном процессе: реестр, политика доступа, диспетчер и порты регистрации.
Добавлен опциональный PostgreSQL-слой: миграции, tenant-хранилище и транзакционные записи.
Добавлен запускаемый HTTP/MCP-хост с Bearer-ключами и проверкой JWT.
Внешнее развёртывание пока не выполнено. Инструкция: [docs/server.md](docs/server.md).

## Состав

- [SKILL.md](SKILL.md) — порядок работы и границы комплекта.
- [references/plugin-contract.md](references/plugin-contract.md) — tools(), handle(), контекст, схемы и ошибки.
- [references/core-architecture.md](references/core-architecture.md) — ядро, БД, пакетная обработка Plaud, четыре проверки и Make.com.
- [references/openai-compat.md](references/openai-compat.md) — экспорты для Responses и Chat Completions.
- [references/mcp-expose.md](references/mcp-expose.md) — транспорт, авторизация и проверка MCP.
- `scripts/` — генератор и проверки вместе с Python-зависимостями этих скриптов.
- `assets/plugin-template/` — шаблоны плагина и адаптеров.
- `server/` — ASGI-хост, официальный MCP SDK, проверка токенов и scopes.
- `core/` — реестр, диспетчер, политика доступа и адаптеры портов.
- `plugins/` — место для генерируемых доменов; бизнес-домены пока не реализованы.
- `tests/` — проверки ядра и интеграции с генератором.

После объединения с загруженным комплектом references содержит девять документов:
четыре исходных (включая MCP) и пять адаптированных проектных материалов:
[БД](references/db-pattern.md), [проверки](references/checks-pipeline.md),
[кабинет](references/cabinet-pattern.md), [Plaud](references/plaud-integration.md),
[домены](references/domain-recipes.md).

## Создание домена

Для полного комплекта рекомендованы Bash и Python 3.12; генератор и локальная проверка используют стандартную библиотеку.
Из корня репозитория:

```bash
bash scripts/new-plugin.sh meetings "$PWD"
bash scripts/check-tools.sh "$PWD/plugins/meetings"
```

Генератор создаёт диагностический инструмент, manifest, план адресов и адаптеры.
Повторный запуск для существующего домена завершается отказом без перезаписи.
Планируемые домены: meetings, tasks, documents, finance.

## Что ещё предстоит реализовать

Ядро работает локально в одном процессе; PostgreSQL подключается отдельно.
Подключение OAuth-провайдера, внешнее развёртывание, четыре ИИ-проверки, кабинет
и бизнес-логика доменов остаются дальнейшими этапами. Генератор не запускает эти компоненты.

Основной MCP-адрес — `/{domain}/mcp`; `/{domain}/mcp/sse` — отдельный
планируемый режим совместимости, пока не включённый в хост. Проверка `scripts/mcp-check.sh` требует уже работающего
настоящего сервера; локальная проверка контракта не подтверждает сетевую совместимость.

## Лицензия

[MIT](LICENSE).

## Запуск и проверка ядра

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m core
```

Последняя команда создаёт временный диагностический домен, регистрирует его и
выполняет вызов через ядро. Данные репозитория не изменяются.

`Registry.register(plugin)` атомарно проверяет контракт и фиксирует копии схем.
`Core.catalog(context, style)` отдаёт один каталог в native, responses,
chat_completions или mcp представлении. `Core.dispatch(plugin, name, args, context)`
повторно проверяет доступ, валидирует вход и выход и ограничивает время выполнения.
`core.ports.bind(host, core, plugin)` подключает оба интерфейса к этому каталогу и
диспетчеру. Реализация HTTP/MCP-хоста находится в `server/`; запуск описан в
[docs/server.md](docs/server.md).

Используйте `core.ports.bind` для нового ядра вместо отдельных template register:
так каталоги тоже проходят проверку членства и включённых доменов ядра.

### Границы этапа

- `AccessPolicy` хранит членство, scopes и включённые домены в памяти процесса.
  Изменения теряются при перезапуске. Для постоянного режима используйте PostgresStore.
- Identity должен формировать аутентифицированный host. Нельзя напрямую передавать
  JSON запроса как context. Поле scopes из context игнорируется: права берутся из policy.
- Плагины — доверенный Python-код. Это не sandbox для чужих модулей; read_only —
  контракт автора плагина, который не доказывает отсутствие побочных эффектов.
- Без PostgresStore записи возвращают NOT_IMPLEMENTED. В PostgreSQL-режиме
  записи требуют idempotency_key и используют одну транзакцию; внешние эффекты блокируются.
- Лимиты JSON и кооперативный async timeout действуют внутри процесса. HTTP host
  должен ограничивать тело до декодирования; зависший синхронный код требует
  изоляции worker-процессом. Отмена запроса не гарантирует отмену внешнего эффекта.
- Журнал содержит метаданные завершённых вызовов/ошибок, без аргументов и результатов.
  Это не постоянный полный журнал аудита. Ошибки handler заменяются безопасным сообщением;
  возвращаемые самим плагином сообщения должны быть безопасны по контракту.
- Проверки политик разных организаций не заменяют тесты tenant-изоляции БД.

Валидация схем использует [jsonschema Draft202012Validator](https://python-jsonschema.readthedocs.io/en/latest/validate/)
и ограниченное подмножество контракта; внешние `$ref` не допускаются.

## Создать отдельную экосистему

Из любой директории, используя абсолютный путь к этому репозиторию:

```bash
bash /absolute/plugin-ecosystem/scripts/new-ecosystem.sh meetings /absolute/new-project
cd /absolute/new-project
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
bash scripts/check-tools.sh "$PWD/plugins/meetings"
python -m unittest discover -s tests -v
python -m core
```

Без пути назначения используется `./ecosystem`. Каталог назначения должен отсутствовать,
а его родитель — существовать. Генератор отказывается от перезаписи и от создания
внутри собственного исходного репозитория. Копируются ядро, скрипты, шаблоны, тесты,
reference и лицензия; первый домен создаётся по текущему контракту.
`cabinet/` и `bridges/` остаются пустыми местами для будущей реализации.
Скрипт не устанавливает зависимости, не соединяется с Plaud и не запускает сервер.

Сведения об объединении: [docs/import-review.md](docs/import-review.md).

## PostgreSQL

[Подключение, миграции, гарантии повторов и outbox](docs/postgres.md).
В режиме БД используйте `await core.catalog_async(...)`; порты поддерживают оба режима.
Запуск тестов без БД явно пропускает PostgreSQL-проверки. Workflow
`.github/workflows/postgres.yml` проверяет их на настоящем PostgreSQL 16.
