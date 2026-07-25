# ADR 0003: JWT Strategy

## Status

Accepted — authentication-foundation phase.

## Context

Access tokens need to be short-lived, verifiable without a database round
trip on every request (for latency), individually revocable (logout), and
bulk-revocable (forced sign-out everywhere, e.g. after a password
compromise) — while leaving room for key rotation and multi-key
verification (JWKS) later without a token-shape migration.

## Decision

1. **Two independent signing secrets** — `jwt_secret_key` (access) and
   `jwt_refresh_secret_key` (refresh), both required, both configured via
   `Settings` with no default usable in production
   (`insecure-development-*-change-me` placeholders that must be
   overridden). A refresh token can never be replayed as an access token
   even if a future code path forgot to check the `type` claim, because
   it is signed with a different key entirely — `decode_token` checks
   both the signature (implicitly, via which secret verifies it) and the
   `type` claim explicitly.

2. **`kid` header, one static value per token type today
   (`jwt_access_key_id`, `jwt_refresh_key_id`), not used to select a key
   yet.** Carried in the JWT header from day one so that introducing
   multi-key verification or JWKS-based rotation later is a change to
   *how* `_secret_and_kid_for` resolves a secret from a `kid`, not a
   change to the token's shape or any client's parsing logic.

3. **`jti` (JWT ID) on every token**, a fresh `uuid4` per issuance.
   Access tokens: used as the logout-denylist key (`app.core.token_denylist`,
   Redis `SETEX jti:<jti> <remaining-ttl> 1`). Refresh tokens: not used for
   denylisting (refresh tokens are DB-backed rows, revoked by flipping
   `status`, not by `jti` lookup) but present for consistency and log
   correlation.

4. **`ver` claim mirrors `User.token_version` at issuance time.**
   `get_current_user` rejects any access token whose `ver` claim doesn't
   match the user's *current* `token_version`. Bumping `token_version`
   (password change, password reset, explicit "log out everywhere")
   invalidates every previously-issued access token instantly, without
   touching Redis or iterating refresh-token rows — an O(1) bulk
   invalidation the denylist alone can't provide (the denylist only knows
   about tokens it was explicitly told to deny).

5. **Three independent revocation mechanisms, deliberately not unified
   into one:**
   | Mechanism | Scope | Store | Latency to take effect |
   |---|---|---|---|
   | Refresh-token family revocation | One login session's refresh chain | PostgreSQL (`RefreshToken.status`) | Immediate (checked on next refresh) |
   | Access-token `jti` denylist | One specific access token | Redis, TTL = token's remaining life | Immediate (checked every request) |
   | `User.token_version` bump | Every access token ever issued to a user | PostgreSQL (`User.token_version`) | Immediate (checked every request) |

   Collapsing these into one mechanism was considered and rejected: a
   single "denylist everything" approach would mean either persisting
   every access token's `jti` for its full lifetime (defeating the point
   of a stateless, short-lived token) or losing the ability to revoke
   *just one* session without invalidating all of a user's other active
   sessions.

6. **Access tokens are short-lived (15 minutes) by design, not because
   revocation is hard.** With the three mechanisms above, a compromised
   access token is only a 15-minute-or-less exposure window even absent
   any explicit revocation action — explicit revocation exists for the
   cases where even that window is unacceptable (logout, detected
   refresh-token theft, forced sign-out).

## Consequences

- Every authenticated request pays one Redis round trip (denylist check)
  in addition to the JWT signature/expiry verification — accepted as the
  cost of supporting immediate logout for a stateless token scheme.
- `Settings.jwt_secret_key` and `jwt_refresh_secret_key` rotation without
  invalidating all outstanding sessions requires the multi-key/JWKS
  extension described in point 2 — not implemented yet, but the `kid`
  header means it doesn't require reissuing the token format when it is.
- A single access token cannot be individually revoked by anything other
  than its own `jti` (there's no "revoke access token by user+device"
  without also going through logout, which the client must call
  explicitly) — acceptable given the 15-minute ceiling.
