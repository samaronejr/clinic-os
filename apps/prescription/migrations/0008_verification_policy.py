"""Install FORCE RLS, resolvers and guards for verification and release."""

from typing import ClassVar

from django.db import migrations

from ._verification_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Reuse the assigned/care predicates and the shared immutable trigger."""

    dependencies: ClassVar = [
        ("prescription", "0007_verification"),
        ("retention", "0002_retention_policy"),
    ]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
