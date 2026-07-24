# Contributing

## Prerequisites

- Docker 24+ and Docker Compose v2
- Node.js 20+ and npm
- Python 3.13
- Git

## Local setup

```bash
git clone <repository-url>
cd sentinelx
cp .env.example .env

make up
```

This starts PostgreSQL, Redis, MinIO, the FastAPI backend, Celery
worker/beat/flower, and the Next.js frontend. See the root
[`README.md`](../README.md) for service URLs.

## Working on the backend

```bash
cd backend
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

uvicorn app.main:app --reload   # run locally against Dockerized Postgres/Redis
pytest                           # run tests
ruff check .                     # lint
ruff format .                    # format
mypy app                         # type check
```

## Working on the frontend

```bash
cd frontend
npm install

npm run dev         # start dev server
npm run lint         # lint
npm run format        # format
npm run typecheck      # type check
npm run build            # production build
```

## Database migrations

```bash
make migrate-generate name="describe your change"
# review the generated file under backend/alembic/versions/ before committing
make migrate
```

## Before opening a pull request

1. `make lint` and `make typecheck` pass locally.
2. `make test` passes locally.
3. New behavior has test coverage.
4. Commit messages follow
   [Conventional Commits](https://www.conventionalcommits.org/).
5. Read [`docs/CODING_STANDARDS.md`](CODING_STANDARDS.md) and, for anything
   touching authentication, scanning, or tenant data,
   [`docs/architecture/SECURITY.md`](architecture/SECURITY.md).

CI runs the same lint/type/test checks automatically on every pull request
(see `.github/workflows/`).
