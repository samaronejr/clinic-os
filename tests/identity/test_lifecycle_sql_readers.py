"""Execute pending-account and self-account SQL reader contracts as clinic_app."""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from datetime import timedelta
from importlib import import_module
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.identity.invitations import enroll_invitation, invitation_token, invite_staff
from apps.identity.models import (
    Clinic,
    Organization,
    Passkey,
    RecoveryCode,
    User,
    UserClinicRole,
)
from apps.identity.recovery import issue_recovery_codes
from apps.identity.session_inventory import session_hash
from apps.identity.signup import submit_signup
from apps.tenancy.db import tenant_context
from apps.tenancy.envelope import protect
from django.contrib.auth.hashers import make_password
from django.db import DatabaseError, ProgrammingError, connection, transaction
from django.test import override_settings
from django.utils import timezone

from auth.passkey_test_support import passkey_login, registered_passkey
from ehr.test_sql_protocols import EHR_ACTOR_PROTOCOL_ORACLES
from identity.lifecycle_test_support import session_key, verified_client
from identity.permission_support import owner_context
from identity.test_onboarding import _capture
from otp_test_support import fixed_otp_time, runtime_role, token_for

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Sequence
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
GUC_NAMES = (
    "app.current_user_id",
    "app.current_tenant",
    "app.current_principal",
    "app.current_patient_session",
)
HISTORICAL_CALLABLES = {
    "clinic_app.identity_invitation_user(uuid,uuid)": "NULL,NULL",
    "clinic_app.identity_invitation_begin(bytea,bytea,text,text)": (
        "NULL,NULL,NULL,NULL"
    ),
    "clinic_app.identity_invitation_finish(bytea,text)": "NULL,NULL",
    "clinic_app.identity_offboarding_execute(uuid,uuid)": "NULL,NULL",
    "clinic_app.identity_retire_authenticators(uuid)": "NULL",
}
LIFECYCLE_TRIGGERS = {
    "clinic_app.identity_invitation_guard": {
        ("identity_staffinvitation", "identity_invitation_boundary"),
    },
    "clinic_app.identity_pending_authority_guard": {
        ("identity_userclinicrole", "pending_membership_guard"),
        ("identity_organization", "pending_organization_guard"),
    },
    "clinic_app.identity_recovery_guard": {
        ("identity_recoverycode", "identity_recoverycode_boundary"),
        ("identity_recoverybatch", "identity_recoverybatch_boundary"),
        ("identity_authenticatorretirement", "identity_retirement_boundary"),
    },
    "clinic_app.identity_reset_guard": {
        ("identity_authenticatorreset", "identity_reset_boundary"),
    },
    "clinic_app.identity_reset_notification_clock": {
        ("comms_integrationoperation", "identity_reset_delivered_clock"),
    },
    "clinic_app.identity_revoke_changed_sessions": {
        ("identity_user", "identity_password_session_rejection"),
        ("identity_userclinicrole", "identity_membership_session_rejection"),
        ("identity_rolegrant", "identity_rolegrant_session_rejection"),
    },
    "clinic_app.identity_session_change_guard": {
        ("identity_sessionrecord", "identity_session_change"),
    },
    "clinic_app.identity_signup_immutable": {
        ("identity_signupregistration", "signup_immutable"),
    },
}
_ACCOUNT_ORACLES = {
    "clinic_app.identity_passkey_session_verified(bytea,uuid,integer)": (
        "test_passkey_sql_gates_refuse_inactive_and_foreign_actors",
    ),
    "clinic_app.identity_passkey_registration_allowed(bytea)": (
        "test_passkey_sql_gates_refuse_inactive_and_foreign_actors",
    ),
    "clinic_app.identity_passkey_use(uuid,bigint)": (
        "test_passkey_sql_gates_refuse_inactive_and_foreign_actors",
    ),
    "clinic_app.identity_session_authenticator_verified(bytea)": (
        "test_passkey_sql_gates_refuse_inactive_and_foreign_actors",
    ),
    "clinic_app.identity_session_verify_passkey(bytea,uuid)": (
        "test_passkey_sql_gates_refuse_inactive_and_foreign_actors",
    ),
    "clinic_app.identity_invitation_accept(bytea,text)": (
        "test_pending_accept_rechecks_the_stored_sender",
    ),
    "clinic_app.identity_invitation_begin(bytea,bytea,text,text)": (
        "test_historical_lifecycle_callables_are_not_runtime_capabilities",
    ),
    "clinic_app.identity_invitation_claim(bytea)": (
        "test_pending_claim_and_password_require_their_exact_context",
    ),
    "clinic_app.identity_invitation_finish(bytea,text)": (
        "test_historical_lifecycle_callables_are_not_runtime_capabilities",
    ),
    "clinic_app.identity_invitation_password(bytea,bytea,text,text)": (
        "test_pending_claim_and_password_require_their_exact_context",
    ),
    "clinic_app.identity_recovery_consume(bytea)": (
        "test_account_gates_refuse_inactive_and_foreign_actors",
    ),
    "clinic_app.identity_session_register(uuid,bytea,text,text,"
    "timestamp with time zone)": (
        "test_account_gates_refuse_inactive_and_foreign_actors",
    ),
    "clinic_app.identity_session_verified(bytea)": (
        "test_account_gates_refuse_inactive_and_foreign_actors",
    ),
    "clinic_app.identity_session_verify(bytea,integer)": (
        "test_account_gates_refuse_inactive_and_foreign_actors",
    ),
    "clinic_app.identity_session_revoke_hash(bytea)": (
        "test_account_gates_refuse_inactive_and_foreign_actors",
    ),
    "clinic_app.identity_session_rotate_hash(bytea)": (
        "test_account_gates_refuse_inactive_and_foreign_actors",
    ),
    "clinic_app.identity_offboarding_clinics(uuid,uuid)": (
        "test_account_gates_refuse_inactive_and_foreign_actors",
    ),
}
ACTOR_PROTOCOL_ORACLES = {
    signature: tuple(f"identity.test_lifecycle_sql_readers.{name}" for name in names)
    for signature, names in _ACCOUNT_ORACLES.items()
} | {
    "clinic_app.agent_record_guard()": (
        "ai.test_agent_guards.test_agent_scope_and_approval_refusals_are_side_effect_free",
        "ai.test_agent_guards.test_fabricated_step_up_and_double_approval_cannot_add_votes",
        "ai.test_agent_execution.test_revoked_approver_blocks_after_approval",
        "ai.test_agent_execution.test_superseded_source_publication_blocks_execution",
        "ai.test_agent_guards.test_native_agent_decisions_use_the_displayed_digest_and_preserve_refusals",
    ),
    "clinic_app.agent_snapshot(uuid,text)": (
        "identity.test_sql_inactive_actor.test_sql_clause_refuses_inactive_actor",
    ),
    "clinic_app.has_permission(text,uuid,uuid)": (
        "identity.test_sql_inactive_actor.test_sql_clause_refuses_inactive_actor",
    ),
    "clinic_app.scribe_assigned(uuid)": (
        "scribe.test_actor_readers.test_native_capture_authority_refuses_inactive_actor",
    ),
    "clinic_app.scribe_guard()": (
        "scribe.test_actor_readers.test_native_capture_authority_refuses_inactive_actor",
        "scribe.test_refusals.test_role_loss_refuses_every_action_without_session_or_domain_mutation",
    ),
    "clinic_app.reception_readiness(uuid,uuid)": (
        "identity.test_reception_readiness_actor.test_readiness_sql_refuses_inactive_existing_actor",
    ),
    # Admission delegates to ai_staff; actor binding also permits retained,
    # content-free cancellation bookkeeping after the actor becomes inactive.
    "clinic_app.ai_ledger_guard()": (
        "ai.test_posture."
        "test_ledger_guard_refuses_inactive_admission_with_raw_actor_context",
        "ai.test_cancellation."
        "test_revoked_actor_cancellation_releases_the_unspent_reservation",
    ),
    "clinic_app.identity_signup_register(uuid,uuid,uuid,uuid,jsonb,text,bytea,text)": (
        "identity.test_onboarding."
        "test_signup_is_protected_pending_and_confers_no_runtime_membership",
        "identity.test_onboarding.test_signup_refuses_ambient_identity_or_tenant_authority",
    ),
    "clinic_app.identity_reset_guard()": (
        "identity.test_sql_inactive_actor.test_delegating_gates_refuse_inactive_actor",
    ),
}
SQL_PROTOCOL_ORACLES = (
    {
        signature.split("(", 1)[0]: names
        for signature, names in ACTOR_PROTOCOL_ORACLES.items()
        if signature
        not in {
            "clinic_app.identity_offboarding_clinics(uuid,uuid)",
            "clinic_app.identity_reset_guard()",
            # Sealed staff/domain oracles already classify these SQL guards;
            # actor protocols add independent inactive-caller proof.
            "clinic_app.agent_record_guard()",
            "clinic_app.agent_snapshot(uuid,text)",
            "clinic_app.has_permission(text,uuid,uuid)",
            "clinic_app.scribe_assigned(uuid)",
            "clinic_app.scribe_guard()",
            # A sealed staff-role oracle already classifies this SQL guard;
            # the actor protocol above adds independent inactive-caller proof.
            "clinic_app.reception_readiness(uuid,uuid)",
        }
    }
    | {
        **dict.fromkeys(
            (
                "clinic_app.identity_passkey_guard",
                "clinic_app.identity_passkey_lookup",
                "clinic_app.identity_passkey_policy_set",
                "clinic_app.identity_passkey_revoke_sessions",
            ),
            (
                "identity.passkey_authority_oracle."
                "test_actual_passkey_authority_for_every_staff_role",
            ),
        ),
        "clinic_app.identity_invitation_user": (
            "identity.test_lifecycle_sql_readers."
            "test_historical_lifecycle_callables_are_not_runtime_capabilities",
        ),
        "clinic_app.identity_offboarding_execute": (
            "identity.test_lifecycle_sql_readers."
            "test_historical_lifecycle_callables_are_not_runtime_capabilities",
        ),
        "clinic_app.identity_retire_authenticators": (
            "identity.test_lifecycle_sql_readers."
            "test_historical_lifecycle_callables_are_not_runtime_capabilities",
            "auth.test_recovery_codes.test_recovery_codes_are_once_shown_hash_only_and_one_use",
        ),
        "clinic_app.identity_session_active": (
            "identity.test_lifecycle_sql_readers."
            "test_account_gates_refuse_inactive_and_foreign_actors",
        ),
        "clinic_app.identity_session_revoked_cookie": (
            "identity.test_lifecycle_sql_readers."
            "test_account_gates_refuse_inactive_and_foreign_actors",
        ),
        "clinic_app.identity_session_step_up": (
            "identity.test_lifecycle_sql_readers."
            "test_account_gates_refuse_inactive_and_foreign_actors",
        ),
    }
    | dict.fromkeys(
        LIFECYCLE_TRIGGERS,
        (
            "identity.test_lifecycle_sql_readers."
            "test_installed_lifecycle_triggers_are_attached_and_enabled",
        ),
    )
    | {
        "clinic_app.ai_ledger_guard": (
            *ACTOR_PROTOCOL_ORACLES["clinic_app.ai_ledger_guard()"],
            "ai.test_posture."
            "test_raw_invocation_cannot_forge_a_cheaper_budget_reservation",
            "ai.test_gateway."
            "test_fake_call_is_metered_once_and_all_content_stays_encrypted",
            "ai.test_posture.test_closed_circuit_can_reopen_without_losing_history",
        ),
        "clinic_app.identity_pending_authority_guard": (
            "identity.test_lifecycle_sql_readers."
            "test_installed_lifecycle_triggers_are_attached_and_enabled",
            "identity.test_lifecycle_sql_readers."
            "test_pending_authority_and_capture_history_refuse_writes",
        ),
        "clinic_app.identity_signup_immutable": (
            "identity.test_lifecycle_sql_readers."
            "test_installed_lifecycle_triggers_are_attached_and_enabled",
            "identity.test_lifecycle_sql_readers."
            "test_pending_authority_and_capture_history_refuse_writes",
        ),
    }
)


