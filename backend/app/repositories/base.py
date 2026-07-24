"""Shared repository primitives.

`Page` is a plain return-type container, not an abstraction over queries —
there is deliberately no generic `Repository[T]` base class here. Hiding a
repository method's tenant-scoping behind a shared base is exactly the kind
of thing that could silently drop a `tenant_id` filter; each repository
below is explicit instead.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Page[ModelT]:
    items: list[ModelT]
    total: int
    limit: int
    offset: int
