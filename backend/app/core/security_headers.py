"""Security response headers middleware.

Adds HSTS (HTTPS responses only), X-Frame-Options, X-Content-Type-Options,
Referrer-Policy, and a configurable Content-Security-Policy to every
response.
"""

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)

        settings = get_settings()
        if not settings.security_headers_enabled:
            return response

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = settings.security_csp_policy

        # HSTS only makes sense on HTTPS — setting it on a plain-HTTP
        # response (e.g. local dev) is a spec no-op but still misleading.
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                f"max-age={settings.security_hsts_max_age_seconds}; includeSubDomains"
            )

        return response