ACTOR_PROTOCOL_ORACLES.update(EHR_ACTOR_PROTOCOL_ORACLES)


def assert_lifecycle_oracles(oracles: Iterable[Sequence[str]]) -> None:
    for names in oracles:
        assert names
        for name in names:
            module, function = name.rsplit(".", 1)
            assert callable(getattr(import_module(module), function)), name


@contextmanager
def _context(
    *, actor: UUID | None = None, tenant: UUID | None = None
) -> Generator[None, None, None]:
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        for setting, value in zip(GUC_NAMES, (actor, tenant, None, None), strict=True):
            cursor.execute(
                "SELECT set_config(%s,%s,true)",
                [setting, str(value) if value is not None else ""],
            )
        yield


def test_historical_lifecycle_callables_are_not_runtime_capabilities() -> None:
    with runtime_role(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user, rolsuper, rolbypassrls, rolcreatedb "
            "FROM pg_roles WHERE rolname=current_user"
        )
        assert cursor.fetchone() == ("clinic_app", False, False, False)
        for signature, arguments in HISTORICAL_CALLABLES.items():
            cursor.execute(
                "SELECT has_function_privilege(current_user,%s,'EXECUTE')",
                [signature],
            )
            assert cursor.fetchone() == (False,), signature
            name = signature.split("(", 1)[0]
            with pytest.raises(ProgrammingError) as failure, transaction.atomic():
                cursor.execute(f"SELECT {name}({arguments})")
            assert getattr(failure.value.__cause__, "sqlstate", None) == "42501"


