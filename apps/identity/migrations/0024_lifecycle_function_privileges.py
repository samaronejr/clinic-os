"""Close legacy PUBLIC execution and grant only the actual lifecycle callers."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._lifecycle_function_privileges_sql import SQL


class Migration(migrations.Migration):
    """Retain every recorded migration, row and mixed reader owner."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0023_authenticator_verification_metadata"),
    ]

    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(SQL)]
