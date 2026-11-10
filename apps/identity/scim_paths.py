"""The sole selector exception: opaque staff/group ids in SCIM resource paths.

Root best judgment after the owner question timed out, 2026-10-23. This does
not authorize clinical ids, account ids, names, emails or filters in URLs.
Search remains POST /.search. No other route or tenant bypass is widened.
"""

from __future__ import annotations

import re
from typing import Final
from uuid import UUID

SCIM_PREFIX: Final = "/scim/v2/"
OPAQUE_ID_PATTERN: Final = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
RESOURCE_PATH_PATTERN: Final = (
    rf"^scim/v2/(?P<kind>Users|Groups)/(?P<resource_id>{OPAQUE_ID_PATTERN})$"
)
COLLECTION_PATH_PATTERN: Final = r"^/scim/v2/(Users|Groups)(/\.search)?$"


class ScimPathError(ValueError):
    """Refuse any value outside the explicitly allowed directory-id grammar."""


def is_scim_endpoint(path: str) -> bool:
    """Reject arbitrary bypass routes even when they have a SCIM prefix."""
    return (
        re.fullmatch(COLLECTION_PATH_PATTERN, path) is not None
        or re.fullmatch(RESOURCE_PATH_PATTERN, path.removeprefix("/")) is not None
    )


def scim_resource_path(*, kind: str, resource_id: UUID) -> str:
    """Construct only the closed SCIM-only UUID4 resource path."""
    if (
        kind not in {"Users", "Groups"}
        or not isinstance(resource_id, UUID)
        or re.fullmatch(OPAQUE_ID_PATTERN, str(resource_id)) is None
    ):
        raise ScimPathError
    return f"{SCIM_PREFIX}{kind}/{resource_id}"
