"""Frozen SQL for offboarding internal reader."""

SQL = """
SET LOCAL ROLE clinic_resolver;
GRANT EXECUTE ON FUNCTION clinic_app.identity_offboarding_clinics(uuid, uuid)
    TO clinic_owner;
RESET ROLE;
"""
