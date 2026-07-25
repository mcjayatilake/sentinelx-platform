# Security (Authentication & Authorization Layer)

Concrete security controls added in the authentication-foundation phase.
This is a companion to
[`docs/architecture/SECURITY.md`](architecture/SECURITY.md) (the
platform-wide guardrails, including active-scanning authorization —
mostly still forward-looking) and
[`docs/tenant-isolation.md`](tenant-isolation.md) (the database-layer
tenant isolation this phase builds request-layer identity on top of).

## Password security

- **Argon2id**, explicit (not default) cost parameters — see
  [ADR 0002](decisions/0002-authentication-strategy.md).
- Never logged: no log statement in `app.services.auth_service` or the
  auth endpoints includes a raw or hashed password.
- Never returned: no response schema includes a password field
  (`UserRead`, `RegisterResponse`, `MeResponse`, etc.).
- Policy: minimum length + configurable character-class requirements
  (`PASSWORD_MIN_LENGTH`, `PASSWORD_REQUIRE_*`), validated on
  register/change/reset, reporting every violation at once (not just the
  first) via `PasswordPolicyError.violations`, mapped to `422`.
- Breached-password hook (`BreachedPasswordChecker` Protocol) exists with
  a no-op default (`NullBreachedPasswordChecker`); see ADR 0002.

## Token security

See [ADR 0003](decisions/0003-jwt-strategy.md) (access tokens) and
[ADR 0004](decisions/0004-refresh-token-strategy.md) (refresh tokens) for
the full design. Summary: separate signing secrets per token type,
`kid`/`jti`/`ver` claims for future key rotation / logout / bulk
invalidation, refresh-token rotation with family-wide reuse-detection
revocation.

## Multi-tenancy

**No endpoint relies on a client-supplied tenant ID.** `Principal.tenant_id`
is resolved once at login/switch-tenant from a verified `TenantMembership`
row, embedded as a signed JWT claim, and re-verified against the live
membership on every subsequent request
(`app.api.deps.get_current_principal`) — see `docs/authentication.md`.
This is layered on top of the database-layer tenant isolation from the
prior phase (repository-contract `tenant_id` requirements + composite
foreign keys, see `docs/tenant-isolation.md`); RBAC (`docs/rbac.md`) is a
third, independent, composed layer answering "can this role do X",
distinct from "which tenant."

## Authorization (RBAC)

See [`docs/rbac.md`](rbac.md) for the full permission matrix and the
`require_permission` dependency pattern used consistently across every
permission-gated endpoint.

## Audit logging

`AuditEvent` (append-only, from the database-foundation phase) is written
automatically for every one of these actions, from a single call site per
action (never duplicated across endpoints):

| Action | `AuditEvent.action` | Outcome(s) written |
|---|---|---|
| Login | `auth.login` | `SUCCESS`, `FAILURE` |
| Logout | `auth.logout` | `SUCCESS` |
| Failed login | `auth.login` | `FAILURE` |
| Registration | `auth.register` | `SUCCESS` |
| Password change (logged in) | `auth.change_password` | `SUCCESS` |
| Password reset request | `auth.password_reset_request` | `SUCCESS` (silently skipped for an unknown email — see below) |
| Password reset | `auth.password_reset` | `SUCCESS`, `FAILURE` (invalid/expired token) |
| Email verification | `auth.email_verification` | `SUCCESS`, `FAILURE` (invalid/expired token) |
| Refresh-token reuse detected | `auth.refresh_token_reuse_detected` | `DENIED` |
| Tenant switch | `auth.switch_tenant` | `SUCCESS` |
| Role change | `membership.role_change` | `SUCCESS` (only written when the role actually changes) |
| Membership added | `membership.add` | `SUCCESS` |
| Membership removed | `membership.remove` | `SUCCESS` |
| API key created | `api_key.create` | `SUCCESS` |
| API key revoked | `api_key.revoke` | `SUCCESS` |

A failed login records `actor_user_id` when the email matched a real
account, or falls back to the attempted email as `resource_id` when it
didn't — deliberately, since this is a security-monitoring platform and
failed-login forensics on an attempted (possibly non-existent) email
address is expected, standard practice, not a privacy leak (it's an
internal audit trail, not a public-facing response).

**Enumeration resistance is an explicit design choice, not an oversight:**
`POST /auth/password/reset/request` returns the identical generic
response whether or not the email is registered, and the service layer
skips writing an audit event entirely for an unregistered email — so
neither the HTTP response timing/shape nor the audit trail's existence
can be used to enumerate accounts. (A failed *login* attempt, by
contrast, is legitimately audited either way — the account not existing
is itself an important row for detecting credential-stuffing sweeps.)

Audit events are queryable per-tenant via `GET /audit-events`
(`AUDIT_VIEW` permission — see `docs/rbac.md`).

## Rate limiting

Redis-backed fixed-window counters (`app/core/rate_limit.py`), keyed by
client IP (first `X-Forwarded-For` entry when present, for
reverse-proxied deployments) and an endpoint name, so different endpoints
never share a budget:

| Endpoint | Setting |
|---|---|
| `POST /auth/login` | `RATE_LIMIT_LOGIN_PER_MINUTE` (default 5) |
| `POST /auth/refresh` | `RATE_LIMIT_REFRESH_PER_MINUTE` (default 10) |
| `POST /auth/password/reset/request` | `RATE_LIMIT_PASSWORD_RESET_PER_MINUTE` (default 3) |
| `POST /api-keys` | `RATE_LIMIT_API_KEY_CREATE_PER_MINUTE` (default 5) |

Exceeding the limit returns `429`. The architecture is Redis-backed by
requirement; the pre-existing generic `RATE_LIMIT_PER_MINUTE` setting
(scaffold phase) is unrelated and unaffected.

## Security response headers

`SecurityHeadersMiddleware` (`app/core/security_headers.py`), applied to
every response (success and error alike) when `SECURITY_HEADERS_ENABLED`
is true (default):

| Header | Value |
|---|---|
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Content-Security-Policy` | `SECURITY_CSP_POLICY` (default `default-src 'self'`) |
| `Strict-Transport-Security` | `max-age=SECURITY_HSTS_MAX_AGE_SECONDS; includeSubDomains` — **HTTPS responses only** (setting it on plain HTTP is a spec no-op but misleading, so it's deliberately omitted for local/plain-HTTP dev) |

## Configuration & secrets

Environment-driven via `Settings` (`app/core/config.py`); see
`.env.example` for the full variable list. No secret defaults are
production-usable — `JWT_SECRET_KEY`, `JWT_REFRESH_SECRET_KEY` ship as
`*-change-me` placeholders that must be overridden per environment.
`.env` itself is `.gitignore`d; only `.env.example` (placeholders) is
committed.

## Known limitations

See "Known limitations / future work" in `docs/authentication.md` and
"Future work" in `docs/rbac.md` for the full list (no OAuth2/SSO wiring,
no breached-password check active, no refresh-token pruning job, no API
key scope enforcement, no Owner-vs-Administrator distinction). RLS
remains deferred per [ADR 0001](decisions/0001-tenant-isolation-and-rls.md) —
this phase satisfies that ADR's stated trigger condition (an
authenticated request/session layer now exists) but does not itself
revisit the RLS decision; that's separate, review-worthy scope.
