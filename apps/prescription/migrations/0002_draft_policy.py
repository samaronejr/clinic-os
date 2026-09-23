"""Install FORCE RLS and immutable prescription scope bindings."""

from typing import ClassVar

from django.db import migrations

from ._draft_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Use the existing assigned-physician predicate and resolver ownership."""

    dependencies: ClassVar = [("prescription", "0001_initial")]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