def test_installed_lifecycle_triggers_are_attached_and_enabled() -> None:
    with connection.cursor() as cursor:
        for function, attachments in LIFECYCLE_TRIGGERS.items():
            cursor.execute(
                "SELECT c.relname,t.tgname,t.tgenabled "
                "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                "WHERE t.tgfoid=to_regprocedure(%s) AND NOT t.tgisinternal",
                [function + "()"],
            )
            assert set(cursor.fetchall()) == {
                (table, trigger, "O") for table, trigger in attachments
            }, function


def test_pending_authority_and_capture_history_refuse_writes(
    rbac_graph: RbacGraph,
) -> None:
    with _context():
        receipt = submit_signup(capture=_capture(), idempotency_key=uuid4())
    with owner_context(receipt.organization_id):
        clinic = Clinic.objects.create(
            organization_id=receipt.organization_id,
            name="Sintetico Pendente",
            timezone="America/Sao_Paulo",
        )
        with pytest.raises(DatabaseError) as failure, transaction.atomic():
            UserClinicRole.objects.create(
                organization_id=receipt.organization_id,
                clinic=clinic,
                user_id=receipt.first_admin_id,
                role=UserClinicRole.Role.ORG_ADMIN,
            )
        cause = failure.value.__cause__
        assert isinstance(cause, psycopg.Error)
        assert cause.sqlstate == "23514"
        assert cause.diag.context is not None
        assert "identity_pending_authority_guard" in cause.diag.context
        with (
            pytest.raises(DatabaseError) as history,
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE clinic_app.identity_signupregistration "
                "SET idempotency_key=%s WHERE organization_id=%s",
                [uuid4(), receipt.organization_id],
            )
        cause = history.value.__cause__
        assert isinstance(cause, psycopg.Error)
        assert cause.sqlstate == "23514"
        assert cause.diag.context is not None
        assert "identity_signup_immutable" in cause.diag.context
    with owner_context(rbac_graph.organization_a):
        with (
            pytest.raises(DatabaseError) as downgrade,
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "UPDATE clinic_app.identity_organization "
                "SET status='pending_activation' WHERE id=%s",
                [rbac_graph.organization_a],
            )
        cause = downgrade.value.__cause__
        assert isinstance(cause, psycopg.Error)
        assert cause.sqlstate == "23514"
        assert cause.diag.context is not None
        assert "identity_pending_authority_guard" in cause.diag.context


