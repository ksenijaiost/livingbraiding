# livingbraiding

Internal mini-site for recording salon visits (masters) and managing directories + reports (admin).

## Quick start (Windows PowerShell)

Create venv and install deps:

```bash
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
```

Init DB and run migrations:

```bash
cd backend
alembic upgrade head
```

Run server:

```bash
uvicorn app.main:app --reload
```

Open:
- `http://127.0.0.1:8000/`

## Default accounts (dev only)
- admin: `admin` / `admin`
- master: `master1` / `master1`