# Authentication

This document describes the concrete authentication implementation. For
*why* it's shaped this way, see
[ADR 0002](decisions/0002-authentication-strategy.md),
[ADR 0003](decisions/0003-jwt-strategy.md), and
[ADR 0004](decisions/0004-refresh-token-strategy.md). For roles and
permissions, see [`docs/rbac.md`](rbac.md). For API keys, see
[`docs/api-keys.md`](api-keys.md).

## Core building blocks

| Component | File |
|---|---|
| Password hashing, JWT encode/decode, password policy | `app/core/security.py` |
| Auth orchestration (register/login/refresh/logout/...) | `app/services/auth_service.py` |
| Request-scoped identity dependencies | `app/api/deps.py` |
| HTTP endpoints | `app/api/v1/endpoints/auth.py` |
| Refresh token persistence | `app/models/refresh_token.py`, `app/repositories/refresh_token_repository.py` |
| Email/reset verification token persistence | `app/models/verification_token.py`, `app/repositories/verification_token_repository.py` |
| Outbound email (stubbed) | `app/services/email_service.py` |
| Access-token logout denylist | `app/core/token_denylist.py` (Redis) |
| Auth-endpoint rate limiting | `app/core/rate_limit.py` (Redis) |

## Identity model

- **`User`** — a global identity (`email`, `hashed_password`,
  `token_version`, `email_verified_at`). Not tenant-scoped.
- **`TenantMembership`** — binds a `User` to a `Tenant` with a `role`
  (see `docs/rbac.md`) and a `status` (`ACTIVE`/`INVITED`/`SUSPENDED`/`REMOVED`).
  A user may belong to multiple tenants.
