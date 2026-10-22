"""Retained staff membership cannot supply patient representation authority."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, assert_never
from uuid import uuid4

import psycopg
import pytest
from apps.identity.delegates import (
    DelegateGrantRequest,
    activate,
    request_delegate_grant,
    revoke,
    switch_delegate,
    verify_delegate_grant,
)
from apps.identity.models import PatientDelegateGrant
from apps.intake.access import PatientAccessDeniedError
from apps.intake.patient_access import patient_session_context
from django.db import connection

from database_urls import database_url_for_name
from identity.permission_support import permission_context
from patient_service_support import runtime_role
from patients.delegate_support import (
    active_grant,
    delegate_world,
    request_grant,
)

if TYPE_CHECKING:
    from pathlib import Path

    from patients.delegate_support import DelegateWorld
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
Command = Literal["request", "verify", "activate", "revoke"]


def _revoke_membership(
    graph: RbacGraph, world: DelegateWorld, configured_url: str
) -> None:
    """Commit the retained revocation through an independent real connection."""
    url = database_url_for_name(configured_url, connection.settings_dict["NAME"])
    with psycopg.connect(url) as admin:
        admin.execute(
            "UPDATE clinic_app.identity_userclinicrole "
            "SET revoked_at=statement_timestamp() "
            "WHERE clinic_id=%s AND user_id=%s AND revoked_at IS NULL",
            [graph.clinic_a, world.verifier_id],
        )


@pytest.mark.parametrize("command", ["request", "verify", "activate", "revoke"])
def test_open_staff_context_refuses_delegate_commands_after_membership_revocation(
    rbac_graph: RbacGraph,
    tmp_path: Path,
    superuser_database_url: str,
    command: Command,
) -> None:
    world = delegate_world(rbac_graph, tmp_path)
    match command:
        case "activate":
            grant = request_grant(rbac_graph, world)
            with permission_context(rbac_graph, world.verifier_id):
                grant = verify_delegate_grant(
                    clinic_id=rbac_graph.clinic_a,
                    grant_id=grant.grant_id,
                    expected_revision=grant.revision,
                    verified=True,
                )
        case "request" | "verify" | "revoke":
            grant = request_grant(rbac_graph, world)
        case unreachable:
            assert_never(unreachable)

    with permission_context(rbac_graph, world.verifier_id):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.has_permission('configuration.clinic',%s,NULL)",
                [rbac_graph.clinic_a],
            )
            assert cursor.fetchone() == (True,)
        _revoke_membership(rbac_graph, world, superuser_database_url)
        match command:
            case "request":
                with pytest.raises(PatientAccessDeniedError):
                    request_delegate_grant(
                        clinic_id=rbac_graph.clinic_a,
                        grant=DelegateGrantRequest(
                            represented_enrollment_id=world.notes.enrollment_id,
                            delegate_enrollment_id=world.delegate_enrollment_id,
                            authority_kind=PatientDelegateGrant.AuthorityKind.GUARDIAN,
                            evidence_attachment_id=world.evidence_id,
                            purposes=("care_processing",),
                            operations=("enrollment_view",),
                            expires_at=world.deadline,
                            idempotency_key=uuid4(),
                        ),
                    )
            case "verify":
                with pytest.raises(PatientAccessDeniedError):
                    verify_delegate_grant(
                        clinic_id=rbac_graph.clinic_a,
                        grant_id=grant.grant_id,
                        expected_revision=grant.revision,
                        verified=True,
                    )
            case "activate":
                with pytest.raises(PatientAccessDeniedError):
                    activate(
                        clinic_id=rbac_graph.clinic_a,
                        grant_id=grant.grant_id,
                        expected_revision=grant.revision,
                    )
            case "revoke":
                with pytest.raises(PatientAccessDeniedError):
                    revoke(
                        clinic_id=rbac_graph.clinic_a,
                        grant_id=grant.grant_id,
                        expected_revision=grant.revision,
                    )
            case unreachable:
                assert_never(unreachable)


def test_verified_patient_grant_does_not_inherit_verifier_staff_membership(
    rbac_graph: RbacGraph,
    tmp_path: Path,
    superuser_database_url: str,
) -> None:
    world = delegate_world(rbac_graph, tmp_path)
    grant = active_grant(rbac_graph, world)
    with runtime_role(), patient_session_context(world.session_id):
        switch_delegate(clinic_id=rbac_graph.clinic_a, grant_id=grant.grant_id)
    _revoke_membership(rbac_graph, world, superuser_database_url)

    with runtime_role(), patient_session_context(world.session_id) as binding:
        assert binding is not None
        assert binding.enrollment_id == world.notes.enrollment_id
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT NULLIF(current_setting('app.current_user_id',true),'')"
            )
            assert cursor.fetchone() == (None,)
