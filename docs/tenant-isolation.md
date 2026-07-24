# Tenant Isolation Strategy

SentinelX is multi-tenant, and this platform will eventually store
customer source-code metadata, credentials-adjacent scan configuration, and
vulnerability findings. A cross-tenant data leak here is a security
incident, not a bug report. This document is the binding strategy for how
isolation is enforced today, and what changes if that strategy needs to
change later.

Row-Level Security is evaluated and explicitly **not** enabled in this
phase — see [`decisions/0001-tenant-isolation-and-rls.md`](decisions/0001-tenant-isolation-and-rls.md)
for the full reasoning and what's required to add it.

## Two independent layers

### 1. Application layer: the repository contract

Every repository method that reads or lists tenant-owned data takes
`tenant_id` as a **required, positional** argument — never optional, never
defaulted, never inferred. There is no method on any tenant-owned
repository that lists or fetches rows without it. Concretely:

```python
# app/repositories/project_repository.py
async def get_by_id(self, tenant_id: uuid.UUID, project_id: uuid.UUID) -> Project | None:
    stmt = select(Project).where(Project.id == project_id, Project.tenant_id == tenant_id)
    return (await self._session.execute(stmt)).scalar_one_or_none()
```

Fetching `project_id` from a different tenant than `tenant_id` returns
`None` — not the row, not an error that could be caught-and-ignored, just
"not found," which is exactly what a caller with the wrong tenant context
should see.

There is deliberately **no generic `Repository[T]` base class** (see
`app/repositories/base.py`) with a shared `get()`/`list()` implementation.
A generic base is exactly the kind of abstraction that could hide a missing
`tenant_id` filter behind a "just works" interface — or have someone add a
new entity by subclassing it and forgetting the filter exists at all. Each
repository is small and explicit enough to review the tenant-scoping in
isolation.

This is enforced by code review and by tests (`test_cross_tenant_*` in
every repository test file — e.g.
`test_membership_repository.py::test_cross_tenant_membership_retrieval_returns_none`),
not by a framework guarantee. That is the honest limitation of this layer:
a future contributor *can* still write an unscoped query. The second layer
below exists because of that.

### 2. Database layer: composite foreign keys propagate `tenant_id`

Every tenant-owned child table's `tenant_id` is **not just a column that
happens to usually match its parent's** — it's DB-enforced to match, via a
composite foreign key against a composite unique constraint on the parent:

```python
# app/models/asset.py
__table_args__ = (
    UniqueConstraint("tenant_id", "id", name="uq_assets_tenant_id_id"),
    ForeignKeyConstraint(
        ["tenant_id", "project_id"],
        ["projects.tenant_id", "projects.id"],
        name="fk_assets_tenant_id_project_id_projects",
        ondelete="RESTRICT",
    ),
)
```

`projects` has a matching `UNIQUE (tenant_id, id)`. The effect: inserting
an `Asset` with `tenant_id=B` but `project_id` pointing at a project that
actually belongs to tenant `A` is rejected by PostgreSQL itself —
`InvalidForeignKey` / `IntegrityError`, before any application code runs.
This chain runs the full depth of the ownership tree: `Project` →
`Asset` (`tenant_id, project_id`) → `Scan` (`tenant_id, asset_id`) →
`Finding` (`tenant_id, asset_id` and `tenant_id, scan_id`) →
`FindingReference` (`tenant_id, finding_id`).

This is what `test_asset_repository.py::test_asset_project_must_belong_to_same_tenant`
tests directly: it constructs an `Asset` whose `project_id` is real but
belongs to a different tenant than the `tenant_id` given, and asserts
PostgreSQL rejects it — independent of anything the repository layer does.

**Why both layers, rather than relying on the DB constraint alone?** The
composite FK only catches a *mismatched* `tenant_id` on write — it can't
stop a query that's simply missing a tenant filter on read (e.g.
`SELECT * FROM projects` with no `WHERE`). The repository contract is what
prevents that. Together: writes can't cross tenant boundaries even if the
application layer has a bug, and reads can't leak across tenants because
there is no code path that doesn't filter.

## What this doesn't do

Nothing here stops a bug in a *future* API endpoint from taking
`tenant_id` from the wrong place (e.g. trusting a client-supplied value
instead of deriving it from the authenticated session). That's an
authentication/authorization-layer concern for the next phase, not a
database-layer one — this document is scoped to what the persistence layer
guarantees on its own.
