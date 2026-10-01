# plugin-ecosystem

Plugin ecosystem core with reusable plugin architecture.

## Заход 1: контракт и заготовки

Репозиторий содержит готовый навык проектирования экосистемы, контракт плагина,
архитектуру, генератор домена и адаптеры регистрации OpenAI function calling / MCP.
Это комплект для разработки, а не запущенный сервер.

## Состав

- [SKILL.md](SKILL.md) — порядок работы и границы комплекта.
- [references/plugin-contract.md](references/plugin-contract.md) — tools(), handle(), контекст, схемы и ошибки.
- [references/core-architecture.md](references/core-architecture.md) — ядро, БД, пакетная обработка Plaud, четыре проверки и Make.com.
- [references/openai-compat.md](references/openai-compat.md) — экспорты для Responses и Chat Completions.
- [references/mcp-expose.md](references/mcp-expose.md) — транспорт, авторизация и проверка MCP.
- `scripts/` — генератор и проверки вместе с Python-зависимостями этих скриптов.
- `assets/plugin-template/` — шаблоны плагина и адаптеров.
- `core/` и `plugins/` — пустые заготовки с `.gitkeep`, чтобы Git сохранял каталоги.

В исходном готовом навыке четыре файла references. Упомянутый пятый файл
в доступном комплекте отсутствует; новый документ вместо него не создавался.

## Создание домена

Нужны Bash и Python 3.10+; генератор и локальная проверка используют стандартную библиотеку.
Из корня репозитория:

```bash
bash scripts/new-plugin.sh meetings "$PWD"
bash scripts/check-tools.sh "$PWD/plugins/meetings"
```

Генератор создаёт диагностический инструмент, manifest, план адресов и адаптеры.
Повторный запуск для существующего домена завершается отказом без перезаписи.
Планируемые домены: meetings, tasks, documents, finance.

## Что ещё предстоит реализовать

`core/` пока не содержит реестр, диспетчер, БД или сервер. PostgreSQL, миграции,
OAuth, настоящий MCP-транспорт, четыре ИИ-проверки, кабинет и бизнес-логика доменов
описаны как дальнейшие этапы. Генератор не запускает эти компоненты.

Основной планируемый MCP-адрес — `/{domain}/mcp`; `/{domain}/mcp/sse` — отдельный
режим совместимости. Проверка `scripts/mcp-check.sh` требует уже работающего
настоящего сервера; локальная проверка контракта не подтверждает сетевую совместимость.

## Лицензия

[MIT](LICENSE).