- **`Principal`** (`app.api.deps.Principal`) — the resolved identity of an
  authenticated, tenant-scoped request: `user`, `tenant_id`, and either
  `role` (a JWT/user-session principal) or `scopes` (an API-key
  principal — see [`docs/api-keys.md`](api-keys.md)), mutually exclusive
  by construction. `tenant_id` always comes from a verified source — the
  access token's own claim plus a live `TenantMembership` re-check, or
  the API key's own stored `tenant_id` — **never from a client-supplied
  header, query parameter, or path parameter.** Every request is also
  re-checked against `Tenant.status`: a suspended/archived tenant is
  rejected immediately, through either authentication path, not just at
  its next token refresh (see
  [ADR 0008](decisions/0008-transaction-and-concurrency-model.md#8-inactive-tenant-enforcement-and-api-key-authentication)).
  `user` is `None` for a tenant-level service-account API key not bound
  to any specific user.

## Endpoints

All under `POST/GET /api/v1/auth/...` unless noted.

| Endpoint | Method | Auth required | Rate limited | Purpose |
|---|---|---|---|---|
| `/register` | POST | No | No | Create a `User` + send verification email |
| `/login` | POST | No | Yes | Authenticate, resolve tenant, issue tokens |
| `/refresh` | POST | No (bearer refresh token in body) | Yes | Rotate a refresh token, issue a new access token |
| `/logout` | POST | Yes | No | Revoke refresh family + denylist the access token |
| `/switch-tenant` | POST | Yes | No | Re-verify membership in another tenant, issue new tokens |
| `/me` | GET | Yes | No | Current user + tenant + role |
| `/password/change` | POST | Yes | No | Change password (requires current password) |
| `/password/reset/request` | POST | No | Yes | Issue + email a reset token (generic response) |
| `/password/reset/confirm` | POST | No | No | Consume a reset token, set new password |
| `/email/verify/request` | POST | Yes | No | Issue + email a verification token |
| `/email/verify/confirm` | POST | No | No | Consume a verification token |
| `/sessions` | GET | Yes | No | List active refresh-token sessions |
| `/sessions/{id}` | DELETE | Yes | No | Revoke one session (its refresh-token family) |
| `/sessions` | DELETE | Yes | No | Revoke every session + bump `token_version` |

Errors from `app.services.auth_service`'s typed exceptions (e.g.
`InvalidCredentialsError`, `TenantAccessDeniedError`,
`RefreshTokenReuseDetectedError`) are mapped to HTTP responses in one
place — `app/core/error_handlers.py` — rather than duplicated per
endpoint; see that module for the full status-code mapping. Password
policy violations (`PasswordPolicyError`) map to `422` with a
`violations` list.

## Login flow

1. `POST /auth/login` with `email`, `password`, optional `tenant_id`.
2. Password is verified (constant-time, via `passlib`). Any failure
   (wrong password, unknown email, inactive account) records a
   `FAILURE`-outcome `auth.login` audit event and returns a **generic**
   401 — the response never reveals whether the email exists.
3. On success, if the hash was produced with weaker-than-current Argon2id
   parameters, it's transparently re-hashed and persisted
   (`needs_rehash`) — without invalidating the session being created.
4. Every `ACTIVE` `TenantMembership` for the user is loaded.
   - **Exactly one** → that tenant/role is used automatically.
   - **`tenant_id` was supplied and matches a membership** → that one is
     used (400/403 if it doesn't match any).
   - **Zero or multiple, and no `tenant_id` supplied** → `token` is
     `null` in the response and `available_tenants` lists the choices;
     the caller re-calls `/auth/login` with `tenant_id` set.
5. On tenant resolution, a fresh access/refresh token pair is issued
   (`AuthService._issue_fresh_token_pair`) and a `SUCCESS`-outcome
   `auth.login` audit event is written.

## Refresh flow

See [ADR 0004](decisions/0004-refresh-token-strategy.md) for the full
rotation/reuse-detection design. In short: every `/auth/refresh` call
rotates the presented token (marks it `ROTATED`, issues a new one in the
same `family_id`); presenting an already-rotated token revokes the entire
family and is treated as a detected compromise
(`auth.refresh_token_reuse_detected`, audit outcome `DENIED`).

## Logout

`POST /auth/logout` takes the refresh token in the body (revoked via the
DB-backed family mechanism) and reads the access token's `jti`/`exp` from
the `Authorization` header (already verified by `AccessTokenPayloadDep`)
to denylist it in Redis for exactly its remaining lifetime — see
[ADR 0003](decisions/0003-jwt-strategy.md) for why these are two separate
mechanisms.

## Password reset and email verification

Both flows share one table (`UserVerificationToken`) and one
issuance/consumption path in `AuthService`, differing only by `purpose`
(`EMAIL_VERIFICATION` / `PASSWORD_RESET`):

1. A high-entropy raw token is generated (`generate_secret`), its
   SHA-256 hash is persisted with an expiry
   (`EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS` / `PASSWORD_RESET_TOKEN_EXPIRE_MINUTES`),
   and any other still-valid, unconsumed token of the same purpose for
   that user is invalidated first (so only the most recently issued link
   ever works).
2. The raw token is embedded in a link
   (`{FRONTEND_BASE_URL}/verify-email?token=...` or
   `/reset-password?token=...`) and handed to `EmailService`.
3. **Email sending is stubbed** (`LoggingEmailSender` — logs instead of
   sending; see `app/services/email_service.py`). **Token generation,
   hashing, expiry, and single-use consumption are full production code**,
   not stubbed — swapping in a real provider (SES, SendGrid, ...) is a
   drop-in `EmailSender` implementation, not a design change.
4. `POST /auth/password/reset/request` **always returns the same generic
   response** regardless of whether the email is registered, and the
   service layer skips the audit trail entirely for an unknown email —
   neither the HTTP response nor the audit log can be used to enumerate
   accounts.
5. Confirming either flow consumes the token (`consumed_at` set) so it
   cannot be replayed; a password reset also bumps `token_version` and
   revokes every refresh token for the user (equivalent to "log out
   everywhere") since the old password may have been compromised.

## Session management

"Session" here means a refresh-token family, not a single access token.
`GET /auth/sessions` returns one row per currently-`ACTIVE` family (i.e.
the current token in each rotation chain). `DELETE /auth/sessions/{id}`
revokes one family (id = the refresh token row's `id`, not a separate
session identifier); `DELETE /auth/sessions` revokes every family for the
user **and** bumps `token_version`, so already-issued access tokens stop
working immediately too — not just future refresh attempts.

## Configuration

All environment-driven via `Settings` (`app/core/config.py`); see
`.env.example` for the authoritative list of variable names and
placeholder values. Key groups: `JWT_*` (secrets, algorithm, expiry,
`kid` values, issuer), `PASSWORD_*` (policy), `ARGON2_*` (hashing cost),
`EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS` / `PASSWORD_RESET_TOKEN_EXPIRE_MINUTES`,
`FRONTEND_BASE_URL` (for building stubbed-email links),
`RATE_LIMIT_*_PER_MINUTE` (per-endpoint), `SECURITY_*` (response
headers — see `docs/security.md`). No secret has a usable default in a
non-`local` environment; the shipped defaults are intentionally labeled
`*-change-me`.

## Known limitations / future work

- No OAuth2/SSO provider is wired up (config placeholders exist from the
  scaffold phase; implementing a real provider is separate scope).
- No breached-password check is active (`NullBreachedPasswordChecker` is
  the default; the hook — `BreachedPasswordChecker` — is ready for a
  real implementation, see ADR 0002).
- No pruning job for old `ROTATED`/`REVOKED` refresh-token rows (see ADR
  0004's consequences).
- No anomaly detection (device/IP fingerprint mismatch) beyond refresh-
  token reuse detection.
- Inviting a brand-new email address (one with no `User` account yet) to
  a tenant is not implemented — `POST /memberships` only adds an
  *existing* user by email. See `docs/rbac.md`.
