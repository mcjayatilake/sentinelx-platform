# SentinelX

**Continuous security validation for teams that ship fast.**

SentinelX is a commercial SaaS platform for continuous, authorized security
validation of applications and infrastructure. It combines static and
dynamic analysis, dependency and container scanning, authenticated
application testing, automated penetration testing, and AI-assisted
business-logic testing into a single continuous-monitoring workflow with
enterprise-grade reporting.

> **Authorized use only.** SentinelX is built exclusively for security
> testing of assets that the operator owns or has explicit, documented
> authorization to test. It is not a hacking tool and must not be used
> against systems without authorization. See
> [`docs/architecture/SECURITY.md`](docs/architecture/SECURITY.md) for the
> platform's authorization and scoping model.

## Capabilities

| Domain | Description |
|---|---|
| Source code scanning | Static analysis (SAST) across supported languages |
| Secret detection | Discovers credentials, keys, and tokens in code and history |
| Dependency analysis | SCA for known-vulnerable and outdated dependencies |
| Container scanning | Image and runtime configuration analysis |
| Website scanning | DAST for authorized web targets |
| API scanning | Schema-aware testing of REST/GraphQL APIs |
| Authenticated security testing | Session-aware scanning behind login |
| Automated penetration testing | Orchestrated, authorized exploit-chain testing |
| AI-powered business logic testing | LLM-assisted discovery of logic flaws |
| Continuous monitoring | Scheduled and event-driven re-scanning |
| Security reporting | Findings, risk scoring, and compliance-ready reports |

This repository currently contains the **project scaffold only** — folder
structure, configuration, infrastructure, and a minimal running application.
Feature/business logic for the capabilities above is implemented
incrementally in follow-up work.

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | Next.js, TypeScript, Tailwind CSS, shadcn/ui, React Query |
| Backend | FastAPI, Python 3.13 |
| Database | PostgreSQL |
| Cache | Redis |
| Workers | Celery (+ Celery Beat, Flower) |
| Container runtime | Docker |
| Object storage | S3-compatible (MinIO locally) |
| AuthN/AuthZ | JWT, OAuth2, refresh tokens |
| Deployment | Docker Compose (local), Kubernetes (staging/production) |

## Repository layout

```
sentinelx/
├── backend/                 FastAPI application, Celery workers, domain modules
├── frontend/                Next.js application
├── infrastructure/
│   └── kubernetes/          Kustomize base + environment overlays
├── docs/
│   ├── architecture/        System architecture and security model
│   ├── CODING_STANDARDS.md
│   └── CONTRIBUTING.md
├── scripts/                 Developer utility scripts
├── .github/workflows/       CI pipelines
├── docker-compose.yml
└── .env.example
```

See [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md)
for the full system design.

## Getting started

### Prerequisites

- Docker 24+ and Docker Compose v2
- Node.js 20+ and npm (for local frontend development outside Docker)
- Python 3.13 (for local backend development outside Docker)

### Run with Docker Compose

```bash
cp .env.example .env
# edit .env and set real secrets before anything but local dev

make up          # builds and starts all services
make logs        # tail logs
```

Once running:

- Frontend: http://localhost:3000
- Backend API: http://localhost:8000
- API health check: http://localhost:8000/api/v1/health
- API docs (OpenAPI): http://localhost:8000/api/v1/docs
- MinIO console: http://localhost:9001
- Flower (Celery monitoring): http://localhost:5555

For hot-reloading frontend development, copy
`docker-compose.override.yml.example` to `docker-compose.override.yml`
before `make up`.

### Run backend locally (without Docker)

```bash
cd backend
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

### Run frontend locally (without Docker)

```bash
cd frontend
npm install
npm run dev
```

## Common tasks

Run `make help` to see all available commands, including:

```bash
make up               # start the stack
make down              # stop the stack
make migrate           # apply database migrations
make test               # run backend + frontend test suites
make lint               # run all linters
make fmt                 # format all code
make typecheck          # run mypy + tsc
```

## Documentation

- [Architecture](docs/architecture/ARCHITECTURE.md)
- [Security & Authorization Model](docs/architecture/SECURITY.md)
- [Database Architecture](docs/database-architecture.md)
- [Data Model](docs/data-model.md)
- [Tenant Isolation Strategy](docs/tenant-isolation.md)
- [ADR 0001: Tenant Isolation & RLS](docs/decisions/0001-tenant-isolation-and-rls.md)
- [Local Development — Database](docs/local-development.md)
- [Coding Standards](docs/CODING_STANDARDS.md)
- [Contributing](docs/CONTRIBUTING.md)

## License

Proprietary and confidential. See [`LICENSE`](LICENSE).
