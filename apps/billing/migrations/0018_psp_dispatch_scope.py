"""Restrict the operation identity to the resolver; authorize dispatch inspection."""

from typing import ClassVar

from django.db import migrations

from apps.billing.migrations._psp_dispatch_scope_sql import SQL


class Migration(migrations.Migration):
    """Never reopen the predecessor's unchecked runtime grant on rollback."""

    dependencies: ClassVar = [("billing", "0017_psp_unknown_refinement")]
    operations: ClassVar = [migrations.RunSQL(SQL)]
