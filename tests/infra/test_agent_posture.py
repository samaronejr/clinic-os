"""Todo 7 exact machine-role grants, not a subset of the staff surface."""

import pytest
from apps.tenancy import posture
from django.db import connection

pytestmark = pytest.mark.django_db(transaction=True)


def test_agent_role_is_an_unprivileged_non_owner_login() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, "
            "rolcreaterole, rolreplication, rolinherit FROM pg_roles "
            "WHERE rolname='clinic_agent'"
        )
        assert cursor.fetchone() == (True, False, False, False, False, False, False)
        cursor.execute(
            "SELECT member::regrole::text, roleid::regrole::text "
            "FROM pg_auth_members WHERE member='clinic_agent'::regrole "
            "OR roleid='clinic_agent'::regrole"
        )
        assert cursor.fetchall() == []
        cursor.execute(
            "SELECT relname FROM pg_class WHERE relowner='clinic_agent'::regrole "
            "UNION ALL SELECT nspname FROM pg_namespace "
            "WHERE nspowner='clinic_agent'::regrole "
            "UNION ALL SELECT datname FROM pg_database "
            "WHERE datdba='clinic_agent'::regrole"
        )
        assert cursor.fetchall() == []


def test_agent_table_column_sequence_and_default_privileges_are_exact() -> None:
    expected = {
        (table, privilege)
        for table, privileges in posture.agent_grants().items()
        for privilege in privileges
    }
    assert expected == {("scheduling_availabilityblock", "SELECT")}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relname, p.privilege FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE',"
            "'TRUNCATE','REFERENCES','TRIGGER']) p(privilege) "
            "WHERE n.nspname='clinic_app' AND c.relkind IN ('r','p','v','m','f') "
            "AND has_table_privilege('clinic_agent', c.oid, p.privilege)"
        )
        assert set(cursor.fetchall()) == expected
        cursor.execute(
            "SELECT c.relname, a.attname, acl.privilege_type "
            "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
            "CROSS JOIN LATERAL aclexplode(a.attacl) acl "
            "WHERE acl.grantee='clinic_agent'::regrole"
        )
        assert cursor.fetchall() == []
        cursor.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname='clinic_app' "
            "AND c.relkind='S' "
            "AND (has_sequence_privilege('clinic_agent',c.oid,'USAGE') "
            "OR has_sequence_privilege('clinic_agent',c.oid,'SELECT') "
            "OR has_sequence_privilege('clinic_agent',c.oid,'UPDATE'))"
        )
        assert cursor.fetchall() == []
        cursor.execute(
            "SELECT acl.privilege_type FROM pg_default_acl d "
            "CROSS JOIN LATERAL aclexplode(d.defaclacl) acl "
            "WHERE acl.grantee='clinic_agent'::regrole"
        )
        assert cursor.fetchall() == []


def test_agent_definer_execute_surface_is_exact() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.proname, pg_get_function_identity_arguments(p.oid) "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='clinic_app' AND p.prosecdef "
            "AND has_function_privilege('clinic_agent',p.oid,'EXECUTE')"
        )
        assert set(cursor.fetchall()) == {
            ("principal_scope", "requested_principal uuid, requested_clinic uuid"),
            ("principal_has", "perm text, clinic uuid"),
        }


def test_agent_availability_policy_is_restrictive_and_exact() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT policyname, permissive, cmd, roles, qual, with_check "
            "FROM pg_policies WHERE schemaname='clinic_app' "
            "AND tablename='scheduling_availabilityblock' AND policyname='agent_grant'"
        )
        assert cursor.fetchone() == (
            "agent_grant",
            "RESTRICTIVE",
            "ALL",
            ["clinic_agent"],
            "principal_has('appointment.read'::text, clinic_id)",
            "principal_has('appointment.read'::text, clinic_id)",
        )
