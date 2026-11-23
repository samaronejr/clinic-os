"""Frozen SQL for the user preference migration."""

from typing import Final

FORWARD_SQL: Final = """
ALTER TABLE clinic_app.identity_userpreference OWNER TO clinic_owner;
ALTER TABLE clinic_app.identity_userpreference ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_userpreference FORCE ROW LEVEL SECURITY;

CREATE POLICY userpreference_owner_only
    ON clinic_app.identity_userpreference
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
    ON TABLE clinic_app.identity_userpreference
    FROM PUBLIC, clinic_app, clinic_resolver;
GRANT SELECT, INSERT ON TABLE clinic_app.identity_userpreference TO clinic_app;
GRANT UPDATE (theme, density, updated_at)
    ON TABLE clinic_app.identity_userpreference
    TO clinic_app;
"""

REVERSE_SQL: Final = """
DROP POLICY IF EXISTS userpreference_owner_only
    ON clinic_app.identity_userpreference;
"""
