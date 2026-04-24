## Backend overview

This folder contains the FastAPI app and its SQLite/Postgres database schema.

Файл **`data/livingbraiding.db`** (SQLite) — это **одна база**: все таблицы живут внутри этого файла, отдельных файлов на каждую таблицу не будет.

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
..\.\.venv\Scripts\python -m ensurepip --upgrade
..\.\.venv\Scripts\python -m pip install -r requirements.txt
```

Enable dev seed (optional):

```powershell
$env:ENABLE_DEV_SEED="1"
```

Run migrations:

```bash
..\.\.venv\Scripts\python -m alembic upgrade head
```

Run server:

```bash
..\.\.venv\Scripts\python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

Run tests:

```bash
..\.\.venv\Scripts\python -m pytest
```

Validate questionnaire JSON examples:

```bash
..\.\.venv\Scripts\python -m app.questionnaire.self_check
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

