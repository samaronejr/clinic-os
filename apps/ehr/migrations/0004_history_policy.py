"""Install append-only history policies and immutable version bindings."""

from typing import ClassVar

from django.db import migrations

from ._history_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Enforce history authority independently of services and views."""

    dependencies: ClassVar = [("ehr", "0003_longitudinal_history")]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
