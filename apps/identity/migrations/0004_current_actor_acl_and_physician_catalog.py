"""Canonicalize identities and lock runtime identity access to read-only resolvers."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.db import migrations, models

from apps.identity import phase1a_identity_migration as data_migration
from apps.identity.phase1a_identity_acl_migration import (
    install_runtime_identity_acl,
    remove_runtime_identity_acl,
)

if TYPE_CHECKING:
    from django.db.migrations.operations.base import Operation

canonicalize_email_for_migration = data_migration.canonicalize_email_for_migration
canonicalize_username_for_migration = data_migration.canonicalize_username_for_migration
canonicalize_identities = data_migration.canonicalize_identities
collapse_duplicate_roles = data_migration.collapse_duplicate_roles


class Migration(migrations.Migration):
    """Install trusted current-actor identity invariants."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0003_totp_device_rls"),
        ("tenancy", "0002_rls_and_resolvers"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunPython(canonicalize_identities, migrations.RunPython.noop),
        migrations.RunPython(collapse_duplicate_roles, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="userclinicrole",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "user", "role"),
                name="identity_userclinicrole_assignment_uniq",
            ),
        ),
        migrations.RunPython(
            install_runtime_identity_acl,
            remove_runtime_identity_acl,
        ),
    ]
