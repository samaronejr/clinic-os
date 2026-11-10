"""Give each longitudinal fact table its own guard (Wave D guard split)."""

from typing import ClassVar

from django.db import migrations

from apps.ehr.migrations._longitudinal_split_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Each table's guard reads only its own NEW columns; behaviour is unchanged."""

    dependencies: ClassVar = [("ehr", "0016_later_pack_draft_guard")]
    operations: ClassVar = [migrations.RunSQL(SQL, reverse_sql=REVERSE_SQL)]
