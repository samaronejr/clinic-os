"""Later initial drafts obey pack guards; retained amendments stay available."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.finalization import amend_document, discard_draft
from apps.ehr.models import ClinicalDocumentVersion, RestrictedNote
from apps.ehr.packs import install_pack, uninstall_pack
from apps.ehr.services import ClinicalAccessDeniedError, create_draft
from apps.identity.models import UserClinicRole
from django.db import DatabaseError, transaction
from psycopg.errors import CheckViolation

from ehr.longitudinal_support import chart_world, finalized
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("number", [2, 3])
@pytest.mark.parametrize("key", ["pediatrics", "psychiatry"])
def test_later_initial_draft_cannot_bypass_pack_guards(
    rbac_graph: RbacGraph, key: str, number: int
) -> None:
    world = chart_world(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        pack = install_pack(clinic_id=rbac_graph.clinic_a, pack_key=key, version=1)
        if key == "pediatrics":
            uninstall_pack(clinic_id=rbac_graph.clinic_a, pack_key=key, version=1)
    with as_actor(rbac_graph, rbac_graph.physician):
        previous = world.version
        for version in range(1, number):
            assert previous.version == version
            discard_draft(clinic_id=rbac_graph.clinic_a, version_id=previous.pk)
            if version + 1 < number:
                previous = create_draft(
                    clinic_id=rbac_graph.clinic_a,
                    encounter_id=world.version.document.encounter_id,
                    template_id=world.version.template_id,
                )
        assert not RestrictedNote.objects.filter(
            document_id=world.version.document_id
        ).exists()
        with pytest.raises(DatabaseError) as denial, transaction.atomic():
            ClinicalDocumentVersion.objects.create(
                organization_id=rbac_graph.organization_a,
                document_id=world.version.document_id,
                template_id=pack.template_id,
                author_id=rbac_graph.physician,
                version=number,
            )
        assert isinstance(denial.value.__cause__, CheckViolation)
        assert not ClinicalDocumentVersion.objects.filter(
            document_id=world.version.document_id, version=number
        ).exists()


def test_later_unknown_and_wrong_clinic_templates_have_identical_denials(
    rbac_graph: RbacGraph,
) -> None:
    world = chart_world(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.get_or_create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            user_id=rbac_graph.clinic_admin,
            role=UserClinicRole.Role.CLINIC_ADMIN,
        )
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        foreign = install_pack(
            clinic_id=rbac_graph.clinic_b, pack_key="psychiatry", version=1
        )
    sql_denials = []
    service_denials = []
    with as_actor(rbac_graph, rbac_graph.physician):
        discard_draft(clinic_id=rbac_graph.clinic_a, version_id=world.version.pk)
        for template in (uuid4(), foreign.template_id):
            with pytest.raises(DatabaseError) as denial, transaction.atomic():
                ClinicalDocumentVersion.objects.create(
                    organization_id=rbac_graph.organization_a,
                    document_id=world.version.document_id,
                    template_id=template,
                    author_id=rbac_graph.physician,
                    version=2,
                )
            cause = denial.value.__cause__
            assert isinstance(cause, CheckViolation)
            sql_denials.append((cause.sqlstate, cause.diag.message_primary))
            with pytest.raises(ClinicalAccessDeniedError) as service:
                create_draft(
                    clinic_id=rbac_graph.clinic_a,
                    encounter_id=world.version.document.encounter_id,
                    template_id=template,
                )
            service_denials.append(service.value.args)
        assert sql_denials[0] == sql_denials[1]
        assert service_denials[0] == service_denials[1]
        assert (
            ClinicalDocumentVersion.objects.filter(
                document_id=world.version.document_id
            ).count()
            == 1
        )


@pytest.mark.parametrize("key", ["general", "psychiatry"])
def test_retired_pack_allows_genuine_retained_history_amendment(
    rbac_graph: RbacGraph, key: str
) -> None:
    world = chart_world(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        pack = install_pack(clinic_id=rbac_graph.clinic_a, pack_key=key, version=1)
    with as_actor(rbac_graph, rbac_graph.physician):
        discard_draft(clinic_id=rbac_graph.clinic_a, version_id=world.version.pk)
        draft = create_draft(
            clinic_id=rbac_graph.clinic_a,
            encounter_id=world.version.document.encounter_id,
            template_id=pack.template_id,
        )
        assert draft.version == 2
    base = finalized(rbac_graph, draft)
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        uninstall_pack(clinic_id=rbac_graph.clinic_a, pack_key=key, version=1)
    with as_actor(rbac_graph, rbac_graph.physician):
        amendment = amend_document(
            clinic_id=rbac_graph.clinic_a,
            version_id=base.pk,
            reason="Synthetic correction",
        )
        assert amendment.amendment_of_id == base.pk
        assert amendment.template_id == pack.template_id
        assert amendment.version == 3
        assert amendment.soap == base.soap
        restricted = RestrictedNote.objects.filter(
            document_id=amendment.document_id
        ).exists()
        assert restricted == (key == "psychiatry")
