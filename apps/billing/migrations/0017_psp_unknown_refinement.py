"""A definite lookup can refine an unknown observation at the same sequence."""

from typing import ClassVar

from django.db import migrations

from apps.billing.migrations._psp_refinement_sql import SQL


class Migration(migrations.Migration):
    """Install exactly the two revised predicates without rewriting a money fact."""

    dependencies: ClassVar = [("billing", "0016_psp_observer")]
    operations: ClassVar = [migrations.RunSQL(SQL)]
