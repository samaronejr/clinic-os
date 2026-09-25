"""Todo 7: real-login machine authority, never a forged staff actor."""

from __future__ import annotations

import os
import secrets
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.core.integration import (
    IntegrationContextError,
    OperationRequest,
    enqueue_operation,
)
from apps.ehr.services import publish_template
from apps.identity.current_context import CurrentActorError
from apps.identity.models import ServicePrincipal, ServicePrincipalGrant, UserClinicRole
from apps.scheduling.models import AvailabilityBlock
from apps.tenancy.db import (
    ServicePrincipalAccessDeniedError,
    TenantAccessDeniedError,
    TenantTransactionNestingError,
    clear_connection_tenant_gucs,
    service_principal_context,
    tenant_context,
)
from django.db import (
    DatabaseError,
    IntegrityError,
    connections,
    transaction,
)
from psycopg import sql

from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from pytest_django.plugin import DjangoDbBlocker

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True, databases={"default", "agent"})


@pytest.fixture(scope="session", autouse=True)
def agent_login(
    django_db_setup: None, django_db_blocker: DjangoDbBlocker
) -> Iterator[None]:
    """Use a real isolated agent login even when the optional runtime alias is off."""
    secret = secrets.token_urlsafe(32)
    with psycopg.connect(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], autocommit=True
    ) as admin:
        old = admin.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname='clinic_agent'"
        ).fetchone()
        assert old is not None
        admin.execute(
            sql.SQL("ALTER ROLE clinic_agent PASSWORD {}").format(sql.Literal(secret))
        )
        with django_db_blocker.unblock():
            agent = connections["agent"]
            agent.close()
            agent.settings_dict["PASSWORD"] = secret
        try:
            yield
        finally:
            with django_db_blocker.unblock():
                connections["agent"].close()
            admin.execute(
                sql.SQL("ALTER ROLE clinic_agent PASSWORD {}").format(
                    sql.Literal(old[0])
                )
            )


@pytest.fixture
def principal(rbac_graph: RbacGraph) -> ServicePrincipal:
    with owner_context(rbac_graph.organization_a):
        result = ServicePrincipal.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            name="sintetico-availability",
            db_identity="clinic_agent",
            purpose="availability",
        )
        ServicePrincipalGrant.objects.create(
            organization_id=rbac_graph.organization_a,
            principal=result,
            permission="appointment.read",
            subject_scope="clinic",
        )
    return result


def _seed_availability(graph: RbacGraph) -> set[UUID]:
    start = datetime(2031, 1, 1, 12, tzinfo=UTC)
    result = set()
    for offset, (org, clinic) in enumerate(
        (
            (graph.organization_a, graph.clinic_a),
            (graph.organization_a, graph.clinic_b),
            (graph.organization_b, graph.clinic_c),
        )
    ):
        with owner_context(org):
            block = AvailabilityBlock.objects.create(
                organization_id=org,
                clinic_id=clinic,
                practitioner_id=graph.physician
                if org == graph.organization_a
                else graph.shared_user,
                start_at=start + timedelta(days=offset),
                end_at=start + timedelta(days=offset, hours=1),
                idempotency_key=uuid4(),
                create_fingerprint=b"s" * 32,
            )
            if clinic == graph.clinic_a:
                result.add(block.pk)
    return result


def _visible() -> set[UUID]:
    return set(AvailabilityBlock.objects.using("agent").values_list("pk", flat=True))


def test_real_login_reads_only_exact_granted_clinic(
    principal: ServicePrincipal, rbac_graph: RbacGraph
) -> None:
    expected = _seed_availability(rbac_graph)
    with service_principal_context(
        principal_id=principal.pk, clinic_id=principal.clinic_id
    ):
        with connections["agent"].cursor() as cursor:
            cursor.execute(
                "SELECT session_user, current_user, "
                "NULLIF(current_setting('app.current_user_id', true), '')"
            )
            assert cursor.fetchone() == ("clinic_agent", "clinic_agent", None)
        assert _visible() == expected
        assert (
            not AvailabilityBlock.objects.using("agent")
            .filter(clinic_id=rbac_graph.clinic_b)
            .exists()
        )
        assert (
            not AvailabilityBlock.objects.using("agent")
            .filter(clinic_id=rbac_graph.clinic_c)
            .exists()
        )
    assert _visible() == set()