def test_pending_claim_and_password_require_their_exact_context(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    username = f"synthetic-pending-{uuid4().hex}"
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        invitation = invite_staff(
            clinic_id=graph.clinic_b,
            email="synthetic-sql-pending@example.invalid",
            bundle=UserClinicRole.Role.RECEPTIONIST,
        )
        envelope = protect(
            purpose="identity.staff.invitation.username",
            plaintext=username.encode(),
        )
    digest = bytes(invitation.token_hash)
    with _context(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM clinic_app.identity_invitation_claim(%s)", [digest]
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[:4] == (
            invitation.pk,
            graph.organization_a,
            graph.clinic_b,
            invitation.pending_user_id,
        )
        for setting in GUC_NAMES:
            with transaction.atomic():
                cursor.execute("SELECT set_config(%s,%s,true)", [setting, str(uuid4())])
                cursor.execute(
                    "SELECT * FROM clinic_app.identity_invitation_claim(%s)", [digest]
                )
                assert cursor.fetchall() == [], setting
                transaction.set_rollback(True)
    for actor, tenant, allowed in (
        (graph.shared_user, graph.organization_a, False),
        (invitation.pending_user_id, graph.organization_b, False),
        (invitation.pending_user_id, graph.organization_a, True),
    ):
        with _context(actor=actor, tenant=tenant), connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.identity_invitation_password(%s,%s,%s,%s)",
                [
                    digest,
                    envelope,
                    make_password(secrets.token_urlsafe(24)),
                    secrets.token_hex(20),
                ],
            )
            result = cursor.fetchone()
            assert result is not None
            if allowed:
                assert type(result[0]) is int
            else:
                assert result == (None,)
            transaction.set_rollback(True)
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT is_active FROM clinic_app.identity_user WHERE id=%s",
            [invitation.pending_user_id],
        )
        assert cursor.fetchone() == (False,)
        assert not UserClinicRole.objects.filter(
            user_id=invitation.pending_user_id
        ).exists()


