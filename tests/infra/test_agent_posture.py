"""Todo 7 exact machine-role grants, not a subset of the staff surface."""

import pytest
from apps.tenancy import posture
from django.db import connection, transaction

from infra.retention_catalog import AGENT_SIGNATURES

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
    assert expected == {
        ("ai_actionexecution", "INSERT"),
        ("ai_actionexecution", "SELECT"),
        ("ai_actionexecution", "UPDATE"),
        ("ai_actionproposal", "SELECT"),
        ("ai_actionproposal", "UPDATE"),
        ("ai_actionreceipt", "INSERT"),
        ("ai_actionreceipt", "SELECT"),
        ("ai_agentpolicyversion", "SELECT"),
        ("ai_agentrun", "SELECT"),
        ("ai_agentrun", "UPDATE"),
        ("ai_approval", "SELECT"),
        ("ai_compensation", "INSERT"),
        ("ai_compensation", "SELECT"),
        ("ai_compensation", "UPDATE"),
        ("scheduling_availabilityblock", "SELECT"),
    }
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
            ("agent_action_payload", "requested uuid, kek text"),
            ("agent_approval_current", "requested uuid, approval_id uuid"),
            (
                "agent_audit",
                "record_id uuid, event_name text, "
                "occurred_at_utc timestamp with time zone, content_hash bytea",
            ),
            ("agent_audit_scope", "record_id uuid, event_name text"),
            ("agent_controls", "requested uuid"),
            ("agent_proposal_scope", "requested uuid"),
            ("agent_source", "requested uuid, source_id uuid"),
            *AGENT_SIGNATURES,
            # Task 80: validate_current re-checks the run's rollout stage on the
            # agent connection (apps/ai/action_validation.py).
            ("ai_rollout_agent_allowed", "subject uuid"),
            # run_agent reads both proposal and run as its registered machine
            # login. Their FORCE-RLS SELECT plans require this predicate.
            ("ai_pending_visible", "relation_name text, requested uuid"),
            ("principal_has", "perm text, clinic uuid"),
            ("principal_scope", "requested_principal uuid, requested_clinic uuid"),
        }


def test_pending_function_execute_is_limited_to_its_callers() -> None:
    # The resolver owns every routine. Its internal helpers have no external
    # caller; neither an omitted ACL (default PUBLIC) nor a grant option is OK.
    external = {
        "ai_pending_visible": ["clinic_agent", "clinic_app"],
        "ai_pending_choice": [],
        "ai_pending_subject": [],
        "ai_pending_refusal": ["clinic_app"],
        "ai_archive_current": ["clinic_app"],
        "interop_finalize_export": ["clinic_app"],
    }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT p.proname, p.proowner::regrole::text, p.prosecdef, p.proconfig,
                   ARRAY(SELECT coalesce(r.rolname, 'PUBLIC')::text
                         FROM aclexplode(
                           coalesce(p.proacl, acldefault('f',p.proowner))) a
                         LEFT JOIN pg_roles r ON r.oid=a.grantee
                         WHERE a.privilege_type='EXECUTE' AND a.grantee<>p.proowner
                         ORDER BY 1),
                   EXISTS(SELECT FROM aclexplode(p.proacl) a WHERE a.is_grantable)
            FROM pg_proc p WHERE p.pronamespace='clinic_app'::regnamespace
              AND p.proname=ANY(%s)
            """,
            [list(external)],
        )
        rows = cursor.fetchall()
    assert len(rows) == len(external)
    assert {r[0] for r in rows} == set(external)
    for name, owner, definer, path, grantees, grant_option in rows:
        assert (owner, definer, path, grant_option) == (
            "clinic_resolver",
            True,
            ["search_path=pg_catalog, clinic_app, pg_temp"],
            False,
        )
        assert grantees == external[name]


@pytest.mark.parametrize(
    "table", ["identity_serviceprincipal", "identity_serviceprincipalgrant"]
)
def test_principal_provision_policy_set_is_exact(table: str) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL search_path = pg_catalog")
        cursor.execute(
            "SELECT policyname, permissive, cmd, roles, qual, with_check "
            "FROM pg_catalog.pg_policies "
            "WHERE schemaname='clinic_app' AND tablename=%s ORDER BY policyname",
            [table],
        )
        policies = cursor.fetchall()
    tenant_predicate = (
        "(organization_id = (NULLIF(current_setting("
        "'app.current_tenant'::text, true), ''::text))::uuid)"
    )
    # Inspect every policy on each authority table: an additional permissive
    # policy must fail just as a widened owner predicate or role target does.
    assert policies == [
        (
            "principal_provision",
            "PERMISSIVE",
            "ALL",
            ["clinic_owner"],
            tenant_predicate,
            tenant_predicate,
        ),
    ]


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
