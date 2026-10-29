"""Clinic-admin installation and terminal retirement on real PostgreSQL roles."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.ehr.models import ClinicalDocumentVersion, Encounter, RestrictedNote
from apps.ehr.pack_catalog import PACKS
from apps.ehr.pack_models import SpecialtyPack
from apps.ehr.packs import install_pack, installed_packs, reorder_packs, uninstall_pack
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    create_draft,
)
from apps.identity.current_context import CurrentActorError
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, connections, transaction

from ehr.longitudinal_support import chart_world
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_encounters import seed

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("key", PACKS)
def test_installation_pins_bundle_owner_and_is_idempotent(
    rbac_graph: RbacGraph, key: str
) -> None:
    seed(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        pack = install_pack(clinic_id=rbac_graph.clinic_a, pack_key=key, version=1)
        again = install_pack(clinic_id=rbac_graph.clinic_a, pack_key=key, version=1)
        assert pack.pk == again.pk
        assert pack.clinical_owner_id == rbac_graph.clinic_admin
        assert pack.template.prompts["schema_version"] == 2
        assert all(
            pack.template.prompts[field] == ""
            for field in ("subjective", "objective", "assessment", "plan")
        )
        assert pack.questionnaire.questions[0]["required"] is False
        assert [row.pk for row in installed_packs(clinic_id=rbac_graph.clinic_a)] == [
            pack.pk
        ]
    with owner_context(rbac_graph.organization_a):
        events = AuditEvent.objects.filter(
            event_type="ehr.pack.installed", affected_record_id=str(pack.pk)
        )
        assert events.count() == 1
        assert set(events.get().payload) == {"clinic_id", "object_verb"}


def test_uninstall_retains_history_and_blocks_new_uses(rbac_graph: RbacGraph) -> None:
    world = chart_world(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        pack = install_pack(
            clinic_id=rbac_graph.clinic_a, pack_key="pediatrics", version=1
        )
        retired = uninstall_pack(
            clinic_id=rbac_graph.clinic_a, pack_key="pediatrics", version=1
        )
        replay = uninstall_pack(
            clinic_id=rbac_graph.clinic_a, pack_key="pediatrics", version=1
        )
        assert retired.uninstalled_at == replay.uninstalled_at
        assert installed_packs(clinic_id=rbac_graph.clinic_a) == ()
        with pytest.raises(ClinicalConflictError, match="pack_retired"):
            install_pack(
                clinic_id=rbac_graph.clinic_a, pack_key="pediatrics", version=1
            )
    with owner_context(rbac_graph.organization_a):
        encounter = Encounter.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            patient_id=world.patient,
            physician_id=rbac_graph.physician,
            unscheduled_reason="documentation_only",
        )
        assert SpecialtyPack.objects.get(pk=pack.pk).template_id == pack.template_id
    with as_actor(rbac_graph, rbac_graph.physician):
        with pytest.raises(ClinicalConflictError, match="pack_retired"):
            create_draft(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=encounter.pk,
                template_id=pack.template_id,
            )
        assert ClinicalDocumentVersion.objects.filter(pk=world.version.pk).exists()


def test_psychiatry_note_is_restricted_before_first_version(
    rbac_graph: RbacGraph,
) -> None:
    world = chart_world(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        pack = install_pack(
            clinic_id=rbac_graph.clinic_a, pack_key="psychiatry", version=1
        )
    with owner_context(rbac_graph.organization_a):
        encounter = Encounter.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            patient_id=world.patient,
            physician_id=rbac_graph.physician,
            unscheduled_reason="documentation_only",
        )
    with as_actor(rbac_graph, rbac_graph.physician):
        note = create_draft(
            clinic_id=rbac_graph.clinic_a,
            encounter_id=encounter.pk,
            template_id=pack.template_id,
        )
        assert RestrictedNote.objects.filter(document_id=note.document_id).exists()


def test_runtime_cannot_rewrite_delete_or_forge_bundle(rbac_graph: RbacGraph) -> None:
    seed(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        pack = install_pack(
            clinic_id=rbac_graph.clinic_a, pack_key="general", version=1
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            SpecialtyPack.objects.filter(pk=pack.pk).update(license_ref="approved")
        with pytest.raises(DatabaseError), transaction.atomic():
            SpecialtyPack.objects.filter(pk=pack.pk).delete()
        with (
            pytest.raises(DatabaseError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute("TRUNCATE clinic_app.ehr_specialtypack CASCADE")


@pytest.mark.parametrize("key", ["__import__", "general"])
def test_unknown_bundle_versions_make_no_publications(
    rbac_graph: RbacGraph, key: str
) -> None:
    seed(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        with pytest.raises(ValidationError):
            install_pack(clinic_id=rbac_graph.clinic_a, pack_key=key, version=2)
        assert installed_packs(clinic_id=rbac_graph.clinic_a) == ()


def test_physician_and_foreign_clinic_cannot_install(rbac_graph: RbacGraph) -> None:
    seed(rbac_graph)
    with (
        as_actor(rbac_graph, rbac_graph.physician),
        pytest.raises(CurrentActorError),
    ):
        install_pack(clinic_id=rbac_graph.clinic_a, pack_key="general", version=1)
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        for clinic_id in (rbac_graph.clinic_c, uuid4()):
            with pytest.raises(CurrentActorError):
                install_pack(clinic_id=clinic_id, pack_key="general", version=1)
        for key in ("general", "unknown"):
            with pytest.raises(ClinicalAccessDeniedError):
                uninstall_pack(clinic_id=rbac_graph.clinic_a, pack_key=key, version=1)


def test_parallel_installation_converges_without_partial_bundles(
    rbac_graph: RbacGraph,
) -> None:
    seed(rbac_graph)
    barrier = Barrier(2, timeout=30)

    def install() -> UUID:
        connections.close_all()
        try:
            with (
                runtime_role(),
                tenant_context(rbac_graph.clinic_admin, rbac_graph.organization_a),
            ):
                barrier.wait()
                return install_pack(
                    clinic_id=rbac_graph.clinic_a, pack_key="general", version=1
                ).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(install)
        second = pool.submit(install)
        assert first.result(timeout=60) == second.result(timeout=60)


def test_ordering_is_clinic_local_complete_and_idempotent(
    rbac_graph: RbacGraph,
) -> None:
    seed(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.get_or_create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            user_id=rbac_graph.clinic_admin,
            role=UserClinicRole.Role.CLINIC_ADMIN,
        )
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        for clinic in (rbac_graph.clinic_a, rbac_graph.clinic_b):
            for key in ("general", "pediatrics"):
                install_pack(clinic_id=clinic, pack_key=key, version=1)
        ordered = reorder_packs(
            clinic_id=rbac_graph.clinic_a, pack_keys=("pediatrics", "general")
        )
        assert [row.key for row in ordered] == ["pediatrics", "general"]
        reorder_packs(
            clinic_id=rbac_graph.clinic_a, pack_keys=("pediatrics", "general")
        )
        assert [row.key for row in installed_packs(clinic_id=rbac_graph.clinic_b)] == [
            "general",
            "pediatrics",
        ]
        for keys in (("general",), ("general", "general"), ("unknown", "general")):
            with pytest.raises(ValidationError):
                reorder_packs(clinic_id=rbac_graph.clinic_a, pack_keys=keys)
        assert [row.key for row in installed_packs(clinic_id=rbac_graph.clinic_a)] == [
            "pediatrics",
            "general",
        ]
    with owner_context(rbac_graph.organization_a):
        assert (
            AuditEvent.objects.filter(
                event_type="ehr.pack.reordered",
                affected_record_id=str(rbac_graph.clinic_a),
            ).count()
            == 1
        )
