## Backend overview

FastAPI-приложение и схема БД (SQLite локально / PostgreSQL на проде).

Краткое описание продукта, роли и функциональность — в корневом [`README.md`](../README.md).

Файл **`data/livingbraiding.db`** (SQLite) — это **одна база**: все таблицы живут внутри этого файла, отдельных файлов на каждую таблицу не будет.

> Примечание: команды ниже предполагают, что текущая папка — `backend/`.

### Key folders

- `app/`: код приложения
  - `main.py`: entrypoint (middleware, подключение роутеров, startup / seed)
  - `routes/`: HTTP-роуты по доменам (клиенты, записи/визиты, склад, ЗП, отчёты, techspec, …)
  - `webui.py`: Jinja2-окружение, фильтры и глобалы шаблонов
  - `auth.py`, `user_roles.py`, `role_access.py`: сессии/куки, мультироли («Кабинет»), проверки доступа
  - `db/`: SQLAlchemy models + session (~65 таблиц)
  - `payroll_fund.py`: журнал фондов ЗП (начисления / сторно / выплаты / переводы)
  - `questionnaire/`: JSON-каталоги/формы анкеты + Pydantic-схемы для `visit_services.details_json`
  - `help_content/`: FAQ по ролям и подсказки «?» (markdown)
  - `media_store.py`: файлы загрузок; бэкап/restore — `routes/techspec_media.py`
  - `seed.py`: dev/prod seed при старте
  - `templates/`, `static/`: серверный HTML + статика
- `alembic/`: миграции (`versions/0001_init.py` … далее по цепочке)
- `scripts/`: прод-запуск (`start_uvicorn.sh` — сначала миграции, потом uvicorn)

### Migrations

Изменения схемы — **новым** файлом в `alembic/versions/`, затем `alembic upgrade head` (без потери данных на проде).

Переписывать `0001_init.py` «под актуальную схему» и удалять локальный `data/livingbraiding.db` допустимо только на пустой личной БД без продакшена. В рабочем проекте так делать нельзя — нужна обычная цепочка миграций.

Локально по умолчанию SQLite (`DATABASE_URL` в `.env` / `.env.example`). На проде — PostgreSQL.

### Design rules (important)

- **No historical recalculation**: цены и настройки могут меняться, но уже зафиксированные визиты/продажи — нет.
  - На сущностях хранятся *snapshots* (цены материала, доля салона, суммы на момент события и т.п.).
  - Отчёты опираются на snapshot-поля и журнал фондов, а не на «текущие» настройки.
- **Money / payroll fund**:
  - Учёт ЗП и долей — журнал `payroll_fund_ledger` (стороны: личный фонд мастера / студийный).
  - Источники начислений: визиты и услуги, консультации, работы на склад, продажи, почасовая работа/помощь и др.
  - Правки задним числом — через сторно + новое начисление; выплаты и переводы — отдельные виды проводок.
  - Закрытый расчётный период блокирует изменения в своём диапазоне.

### Dev commands (PowerShell)

Install deps:

```bash
py -m venv ..\.venv
..\.venv\Scripts\python -m ensurepip --upgrade
..\.venv\Scripts\python -m pip install -r requirements.txt
```

Enable dev seed (optional):

```powershell
$env:ENABLE_DEV_SEED="1"
```

Run migrations:

```bash
..\.venv\Scripts\python -m alembic upgrade head
```

Run server:

