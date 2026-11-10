"""Independently authenticated, rate-limited, transaction-local SCIM scope."""

from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import connection, transaction

from apps.identity.enterprise_authority import (
    GovernanceDeniedError,
    require_synthetic_identity,
)
from apps.tenancy.db import clear_connection_tenant_gucs

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID


class ScimRateLimitError(GovernanceDeniedError):
    """A fixed token budget was exceeded; bearer material is never reflected."""


@dataclass(frozen=True, slots=True)
class DirectoryScope:
    """Only the stored token, not a body tenant claim, can supply this binding."""

    clinic_id: UUID
    connection_id: UUID
    organization_id: UUID


@contextmanager
def scim_context(*, bearer: str) -> Iterator[DirectoryScope]:
    """Count authentication independently of refused resource transactions."""
    require_synthetic_identity()
    if (
        connection.in_atomic_block
        or not isinstance(bearer, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", bearer) is None
    ):
        raise GovernanceDeniedError
    digest = hashlib.sha256(bearer.encode("ascii")).digest()
    with transaction.atomic(durable=True), connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.enterprise_scim_auth(%s)", [digest])
        row = cursor.fetchone()
    if row is None:
        raise GovernanceDeniedError
    connection_id, organization_id, clinic_id, limited = row
    if limited is True:
        raise ScimRateLimitError
    try:
        with transaction.atomic(durable=True), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true),"
                "set_config('app.current_user_id','',true),"
                "set_config('app.current_scim_token',%s,true)",
                [str(organization_id), digest.hex()],
            )
            yield DirectoryScope(clinic_id, connection_id, organization_id)
    finally:
        clear_connection_tenant_gucs()
