"""Readiness endpoint: real database (and Redis, where available) connectivity."""

from httpx import AsyncClient


async def test_readiness_reports_database_status(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["dependencies"]["database"] == "ok"
    assert body["status"] in ("ok", "degraded")


async def test_readiness_overall_status_matches_dependencies(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health/ready")
    body = response.json()

    dependencies = body["dependencies"]
    expected_overall = (
        "ok" if dependencies["database"] == "ok" and dependencies["redis"] == "ok" else "degraded"
    )
    assert body["status"] == expected_overall
