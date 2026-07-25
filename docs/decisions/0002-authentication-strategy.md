# ADR 0002: Authentication Strategy

## Status

Accepted — authentication-foundation phase.

## Context

The database-foundation phase built `User`, `TenantMembership`, and
`APIKeyMetadata` as pure data, explicitly deferring "no password storage
or authentication implementation yet." This phase has to decide how a
user actually proves their identity, and how that identity gets resolved
into a tenant-scoped, role-aware request context on every API call.

## Decision

**Argon2id for password hashing, JWT access tokens + DB-backed refresh
tokens for sessions, tenant/role resolved and embedded at issuance time.**

1. **Argon2id, not bcrypt.** `app/core/security.py` hashes every password
   with Argon2id via `passlib`, with explicit (not library-default)
   parameters read from `Settings` (`argon2_time_cost`,
   `argon2_memory_cost_kib`, `argon2_parallelism`) so a dependency upgrade
   can't silently change the work factor. There is no bcrypt fallback:
   `hashed_password` is a brand-new column with no pre-existing hashes in
   any other scheme to stay compatible with, so a `deprecated="auto"`
   legacy scheme has nothing to do. `needs_rehash()` still exists — it's
   for a future tightening of the `ARGON2_*` settings, not a bcrypt
   migration path.

2. **A password is never logged or returned.** No log statement anywhere
   in `app.services.auth_service` or `app.api.v1.endpoints.auth` includes
   a raw or hashed password; no response schema (`UserRead`, `RegisterResponse`,
   `MeResponse`) has a password field.

3. **Tenant and role are resolved once, at login/switch-tenant, and
   embedded as signed JWT claims** (`tenant_id`, `role`) — never read from
   a client-supplied header, query parameter, or path parameter on
   subsequent requests. `login` accepts an optional `tenant_id`; if the
   account has exactly one active `TenantMembership` it's inferred,
   otherwise the caller must choose from `LoginResponse.available_tenants`
   with a follow-up call. See `docs/authentication.md` for the full flow.

4. **`app.api.deps.get_current_principal` re-verifies membership on every
   request**, not just at token-issuance time — a role change or removal
   takes effect on the very next request, not when the access token
   happens to expire (up to 15 minutes later otherwise).

5. **A password-policy hook (`BreachedPasswordChecker`) exists but has no
   real implementation.** `NullBreachedPasswordChecker` (always returns
   `False`) is the default everywhere; a k-anonymity range lookup against
   a breach corpus (e.g. HaveIBeenPwned) is a drop-in replacement at the
   same call site (`AuthService(..., breached_password_checker=...)`),
   not a new integration point to build later.

6. **Email verification and password reset share one token table and one
   issuance/consumption code path** (`UserVerificationToken`,
   `VerificationTokenRepository`) — `purpose` is the only thing that
   differs. Password-reset *email delivery* is stubbed
   (`LoggingEmailSender`) per this phase's explicit scope; token
   generation, hashing, expiry, and single-use consumption are production
   code, not stubs. See ADR 0004 for why the tokens themselves are
   SHA-256-hashed rather than Argon2id-hashed like passwords.

## Alternatives considered

- **Session cookies instead of JWTs.** Rejected for this phase: SentinelX
  is an API-first platform (frontend is a separate Next.js app, and API
  keys serve programmatic/CI access) — bearer tokens fit both audiences
  without a separate cookie-based auth path.
- **OAuth2/SSO as the only login path.** Config placeholders
  (`OAUTH2_GOOGLE_CLIENT_ID`, etc.) already exist from the scaffold phase,
  but wiring an actual provider is out of scope here — email/password is
  the baseline every tenant needs regardless of SSO.
- **Storing role/tenant server-side only, looked up per request by user
  ID alone.** Rejected: that would require every request to disambiguate
  *which* tenant a multi-tenant user means, from something client-supplied
  — reintroducing exactly the "client-supplied tenant_id" trust problem
  this phase is required to avoid. Embedding the verified tenant/role in
  the signed token, established once at login, avoids it.

## Consequences

- A role change or membership removal is enforced within one request
  (via `get_current_principal`'s live re-check), not merely at the next
  token refresh — at the cost of one extra `TenantMembership` lookup per
  authenticated request. Accepted: correctness here matters more than
  saving one indexed lookup.
- Switching between tenants requires a new token pair
  (`POST /auth/switch-tenant`), not a claim the client can flip locally —
  by design, since the switch itself must be a server-verified membership
  check.
- There is no way to force-expire a single access token by ID (only by
  `jti` via the logout denylist, or all-at-once via `token_version`) — see
  ADR 0003 for why that's an acceptable trade-off for a 15-minute-lived
  token.
