"""Shared setup for spawned load processes (each has its own GIL and connection)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ops.perf.runtime import CLIENT_HOST

if TYPE_CHECKING:
    from ops.perf.dataset import Tenant


@dataclass(frozen=True, slots=True)
class ChildContext:
    """Plain values a spawned process needs to reach the same database."""

    database_name: str
    secret_backend: str
    secret_dir: str
    organization_id: str
    clinic_id: str
    admin_id: str


def context_for(tenant: Tenant) -> ChildContext:
    """Capture the parent's database and key settings for one tenant."""
    from django.conf import settings  # noqa: PLC0415
    from django.db import connection  # noqa: PLC0415

    return ChildContext(
        database_name=str(connection.settings_dict["NAME"]),
        secret_backend=str(settings.CLINIC_SECRET_BACKEND),
        secret_dir=str(settings.CLINIC_SECRET_DIR),
        organization_id=str(tenant.organization_id),
        clinic_id=str(tenant.clinic_id),
        admin_id=str(tenant.admin_id),
    )


def child_setup(context: ChildContext) -> None:
    """Configure Django in a spawned process exactly like its parent."""
    import django  # noqa: PLC0415

    django.setup()
    from django.conf import settings  # noqa: PLC0415
    from django.db import connection  # noqa: PLC0415

    connection.settings_dict["NAME"] = context.database_name
    settings.CLINIC_SECRET_BACKEND = context.secret_backend
    settings.CLINIC_SECRET_DIR = context.secret_dir
    settings.ALLOWED_HOSTS = [*settings.ALLOWED_HOSTS, CLIENT_HOST]
