"""SecurityHeadersMiddleware: applied to every response."""

import pytest
from httpx import AsyncClient

import app.core.security_headers as security_headers_module
from app.core.config import get_settings


async def test_security_headers_present_on_a_plain_response(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "content-security-policy" in response.headers


async def test_hsts_not_set_on_plain_http_response(client: AsyncClient) -> None:
    # The ASGI test transport reports an http:// URL — HSTS on a
    # plain-HTTP response would be misleading, so it must be absent.
    response = await client.get("/api/v1/health")
    assert "strict-transport-security" not in response.headers


async def test_security_headers_present_on_error_responses_too(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")  # 401, no Authorization header
    assert response.status_code == 401
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


async def test_csp_policy_reflects_configured_value(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.headers["content-security-policy"] == get_settings().security_csp_policy


async def test_headers_omitted_when_disabled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    disabled_settings = get_settings().model_copy(update={"security_headers_enabled": False})
    monkeypatch.setattr(security_headers_module, "get_settings", lambda: disabled_settings)

    response = await client.get("/api/v1/health")
    assert "x-content-type-options" not in response.headers
    assert "content-security-policy" not in response.headers
