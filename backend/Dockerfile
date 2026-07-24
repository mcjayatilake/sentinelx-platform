FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpq-dev \
        gcc \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 sentinelx \
    && useradd --uid 1000 --gid sentinelx --shell /bin/bash --create-home sentinelx

# ---------------------------------------------------------------------------
FROM base AS dev

COPY pyproject.toml README.md ./
RUN pip install -e ".[dev]"

COPY . .
RUN chown -R sentinelx:sentinelx /app
USER sentinelx

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---------------------------------------------------------------------------
FROM base AS production

COPY pyproject.toml README.md ./
RUN pip install .

COPY . .
RUN chown -R sentinelx:sentinelx /app
USER sentinelx

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
