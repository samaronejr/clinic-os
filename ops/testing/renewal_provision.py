"""Seed one renewal browser fixture through the owner bootstrap service.

Runs under ``config.settings.base`` with ``APP_DATABASE_URL`` bound to the
owner role; every identifier arrives through the runner's private environment.
"""

from __future__ import annotations

import os
import sys
from uuid import UUID

REQUIRED_ENVIRONMENT = (
    "RENEWAL_CLINIC_ID",
    "RENEWAL_CLINIC_NAME",
    "RENEWAL_CNPJ",
    "RENEWAL_CRM_UF",
    "RENEWAL_ORGANIZATION_ID",
    "RENEWAL_ORGANIZATION_NAME",
    "RENEWAL_OWNER_EMAIL",
    "RENEWAL_OWNER_ID",
    "RENEWAL_OWNER_PASSWORD",
    "RENEWAL_OWNER_USERNAME",
    "RENEWAL_TIMEZONE",
)


def main() -> int:
    """Provision the first clinic and owner; reject any missing input."""
    missing = [name for name in REQUIRED_ENVIRONMENT if not os.environ.get(name)]
    if missing:
        sys.stderr.write(f"renewal-provision: missing {missing[0]}\n")
        return 2
    import django  # noqa: PLC0415 - setup must follow the env validation.

    django.setup()
    from apps.identity.management.bootstrap import (  # noqa: PLC0415
        BootstrapRequest,
        bootstrap_clinic,
    )

    request = BootstrapRequest(
        organization_id=UUID(os.environ["RENEWAL_ORGANIZATION_ID"]),
        organization_name=os.environ["RENEWAL_ORGANIZATION_NAME"],
        cnpj=os.environ["RENEWAL_CNPJ"],
        clinic_id=UUID(os.environ["RENEWAL_CLINIC_ID"]),
        clinic_name=os.environ["RENEWAL_CLINIC_NAME"],
        crm_uf=os.environ["RENEWAL_CRM_UF"],
        timezone=os.environ["RENEWAL_TIMEZONE"],
        owner_user_id=UUID(os.environ["RENEWAL_OWNER_ID"]),
        owner_username=os.environ["RENEWAL_OWNER_USERNAME"],
        owner_email=os.environ["RENEWAL_OWNER_EMAIL"],
    )
    bootstrap_clinic(request, os.environ["RENEWAL_OWNER_PASSWORD"])
    if os.environ.get("CLINIC_SECRET_BACKEND"):
        _issue_tenant_key(str(request.organization_id))
    sys.stdout.write("renewal-provision: owner fixture provisioned\n")
    return 0


def _issue_tenant_key(organization_id: str) -> None:
    """Give the seeded organization its active DEK through the owner boundary."""
    from apps.tenancy.envelope import issue_tenant_key  # noqa: PLC0415
    from django.db import connection, transaction  # noqa: PLC0415

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [organization_id],
        )
        issue_tenant_key()
    sys.stdout.write("renewal-provision: tenant key issued\n")


if __name__ == "__main__":
    raise SystemExit(main())
