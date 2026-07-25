# RBAC (Role-Based Access Control)

## Roles

`MembershipRole` (`app/models/enums.py`, unchanged from the
database-foundation phase — this phase adds enforcement, not new roles):

| Role | Intent |
|---|---|
| `OWNER` | Full control of the tenant, including membership/role management |
| `ADMINISTRATOR` | Full control, functionally equivalent to Owner in this phase |
| `SECURITY_ANALYST` | Read access to members/API keys, read access to the audit trail |
| `DEVELOPER` | Read access to members/API keys, no audit access |
| `VIEWER` | Read access to members only |

`OWNER` and `ADMINISTRATOR` are functionally identical today. A finer
distinction (e.g. only an `OWNER` may remove another `OWNER`, or only an
`OWNER` may delete the tenant) is deferred rather than guessed at without
a concrete product requirement driving it — see "Future work" below.

## Permission matrix

`app/core/permissions.py` defines the matrix as data, not scattered
`if role == ...` checks:

```python
class Permission(StrEnum):
    MEMBERS_VIEW = "members:view"
    MEMBERS_MANAGE = "members:manage"
    API_KEYS_VIEW = "api_keys:view"
    API_KEYS_MANAGE = "api_keys:manage"
    AUDIT_VIEW = "audit:view"

ROLE_PERMISSIONS: dict[MembershipRole, frozenset[Permission]] = {
    MembershipRole.OWNER: frozenset(Permission),          # everything
    MembershipRole.ADMINISTRATOR: frozenset(Permission),  # everything
    MembershipRole.SECURITY_ANALYST: {MEMBERS_VIEW, API_KEYS_VIEW, AUDIT_VIEW},
    MembershipRole.DEVELOPER: {MEMBERS_VIEW, API_KEYS_VIEW},
    MembershipRole.VIEWER: {MEMBERS_VIEW},
}
```

Every `*_MANAGE` permission implies its corresponding `*_VIEW` permission
for every role that has it (enforced by construction here, and asserted
by `test_permissions.py::test_manage_permission_implies_view_across_all_roles`)
— there is no role that can manage a resource it can't view.

## Enforcing a permission

**One dependency factory, used everywhere — no endpoint hand-rolls a role
check:**

```python
@router.post(
    "",
    dependencies=[Depends(require_permission(Permission.API_KEYS_MANAGE))],
)
async def create_api_key(..., principal: CurrentPrincipalDep) -> ...:
    ...
```

`require_permission(Permission.X)` returns a FastAPI dependency that
resolves `CurrentPrincipalDep` (see `docs/authentication.md`) and raises
`403` if `has_permission(principal.role, Permission.X)` is `False`.

It's wired as a **router-level `dependencies=[...]` entry**, not as the
value bound to the `principal` parameter — mixing
`Annotated[Principal, Depends(get_current_principal)]` (what
`CurrentPrincipalDep` already is) with an explicit
`= Depends(require_permission(...))` default on the same parameter would
give it two conflicting `Depends`. Both the `dependencies=[]` entry and
the plain `principal: CurrentPrincipalDep` parameter resolve the same
FastAPI-cached `get_current_principal` call per request, so this costs
nothing extra — see `app/api/v1/endpoints/api_keys.py` for the pattern
applied consistently across `api_keys.py`, `memberships.py`, and
`audit_events.py`.

## Where it's applied

| Resource | View permission | Manage permission |
|---|---|---|
| Tenant memberships | `MEMBERS_VIEW` (list) | `MEMBERS_MANAGE` (add/role-change/remove) |
| API keys | `API_KEYS_VIEW` (list) | `API_KEYS_MANAGE` (create/revoke/rotate) |
| Audit events | `AUDIT_VIEW` (list) | — (append-only; nothing to manage) |

Every mutation additionally writes an `AuditEvent` — see
`docs/security.md`'s audit section for the full list of automatically
audited actions, including `membership.role_change`, which records the
old and new role in `event_metadata`.

## Tenant scoping

RBAC answers "can this role do X" — it does not answer "in which
tenant." That's `Principal.tenant_id`, resolved separately (see
`docs/authentication.md`) and always used to scope the underlying
repository query (`TenantMembershipRepository.list_by_tenant(principal.tenant_id, ...)`,
etc.). A `VIEWER` in tenant A calling `GET /memberships` can never see
tenant B's members regardless of role, because the query itself is
scoped by `principal.tenant_id` — RBAC and tenant isolation are two
independent, composed checks, not one mechanism.

## Future work

- Owner-only actions distinct from Administrator (e.g. removing another
  Owner, deleting the tenant) — no product requirement yet.
- Inviting a brand-new email address (no existing `User` account) to a
  tenant — `POST /memberships` currently only adds an *existing* user by
  email; a real invitation-email flow is separate scope.
- Per-scope API key permission enforcement (see `docs/api-keys.md`'s
  "Scopes" section) — `APIKeyMetadata.scopes` is persisted but not
  enforced against any action yet, since there are no scannable/product
  endpoints for scopes to gate in this phase.
