# SentinelX Backend

FastAPI + Celery services for the SentinelX platform. See the repository
root [`README.md`](../README.md) and [`docs/`](../docs) for full project
documentation.

## Layout

```
app/
├── main.py            FastAPI app instance, middleware, router wiring
├── core/               Settings, structured logging, JWT/password utilities
├── api/v1/             HTTP API layer (versioned)
├── db/                 SQLAlchemy engine, session, declarative base
├── models/              ORM models (added incrementally)
├── schemas/              Pydantic request/response schemas
├── services/              Application/business logic
├── workers/               Celery application and tasks
└── modules/                One subpackage per product capability
```

## Quickstart

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

uvicorn app.main:app --reload
pytest
ruff check .
mypy app
```

See [`../docs/CODING_STANDARDS.md`](../docs/CODING_STANDARDS.md) for
conventions.
