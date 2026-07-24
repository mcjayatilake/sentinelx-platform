"""Shared schema primitives: pagination and field validators.

Validators here are reused by multiple entity schemas via `field_validator`
so slug/email normalization rules live in exactly one place.
"""

import re

from pydantic import BaseModel, ConfigDict

_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def normalize_slug(value: str) -> str:
    value = value.strip().lower()
    if not _SLUG_PATTERN.match(value):
        raise ValueError(
            "slug must be lowercase alphanumeric segments separated by single hyphens "
            "(e.g. 'my-project')"
        )
    return value


def normalize_email(value: str) -> str:
    return value.strip().lower()


class PaginationParams(BaseModel):
    """Input parameters for a paginated repository list call."""

    model_config = ConfigDict(frozen=True)

    limit: int = 50
    offset: int = 0


class Page[T](BaseModel):
    """A page of results, mirroring `app.repositories.base.Page`."""

    items: list[T]
    total: int
    limit: int
    offset: int
