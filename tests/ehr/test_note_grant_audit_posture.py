"""The note-revocation receipt is a closed owner-only transition sink."""

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db


def test_note_revocation_audit_has_exact_closed_trigger_posture() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT r.rolname,p.prosecdef,p.provolatile,p.proconfig,"
            "has_function_privilege('clinic_app',p.oid,'EXECUTE'),"
            "has_function_privilege('clinic_resolver',p.oid,'EXECUTE'),"
            "has_function_privilege('clinic_agent',p.oid,'EXECUTE'),"
            "pg_get_function_result(p.oid) "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "JOIN pg_roles r ON r.oid=p.proowner "
            "WHERE n.nspname='clinic_app' "
            "AND p.proname='ehr_note_revocation_audit'"
        )
        assert cursor.fetchall() == [
            (
                "clinic_owner",
                True,
                "v",
                ["search_path=pg_catalog, pg_temp"],
                False,
                False,
                False,
                "trigger",
            )
        ]
        cursor.execute(
            "SELECT c.relname,t.tgtype,t.tgenabled,t.tgqual IS NOT NULL "
            "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "WHERE t.tgfoid="
            "'clinic_app.ehr_note_revocation_audit()'::regprocedure"
        )
        assert cursor.fetchall() == [("ehr_noteaccessgrant", 17, "O", True)]
