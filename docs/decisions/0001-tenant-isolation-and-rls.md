# ADR 0001: Tenant Isolation Strategy and Deferral of Row-Level Security

## Status

Accepted — database-foundation phase.

## Context

SentinelX is multi-tenant and will store sensitive customer data (source
code metadata, scan results, findings). Cross-tenant data access is a
security incident. PostgreSQL offers Row-Level Security (RLS) as a
database-enforced isolation mechanism: policies attached to a table that
transparently filter every query by a session-level setting (typically
`SET LOCAL app.tenant_id = '...'`), regardless of what the application
code does or forgets to do.

This phase needs to decide the isolation strategy for the ten entities
being introduced now, and explicitly document whether RLS is part of it.

## Decision

**RLS is not enabled in this phase.** Tenant isolation is enforced by two
layers instead (see [`tenant-isolation.md`](../tenant-isolation.md) for the
full mechanics):

1. **Repository contract** — every tenant-owned repository method requires
   `tenant_id` as an explicit, non-optional argument, with no unscoped
   list/get method anywhere.
2. **Composite foreign keys** — every tenant-owned child table's
   `tenant_id` is DB-enforced to match its parent's via a composite FK
   against a `UNIQUE (tenant_id, id)` constraint on the parent, so a
   write with a mismatched `tenant_id` is rejected by PostgreSQL itself.

## Why RLS is deferred, not rejected

RLS is a strong mechanism and worth adopting eventually, but adopting it
*now* would add operational complexity this phase can't justify yet:

- RLS policies filter by a **session-level GUC** (e.g. `current_setting('app.tenant_id')`),
  which means every connection checkout needs `SET LOCAL app.tenant_id = ...`
  run before any query — that's naturally driven by request/auth
  middleware. There is no API layer yet in this phase (no endpoints, no
  authentication) to set that value from, so there's nothing to test the
  policy against beyond synthetic session variables.
- Getting RLS wrong is not a soft failure — a mis-scoped policy (e.g.
  forgetting `FORCE ROW LEVEL SECURITY`, which is required for the table
  owner not to bypass RLS by default) can silently pass and give a false
  sense of protection, or silently fail and expose data. Validating it
  properly requires a running policy plus adversarial tests exercised
  through the layer that sets the session variable — which doesn't exist
  yet.
- Introducing RLS now, before there's a stable authentication/session
  model, means likely reworking the policies once that model lands anyway.

Given that, the two layers above give strong, testable guarantees today
(and are exercised directly by `test_*cross_tenant*` tests across the
suite) without taking on a mechanism that can't be properly validated end
to end yet.

## What would be required to introduce RLS later

When an authenticated request/session layer exists:

1. Enable RLS per tenant-owned table: `ALTER TABLE projects ENABLE ROW LEVEL SECURITY; ALTER TABLE projects FORCE ROW LEVEL SECURITY;`
   (the `FORCE` variant is required, or the table owner — likely the
   application's own DB role — bypasses RLS entirely).
2. Add a policy per table, e.g.:
   ```sql
   CREATE POLICY tenant_isolation ON projects
       USING (tenant_id = current_setting('app.tenant_id')::uuid);
   ```
3. Have the connection-checkout path (a SQLAlchemy event listener, or a
   FastAPI dependency wrapping `get_db`) issue
   `SET LOCAL app.tenant_id = :tenant_id` at the start of every
   request-scoped transaction, sourced from the authenticated session —
   never from a client-supplied header/parameter directly.
4. Decide the policy for tables with nullable `tenant_id` (`AuditEvent`) —
   likely a policy that allows the row when `tenant_id IS NULL OR tenant_id = current_setting(...)`.
5. Add tests that exercise RLS through that connection-checkout path
   specifically (not just via a manually-issued `SET LOCAL` in a test),
   including the negative case: a session with no `app.tenant_id` set
   should see zero rows, not an error and not all rows.
6. Keep the two existing layers (repository contract, composite FKs) as
   defense in depth — RLS is an additional layer, not a replacement for
   either.

## Consequences

- Cross-tenant isolation today depends on repository code being written
  correctly and reviewed — there is no database-level backstop against a
  missing `tenant_id` filter on a *read*. This is documented, tested, and
  accepted as the trade-off for this phase.
- Writes cannot cross tenant boundaries regardless of application-layer
  bugs, due to the composite FK layer.
- Revisit this decision once authentication/session handling exists in the
  codebase — that's the trigger condition for step 3 above to become
  possible.
