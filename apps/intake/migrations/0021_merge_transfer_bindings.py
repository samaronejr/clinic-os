"""Keep forward links and narrowly scoped queue moves inside the adjudication."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.intake.migrations._merge_transfer_sql import SQL


class Migration(migrations.Migration):
    """Enforce retained transfer provenance without historical repointing."""

    dependencies: ClassVar = [("intake", "0020_alias_released_reads")]
    operations: ClassVar[list[Operation]] = [migrations.RunSQL(SQL)]
