# PostgreSQL: локальная БД и транзакционные инструменты

Реализовано: версия миграции с checksum, organizations/memberships/domains,
Item/Event, идемпотентность, outbox и журнал успешных записей/повторов.
Проверяемая конфигурация CI — PostgreSQL 16 и Python 3.12.
HTTP/MCP transport, OAuth, обработчик Plaud и четыре ИИ-проверки сюда не входят.

## Подключение

1. Подготовить отдельную БД и административное подключение. Создать отдельную роль
   приложения LOGIN, NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE, без членства
   в административных ролях. Пароль передавать через секреты вашей среды.
2. Установить `python -m pip install -r requirements.txt`.
3. Задать DATABASE_ADMIN_URL и DATABASE_APP_ROLE в окружении. Выполнить:

```bash
python -m core.db.migrate
```

Мигратор не создаёт БД/пользователей. DDL и запись версии выполняются одной
транзакцией под advisory lock. Повторный запуск проверяет checksum, не пересоздаёт
данные. Изменение применённой SQL-миграции запрещено: добавляйте следующий файл.
Автоматического destructive downgrade нет; откат релиза планируется отдельно.

4. Администратор добавляет organization, membership со scopes и organization_domains.
   App-role может только читать эти таблицы и не умеет сама повышать себе права.
5. Задать DATABASE_URL с подключением ограниченной роли и создать ядро:

```python
import os
from core import Core, Registry
from core.db import PostgresStore

registry = Registry()
# registry.register(trusted_plugin_module)
core = Core(registry, None, persistence=PostgresStore(os.environ['DATABASE_URL']))
# context выдаёт аутентифицированный host:
# organization_id, actor_id, idempotency_key для записи.
# await core.catalog_async(context, 'mcp')
# await core.dispatch(plugin, tool_name, arguments, context)
```

DSN не сохранять в файлы репозитория и не показывать в журналах. Для удалённой БД
использовать TLS с проверкой сертификата (`sslmode=verify-full` и доверенный CA).
Приложение отклоняет superuser/BYPASSRLS/CREATEROLE/CREATEDB и владельца таблиц.
Отдельно проверьте отсутствие лишних членств и grants у роли перед эксплуатацией.

## Обработчик плагина

Контракт async handle(name, arguments, context) и ToolResult сохранён.
В режиме БД context['repository'] — UnitOfWork на время одного вызова:

- `await repository.create_item(kind, meta)` создаёт ingested Item + Event + outbox.
- `await repository.get_item(id)` читает только свою организацию и домен.
- `await repository.update_item(id, expected_revision, meta)` увеличивает revision;
  устаревшая revision вызывает CONFLICT и откатывает вызов.

Не сохранять UnitOfWork за пределами handler, не выполнять commit самостоятельно.
SQL-подключение является внутренней деталью. Плагины — доверенный код, не sandbox.
Для доменных таблиц добавлять методы repository и миграции с теми же границами.
Произвольной выдачи published нет: статусы пока ingested/needs_review/archived.
Будущий публикационный барьер требует отдельной миграции и проверки pipeline.

## Повторы и транзакции

Для каждой записи нужен idempotency_key длиной 1–200 из букв ASCII, цифр, `._:-`.
Host передаёт ключ отдельно от arguments. Ключ резервируется по
(organization_id, operation, key) внутри той же транзакции, что Item/Event/outbox/result.
Резервация учитывает actor_id и SHA-256 канонического JSON аргументов.

- Одновременные повторы ожидают одну транзакцию и получают сохранённый результат.
- Тот же ключ с другими аргументами или actor возвращает CONFLICT без чужого результата.
- Права и включённый домен проверяются заново перед каждым повтором.
- Ошибка handler, некорректный результат, тайм-аут или отмена откатывают изменения.
- Потеря ответа в момент commit имеет неопределённый исход для клиента: повторять
  **тот же** ключ. Новый ключ может создать повторный бизнес-объект.
- `open_world` записи блокируются: внешние действия должны идти через outbox consumer.

Гарантия распространяется на записи в этой транзакции. Нельзя атомарно откатить
произвольный HTTP-вызов из handler. Read-only выполняется в READ ONLY транзакции.
Кэш идемпотентности хранится до явной политики очистки; её здесь ещё нет.
Проверка прав действует на момент начала вызова; отзыв не отменяет уже исполняющийся вызов.

## Outbox

`claim_outbox(context, domain)` требует `<domain>:outbox`; использует
FOR UPDATE SKIP LOCKED, ограниченный lease и уникальный lease_token.
`finish_outbox(..., delivered=True)` подтверждает только текущий неистёкший lease.
`delivered=False` планирует повтор; исчерпанные попытки переходят в dead_at.
Зависший worker допускает повторный захват после истечения lease; старый ACK отклоняется.

Это хранилище очереди, не работающий отправитель сообщений. Доставка будет at-least-once:
получатель обязан подавлять дубли по organization_id/event_id. Внешний транспорт,
расписание workers, операционный UI dead-letter и административный replay — будущий этап.

## Изоляция и ограничения

На tenant-таблицах включены ENABLE/FORCE RLS; tenant/domain устанавливаются transaction-local.
Нет общего pool: каждое выполнение открывает отдельное подключение; пул и лимит
конкурентности добавляются отдельно перед высокой нагрузкой.
Составные FK исключают связь Event с Item чужой организации/домена.
Events и audit не дают app-role UPDATE/DELETE. Отклонённые/неуспешные запросы
не записываются в постоянный аудит этого этапа: их мониторинг нужен в host.

RLS дополняет авторизацию, но не защищает от владельца административного DSN или
произвольного доверенного кода, который изменяет session settings. Identity нельзя
брать прямо из тела запроса. Сеть сервиса должна быть закрыта до реализации host auth.

## Тесты

Без тестовой БД запускаются 21 локальный тест; интеграционные тесты явно skipped.
Для отдельной одноразовой PostgreSQL БД с ролью ecosystem_app задать
TEST_DATABASE_ADMIN_URL, TEST_DATABASE_URL и TEST_DATABASE_DISPOSABLE=yes:

```bash
python -m unittest discover -s tests -v
```

Тесты изменяют тестовые данные и запись checksum. Не запускать на рабочей БД.
GitHub Actions создаёт одноразовый PostgreSQL service и ограниченную роль автоматически.
Фиксированные пароли в workflow относятся только к этому изолированному service.

Документация: [PostgreSQL RLS](https://www.postgresql.org/docs/16/ddl-rowsecurity.html),
[INSERT/ON CONFLICT](https://www.postgresql.org/docs/16/sql-insert.html),
[Psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/transactions.html).
