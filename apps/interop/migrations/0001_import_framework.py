"""Publish the governed import schema, guards, visibility policies and grants."""

from typing import ClassVar

from django.conf import settings
from django.db import migrations

from apps.interop.migrations._import_sql import SQL
from apps.interop.migrations._schema import MODELS, ProtectedSchema


class Migration(migrations.Migration):
    """Irreversible protected schema; rollback requires a restore."""

    initial = True
    atomic = False
    # Dependencies are only what this migration touches when it runs: the user,
    # clinic and patient tables and has_permission (identity), intake's latest
    # node (its enrollment and duplicate-candidate tables get the restrictive
    # visibility policies and its contact tables resolver grants) and comms
    # participants (resolver grant). The rollback activity check is plpgsql and
    # resolves every other relation when it runs. A later node of another app
    # here would make that app's reversible migrations irreversible, because
    # this migration can never be unapplied.
    dependencies: ClassVar = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("comms", "0008_omnichannel_inbox"),
        ("identity", "0047_insurance_approval_permission"),
        ("intake", "0029_membership_payer"),
    ]
    operations: ClassVar = [
        ProtectedSchema(
            database_operations=[*MODELS, migrations.RunSQL(SQL)],
            state_operations=MODELS,
        )
    ]