@pytest.mark.parametrize("selector", ["unknown", "same_org", "foreign_org"])
def test_wrong_principal_or_clinic_is_indistinguishable(
    principal: ServicePrincipal, rbac_graph: RbacGraph, selector: str
) -> None:
    principal_id = uuid4() if selector == "unknown" else principal.pk
    clinic = {
        "unknown": principal.clinic_id,
        "same_org": rbac_graph.clinic_b,
        "foreign_org": rbac_graph.clinic_c,
    }[selector]
    with (
        pytest.raises(
            ServicePrincipalAccessDeniedError, match="service principal access denied"
        ),
        service_principal_context(principal_id=principal_id, clinic_id=clinic),
    ):
        pytest.fail("unauthorized context entered")


@pytest.mark.parametrize("target", ["principal", "grant"])
def test_revocation_is_visible_inside_existing_context(
    principal: ServicePrincipal, rbac_graph: RbacGraph, target: str
) -> None:
    expected = _seed_availability(rbac_graph)
    with service_principal_context(
        principal_id=principal.pk, clinic_id=principal.clinic_id
    ):
        assert _visible() == expected
        # A distinct owner connection commits while the agent transaction is open.
        with owner_context(principal.organization_id):
            if target == "principal":
                ServicePrincipal.objects.filter(pk=principal.pk).update(active=False)
            else:
                ServicePrincipalGrant.objects.filter(principal=principal).update(
                    active=False
                )
        assert _visible() == set()
    with (
        pytest.raises(ServicePrincipalAccessDeniedError),
        service_principal_context(
            principal_id=principal.pk, clinic_id=principal.clinic_id
        ),
    ):
        pytest.fail("revoked context entered")


def test_principal_context_refuses_nesting(principal: ServicePrincipal) -> None:
    with (
        transaction.atomic(),
        pytest.raises(TenantTransactionNestingError),
        service_principal_context(
            principal_id=principal.pk, clinic_id=principal.clinic_id
        ),
    ):
        pytest.fail("nested staff transaction")
    with (
        service_principal_context(
            principal_id=principal.pk, clinic_id=principal.clinic_id
        ),
        pytest.raises(TenantTransactionNestingError),
        service_principal_context(
            principal_id=principal.pk, clinic_id=principal.clinic_id
        ),
    ):
        pytest.fail("nested agent transaction")


@pytest.mark.parametrize("guc", ["app.current_user_id", "app.current_patient_session"])
def test_forged_human_context_never_authorizes_machine_reads(
    principal: ServicePrincipal, rbac_graph: RbacGraph, guc: str
) -> None:
    _seed_availability(rbac_graph)
    with service_principal_context(
        principal_id=principal.pk, clinic_id=principal.clinic_id
    ):
        with connections["agent"].cursor() as cursor:
            cursor.execute(
                "SELECT set_config(%s,%s,true)", [guc, str(rbac_graph.physician)]
            )
        assert _visible() == set()


@contextmanager
def _agent_as_default() -> Iterator[None]:
    original = connections["default"]
    connections["default"] = connections["agent"]
    try:
        yield
    finally:
        connections["default"] = original


def test_forged_physician_cannot_call_ehr_service(
    principal: ServicePrincipal, rbac_graph: RbacGraph
) -> None:
    # A physician-owner really can publish this template as a human. The same
    # UUID on the machine login must fail before any EHR read/write or audit.
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=principal.clinic_id,
            user_id=rbac_graph.physician,
            role="owner",
        )
    prompts: dict[str, str] = dict.fromkeys(
        ("subjective", "objective", "assessment", "plan"), "Sintetico"
    )
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        assert (
            publish_template(
                clinic_id=principal.clinic_id,
                key="synthetic-human",
                title="Sintetico",
                prompts=prompts,
            ).key
            == "synthetic-human"
        )
    with service_principal_context(
        principal_id=principal.pk, clinic_id=principal.clinic_id
    ):
        with connections["agent"].cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true)",
                [str(rbac_graph.physician)],
            )
        with _agent_as_default(), pytest.raises(CurrentActorError):
            publish_template(
                clinic_id=principal.clinic_id,
                key="synthetic",
                title="Sintetico",
                prompts=prompts,
            )


@pytest.mark.parametrize(
    "table",
    [
        "audit_event",
        "tenancy_tenantdatakey",
        "identity_user",
        "identity_serviceprincipal",
        "ehr_encounter",
    ],
)
def test_agent_cannot_read_ungranted_sensitive_tables(
    principal: ServicePrincipal, table: str
) -> None:
    with service_principal_context(
        principal_id=principal.pk, clinic_id=principal.clinic_id
    ):
        with (
            pytest.raises(DatabaseError) as error,
            transaction.atomic(using="agent"),
            connections["agent"].cursor() as cursor,
        ):
            cursor.execute(
                sql.SQL("SELECT * FROM clinic_app.{}").format(sql.Identifier(table))
            )
        assert isinstance(error.value.__cause__, psycopg.errors.InsufficientPrivilege)


