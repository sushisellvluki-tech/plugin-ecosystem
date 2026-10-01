# MCP-интерфейс

Содержание: адреса; версии; маппинг; авторизация; клиенты; проверки; источники.

## Адреса

Основной транспорт — Streamable HTTP, путь `/{domain}/mcp`. Суффикс URL не определяет протокол. Старый HTTP+SSE — отдельный режим: `/{domain}/mcp/sse` плюс обратный POST-маршрут SDK. Одного SSE GET недостаточно.

| Домен | Основной путь | Опциональный legacy |
|---|---|---|
| meetings | /meetings/mcp | /meetings/mcp/sse |
| tasks | /tasks/mcp | /tasks/mcp/sse |
| documents | /documents/mcp | /documents/mcp/sse |
| finance | /finance/mcp | /finance/mcp/sse |

Это будущие пути, не работающие публичные службы. Имя подключения — ярлык клиента, не способ авторизации. Общий `/mcp` возможен позднее с фильтрацией доступных пользователю доменов.

## Версии

На 02.10.2026 опубликована редакция 2026-07-28: metadata передаётся с каждым запросом вместо initialize handshake; POST возвращает JSON или SSE, прежних протокольных сессий и GET stream нет. Профиль 2025-11-25 использует initialize/initialized и может выдавать session ID. Не смешивать правила.

Планировать оба профиля при подтверждённой поддержке SDK/клиента. Старый двухканальный HTTP+SSE подключать только по необходимости. Фиксировать версии SDK, протокола и клиента, фактически прошедшие тест. Не обещать универсальную совместимость.

## Адаптер

| ToolSpec | MCP Tool |
|---|---|
| name, description | name, description |
| input_schema | inputSchema |
| output_schema | outputSchema |
| read_only | annotations.readOnlyHint |
| idempotent | annotations.idempotentHint |
| destructive | annotations.destructiveHint |
| open_world | annotations.openWorldHint |

Успех: data в structuredContent и сериализованная копия в text content. Ошибка выполнения: isError=true с безопасным кодом и сообщением. Протокольные ошибки, аутентификация и транспорт — ответственность SDK/host. Аннотации не дают права.

manifest.json принадлежит нашему загрузчику; это не обязательный «MCP manifest». Обнаружение инструментов выполняется протоколом. Порт add_mcp_endpoint реализовать официальным SDK; не копировать второй реестр через независимые декораторы.

## Авторизация

Для интерактивных внешних клиентов предусмотреть OAuth resource server: Protected Resource Metadata/discovery, проверка issuer/audience/expiration, scopes и организации. Токен передаётся в Authorization каждого запроса, не в URL. Неверный токен даёт 401, недостаточные права — 403. Генератор OAuth не реализует.

Для Make.com server-to-server и клиентов с подтверждённой поддержкой заголовков допустим scoped API key. Это не замена OAuth discovery. Ключ привязывать к организации, сервисному аккаунту и правам, хранить хеш, поддерживать отзыв/срок действия. Не выдавать один общий бессрочный ключ на весь домен. Не использовать токены Plaud/OpenAI как наши MCP-токены.

Отсутствие поля токена на скриншоте не доказывает способ входа. Если клиент не поддерживает нужный OAuth/заголовок, решать совместимость, а не открывать анонимный доступ.

На границе: HTTPS, проверка Origin при наличии, лимиты размера/времени, отсутствие секретов в логах, localhost при локальной разработке. Защищать оба legacy-маршрута.

## Клиенты

| Клиент | Что подтвердить |
|---|---|
| xAI API / Grok через API | Документация описывает HTTP/SSE и authorization/headers; проверить версию протокола |
| Grok: пользовательский экран | Проверить этот конкретный интерфейс; возможности API не переносить автоматически |
| Claude | Продукт web/desktop/Code, версия/тариф, remote transport, OAuth |
| Cursor | Версия, transport, заголовки/OAuth |
| OpenAI API | Выбрать remote MCP или function calling; это разные пути |

Успех одного клиента не подтверждает остальные. Сервер проверяет доступ независимо от client allowed_tools.

## Проверка сервера

Запустить `scripts/mcp-check.sh URL --protocol 2025-11-25` либо `--protocol 2026-07-28`. Секрет — только MCP_ACCESS_TOKEN в окружении, не аргумент/URL. HTTP разрешён только для loopback, удалённо нужен HTTPS.

Скрипт проверяет отказ анонимному вызову, авторизованный непустой tools/list и выбранный профиль. Старый профиль проходит initialize/initialized; новый посылает metadata и заголовки каждого запроса. Для проверки вызова явно указать `--tool meetings__describe --arguments '{}'`. Требуется readOnlyHint=true; реальные эффекты инструмента всё равно должны быть известны оператору.

Smoke-check не доказывает OAuth discovery, tenant-изоляцию, устойчивость и работу конкретного внешнего клиента. Legacy HTTP+SSE проверять официальным SDK/Inspector с обратным POST. Возврат HTML/health/HTTP 200 сам по себе не является успехом MCP.

## Источники

Проверено 02.10.2026; перепроверять перед runtime:

- [Streamable HTTP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
- [Versioning and Compatibility](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)
- [Transport 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
- [Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [xAI Remote MCP](https://docs.x.ai/developers/tools/remote-mcp)
