"""Portal v2 representation choices and channel summaries through resolvers."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.intake.migrations._portal_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Read-only resolvers; no row, grant or retained state changes."""

    dependencies: ClassVar = [("intake", "0026_data_subject_requests")]
    operations: ClassVar[list[Operation]] = [migrations.RunSQL(SQL, REVERSE_SQL)]