def test_pending_accept_rechecks_the_stored_sender(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    username = f"synthetic-accept-{uuid4().hex}"
    password = secrets.token_urlsafe(24) + "1!Aa"
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        invitation = invite_staff(
            clinic_id=graph.clinic_b,
            email="synthetic-sql-accept@example.invalid",
            bundle=UserClinicRole.Role.RECEPTIONIST,
        )
    with _context():
        enrollment = enroll_invitation(
            token=invitation_token(invitation), username=username, password=password
        )
    with (
        _context(actor=invitation.pending_user_id, tenant=graph.organization_a),
        fixed_otp_time(),
    ):
        assert enrollment.device.verify_token(token_for(enrollment.device))
        enrollment.device.confirmed = True
        enrollment.device.save(update_fields=("confirmed",))
    for active in (True, False, True):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE clinic_app.identity_user SET is_active=%s WHERE id=%s",
                [active, invitation.invited_by_id],
            )
            with _context(
                actor=invitation.pending_user_id, tenant=graph.organization_a
            ):
                cursor.execute(
                    "SELECT clinic_app.identity_invitation_accept(%s,%s)",
                    [bytes(invitation.token_hash), username],
                )
                assert cursor.fetchone() == (
                    (invitation.pending_user_id,) if active else (None,)
                )
            transaction.set_rollback(True)


def test_account_gates_refuse_inactive_and_foreign_actors(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    client = verified_client(graph)
    digest = session_hash(session_key(client))
    target = User.objects.create(username=f"synthetic-offboarding-{uuid4().hex}")
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            user=target,
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_b,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        issued = issue_recovery_codes(
            clinic_id=graph.clinic_b, session_key=session_key(client)
        )
        record = RecoveryCode.objects.filter(batch_id=issued.batch_id).first()
        assert record is not None
    code_digest = bytes(record.code_hash)
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT verification_device_id FROM clinic_app.identity_sessionrecord "
            "WHERE session_key_hash=%s",
            [digest],
        )
        (device_id,) = cursor.fetchone()
        assert type(device_id) is int
    for actor, active in (
        (graph.clinic_admin, True),
        (graph.clinic_admin, False),
        (graph.shared_user, True),
    ):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE clinic_app.identity_user SET is_active=%s WHERE id=%s",
                [active, graph.clinic_admin],
            )
            allowed = actor == graph.clinic_admin and active
            with _context(actor=actor, tenant=graph.organization_a):
                cursor.execute(
                    "SELECT clinic_app.identity_session_register(%s,%s,%s,%s,%s)",
                    [
                        graph.clinic_admin,
                        secrets.token_bytes(32),
                        "192.0.2.0/24",
                        "other",
                        timezone.now() + timedelta(hours=1),
                    ],
                )
                row = cursor.fetchone()
                assert row is not None
                if allowed:
                    assert row[0] is not None
                else:
                    assert row == (None,)
                cursor.execute(
                    "SELECT clinic_app.identity_session_verified(%s)", [digest]
                )
                assert cursor.fetchone() == (allowed,)
                cursor.execute(
                    "SELECT clinic_app.identity_session_active(%s,%s), "
                    "clinic_app.identity_session_active(%s,%s), "
                    "clinic_app.identity_session_revoked_cookie(%s), "
                    "clinic_app.identity_session_step_up(%s,%s)",
                    [
                        graph.clinic_admin,
                        digest,
                        graph.shared_user,
                        digest,
                        digest,
                        digest,
                        graph.clinic_b,
                    ],
                )
                # Cookie projections identify the stored digest's account;
                # verified/step-up authority still requires this bound actor.
                assert cursor.fetchone() == (active, False, not active, allowed)
                cursor.execute(
                    "SELECT clinic_app.identity_offboarding_clinics(%s,%s)",
                    [target.pk, graph.clinic_b],
                )
                assert cursor.fetchone() == (
                    ([graph.clinic_b],) if allowed else (None,)
                )
                queries: tuple[tuple[str, tuple[bytes | int, ...]], ...] = (
                    ("identity_session_verify(%s,%s)", (digest, device_id)),
                    ("identity_recovery_consume(%s)", (code_digest,)),
                    ("identity_session_revoke_hash(%s)", (digest,)),
                    ("identity_session_rotate_hash(%s)", (digest,)),
                )
                for statement, parameters in queries:
                    with transaction.atomic():
                        cursor.execute("SELECT clinic_app." + statement, parameters)
                        assert cursor.fetchone() == (allowed,), statement
                        transaction.set_rollback(True)
            transaction.set_rollback(True)


