"""Finalization, amendment lineage and closure acceptance on real PostgreSQL."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.ehr.finalization import (
    amend_document,
    close_encounter,
    content_digest,
    discard_draft,
    finalize_version,
)
from apps.ehr.models import ClinicalDocumentVersion, Encounter
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    create_draft,
    open_encounter,
    record_clinical_note,
    view_version,
)
from apps.identity.models import User, UserClinicRole
from apps.identity.stepup import StepUpRequired
from apps.scheduling.services import create_availability
from apps.tenancy.db import tenant_context
from apps.tenancy.envelope import reveal
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, connections, transaction
from django.http import HttpRequest
from django.utils import timezone

from auth.stepup_test_support import (
    clear_freshness,
    seed_freshness,
    verified_request,
)
from otp_test_support import get_totp_device
from patient_service_support import runtime_role
from renewal.test_encounters import (
    CONTENT,
    physician_client,
    seed,
    seed_appointment_setup_for_existing,
    setup_context,
)
from scheduling.appointment_service_support import (
    AppointmentSetup,
    create_synthetic_appointment,
)

if TYPE_CHECKING:
    from apps.ehr.models import SpecialtyTemplate
    from apps.scheduling.models import Appointment
    from django.test import Client

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

AMENDED: dict[str, str] = {
    "subjective": "Relato sintético retificado",
    "objective": "Exame sintético",
    "assessment": "Avaliação sintética",
    "plan": "Plano sintético",
}


def saved_draft(
    graph: RbacGraph, appointment: Appointment, template: SpecialtyTemplate
) -> ClinicalDocumentVersion:
    """Open the encounter, create the draft and save the synthetic SOAP body."""
    encounter = open_encounter(clinic_id=graph.clinic_a, appointment_id=appointment.pk)
    version = create_draft(
        clinic_id=graph.clinic_a, encounter_id=encounter.pk, template_id=template.pk
    )
    return record_clinical_note(
        clinic_id=graph.clinic_a,
        version_id=version.pk,
        expected_revision=1,
        content=CONTENT,
    )


def test_finalize_digest_idempotent_and_immutable(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = saved_draft(graph, appointment, template)
        expected = content_digest(version)
        finalized = finalize_version(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=2,
            request=request,
        )
        assert finalized.state == "finalized"
        assert finalized.content_digest == expected
        assert finalized.finalized_at is not None
        # A repeated finalize returns the first result, never a second version.
        again = finalize_version(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=1,
            request=request,
        )
        assert again.pk == finalized.pk
        assert ClinicalDocumentVersion.objects.count() == 1
        # The runtime role's in-place write hits the binding trigger too.
        with pytest.raises(DatabaseError), transaction.atomic():
            ClinicalDocumentVersion.objects.filter(pk=version.pk).update(
                revision=version.revision + 1
            )
        stored = view_version(clinic_id=graph.clinic_a, version_id=version.pk)
        assert stored.subjective == CONTENT["subjective"]
        assert stored.content_digest == expected
    # Owner-role in-place writes hit the binding trigger, not the service.
    with setup_context(graph.organization_a):
        with pytest.raises(DatabaseError), transaction.atomic():
            ClinicalDocumentVersion.objects.filter(pk=version.pk).update(
                revision=version.revision + 1
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            ClinicalDocumentVersion.objects.filter(pk=version.pk).update(state="draft")
        with pytest.raises(DatabaseError), transaction.atomic():
            ClinicalDocumentVersion.objects.filter(pk=version.pk).delete()
        stored = ClinicalDocumentVersion.objects.get(pk=version.pk)
        assert stored.subjective == CONTENT["subjective"]
        assert stored.state == "finalized"
        assert (
            AuditEvent.objects.filter(event_type="ehr.document.finalized").count() == 1
        )


def test_finalize_missing_content_stale_and_terminal_states(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        encounter = open_encounter(
            clinic_id=graph.clinic_a, appointment_id=appointment.pk
        )
        version = create_draft(
            clinic_id=graph.clinic_a,
            encounter_id=encounter.pk,
            template_id=template.pk,
        )
        with pytest.raises(ClinicalConflictError, match="missing_required_content"):
            finalize_version(
                clinic_id=graph.clinic_a,
                version_id=version.pk,
                expected_revision=1,
                request=request,
            )
        assert ClinicalDocumentVersion.objects.get(pk=version.pk).state == "draft"
        record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=1,
            content=CONTENT,
        )
        with pytest.raises(ClinicalConflictError, match="stale_revision"):
            finalize_version(
                clinic_id=graph.clinic_a,
                version_id=version.pk,
                expected_revision=1,
                request=request,
            )
        assert ClinicalDocumentVersion.objects.get(pk=version.pk).state == "draft"
        finalize_version(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=2,
            request=request,
        )
        # A superseded version can never be finalized or edited again.
        amendment = amend_document(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            reason="Correção sintética",
        )
        record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=amendment.pk,
            expected_revision=1,
            content=AMENDED,
        )
        finalize_version(
            clinic_id=graph.clinic_a,
            version_id=amendment.pk,
            expected_revision=2,
            request=request,
        )
        with pytest.raises(ClinicalConflictError, match="precondition_failed"):
            finalize_version(
                clinic_id=graph.clinic_a,
                version_id=version.pk,
                expected_revision=2,
                request=request,
            )
        # Saving the superseded original is the same conflict, not an error:
        # the UPDATE policy hides it from select_for_update, and the service
        # maps that non-draft path to precondition_failed.
        with pytest.raises(ClinicalConflictError, match="precondition_failed"):
            record_clinical_note(
                clinic_id=graph.clinic_a,
                version_id=version.pk,
                expected_revision=2,
                content=dict.fromkeys(CONTENT, "edição tardia"),
            )
        base = view_version(clinic_id=graph.clinic_a, version_id=version.pk)
        assert base.state == "superseded"
        assert base.subjective == CONTENT["subjective"]
        assert base.content_digest


def test_finalize_requires_recent_verification_at_service_boundary(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    stale_request = verified_request(
        graph.physician, verified_at=int(time.time()) - 301
    )
    anonymous = HttpRequest()
    anonymous.session = SessionStore()
    anonymous.user = AnonymousUser()
    unverified = HttpRequest()
    unverified.session = SessionStore()
    unverified.user = User.objects.get(pk=graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = saved_draft(graph, appointment, template)
        for bad_request in (anonymous, unverified, stale_request):
            with pytest.raises(StepUpRequired):
                finalize_version(
                    clinic_id=graph.clinic_a,
                    version_id=version.pk,
                    expected_revision=2,
                    request=bad_request,
                )
        # Every denial preserved the draft and its provenance.
        stored = ClinicalDocumentVersion.objects.get(pk=version.pk)
        assert stored.state == "draft"
        assert stored.subjective == CONTENT["subjective"]
        assert stored.content_digest == ""
        finalized = finalize_version(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=2,
            request=request,
        )
        assert finalized.state == "finalized"
    with setup_context(graph.organization_a):
        assert (
            AuditEvent.objects.filter(
                event_type="ehr.access.denied",
                payload__reason_code="step_up_required",
            ).count()
            == 3
        )


def test_amendment_lineage_and_conflicts(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = saved_draft(graph, appointment, template)
        # No amendment while a draft is open or without a reason.
        with pytest.raises(ClinicalConflictError, match="draft_in_progress"):
            amend_document(
                clinic_id=graph.clinic_a,
                version_id=version.pk,
                reason="Cedo demais",
            )
        finalize_version(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=2,
            request=request,
        )
        with pytest.raises(ValidationError):
            amend_document(
                clinic_id=graph.clinic_a, version_id=version.pk, reason="   "
            )
        amendment = amend_document(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            reason="Correção sintética",
        )
        assert amendment.state == "draft"
        assert amendment.version == 2
        assert amendment.amendment_of_id == version.pk
        assert amendment.amendment_reason == "Correção sintética"
        assert amendment.subjective == CONTENT["subjective"]
        assert amendment.author_id == graph.physician
        # A second amendment is refused while the linked draft is open.
        with pytest.raises(ClinicalConflictError, match="draft_in_progress"):
            amend_document(
                clinic_id=graph.clinic_a,
                version_id=version.pk,
                reason="Outra retificação",
            )
        record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=amendment.pk,
            expected_revision=1,
            content=AMENDED,
        )
        finalize_version(
            clinic_id=graph.clinic_a,
            version_id=amendment.pk,
            expected_revision=2,
            request=request,
        )
        # The stale base is a conflict; the superseded original is preserved.
        with pytest.raises(ClinicalConflictError, match="stale_revision"):
            amend_document(
                clinic_id=graph.clinic_a,
                version_id=version.pk,
                reason="Base antiga",
            )
        lineage = list(
            ClinicalDocumentVersion.objects.order_by("version").values_list(
                "version", "state", "amendment_reason"
            )
        )
        assert lineage == [
            (1, "superseded", ""),
            (2, "finalized", "Correção sintética"),
        ]
        current = ClinicalDocumentVersion.objects.get(state="finalized")
        assert current.subjective == AMENDED["subjective"]
        assert content_digest(current) == current.content_digest
        assert content_digest(current) != content_digest(
            ClinicalDocumentVersion.objects.get(version=1)
        )
    with setup_context(graph.organization_a):
        events = AuditEvent.objects.filter(
            event_type__in=("ehr.document.amended", "ehr.document.finalized")
        )
        assert events.filter(event_type="ehr.document.amended").count() == 1
        assert events.filter(event_type="ehr.document.finalized").count() == 2


def test_discard_redraft_and_close_encounter(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = saved_draft(graph, appointment, template)
        encounter = version.document.encounter
        with pytest.raises(ClinicalConflictError, match="draft_in_progress"):
            close_encounter(clinic_id=graph.clinic_a, encounter_id=encounter.pk)
        # The database close guard rejects the same transition under raw SQL.
        with pytest.raises(DatabaseError), transaction.atomic():
            Encounter.objects.filter(pk=encounter.pk).update(
                state="closed", closed_at=timezone.now()
            )
        assert Encounter.objects.get(pk=encounter.pk).state == "open"
        discarded = discard_draft(clinic_id=graph.clinic_a, version_id=version.pk)
        assert discarded.state == "discarded"
        # Discarded rows expose metadata only: the runtime role reads the row
        # but every SOAP field is blank, and the owner-only archive holds the
        # preserved body the runtime role cannot reach.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT state,content,content_sha256 "
                "FROM clinic_app.ehr_clinicaldocumentversion WHERE id=%s",
                [version.pk],
            )
            assert cursor.fetchone() == ("discarded", None, "")
            with pytest.raises(DatabaseError), transaction.atomic():
                cursor.execute(
                    "SELECT content FROM clinic_app.ehr_discarded_content "
                    "WHERE version_id=%s",
                    [version.pk],
                )
        assert discarded.subjective == ""
        # Discarded content is never served, even to the author.
        with pytest.raises(ClinicalAccessDeniedError):
            view_version(clinic_id=graph.clinic_a, version_id=version.pk)
        with pytest.raises(ClinicalConflictError, match="precondition_failed"):
            discard_draft(clinic_id=graph.clinic_a, version_id=version.pk)
        # A fresh draft resumes on the same document with the next version.
        redraft = create_draft(
            clinic_id=graph.clinic_a,
            encounter_id=encounter.pk,
            template_id=template.pk,
        )
        assert redraft.version == 2
        assert redraft.state == "draft"
        record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=redraft.pk,
            expected_revision=1,
            content=CONTENT,
        )
        finalize_version(
            clinic_id=graph.clinic_a,
            version_id=redraft.pk,
            expected_revision=2,
            request=request,
        )
        closed = close_encounter(clinic_id=graph.clinic_a, encounter_id=encounter.pk)
        assert closed.state == "closed"
        assert closed.closed_at is not None
        again = close_encounter(clinic_id=graph.clinic_a, encounter_id=encounter.pk)
        assert again.pk == closed.pk
        assert again.closed_at == closed.closed_at
        # Amendments remain possible on the closed encounter; new documents do not.
        amendment = amend_document(
            clinic_id=graph.clinic_a,
            version_id=redraft.pk,
            reason="Retificação após encerramento",
        )
        assert amendment.version == 3
        record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=amendment.pk,
            expected_revision=1,
            content=AMENDED,
        )
        finalize_version(
            clinic_id=graph.clinic_a,
            version_id=amendment.pk,
            expected_revision=2,
            request=request,
        )
        assert Encounter.objects.get(pk=encounter.pk).state == "closed"
        assert list(
            ClinicalDocumentVersion.objects.order_by("version").values_list(
                "version", "state"
            )
        ) == [(1, "discarded"), (2, "superseded"), (3, "finalized")]
    with setup_context(graph.organization_a):
        assert AuditEvent.objects.filter(event_type="ehr.encounter.closed").count() == 1
        assert (
            AuditEvent.objects.filter(event_type="ehr.document.discarded").count() == 1
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT content FROM clinic_app.ehr_discarded_content "
                "WHERE version_id=%s",
                [version.pk],
            )
            row = cursor.fetchone()
            assert row is not None
            archived = json.loads(
                reveal(
                    purpose="ehr.clinicaldocumentversion.content",
                    envelope=bytes(row[0]),
                )
            )
            assert archived == CONTENT


def test_concurrent_finalize_and_amend_converge(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = saved_draft(graph, appointment, template)
    barrier = Barrier(2, timeout=15)

    def finalize(_: int) -> tuple[str, str]:
        try:
            with runtime_role(), tenant_context(graph.physician, graph.organization_a):
                barrier.wait()
                finalized = finalize_version(
                    clinic_id=graph.clinic_a,
                    version_id=version.pk,
                    expected_revision=2,
                    request=request,
                )
                return str(finalized.pk), finalized.state
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finalize, range(2)))
    assert results[0] == results[1] == (str(version.pk), "finalized")

    def amend(_: int) -> str:
        try:
            with runtime_role(), tenant_context(graph.physician, graph.organization_a):
                barrier.wait()
                try:
                    amend_document(
                        clinic_id=graph.clinic_a,
                        version_id=version.pk,
                        reason="Corrida sintética",
                    )
                except ClinicalConflictError as error:
                    return error.reason_code
                return "amended"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(amend, range(2))) == ["amended", "draft_in_progress"]
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert ClinicalDocumentVersion.objects.filter(state="draft").count() == 1
        assert ClinicalDocumentVersion.objects.count() == 2


@pytest.mark.parametrize("actor", ["shared_user", "clinic_admin"])
def test_finalize_amend_close_denied_to_non_physicians(
    rbac_graph: RbacGraph, actor: str
) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = saved_draft(graph, appointment, template)
    with runtime_role(), tenant_context(getattr(graph, actor), graph.organization_a):
        for call in (
            lambda: finalize_version(
                clinic_id=graph.clinic_a,
                version_id=version.pk,
                expected_revision=2,
                request=request,
            ),
            lambda: amend_document(
                clinic_id=graph.clinic_a, version_id=version.pk, reason="x"
            ),
            lambda: discard_draft(clinic_id=graph.clinic_a, version_id=version.pk),
            lambda: close_encounter(
                clinic_id=graph.clinic_a,
                encounter_id=version.document.encounter_id,
            ),
        ):
            with pytest.raises(ClinicalAccessDeniedError):
                call()
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert ClinicalDocumentVersion.objects.get(pk=version.pk).state == "draft"


def test_care_physician_reads_but_cannot_amend(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = saved_draft(graph, appointment, template)
        finalize_version(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=2,
            request=request,
        )
    setup = seed_appointment_setup_for_existing(graph, appointment)
    with setup_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=graph.clinic_admin,
            role="physician",
        )
    # A second appointment gives the other physician a real care relationship
    # without touching the original encounter's assignment.
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        create_availability(
            clinic_id=graph.clinic_a,
            practitioner_id=graph.clinic_admin,
            start_local="2035-06-03T08:00",
            end_local="2035-06-03T12:00",
            idempotency_key=uuid4(),
        )
        create_synthetic_appointment(
            AppointmentSetup(
                setup.organization_id,
                setup.clinic_id,
                setup.actor_id,
                graph.clinic_admin,
                setup.enrollment_id,
                setup.patient_id,
            ),
            start_local="2035-06-03T09:00",
            end_local="2035-06-03T10:00",
        )
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        viewed = view_version(clinic_id=graph.clinic_a, version_id=version.pk)
        assert viewed.state == "finalized"
        assert viewed.subjective == CONTENT["subjective"]
        with pytest.raises(ClinicalAccessDeniedError):
            amend_document(clinic_id=graph.clinic_a, version_id=version.pk, reason="x")
        with pytest.raises(ClinicalAccessDeniedError):
            finalize_version(
                clinic_id=graph.clinic_a,
                version_id=version.pk,
                expected_revision=2,
                request=request,
            )
        with pytest.raises(ClinicalAccessDeniedError):
            close_encounter(
                clinic_id=graph.clinic_a,
                encounter_id=version.document.encounter_id,
            )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert ClinicalDocumentVersion.objects.get(pk=version.pk).state == "finalized"


def post(client: Client, url: str, **fields: object) -> int:
    """Submit one workspace action and return only the status code."""
    return client.post(url, {k: str(v) for k, v in fields.items()}).status_code


def _assert_unsaved_conflict(
    client: Client, url: str, version: ClinicalDocumentVersion, sentinel: str
) -> None:
    """A stale save over a non-draft version renders the edits as unsaved."""
    response = client.post(
        url,
        {
            "action": "save",
            "version_id": str(version.pk),
            "revision": "2",
            **CONTENT,
            "subjective": sentinel,
        },
    )
    assert response.status_code == 409
    assert response.context["unsaved"] is True
    assert response.context["form"].data["subjective"] == sentinel
    html = response.content.decode()
    assert sentinel in html
    assert 'data-state="unsaved"' in html
    assert CONTENT["subjective"] in html
    assert 'id="soap-form"' not in html
    assert 'data-finalization="local"' not in html


def _finalize_amendment(
    client: Client, url: str, current: ClinicalDocumentVersion
) -> ClinicalDocumentVersion:
    """Amend the finalized version, save the corrected body and finalize it."""
    assert (
        post(
            client,
            url,
            action="amend",
            version_id=current.pk,
            reason="Correção sintética",
        )
        == 302
    )
    amendment: ClinicalDocumentVersion = client.get(url).context["version"]
    assert amendment.state == "draft"
    assert amendment.amendment_of_id == current.pk
    assert (
        post(
            client,
            url,
            action="save",
            version_id=amendment.pk,
            revision=1,
            **AMENDED,
        )
        == 302
    )
    assert (
        post(
            client,
            url,
            action="finalize",
            version_id=amendment.pk,
            revision=2,
        )
        == 302
    )
    return amendment


def test_http_finalize_step_up_amend_review_and_close(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    url = f"/ehr/clinics/{graph.clinic_a}/encounter/"
    with physician_client(graph) as client:
        assert post(client, url, action="open", appointment_id=appointment.pk) == 302
        assert post(client, url, action="template", template_id=template.pk) == 302
        page = client.get(url)
        version = page.context["version"]
        assert (
            post(
                client,
                url,
                action="save",
                version_id=version.pk,
                revision=1,
                **CONTENT,
            )
            == 302
        )

        # Missing step-up is denied and audited; the draft stays a draft.
        clear_freshness(client)
        denied = client.post(
            url,
            {"action": "finalize", "version_id": version.pk, "revision": 2},
        )
        assert denied.status_code == 302
        assert denied.headers["Location"].startswith("/auth/step-up/")
        device = get_totp_device(graph.physician, confirmed=True)
        seed_freshness(client, device, verified_at=int(time.time()))
        assert (
            post(
                client,
                url,
                action="finalize",
                version_id=version.pk,
                revision=2,
            )
            == 302
        )
        page = client.get(url)
        current = page.context["version"]
        assert current.state == "finalized"
        assert current.content_digest
        # A repeated finalize POST is idempotent.
        assert (
            post(
                client,
                url,
                action="finalize",
                version_id=version.pk,
                revision=2,
            )
            == 302
        )
        # A stale save over the finalized version is a 409, never a 500, and
        # the rendered page keeps the failed edits visible as unsaved.
        _assert_unsaved_conflict(
            client, url, current, "Edição não salva sobre versão finalizada"
        )
        # Amendments need a reason; the base is preserved and linked.
        assert (
            post(client, url, action="amend", version_id=current.pk, reason=" ") == 400
        )
        amendment = _finalize_amendment(client, url, current)
        # The stale base and stale saves are conflicts; the lineage is readable.
        assert (
            post(
                client,
                url,
                action="amend",
                version_id=current.pk,
                reason="Base antiga",
            )
            == 409
        )
        assert (
            post(
                client,
                url,
                action="save",
                version_id=amendment.pk,
                revision=2,
                **CONTENT,
            )
            == 409
        )
        # The superseded original's stale editor gets the same 409 conflict,
        # never a 500, and the rendered page keeps the failed edits visible
        # as unsaved while the preserved stored content stays readable.
        _assert_unsaved_conflict(client, url, current, "Edição do editor antigo")
        encounter_id = page.context["encounter"].pk
        assert (
            post(
                client,
                url,
                action="review",
                encounter_id=encounter_id,
                version_id=current.pk,
            )
            == 302
        )
        page = client.get(url)
        review = page.context["review"]
        assert review.pk == current.pk
        assert review.state == "superseded"
        assert review.subjective == CONTENT["subjective"]
        assert review.content_digest == current.content_digest
        assert post(client, url, action="current") == 302

        # Close is idempotent; amendments still work on the closed encounter.
        assert post(client, url, action="close", encounter_id=encounter_id) == 302
        page = client.get(url)
        assert page.context["encounter"].state == "closed"
        assert post(client, url, action="close", encounter_id=encounter_id) == 302
        assert (
            post(
                client,
                url,
                action="amend",
                version_id=amendment.pk,
                reason="Retificação final",
            )
            == 302
        )
        assert post(client, url, action="invalid") == 403
    with setup_context(graph.organization_a):
        assert (
            AuditEvent.objects.filter(
                event_type="ehr.access.denied",
                payload__reason_code="step_up_required",
            ).count()
            == 1
        )
        assert AuditEvent.objects.filter(event_type="ehr.encounter.closed").count() == 1
        assert AuditEvent.objects.filter(event_type="ehr.document.amended").count() == 2
        lineage = list(
            ClinicalDocumentVersion.objects.order_by("version").values_list(
                "version", "state"
            )
        )
        assert lineage == [
            (1, "superseded"),
            (2, "finalized"),
            (3, "draft"),
        ]


def test_raw_insert_and_update_guards(rbac_graph: RbacGraph) -> None:
    graph = rbac_graph
    appointment, template = seed(graph)
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        version = saved_draft(graph, appointment, template)
        finalize_version(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=2,
            request=request,
        )
        document_id = version.document_id
        # Direct inserts cannot skip the amendment contract or the sequence.
        for bad in (
            {"version": 99, "amendment_of_id": version.pk},
            {"version": 2, "amendment_of_id": None},
            {"version": 2, "amendment_of_id": version.pk, "amendment_reason": ""},
        ):
            fields = {
                "organization_id": version.organization_id,
                "document_id": document_id,
                "template_id": version.template_id,
                "author_id": graph.physician,
                "amendment_reason": "motivo",
                **bad,
            }
            with pytest.raises(DatabaseError), transaction.atomic():
                ClinicalDocumentVersion.objects.create(**fields)
        assert ClinicalDocumentVersion.objects.count() == 1
