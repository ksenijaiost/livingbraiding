## Backend overview

This folder contains the FastAPI app and its SQLite/Postgres database schema.

### Key folders

- `app/`: application code
  - `main.py`: routes + template rendering (server-side HTML)
  - `auth.py`: cookie session auth + role checks
  - `db/`: SQLAlchemy models + session
  - `seed.py`: creates dev users and default settings on startup
  - `templates/`: Jinja templates (minimal UI)
- `alembic/`: database migrations

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

Run migrations:

```bash
..\.\.venv\Scripts\python -m alembic upgrade head
```

Run server:

```bash
..\.\.venv\Scripts\python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

