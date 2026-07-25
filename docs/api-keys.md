# API Keys

Programmatic/CI access to the SentinelX API without a user login flow.
Tenant-scoped, permission-gated, and — like refresh tokens — never stored
in plaintext. See [ADR 0004](decisions/0004-refresh-token-strategy.md)
for why the secret is SHA-256-hashed rather than Argon2id-hashed.

## Shape

An issued key looks like `sx_ab12cd34ef.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`:

- **`prefix`** (`sx_` + 8 random characters) — a public, non-secret lookup
  identifier. Stored in the clear (`APIKeyMetadata.prefix`, unique),
  displayed in `APIKeyMetadataRead`, used to locate the row before the
  secret is known.
- **`secret`** (the part after `.`) — the actual bearer credential, 256
  bits of `secrets.token_urlsafe` randomness. **Never stored.** Only
  `hash_secret(secret)` (SHA-256 hex digest) is persisted, in
  `APIKeyMetadata.hashed_secret`.

The full `prefix.secret` string is returned exactly once, in the response
body of `POST /api-keys` or `POST /api-keys/{id}/rotate`
(`APIKeyIssueResponse.api_key`). There is no endpoint, repository method,
or schema field anywhere that can retrieve it again — `APIKeyMetadataRead`
and `APIKeyMetadataUpdate` never include `hashed_secret` or a plaintext
key field.

## Endpoints

All under `/api/v1/api-keys`, all `require_permission`-gated (see
`docs/rbac.md`):

| Endpoint | Method | Permission | Rate limited | Notes |
|---|---|---|---|---|
| `/` | POST | `API_KEYS_MANAGE` | Yes | Issues a key; plaintext returned once |
| `/` | GET | `API_KEYS_VIEW` | No | Paginated list, no secrets |
| `/{id}/revoke` | POST | `API_KEYS_MANAGE` | No | Sets `status=REVOKED`, `revoked_at` |
| `/{id}/rotate` | POST | `API_KEYS_MANAGE` | No | Revokes the old key, issues a new one with the same name/scopes/expiry |

`create_api_key` and `revoke_api_key` each write an `AuditEvent`
(`api_key.create` / `api_key.revoke`); `rotate_api_key` writes both (a
revoke for the old key, a create for the new one), tagged
`event_metadata={"reason": "rotated", ...}` so the audit trail
distinguishes an explicit revoke from a rotation.

## Verification (`APIKeyService.authenticate`)

Given a raw `prefix.secret` string:

1. Split on the first `.`; malformed input (no `.`) → `None`.
2. Look up by `prefix` (not tenant-scoped — the caller doesn't know which
   tenant a bearer key belongs to until it's looked up, same reasoning as
   `UserRepository.get_by_email`).
3. Reject if not found, `status != ACTIVE`, or `expires_at` has passed.
4. Compare `hash_secret(secret)` against the stored hash with
   `hmac.compare_digest` (constant-time).
5. On success, bump `last_used_at` and return the record.

**Every failure mode returns `None` without distinguishing why** —
unknown prefix, wrong secret, revoked, and expired all look identical to
the caller, so a response can't be used to probe for valid prefixes or
learn a key's state.

**`X-API-Key` is now a real request-authentication path**
(`app.api.deps._principal_from_api_key`, wired into `get_current_principal`
— see [ADR 0008](decisions/0008-transaction-and-concurrency-model.md#8-inactive-tenant-enforcement-and-api-key-authentication)).
If a request carries an `X-API-Key` header, it is authenticated
exclusively through it — any `Authorization: Bearer` sent alongside is
ignored, a simple deterministic rule. The `last_used_at` bump commits in
its own short transaction immediately, rather than being held for the
rest of the request, so one automation key receiving many concurrent
requests never serializes behind whichever request's `UPDATE` landed
first. The tenant-active gate (`docs/security.md`) applies identically
to an API-key principal as it does to a JWT one — a suspended/archived
tenant's keys stop working immediately, not at their next issuance
check.

A tenant-level service-account key (`APIKeyMetadata.user_id IS NULL`)
resolves to a `Principal` with `user=None` — endpoints that are
inherently user-session concepts (`/auth/me`, `/auth/logout`) reject an
API-key-authenticated principal outright rather than crash on a missing
user.

## Lifecycle

- **Expiry** (`expires_at`, optional) — set at issuance
  (`APIKeyIssueRequest.expires_at`); checked in `authenticate()`. There is
  no endpoint to change a key's expiry after issuance — rotate instead.
- **Revocation** — immediate; `authenticate()` checks `status` on every
  call, so a revoked key stops working on its very next use, not at its
  next cache refresh (there is no cache).
- **Rotation** — revoke + reissue with the same `name`/`scopes`/`expires_at`,
  in one API call. The old key's plaintext is gone forever the moment it
  was first returned; rotation is how a caller replaces a key it can no
  longer produce a working copy of (lost secret, suspected leak) without
  losing its configured scopes.

## Scopes

`APIKeyMetadata.scopes` (`list[str]`) is now **enforced** through the same
central permission mechanism every other principal type uses.
`app.core.permissions.principal_has_permission()` is the single dispatch
point `require_permission` calls: for an API-key principal, it checks
`permission.value in principal.scopes` and **never** falls back to a
role-based grant — a key structurally cannot exceed what it was issued,
regardless of how permissive the issuing tenant's own roles are (see
[ADR 0008](decisions/0008-transaction-and-concurrency-model.md#8-inactive-tenant-enforcement-and-api-key-authentication)).

Scope values are the same strings as `app.core.permissions.Permission`
(e.g. `"scans:view"`, `"scans:manage"`) and are **validated at
issuance time** (`POST /api-keys` — see
`app.api.v1.endpoints.api_keys._validate_scopes`), not just checked
later: an unknown scope string is rejected with `422` before it can ever
be stored, so nothing invalid can end up on a key in the first place.
An empty `scopes: []` (the default) grants nothing.
