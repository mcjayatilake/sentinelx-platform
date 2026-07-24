"""Repository layer: one explicit, typed class per entity.

No generic `Repository[T]` base — see `app.repositories.base` for why.
Tenant-owned entities require `tenant_id` as an explicit argument on every
read/list method; there is no unscoped "list all" method for tenant-owned
data anywhere in this package.
"""
