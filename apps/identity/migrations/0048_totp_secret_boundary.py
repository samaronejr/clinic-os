"""Relocate the TOTP secret and verify RFC 6238 codes inside the database.

Reversible without loss. The forward pass moves each device secret out of
``otp_totp_totpdevice.key`` into the resolver-owned ``identity_totpsecret`` and
clears ``key`` (refusing, before anything moves, a key that is not hex). The
reverse writes every hidden secret back into ``key`` as ``clinic_resolver``
before it drops the store, the verifier and the guard: that is the pre-0048
posture (the runtime role can read the seed again), and it keeps a rollback to
code that verifies in Python from stranding any user. Secrets are never logged.

``identity_totpsecret`` is registered in the ORM state (so it joins the ORM
model set and the test-teardown TRUNCATE) via ``SeparateDatabaseAndState``; its
real table, resolver ownership, FORCE RLS and grants are installed by the
``RunSQL`` below, which also creates the in-database verifier and device guard.
"""

from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._totp_secret_sql import (
    FUNCTIONS_SQL,
    GUARD_SQL,
    INVITATION_SECRET_SQL,
    REVERSE_SQL,
    SESSION_VERIFY_SQL,
    STORE_TABLE_SQL,
)


class Migration(migrations.Migration):
    """Install the resolver-owned secret store and in-database verifier."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0047_insurance_approval_permission"),
        ("otp_totp", "0003_add_timestamps"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="TotpSecret",
                    fields=[
                        (
                            "device",
                            models.OneToOneField(
                                db_column="device_id",
                                on_delete=django.db.models.deletion.DO_NOTHING,
                                primary_key=True,
                                related_name="clinic_totp_secret",
                                serialize=False,
                                to="otp_totp.totpdevice",
                            ),
                        ),
                        ("secret", models.BinaryField()),
                    ],
                    options={"db_table": "identity_totpsecret"},
                ),
            ],
            database_operations=[],
        ),
        migrations.RunSQL(
            sql=STORE_TABLE_SQL
            + FUNCTIONS_SQL
            + SESSION_VERIFY_SQL
            + GUARD_SQL
            + INVITATION_SECRET_SQL,
            reverse_sql=REVERSE_SQL,
        ),
    ]
