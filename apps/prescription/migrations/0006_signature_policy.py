"""Install FORCE RLS and transition enforcement for signature operations."""

from typing import ClassVar

from django.db import migrations

from ._signature_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Reuse the assigned-issuer predicates and the shared immutable trigger."""

    dependencies: ClassVar = [("prescription", "0005_signature_operation")]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