@pytest.mark.parametrize(
    "permission",
    [
        "clinical.finalize",
        "prescription.sign",
        "refund.approve",
        "*",
        "appointment.read; DROP TABLE x",
    ],
)
def test_machine_grants_reject_unsafe_permissions(
    principal: ServicePrincipal, permission: str
) -> None:
    with (
        owner_context(principal.organization_id),
        pytest.raises(IntegrityError),
        transaction.atomic(),
    ):
        ServicePrincipalGrant.objects.create(
            organization_id=principal.organization_id,
            principal=principal,
            permission=permission,
            subject_scope="clinic",
        )


def test_login_binding_is_unique_and_history_cannot_be_rewritten(
    principal: ServicePrincipal,
) -> None:
    with owner_context(principal.organization_id):
        with pytest.raises(IntegrityError), transaction.atomic():
            ServicePrincipal.objects.create(
                organization_id=principal.organization_id,
                clinic_id=principal.clinic_id,
                name="sintetico-other",
                db_identity="clinic_agent",
                purpose="availability",
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            ServicePrincipal.objects.filter(pk=principal.pk).update(purpose="other")
        with pytest.raises(IntegrityError), transaction.atomic():
            ServicePrincipal.objects.filter(pk=principal.pk).delete()
        ServicePrincipal.objects.filter(pk=principal.pk).update(active=False)
        with pytest.raises(IntegrityError), transaction.atomic():
            ServicePrincipal.objects.filter(pk=principal.pk).update(active=True)


def test_agent_cannot_switch_to_staff_or_resolver(principal: ServicePrincipal) -> None:
    with service_principal_context(
        principal_id=principal.pk, clinic_id=principal.clinic_id
    ):
        for role in ("clinic_app", "clinic_owner", "clinic_resolver"):
            with (
                pytest.raises(DatabaseError),
                transaction.atomic(using="agent"),
                connections["agent"].cursor() as cursor,
            ):
                cursor.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))


def test_caller_cannot_select_another_registered_login(
    principal: ServicePrincipal,
) -> None:
    with owner_context(principal.organization_id):
        other = ServicePrincipal.objects.create(
            organization_id=principal.organization_id,
            clinic_id=principal.clinic_id,
            name="sintetico-other",
            db_identity="clinic_agent_other",
            purpose="availability",
        )
        ServicePrincipalGrant.objects.create(
            organization_id=principal.organization_id,
            principal=other,
            permission="appointment.read",
            subject_scope="clinic",
        )
    with (
        pytest.raises(ServicePrincipalAccessDeniedError),
        service_principal_context(principal_id=other.pk, clinic_id=other.clinic_id),
    ):
        pytest.fail("a different login's identity was accepted")


def test_raw_guc_forgery_cannot_replace_a_clinic_grant(
    principal: ServicePrincipal,
    rbac_graph: RbacGraph,
) -> None:
    _seed_availability(rbac_graph)
    with service_principal_context(
        principal_id=principal.pk, clinic_id=principal.clinic_id
    ):
        with connections["agent"].cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true)",
                [str(rbac_graph.organization_b)],
            )
        assert _visible() == set()


@pytest.mark.parametrize("subject_scope", ["*", "organization", "patient", ""])
def test_unsupported_subject_scope_is_not_a_wildcard(
    principal: ServicePrincipal,
    subject_scope: str,
) -> None:
    with (
        owner_context(principal.organization_id),
        pytest.raises(IntegrityError),
        transaction.atomic(),
    ):
        ServicePrincipalGrant.objects.create(
            organization_id=principal.organization_id,
            principal=principal,
            permission="appointment.read",
            subject_scope=subject_scope,
        )


def test_scope_foreign_keys_cannot_cross_organizations(
    principal: ServicePrincipal,
    rbac_graph: RbacGraph,
) -> None:
    with owner_context(rbac_graph.organization_b):
        with pytest.raises(IntegrityError) as grant_error, transaction.atomic():
            ServicePrincipalGrant.objects.create(
                organization_id=rbac_graph.organization_b,
                principal=principal,
                permission="appointment.read",
                subject_scope="clinic",
                active=False,
            )
        assert isinstance(
            grant_error.value.__cause__, psycopg.errors.ForeignKeyViolation
        )
        assert (
            grant_error.value.__cause__.diag.constraint_name
            == "identity_principal_grant_org_fk"
        )
        with pytest.raises(IntegrityError) as principal_error, transaction.atomic():
            ServicePrincipal.objects.create(
                organization_id=rbac_graph.organization_b,
                clinic_id=rbac_graph.clinic_a,
                name="sintetico-cross",
                db_identity="clinic_agent_cross",
                purpose="availability",
            )
        assert isinstance(
            principal_error.value.__cause__, psycopg.errors.ForeignKeyViolation
        )
        assert (
            principal_error.value.__cause__.diag.constraint_name
            == "identity_principal_clinic_fk"
        )


