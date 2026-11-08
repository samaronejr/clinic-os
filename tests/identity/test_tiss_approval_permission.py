from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.migrations._insurance_permission_sql import SQL
from apps.identity.models import ProfessionalRegistration, RoleGrant
from django.db import connection
from django.utils import timezone

from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "role",
    [
        "physician",
        "nurse",
        "allied_professional",
        "receptionist",
        "scheduler",
        "finance",
        "clinic_admin",
        "clinic_manager",
        "owner",
        "org_admin",
        "patient_delegate",
        "support",
        "service_principal",
    ],
)
def test_v2_approval_permission_is_only_for_registered_clinic_physicians(
    rbac_graph: RbacGraph, role: str
) -> None:
    actor, _enrollment = permission_actor(rbac_graph, role)
    with permission_context(rbac_graph, actor):
        if role == "physician":
            assert (
                require_permission(
                    "tiss.clinical_approve", clinic_id=rbac_graph.clinic_a
                )
                == actor
            )
        else:
            with pytest.raises(CurrentActorError):
                require_permission(
                    "tiss.clinical_approve", clinic_id=rbac_graph.clinic_a
                )
        with pytest.raises(CurrentActorError):
            require_permission("tiss.clinical_approve", clinic_id=rbac_graph.clinic_b)


@pytest.mark.parametrize("revocation", ["registration", "subtraction"])
def test_clinical_approval_rechecks_registration_and_v2_subtractions(
    rbac_graph: RbacGraph, revocation: str
) -> None:
    actor, _enrollment = permission_actor(rbac_graph, "physician")
    with permission_context(rbac_graph, actor):
        require_permission("tiss.clinical_approve", clinic_id=rbac_graph.clinic_a)
    with owner_context(rbac_graph.organization_a):
        if revocation == "registration":
            assert (
                ProfessionalRegistration.objects.filter(user_id=actor).update(
                    revoked_at=timezone.now()
                )
                == 1
            )
        else:
            RoleGrant.objects.create(
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_a,
                role="physician",
                permission="tiss.clinical_approve",
                bundle_version=2,
                valid_from=timezone.now(),
            )
    with permission_context(rbac_graph, actor), pytest.raises(CurrentActorError):
        require_permission("tiss.clinical_approve", clinic_id=rbac_graph.clinic_a)


def test_permission_extension_installs_its_static_composition_repeatably() -> None:
    # Given: 0047's SQL, composed statically from 0044 as 0044 composes from 0042.
    body = SQL.split("$f$")[1]
    evaluator = (
        "SELECT prosrc, pg_get_functiondef(oid) FROM pg_proc WHERE oid="
        "'clinic_app.identity_actor_permission(text,uuid,uuid,uuid,uuid)'"
        "::regprocedure"
    )
    bundle = (
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid='clinic_app.identity_rolegrant'::regclass "
        "AND conname='identity_rolegrant_bundle'"
    )
    with connection.cursor() as cursor:
        cursor.execute(evaluator)
        before = cursor.fetchone()
        cursor.execute(bundle)
        constraint = cursor.fetchone()
        # Then: the migrated evaluator body is exactly the static composition.
        assert before is not None
        assert before[0] == body
        assert "perm='privacy.requests'" in body
        assert "perm='tiss.clinical_approve'" in body
        assert constraint is not None
        assert "tiss.clinical_approve" in constraint[0]
        # When: the static SQL runs again on the migrated database.
        cursor.execute(SQL)
        # Then: neither the evaluator nor the bundle constraint changes.
        cursor.execute(evaluator)
        assert cursor.fetchone() == before
        cursor.execute(bundle)
        assert cursor.fetchone() == constraint
        cursor.execute(
            "SELECT pg_get_functiondef("
            "'clinic_app.has_permission(text,uuid,uuid)'::regprocedure)"
        )
        wrapper = cursor.fetchone()
        assert wrapper is not None
        assert (
            "identity_actor_permission(perm,clinic,enrollment,actor,tenant)"
            in wrapper[0]
        )