```bash
..\.venv\Scripts\python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

### Production (Linux, без Docker)

Если вы запускаете только `uvicorn …`, а `alembic upgrade head` делаете вручную **после**, новый код может упасть при старте: ORM уже читает колонки, которых ещё нет в БД.

Используйте один скрипт — **сначала миграции, потом сервер** ([`scripts/start_uvicorn.sh`](scripts/start_uvicorn.sh)):

```bash
cd /path/to/livingbraiding/backend
source /path/to/venv/bin/activate   # чтобы были python и alembic из venv
chmod +x scripts/start_uvicorn.sh   # один раз
./scripts/start_uvicorn.sh
```

По умолчанию `--host 0.0.0.0 --port 8080` (переменная `PORT`). На VPS без прокси часто нужно `PORT=80 ./scripts/start_uvicorn.sh`.

#### Timeweb Cloud Apps (и аналоги)

Платформа по умолчанию запускает только `uvicorn …` — миграции **не** выполнятся, пока вы **явно** не зададите команду запуска.

1. **Run Command** (рабочий каталог `backend/`):
   ```bash
   bash scripts/start_uvicorn.sh
   ```
2. **Порт HTTP** в панели приложения: **8080** (или тот же, что в переменной `PORT`).
3. Переменная окружения: `PORT=8080` (если в логах видите `Uvicorn running on …:80`, а в деплое — `No HTTP ports discovered`, прокси и приложение слушают **разные** порты → белый экран).
4. **Health check** (если есть): путь `/health`, начальная задержка ≥ 30 с (миграции идут до uvicorn).
5. Хост `*.twc1.net` из панели — правильный; белый экран при «успешном» контейнере почти всегда = порт/маршрутизация, не «неверный домен».

#### DigitalOcean App Platform (и аналоги)

Платформа по умолчанию запускает только `uvicorn …` — миграции **не** выполнятся, пока вы **явно** не зададите команду запуска.

1. **Settings** → **Run Command** (или аналог): укажите скрипт, а не сырой uvicorn. Примеры (зависит от того, что у сервиса задано как *Root Directory* / рабочий каталог):
   - если приложение собирается из корня репозитория и код лежит в `backend/`:
     ```bash
     bash backend/scripts/start_uvicorn.sh
     ```
   - если корень сервиса уже `backend/` (в логах часто `/app/backend/`):
     ```bash
     bash scripts/start_uvicorn.sh
     ```
   Можно вызывать через `bash …` — тогда `chmod +x` не обязателен; иначе добавьте исполняемый бит и закоммитьте файл.

2. **Сбой миграций:** в скрипте стоит `set -e` — если `alembic upgrade head` завершится с ошибкой, контейнер не поднимется (цикл перезапусков). Смотрите логи деплоя/рантайма, исправьте миграцию или схему и задеплойте снова.

3. **Порт:** в скрипте по умолчанию `PORT=80`. Если в настройках App Platform другой HTTP-порт, задайте переменную окружения `PORT` или поправьте команду запуска вместе с настройками платформы.

Open:

- `http://127.0.0.1:8010/`

Run tests:

```bash
..\.venv\Scripts\python -m pytest
```

Validate questionnaire JSON examples:

```bash
..\.venv\Scripts\python -m app.questionnaire.self_check
```

### First start (no seeds): TECHSPEC user

If the DB is empty and you start the app without dev seed, it will create an initial technical user:

- username/password: `techspec` / `techspec`
- role: `TECHSPEC` (has access to all pages, but is not treated as an employee in reports/payout lists)

You can override defaults via env vars:

```powershell
$env:LB_TECHSPEC_USERNAME="techspec"
$env:LB_TECHSPEC_PASSWORD="change_me"
$env:LB_TECHSPEC_DISPLAY_NAME="Техспец"
```

### Медиа-бэкап (TECHSPEC)

Фото хранятся в `LB_MEDIA_ROOT` (по умолчанию `data/uploads`). На эфемерном хостинге каталог пропадает при пересборке — делайте бэкап перед рестартом.

**Первый раз**

1. На главной (роль TECHSPEC): **Скачать manifest.json** и **Скачать всё (backup.zip)**.
2. Локально: `lb-media/manifest.json` + zip-части (при необходимости разбейте архив на части &lt; 1 ГБ для restore).

**Перед следующим рестартом**

1. Загрузите сохранённый `manifest.json` → **Скачать дельта-zip** (только новые файлы).
2. Распакуйте дельту в локальную папку с фото.
3. **Скачайте свежий manifest.json** и замените старый.

**Восстановление на сервер**

Форма «Восстановить на сервер» — по одному zip-файлу за раз. Лимиты (env): `LB_MEDIA_RESTORE_MAX_ZIP_BYTES` (по умолчанию 1 ГБ), `LB_MEDIA_RESTORE_MAX_BYTES` (1.2 ГБ после распаковки).

**Рекомендация для прода:** смонтировать постоянный том на `LB_MEDIA_ROOT`, чтобы не качать бэкап перед каждым деплоем. Object storage (S3/Spaces) — отдельная задача на будущее.

### Уведомления мастерам (Telegram)

Привязка аккаунта: в карточке сотрудника (суперадмин) → «Подключить Telegram» → ссылка `https://t.me/<BOT_USERNAME>?start=<код>`. Вебхук `POST /webhooks/telegram` принимает `/start <код>` и сохраняет `chat.id`.

Переменные окружения (см. `.env.example`):

- `TELEGRAM_BOT_TOKEN` — токен бота (отправка и API)
- `TELEGRAM_BOT_USERNAME` — username бота без `@` (для deep link)
- `TELEGRAM_WEBHOOK_SECRET` — секрет `secret_token` вебхука (заголовок `X-Telegram-Bot-Api-Secret-Token`)

Выставить вебхук (подставьте токен, домен и секрет):

```text
https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<ДОМЕН>/webhooks/telegram&secret_token=<TELEGRAM_WEBHOOK_SECRET>
```

Отправка из CRM в чат идёт через outbox (`process_outbox`); хуки на создание брони — отдельно.
