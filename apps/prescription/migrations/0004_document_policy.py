"""Install FORCE RLS and insert-only enforcement for rendered artifacts."""

from typing import ClassVar

from django.db import migrations

from ._document_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Reuse the assigned/care predicates and the shared immutable trigger."""

    dependencies: ClassVar = [("prescription", "0003_prescription_document")]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
