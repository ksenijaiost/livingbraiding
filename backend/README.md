## Backend overview

This folder contains the FastAPI app and its SQLite/Postgres database schema.

Файл **`data/livingbraiding.db`** (SQLite) — это **одна база**: все таблицы живут внутри этого файла, отдельных файлов на каждую таблицу не будет.

> Примечание: команды ниже предполагают, что текущая папка — `backend/`.

### Key folders

- `app/`: application code
  - `main.py`: routes + template rendering (server-side HTML)
  - `auth.py`: cookie session auth + role checks
  - `db/`: SQLAlchemy models + session
  - `questionnaire/`: JSON-каталоги/формы анкеты + Pydantic-схемы для `visit_services.details_json`
  - `seed.py`: creates dev users and default settings on startup
  - `templates/`: Jinja templates (minimal UI)
- `alembic/`: database migrations

### Migrations

Пока проект только у тебя локально и данных нет, **можно не копить цепочку миграций**: правим `alembic/versions/0001_init.py` под актуальную схему, удаляем файл БД `data/livingbraiding.db` (если был) и снова `alembic upgrade head`.

Когда появится продакшен с реальными данными, миграции нужны, чтобы менять схему **без потери** существующих строк — тогда каждое изменение оформляется новым файлом в `versions/`.

### Design rules (important)

- **No historical recalculation**: prices/settings can change, but old visits must not change.
  - We store *snapshots* inside `visits` (e.g. `*_price_per_gram_at_time`, `salon_cut_pct_at_time`).
  - Reports must use snapshot fields, not “current settings”.
- **Money model** (current MVP):
  - `profit_before_split` should already reflect all deductions, including addons (addons reduce profit).
  - salon and master payouts are derived from snapshot profit and snapshot salon cut.

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
