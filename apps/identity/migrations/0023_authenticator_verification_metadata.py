"""Expose only verification metadata to narrow lifecycle resolvers."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._authenticator_verification_metadata_sql import SQL


class Migration(migrations.Migration):
    """Keep OTP seed/key columns inaccessible to the resolver role."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0022_lifecycle_surface_scope"),
    ]
    operations: ClassVar[Sequence[Operation]] = [
        migrations.RunSQL(SQL),
    ]
