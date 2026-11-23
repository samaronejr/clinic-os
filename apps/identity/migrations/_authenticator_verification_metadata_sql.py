"""Frozen SQL for authenticator verification metadata."""

SQL = """
GRANT SELECT (id, user_id, confirmed, last_t, last_used_at)
    ON clinic_app.otp_totp_totpdevice TO clinic_resolver;
GRANT SELECT (id, user_id, confirmed)
    ON clinic_app.otp_static_staticdevice TO clinic_resolver;
"""
