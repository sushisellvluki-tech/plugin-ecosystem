# Рецепты доменов

Статус: планы реализации, не готовые инструменты. Имена следуют контракту v0.1:
`domain__action`, async handle(), ToolResult, общие scopes и проверка tenant.
Генератор создаёт только `domain__describe` без бизнес-эффектов.

| Домен | Сущности | Кандидаты tools | Особые проверки |
|---|---|---|---|
| meetings | recording, transcript_version, protocol, participant | meetings__ingest, meetings__list, meetings__get, meetings__extract_actions | Исходные фрагменты, спикеры, версии и таймкоды |
| tasks | task, assignment, comment | tasks__create, tasks__list, tasks__assign, tasks__update_status | Исполнитель в организации, переходы статусов, concurrent revision |
| documents | document, version, tag | documents__ingest, documents__search, documents__get_content | MIME/парсер, версия, ссылка на страницу/фрагмент, доступ к файлу |
| finance | transaction, invoice, category | finance__import_statement, finance__list_transactions, finance__summary | Точные суммы/валюты, сверка строк и итога, дубли по источнику |
| comms (дополнительно) | thread, message, followup | comms__ingest_thread, comms__list_threads, comms__summarize | Права источника и участников; отправка требует отдельной команды |

## Порядок

1. `bash scripts/new-plugin.sh <domain> /absolute/project-root`.
2. Определить sources/entities в документации домена и модели/миграции.
3. Добавить ToolSpec, реализовать handler через tenant-scoped repository,
   назначить required_scopes и честные признаки побочных эффектов.
4. Подключить общие четыре и доменные проверки; до публикации хранить черновики.
5. Проверить `bash scripts/check-tools.sh /absolute/project-root/plugins/<domain>`;
   добавить тесты бизнес-логики, БД и прав. Скрипт проверяет диагностическую заготовку,
   не сертифицирует полноценный домен.

Поручение из встречи создаётся сервисом tasks/событием после публикационного барьера;
не писать напрямую в таблицы tasks из meetings. Хранить связи с исходной версией.
Денежные суммы требуют точного decimal/минимальных единиц и валюты, а не float.
Финансовая аналитика не означает разрешение на оплату. Отправка сообщений также
не входит автоматически в приём/анализ переписки.
