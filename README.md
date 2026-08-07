# Ledgerline

A payments backend, built in phases. This is **Phase 0: Foundation** — repo,
Docker, Alembic, and CI scaffolding, with no payment/ledger/account logic yet.

## Stack

- FastAPI + uvicorn
- Postgres 16 (Docker Compose)
- SQLAlchemy 2.0 async (asyncpg)
- Alembic (async-configured)
- pytest + httpx
- ruff
- pydantic-settings (config from `.env`)

## Running locally (Windows / PowerShell)

```powershell
# 1. Start Postgres
docker compose up -d

# 2. Create + activate a venv, install deps
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3. Run migrations
alembic upgrade head

# 4. Run the app
uvicorn app.main:app --reload

# 5. Smoke check (separate terminal)
curl.exe http://localhost:8000/health
```

## Phase 0 smoke acceptance criteria

- `curl.exe http://localhost:8000/health` returns `{"status":"ok"}`
- `pytest` passes locally
- `ruff check .` is clean
- CI is green on the first push

## Notes

- `/health` is dependency-free (no DB call) so it works without Postgres running.
- `.env` is gitignored; copy `.env.example` to `.env` to configure locally.
