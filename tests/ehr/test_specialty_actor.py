"""Execute pack provenance and inactive-actor SQL boundaries as clinic_app."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.pack_catalog import (
    LICENSE_REF,
    PACKS,
    questionnaire_definition,
    template_definition,
)
from apps.ehr.pack_models import SpecialtyPack
from apps.ehr.services import publish_template
from apps.identity.models import User
from apps.intake.questionnaires import publish_template as publish_questionnaire
from django.db import IntegrityError, ProgrammingError, connection, transaction
from django.utils import timezone
from psycopg.errors import CheckViolation, InsufficientPrivilege

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_encounters import seed

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass(frozen=True, slots=True)
class PackBinding:
    organization: UUID
    clinic: UUID
    actor: UUID
    template: UUID
    questionnaire: UUID


@pytest.fixture
def pack_binding(rbac_graph: RbacGraph) -> PackBinding:
    seed(rbac_graph)
    definition = PACKS["general"]
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        template = publish_template(
            clinic_id=rbac_graph.clinic_a,
            key="pack_general_v1",
            title=str(definition.title),
            prompts=template_definition(definition),
        )
        questionnaire = publish_questionnaire(
            clinic_id=rbac_graph.clinic_a,
            key="pack_general_v1",
            title=str(definition.title),
            questions=questionnaire_definition(definition),
        )
    return PackBinding(
        rbac_graph.organization_a,
        rbac_graph.clinic_a,
        rbac_graph.clinic_admin,
        template.pk,
        questionnaire.pk,
    )


def _insert(binding: PackBinding, claimed_owner: UUID) -> None:
    """Bind raw GUCs so Python's active-actor guard cannot mask the SQL check."""
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true),"
            "set_config('app.current_user_id',%s,true)",
            [str(binding.organization), str(binding.actor)],
        )
        identifier = uuid4()
        cursor.execute(
            "INSERT INTO clinic_app.ehr_specialtypack"
            "(id,organization_id,clinic_id,key,version,position,template_id,"
            "questionnaire_id,calculators,license_ref,clinical_owner_id,installed_at)"
            " VALUES (%s,%s,%s,'general',1,1,%s,%s,%s::jsonb,%s,%s,%s) RETURNING id",
            [
                identifier,
                binding.organization,
                binding.clinic,
                binding.template,
                binding.questionnaire,
                json.dumps(
                    [{"key": "bmi", "version": 1}, {"key": "egfr", "version": 1}]
                ),
                LICENSE_REF,
                claimed_owner,
                timezone.now(),
            ],
        )
        assert cursor.fetchone() == (identifier,)
        transaction.set_rollback(True)


def test_pack_sql_rechecks_inactive_actor(pack_binding: PackBinding) -> None:
    # Given valid published bindings, keep membership, actor and tenant unchanged.
    for active in (True, False, True):
        User.objects.filter(pk=pack_binding.actor).update(is_active=active)
        # When the runtime role inserts directly, only SQL can refuse this actor.
        if active:
            _insert(pack_binding, pack_binding.actor)
        else:
            with pytest.raises(ProgrammingError) as denied:
                _insert(pack_binding, pack_binding.actor)
            assert type(denied.value.__cause__) is InsufficientPrivilege
    # Then neither accepted probes nor refusals leave a bundle behind.
    with owner_context(pack_binding.organization):
        assert SpecialtyPack.objects.count() == 0


def test_pack_guard_refuses_forged_installer(
    pack_binding: PackBinding, rbac_graph: RbacGraph
) -> None:
    # Given a valid active manager and otherwise valid published bindings.
    # When a direct insert attributes the installation to another actor.
    with pytest.raises(IntegrityError) as denied:
        _insert(pack_binding, rbac_graph.physician)
    # Then the row-binding trigger, not a Python check, rejects the forgery.
    assert type(denied.value.__cause__) is CheckViolation
    with owner_context(pack_binding.organization):
        assert SpecialtyPack.objects.count() == 0
