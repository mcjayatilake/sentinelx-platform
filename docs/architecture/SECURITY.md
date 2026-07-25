# Security & Authorization Model

SentinelX performs actions (scanning, exploitation-chain testing) that would
be illegal or harmful if run against systems the operator does not own or
control. This document defines the non-negotiable guardrails the platform
must enforce as capabilities are built out.

## 1. Authorized-use principle

SentinelX **must never initiate active scanning or testing against an asset
without a verified authorization record.** This is a product requirement,
not just a legal disclaimer:

- Every `Asset` a customer registers must be linked to an `AuthorizationRecord`
  establishing ownership or documented permission to test (e.g. domain
  verification, cloud account role assumption, signed authorization
  attestation).
- Active testing modules (website scanning, API scanning, authenticated
  testing, automated penetration testing, AI business-logic testing) must
  refuse to run against an asset lacking a current, unexpired authorization
  record.
- Passive/static modules (source code scanning, secret detection,
  dependency analysis, container image scanning) still require the asset to
  belong to a tenant's verified project, but do not touch live systems.
- Authorization records are auditable: who granted it, when, scope
  (hostnames/IP ranges/repositories), and expiry.

## 2. Tenancy & isolation

- All data is tenant-scoped (`organization_id` on every row that isn't
  global reference data). Cross-tenant reads/writes must be structurally
  impossible, not just filtered at the API layer.
- Scan execution for one tenant must not be able to observe or affect
  another tenant's scan jobs, artifacts, or credentials.
- Scan engines that execute arbitrary or untrusted logic (e.g. analyzing
  customer-supplied code or containers) run in isolated, resource-limited,
  network-restricted Docker containers with no access to platform
  credentials or other tenants' data.

## 3. AuthN / AuthZ

**Implemented** — see [`docs/authentication.md`](../authentication.md),
[`docs/rbac.md`](../rbac.md), [`docs/api-keys.md`](../api-keys.md), and
[`docs/security.md`](../security.md) for the full design and ADRs
0002–0004 for the decisions behind it. Summary:

- **JWT access tokens**: short-lived (default 15 minutes), signed with a
  strong secret (independent secret from refresh tokens), validated on
  every request, individually revocable via a Redis logout denylist and
  bulk-revocable via `User.token_version`.
- **Refresh tokens**: long-lived, stored hashed (SHA-256, not Argon2id —
  see ADR 0004), rotated on every use with family-wide reuse-detection
  revocation, and revocable (logout, password change, "log out
  everywhere", or detected compromise).
- **RBAC**: roles are scoped per tenant (`Owner`, `Administrator`,
  `Security Analyst`, `Developer`, `Viewer`), enforced by a single
  `require_permission` dependency against a `Permission` matrix — no
  endpoint hand-rolls a role check.
- **OAuth2**: authorization-code flow with PKCE for SSO providers is
  **not yet implemented** — config placeholders exist from the scaffold
  phase; email/password plus API keys are the current auth surface. The
  platform will never store third-party IdP passwords once added.
- Secrets (JWT signing keys, OAuth client secrets, database credentials,
  storage keys) are supplied via environment variables / secret managers,
  never committed to source control; no shipped default is
  production-usable (see `.env.example`).

## 4. Secrets & credential handling

- Customer-provided credentials for authenticated scanning (application
  logins, API keys, cloud role ARNs) are encrypted at rest using envelope
  encryption and are only decrypted transiently inside the worker process
  performing the scan.
- Secrets discovered by the secret-detection module are never logged or
  displayed in plaintext outside the finding detail view; they are
  redacted in exports and third-party integrations (e.g. Slack/webhook
  notifications) by default.

## 5. Data protection

- TLS in transit everywhere (frontend↔API, API↔datastores where supported,
  API↔object storage).
- Encryption at rest for PostgreSQL and S3-compatible storage in
  staging/production.
- Scan artifacts and reports are access-controlled via the same tenancy
  model as the rest of the platform; object storage uses short-lived
  presigned URLs rather than public buckets.

## 6. Rate limiting & abuse prevention

- API rate limiting (`RATE_LIMIT_PER_MINUTE`) protects against credential
  stuffing and scan-triggering abuse.
- Active testing modules apply configurable request-rate ceilings against
  target assets to avoid degrading the customer's own production systems
  (denial-of-service is an explicit non-goal of every testing module).

## 7. Audit logging

- Authentication events, authorization-record changes, and every scan
  execution (who, what asset, what scope, when) are written to an
  append-only audit log, queryable by tenant admins.

## 8. Responsible use of AI-assisted testing

- AI-powered business-logic testing operates only within the authorized
  scope of a registered asset and respects the same rate limits and
  destructive-action safeguards as other active testing modules.
- Generated test actions that could be destructive (e.g. data deletion,
  state-changing transactions) require explicit opt-in configuration per
  asset before the AI testing module will attempt them.

## 9. Current implementation state

Tenancy (database-layer isolation — see
[`docs/tenant-isolation.md`](../tenant-isolation.md)) and authentication/
authorization (this document's section 3, fully implemented — see
[`docs/authentication.md`](../authentication.md)) are built. No scanning,
authorization-record, or active-testing logic is implemented yet — the
guardrails in sections 1, 2 (asset/scan-specific parts), 4–8 remain
binding design constraints for that future work, not retrofitted later.
RLS (row-level security) remains a deferred decision — see
[ADR 0001](../decisions/0001-tenant-isolation-and-rls.md); this phase
satisfies that ADR's stated trigger condition (an authenticated
request/session layer now exists) without itself revisiting the
decision.
