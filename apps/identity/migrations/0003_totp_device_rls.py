"""Install forced row security around persistent TOTP seed material."""

from typing import ClassVar, Final

from django.db import migrations
from django.db.migrations.operations.base import Operation

FORWARD_SQL: Final = """
ALTER TABLE clinic_app.otp_totp_totpdevice OWNER TO clinic_owner;

ALTER TABLE clinic_app.otp_totp_totpdevice ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.otp_totp_totpdevice FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS otp_totp_device_user_isolation
    ON clinic_app.otp_totp_totpdevice;
CREATE POLICY otp_totp_device_user_isolation
    ON clinic_app.otp_totp_totpdevice
    AS PERMISSIVE
    FOR ALL
    TO clinic_app
    USING (
        user_id = NULLIF(
            pg_catalog.current_setting('app.current_user_id', true),
            ''
        )::pg_catalog.uuid
    )
    WITH CHECK (
        user_id = NULLIF(
            pg_catalog.current_setting('app.current_user_id', true),
            ''
        )::pg_catalog.uuid
    );

REVOKE ALL PRIVILEGES
    ON TABLE clinic_app.otp_totp_totpdevice
    FROM PUBLIC, clinic_app, clinic_resolver;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON TABLE clinic_app.otp_totp_totpdevice
    TO clinic_app;

REVOKE ALL PRIVILEGES
    ON SEQUENCE clinic_app.otp_totp_totpdevice_id_seq
    FROM PUBLIC, clinic_app, clinic_resolver;
GRANT USAGE, SELECT
    ON SEQUENCE clinic_app.otp_totp_totpdevice_id_seq
    TO clinic_app;
"""

REVERSE_SQL: Final = """
DROP POLICY IF EXISTS otp_totp_device_user_isolation
    ON clinic_app.otp_totp_totpdevice;
ALTER TABLE clinic_app.otp_totp_totpdevice NO FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.otp_totp_totpdevice DISABLE ROW LEVEL SECURITY;
"""


class Migration(migrations.Migration):
    """Bind each TOTP device row to the transaction's current user GUC."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0002_userclinicrole_org_clinic_fk"),
        ("otp_totp", "0003_add_timestamps"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]
