# ADR 0004: Refresh Token Strategy

## Status

Accepted — authentication-foundation phase.

## Context

Refresh tokens are long-lived (7 days by default) bearer secrets — losing
one is a much bigger blast radius than losing a 15-minute access token.
The strategy needs to detect theft (not just prevent replay of an
individually-expired token), and needs a hashing choice for stored
secrets that doesn't conflict with the choice made for passwords (ADR
0002).

## Decision

1. **Refresh tokens are high-entropy random secrets
   (`secrets.token_urlsafe(32)`), not JWTs.** Unlike access tokens, a
   refresh token carries no claims of its own — the server looks it up in
   `refresh_tokens` by hash and reads `tenant_id`/`user_id`/`family_id`/
   `status` from the row. This means revoking one is a single `UPDATE`,
   not dependent on the token's own (unforgeable but also
   un-*un*-forgeable) expiry claim.

2. **Stored as a SHA-256 hash (`hashed_token`), not Argon2id.** This is a
   deliberate departure from password hashing (ADR 0002), not an
   oversight: Argon2id is slow *on purpose*, to resist brute-forcing a
   low-entropy, human-chosen password. A refresh token is 256 bits of
   `secrets.token_urlsafe` randomness — there is no low-entropy secret to
   protect against guessing, and it is verified on *every* refresh call.
   Running that through a deliberately slow KDF is a self-inflicted
   denial-of-service vector (an attacker forcing refresh calls could burn
   CPU proportional to the Argon2id cost, not just bandwidth), not a
   security improvement. The same reasoning applies to API key secrets
   (`app.services.api_key_service`) and email-verification/password-reset
   tokens (`UserVerificationToken.hashed_token`) — all three use
   `app.core.security.hash_secret` (SHA-256), not `hash_password`.

3. **Rotation on every use, grouped by `family_id`.** Every successful
   `/auth/refresh` call:
   - marks the presented token `ROTATED` and sets `replaced_by_id` to the
     new row,
   - issues a new token in the *same* `family_id`,
   so a family is the full lineage of one login session's refresh chain,
   not just the single currently-valid token.

4. **Reuse of an already-`ROTATED` token revokes the entire family
   immediately**, not just the one replayed request. This is the classic
   stolen-refresh-token signature: if the *legitimate* client and an
   *attacker* both hold a copy of the same refresh token, whichever one
   uses it first causes rotation; when the second party (attacker or
   legitimate client, whichever lost the race) then presents the
   now-stale token, that presentation is unambiguous evidence the token
   was exposed to two parties. `AuthService.refresh` treats this as a
   security event: `RefreshTokenRepository.revoke_family` revokes every
   token in the chain (so neither party's session survives), and an
   `AuditEvent` (`auth.refresh_token_reuse_detected`, outcome `DENIED`) is
   written — this is the one auth event whose outcome is `DENIED` rather
   than `SUCCESS`/`FAILURE`, since it's neither a normal failure nor a
   normal success but a detected attack.

5. **Database-backed, not Redis-backed.** Unlike the access-token
   denylist (ADR 0003, point 5), refresh-token state is the source of
   truth in PostgreSQL, not a cache. A refresh token controls issuance of
   new access tokens for up to 7 days — that's state worth durability
   guarantees a cache doesn't provide, and session-listing
   (`GET /auth/sessions`) needs to query it directly.

## Consequences

- Every refresh call is a write (rotate), not just a read — by design,
  since rotation is what makes reuse detectable at all. A stateless
  "just check the JWT" refresh token could not offer this.
- `list_active_sessions` (backing `GET /auth/sessions`) returns one row
  per family currently `ACTIVE` — i.e. the *current* token in each
  rotation chain — which is the correct "sessions" semantic from the
  user's point of view, but means the table accumulates `ROTATED` history
  rows rather than only ever holding one row per session. No pruning job
  exists yet for old `ROTATED`/`REVOKED` rows; this is acceptable for now
  and flagged as future work in `docs/authentication.md`.
- A stolen refresh token is only exploitable until its *next* legitimate
  use (or the attacker's own first use, whichever happens first) — after
  that, reuse detection revokes the family. A token that's stolen and
  never used again by the legitimate client is not automatically detected
  until it expires naturally (7 days) or the family is revoked some other
  way (logout, "log out everywhere") — there is no anomaly detection
  (e.g. IP/device fingerprint mismatch) in this phase.
