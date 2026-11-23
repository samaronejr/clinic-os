"""Allow physician publication and guard v2 assignment state in the database."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.intake.migrations._questionnaire_branching_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Keep all earlier list schemas usable without rewriting sensitive answers."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("intake", "0014_delegate_sessions"),
    ]
    operations: ClassVar[list[Operation]] = [migrations.RunSQL(SQL, REVERSE_SQL)]
