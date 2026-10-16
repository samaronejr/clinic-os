"""Close legacy PUBLIC execution and grant only the actual lifecycle callers."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

SQL = """
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION
    clinic_app.identity_invitation_user(uuid, uuid),
    clinic_app.identity_invitation_begin(bytea, bytea, text, text),
    clinic_app.identity_invitation_finish(bytea, text),
    clinic_app.identity_invitation_claim(bytea),
    clinic_app.identity_offboarding_clinics(uuid, uuid),
    clinic_app.identity_offboarding_execute(uuid, uuid),
    clinic_app.identity_recovery_consume(bytea),
    clinic_app.identity_reset_execute(uuid, bytea),
    clinic_app.identity_reset_notification_clock(),
    clinic_app.identity_reset_recipient(uuid),
    clinic_app.identity_reset_target(uuid, uuid),
    clinic_app.identity_retire_authenticators(uuid),
    clinic_app.identity_revoke_changed_sessions(),
    clinic_app.identity_session_active(uuid, bytea),
    clinic_app.identity_session_register(uuid, bytea, text, text, timestamptz),
    clinic_app.identity_session_revoke_hash(bytea),
    clinic_app.identity_session_step_up(bytea, uuid),
    clinic_app.identity_session_verified(bytea),
    clinic_app.identity_session_verify(bytea, integer),
    clinic_app.identity_staff_directory(uuid)
    FROM PUBLIC, clinic_app, clinic_agent;
GRANT EXECUTE ON FUNCTION
    clinic_app.identity_invitation_claim(bytea),
    clinic_app.identity_offboarding_clinics(uuid, uuid),
    clinic_app.identity_recovery_consume(bytea),
    clinic_app.identity_reset_execute(uuid, bytea),
    clinic_app.identity_reset_recipient(uuid),
    clinic_app.identity_reset_target(uuid, uuid),
    clinic_app.identity_session_active(uuid, bytea),
    clinic_app.identity_session_register(uuid, bytea, text, text, timestamptz),
    clinic_app.identity_session_revoke_hash(bytea),
    clinic_app.identity_session_step_up(bytea, uuid),
    clinic_app.identity_session_verified(bytea),
    clinic_app.identity_session_verify(bytea, integer),
    clinic_app.identity_staff_directory(uuid)
    TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.identity_retire_authenticators(uuid)
    TO clinic_owner;
RESET ROLE;
"""


class Migration(migrations.Migration):
    """Retain every recorded migration, row and mixed reader owner."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0023_authenticator_verification_metadata"),
    ]

    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(SQL)]