@override_settings(WEBAUTHN_RP_ID="testserver", WEBAUTHN_ORIGIN="http://testserver")
def test_passkey_sql_gates_refuse_inactive_and_foreign_actors(
    rbac_graph: RbacGraph,
) -> None:
    # Given a genuinely registered credential and a signed, verified session.
    graph = rbac_graph
    registered = registered_passkey(graph)
    signed = passkey_login(registered)
    original_hash = session_hash(session_key(signed))
    with owner_context(graph.organization_a):
        Organization.objects.filter(pk=graph.organization_a).update(
            passkey_step_up_enabled=True
        )
        credential = Passkey.objects.get(user_id=graph.clinic_admin)
    decisions: dict[str, list[bool]] = {}
    for actor, active in (
        (graph.clinic_admin, True),
        (graph.clinic_admin, False),
        (graph.clinic_admin, True),
        (graph.shared_user, True),
    ):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE clinic_app.identity_user SET is_active=%s WHERE id=%s",
                [active, graph.clinic_admin],
            )
            # Deactivation revokes existing sessions. Copy the real verified
            # subject AFTER that trigger so a revoked row cannot mask the gate.
            digest = secrets.token_bytes(32)
            cursor.execute(
                "INSERT INTO clinic_app.identity_sessionrecord "
                "(id,user_id,session_key_hash,created_at,last_seen,ip_network,"
                "user_agent_family,expires_at,revoked_at,verification_device_id,"
                "verification_passkey_id,verified_at) "
                "SELECT gen_random_uuid(),user_id,%s,created_at,last_seen,ip_network,"
                "user_agent_family,expires_at,NULL,verification_device_id,"
                "verification_passkey_id,verified_at "
                "FROM clinic_app.identity_sessionrecord WHERE session_key_hash=%s "
                "RETURNING revoked_at,verification_device_id,verification_passkey_id",
                [digest, original_hash],
            )
            assert cursor.fetchone() == (None, None, credential.pk)
            queries: tuple[tuple[str, tuple[bytes | UUID | int | None, ...]], ...] = (
                (
                    "identity_passkey_session_verified(%s,%s,%s)",
                    (digest, credential.pk, None),
                ),
                ("identity_session_authenticator_verified(%s)", (digest,)),
                ("identity_passkey_registration_allowed(%s)", (digest,)),
                (
                    "identity_passkey_use(%s,%s)",
                    (credential.pk, credential.sign_count + 1),
                ),
                ("identity_session_verify_passkey(%s,%s)", (digest, credential.pk)),
            )
            # When clinic_app binds the GUCs directly, no Python active-user
            # gate or tenant_context can refuse before the SQL under test.
            with _context(actor=actor, tenant=graph.organization_a):
                for statement, parameters in queries:
                    with transaction.atomic():
                        cursor.execute("SELECT clinic_app." + statement, parameters)
                        result = cursor.fetchone()
                        assert result is not None, statement
                        (decision,) = result
                        assert type(decision) is bool, statement
                        name = "clinic_app." + statement.split("(", 1)[0]
                        decisions.setdefault(name, []).append(decision)
                        transaction.set_rollback(True)
            transaction.set_rollback(True)
    # Then active controls succeed; only inactive/foreign actors are refused.
    oracle = (
        "identity.test_lifecycle_sql_readers."
        "test_passkey_sql_gates_refuse_inactive_and_foreign_actors"
    )
    expected = {
        signature.split("(", 1)[0]: (True, False, True, False)
        for signature, oracles in ACTOR_PROTOCOL_ORACLES.items()
        if oracle in oracles
    }
    assert expected
    assert {name: tuple(values) for name, values in decisions.items()} == expected