def test_principal_helper_fails_closed_on_malformed_or_ungranted_input(
    principal: ServicePrincipal,
) -> None:
    with (
        service_principal_context(
            principal_id=principal.pk, clinic_id=principal.clinic_id
        ),
        connections["agent"].cursor() as cursor,
    ):
        for permission in (
            "clinical.finalize",
            "*",
            "SINTETICO-SENTINELA-INJECTION",
        ):
            cursor.execute(
                "SELECT clinic_app.principal_has(%s,%s)",
                [permission, principal.clinic_id],
            )
            assert cursor.fetchone() == (False,)
        cursor.execute("SELECT set_config('app.current_principal','malformed',true)")
        cursor.execute(
            "SELECT clinic_app.principal_has('appointment.read',%s)",
            [principal.clinic_id],
        )
        assert cursor.fetchone() == (False,)


def test_poisoned_session_and_exception_exit_clear_all_machine_context(
    principal: ServicePrincipal,
    rbac_graph: RbacGraph,
) -> None:
    with connections["agent"].cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_user_id',%s,false), "
            "set_config('app.current_principal',%s,false), "
            "set_config('app.current_tenant',%s,false), "
            "set_config('app.current_patient_session',%s,false)",
            [
                str(rbac_graph.physician),
                str(uuid4()),
                str(rbac_graph.organization_b),
                str(uuid4()),
            ],
        )
    with (
        pytest.raises(ServicePrincipalAccessDeniedError),
        service_principal_context(
            principal_id=principal.pk, clinic_id=principal.clinic_id
        ),
    ):
        pytest.fail("mixed human context was accepted")
    message = "synthetic rollback"
    with (
        pytest.raises(RuntimeError, match=message),
        service_principal_context(
            principal_id=principal.pk, clinic_id=principal.clinic_id
        ),
    ):
        raise RuntimeError(message)
    with connections["agent"].cursor() as cursor:
        cursor.execute(
            "SELECT NULLIF(current_setting('app.current_principal',true),''), "
            "NULLIF(current_setting('app.current_tenant',true),''), "
            "NULLIF(current_setting('app.current_user_id',true),''), "
            "NULLIF(current_setting('app.current_patient_session',true),'')"
        )
        assert cursor.fetchone() == (None, None, None, None)


def test_machine_context_cannot_borrow_stale_staff_outbox_authority(
    principal: ServicePrincipal,
    rbac_graph: RbacGraph,
) -> None:
    with runtime_role():
        with connections["default"].cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,false), "
                "set_config('app.current_tenant',%s,false)",
                [str(rbac_graph.physician), str(rbac_graph.organization_a)],
            )
        try:
            with (
                service_principal_context(
                    principal_id=principal.pk, clinic_id=principal.clinic_id
                ),
                pytest.raises(IntegrationContextError),
            ):
                enqueue_operation(
                    OperationRequest(
                        channel="email",
                        provider="synthetic",
                        clinic_id=principal.clinic_id,
                        subject_type="synthetic.action",
                        subject_id=uuid4(),
                        idempotency_key=uuid4(),
                    )
                )
        finally:
            clear_connection_tenant_gucs()


def test_machine_login_cannot_install_staff_context(
    principal: ServicePrincipal,
    rbac_graph: RbacGraph,
) -> None:
    with (
        _agent_as_default(),
        pytest.raises(TenantAccessDeniedError),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        pytest.fail("machine login installed a staff context")
    with connections["agent"].cursor() as cursor:
        cursor.execute("SELECT NULLIF(current_setting('app.current_user_id',true),'')")
        assert cursor.fetchone() == (None,)


def test_repeatable_read_cannot_hide_committed_revocation(
    principal: ServicePrincipal,
) -> None:
    agent = connections["agent"]
    with agent.cursor() as cursor:
        cursor.execute("SET default_transaction_isolation='repeatable read'")
    try:
        with (
            pytest.raises(ServicePrincipalAccessDeniedError),
            service_principal_context(
                principal_id=principal.pk, clinic_id=principal.clinic_id
            ),
        ):
            pytest.fail("stale-snapshot context accepted")
    finally:
        with agent.cursor() as cursor:
            cursor.execute("RESET default_transaction_isolation")
