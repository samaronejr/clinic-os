"""Plan item 27 addendum drafts: other clinicians write beside the main draft.

Real policies and triggers as ``clinic_app``; permission expectations derive
from ``BUNDLES_V2`` over the full stored role catalog, a statement spy pins
the exact permission name, races use ``Barrier``.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.ehr.addenda import autosave_addendum, open_addendum, view_addendum
from apps.ehr.autosave import (
    AutosaveIdempotencyError,
    AutosaveResult,
    AutosaveStatus,
)
from apps.ehr.finalization import close_encounter, finalize_version
from apps.ehr.models import (
    AddendumSaveReceipt,
    ClinicalDocumentVersion,
    DraftSaveReceipt,
    Encounter,
    EncounterAddendum,
)
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.identity.models import (
    CareTeamMembership,
    ProfessionalRegistration,
    User,
    UserClinicRole,
)
from apps.intake.models import Patient, PatientClinicEnrollment
from django.contrib.auth.hashers import make_password
from django.db import DatabaseError, connection, connections, transaction
from django.test import Client
from django.utils import timezone

from auth.stepup_test_support import verified_request
from ehr.test_autosave import (
    ROLES,
    SECTIONS,
    SENTINEL,
    as_actor,
    asked_permissions,
    count,
    draft_world,
    open_walk_in,
    permitted,
    registration,
    save,
    stored,
)
from identity.permission_support import owner_context, permission_context
from otp_test_support import create_totp_device, fixed_otp_time, token_for
from otp_test_support import runtime_role as http_runtime_role
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

COUNCIL = {"physician": "CRM", "nurse": "COREN", "allied_professional": "CRP"}
TEXT = f"Complemento sintético {SENTINEL}"


def enrollment_of(graph: RbacGraph, patient: UUID, clinic: UUID) -> UUID:
    with owner_context(graph.organization_a):
        found = PatientClinicEnrollment.objects.filter(
            patient_id=patient, clinic_id=clinic
        ).first()
        if found is None:
            found = PatientClinicEnrollment.objects.create(
                organization_id=graph.organization_a,
                clinic_id=clinic,
                patient_id=patient,
                idempotency_key=uuid4(),
                create_fingerprint=b"s" * 32,
            )
        return found.pk


def clinician(
    graph: RbacGraph, role: str, enrolled: UUID, clinic: UUID | None = None
) -> UUID:
    """A clinic member of ``role``; clinical roles are registered care-team members."""
    clinic = clinic or graph.clinic_a
    user = User.objects.create(
        username=f"sintetico-addendum-{uuid4().hex}",
        password=make_password(RBAC_RAW_CREDENTIAL),
    )
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a, clinic_id=clinic, user=user, role=role
        )
        if role in COUNCIL:
            ProfessionalRegistration.objects.create(
                organization_id=graph.organization_a,
                clinic_id=clinic,
                user=user,
                role=role,
                council=COUNCIL[role],
                number="SINTETICO-127",
                jurisdiction="SP" if clinic == graph.clinic_a else "RJ",
                specialty="Sintetico",
                status="regular",
                valid_from=timezone.now() - timedelta(days=1),
                valid_to=timezone.now() + timedelta(days=1),
            )
        if role not in COUNCIL:
            # Care teams hold clinical roles only (identity_careteam_clinical_role).
            return user.pk
        CareTeamMembership.objects.create(
            organization_id=graph.organization_a,
            clinic_id=clinic,
            patient_enrollment_id=enrolled,
            user=user,
            role=role,
            valid_from=timezone.now() - timedelta(days=1),
            valid_to=timezone.now() + timedelta(days=1),
        )
    return user.pk


def join(graph: RbacGraph, user: UUID, role: str, clinic: UUID, enrolled: UUID) -> None:
    """Give an existing clinician the same standing in another clinic."""
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=clinic,
            user_id=user,
            role=role,
        )
    registration(graph, user, clinic)
    with owner_context(graph.organization_a):
        CareTeamMembership.objects.create(
            organization_id=graph.organization_a,
            clinic_id=clinic,
            patient_enrollment_id=enrolled,
            user_id=user,
            role=role,
            valid_from=timezone.now() - timedelta(days=1),
            valid_to=timezone.now() + timedelta(days=1),
        )


def main_draft(graph: RbacGraph, version: UUID) -> tuple[int, str, object]:
    with owner_context(graph.organization_a):
        row = ClinicalDocumentVersion.objects.get(pk=version)
        return row.revision, row.content_sha256, row.updated_at


def audit_count(graph: RbacGraph) -> int:
    with owner_context(graph.organization_a):
        return AuditEvent.objects.count()


def write(  # noqa: PLR0913 - test shorthand for the command keyword contract
    graph: RbacGraph,
    addendum: UUID,
    expected: int,
    text: str = TEXT,
    *,
    command: UUID | None = None,
    clinic: UUID | None = None,
) -> AutosaveResult:
    return autosave_addendum(
        clinic_id=clinic or graph.clinic_a,
        addendum_id=addendum,
        expected_revision=expected,
        editor_command_id=command or uuid4(),
        text=text,
    )


# --------------------------------------------------------------------------
# Permission: exact name, every catalog role, actor states; no side effects.
# --------------------------------------------------------------------------


def test_addendum_open_is_decided_by_clinical_write(rbac_graph: RbacGraph) -> None:
    version = draft_world(rbac_graph)
    encounter = version.document.encounter
    enrolled = enrollment_of(rbac_graph, encounter.patient_id, rbac_graph.clinic_a)
    assert permitted("clinical.write") == {"physician"}
    before = audit_count(rbac_graph)
    hidden: set[str] = set()
    for role in ROLES:
        actor = clinician(rbac_graph, role, enrolled)
        with permission_context(rbac_graph, actor):
            # Roles the encounter read policy hides get the unknown-record
            # denial before any permission question; the rest ask exactly one.
            visible = Encounter.objects.filter(pk=encounter.pk).exists()
        with permission_context(rbac_graph, actor), asked_permissions() as asked:
            if role in permitted("clinical.write"):
                addendum = open_addendum(
                    clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk
                )
                assert (addendum.author_id, addendum.revision) == (actor, 1)
                assert (addendum.encounter_id, addendum.patient_id) == (
                    encounter.pk,
                    encounter.patient_id,
                )
            else:
                with pytest.raises(ClinicalAccessDeniedError):
                    open_addendum(
                        clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk
                    )
        assert asked == (["clinical.write"] if visible else []), role
        hidden.update(set() if visible else {role})
    # Physicians and every staff role the encounter policy admits reach it.
    assert hidden == {
        "nurse",
        "allied_professional",
        "scheduler",
        "clinic_manager",
        "finance",
        "org_admin",
    }
    # Refusals wrote nothing: one addendum and its one "opened" audit event.
    assert count(rbac_graph, EncounterAddendum) == 1
    assert audit_count(rbac_graph) == before + 1


def test_addendum_refusals_cover_actor_states_scope_and_the_assignee(
    rbac_graph: RbacGraph,
) -> None:
    version = draft_world(rbac_graph)
    encounter = version.document.encounter
    enrolled = enrollment_of(rbac_graph, encounter.patient_id, rbac_graph.clinic_a)
    inactive = clinician(rbac_graph, "physician", enrolled)
    User.objects.filter(pk=inactive).update(is_active=False)
    elsewhere = clinician(
        rbac_graph,
        "physician",
        enrollment_of(rbac_graph, encounter.patient_id, rbac_graph.clinic_b),
        rbac_graph.clinic_b,
    )
    outsider = User.objects.create(username=f"sintetico-none-{uuid4().hex}").pk
    unregistered_colleague = User.objects.create(username=f"sintetico-u-{uuid4().hex}")
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user=unregistered_colleague,
            role="physician",
        )
    before = audit_count(rbac_graph)
    for actor in (inactive, elsewhere, outsider, unregistered_colleague.pk):
        with (
            permission_context(rbac_graph, actor),
            pytest.raises(ClinicalAccessDeniedError),
        ):
            open_addendum(clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk)
    colleague = clinician(rbac_graph, "physician", enrolled)
    with permission_context(rbac_graph, colleague):
        for clinic, target in (
            (rbac_graph.clinic_b, encounter.pk),
            (rbac_graph.clinic_c, encounter.pk),
            (rbac_graph.clinic_a, uuid4()),
        ):
            with pytest.raises(ClinicalAccessDeniedError):
                open_addendum(clinic_id=clinic, encounter_id=target)
    assert count(rbac_graph, EncounterAddendum) == 0
    assert audit_count(rbac_graph) == before
    # The assigned physician writes the main draft, never an addendum.
    registration(rbac_graph, rbac_graph.physician, rbac_graph.clinic_a)
    with (
        as_actor(rbac_graph, rbac_graph.physician),
        pytest.raises(ClinicalConflictError, match="use_main_draft"),
    ):
        open_addendum(clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk)
    # A closed encounter takes no new addendum.
    opener = clinician(rbac_graph, "physician", enrolled)
    with as_actor(rbac_graph, opener):
        visit = open_walk_in(rbac_graph, enrolled)
        close_encounter(clinic_id=rbac_graph.clinic_a, encounter_id=visit.pk)
    with (
        as_actor(rbac_graph, colleague),
        pytest.raises(ClinicalConflictError, match="encounter_closed"),
    ):
        open_addendum(clinic_id=rbac_graph.clinic_a, encounter_id=visit.pk)
    assert count(rbac_graph, EncounterAddendum) == 0


# --------------------------------------------------------------------------
# Autosave contract: CAS, idempotency, compare; the main draft never moves.
# --------------------------------------------------------------------------


def test_addendum_autosave_keeps_cas_idempotency_and_the_main_draft(
    rbac_graph: RbacGraph,
) -> None:
    version = draft_world(rbac_graph)
    encounter = version.document.encounter
    enrolled = enrollment_of(rbac_graph, encounter.patient_id, rbac_graph.clinic_a)
    colleague = clinician(rbac_graph, "physician", enrolled)
    with as_actor(rbac_graph, rbac_graph.physician):
        save(rbac_graph, version.pk, 1)
    main_before = main_draft(rbac_graph, version.pk)
    command = uuid4()
    with as_actor(rbac_graph, colleague):
        addendum = open_addendum(
            clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk
        )
        again = open_addendum(clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk)
        assert again.pk == addendum.pk
        first = write(rbac_graph, addendum.pk, 1, command=command)
        assert (first.status, first.revision) == (AutosaveStatus.SAVED, 2)
        replay = write(rbac_graph, addendum.pk, 1, command=command)
        assert (replay.status, replay.revision) == (AutosaveStatus.REPLAYED, 2)
        with pytest.raises(AutosaveIdempotencyError):
            write(rbac_graph, addendum.pk, 1, "Outro texto", command=command)
        stale = write(rbac_graph, addendum.pk, 1, "Linha nova")
        assert (stale.status, stale.revision) == (AutosaveStatus.CONFLICT, 2)
        assert [(line.kind, line.text) for line in stale.diff[0].lines] == [
            ("removed", TEXT),
            ("added", "Linha nova"),
        ]
        merged = write(rbac_graph, addendum.pk, 2, f"{TEXT}\nLinha nova")
        assert (merged.status, merged.revision) == (AutosaveStatus.SAVED, 3)
        same = write(rbac_graph, addendum.pk, 3, f"{TEXT}\nLinha nova")
        assert (same.status, same.revision) == (AutosaveStatus.SAVED, 3)
        assert view_addendum(
            clinic_id=rbac_graph.clinic_a, addendum_id=addendum.pk
        ).text == (f"{TEXT}\nLinha nova")
    assert main_draft(rbac_graph, version.pk) == main_before
    assert stored(rbac_graph, version.pk) == (2, SECTIONS)
    # The main author keeps writing the main draft independently.
    with as_actor(rbac_graph, rbac_graph.physician):
        assert (
            save(rbac_graph, version.pk, 2, sections={**SECTIONS, "plan": "P"}).revision
            == 3
        )
    with owner_context(rbac_graph.organization_a):
        assert EncounterAddendum.objects.get(pk=addendum.pk).revision == 3
        receipts = list(
            AddendumSaveReceipt.objects.order_by("base_revision").values_list(
                "base_revision", "revision"
            )
        )
        payloads = list(AuditEvent.objects.values_list("event_type", "payload"))
    assert receipts == [(1, 2), (2, 3), (3, 3)]
    assert [kind for kind, _ in payloads].count("ehr.addendum.saved") == 2
    assert all(SENTINEL not in json.dumps(payload) for _, payload in payloads)


def test_two_tabs_racing_one_addendum_revision_save_once(
    rbac_graph: RbacGraph,
) -> None:
    version = draft_world(rbac_graph)
    encounter = version.document.encounter
    enrolled = enrollment_of(rbac_graph, encounter.patient_id, rbac_graph.clinic_a)
    colleague = clinician(rbac_graph, "physician", enrolled)
    with as_actor(rbac_graph, colleague):
        addendum = open_addendum(
            clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk
        )
    barrier = Barrier(2, timeout=15)

    def attempt(text: str) -> str:
        try:
            with as_actor(rbac_graph, colleague):
                barrier.wait()
                return write(rbac_graph, addendum.pk, 1, text).status.value
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(attempt, ("Aba A", "Aba B")))
    assert results == ["conflict", "saved"]
    assert count(rbac_graph, AddendumSaveReceipt) == 1


# --------------------------------------------------------------------------
# Record scope and relations in Python and SQL, including direct writes.
# --------------------------------------------------------------------------


def colleague_direct_writes_refused(  # noqa: PLR0913 - one scene's actors and rows
    graph: RbacGraph,
    colleague: UUID,
    other: UUID,
    base: dict[str, UUID],
    stranger: UUID,
    ids: tuple[UUID, UUID],
) -> None:
    """Direct ``clinic_app`` writes by the addendum's own author stay bounded."""
    addendum_pk, version_pk = ids
    with permission_context(graph, colleague):
        for change in (
            {"author_id": other},
            {"clinic_id": graph.clinic_b},
            {"patient_id": stranger},
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                EncounterAddendum.objects.create(
                    **{**base, "author_id": colleague, **change}
                )
        # The main draft is outside every addendum author's write path.
        assert (
            ClinicalDocumentVersion.objects.filter(pk=version_pk).update(revision=9)
            == 0
        )
        for change in ({"author_id": other}, {"encounter_id": uuid4()}):
            with pytest.raises(DatabaseError), transaction.atomic():
                EncounterAddendum.objects.filter(pk=addendum_pk).update(**change)
        with pytest.raises(DatabaseError), transaction.atomic():
            EncounterAddendum.objects.filter(pk=addendum_pk).update(revision=5)
        with pytest.raises(DatabaseError), transaction.atomic():
            AddendumSaveReceipt.objects.create(
                organization_id=graph.organization_a,
                addendum_id=addendum_pk,
                command_id=uuid4(),
                request_sha256="0" * 64,
                base_revision=2,
                revision=3,
                saved_at=timezone.now(),
            )
        receipt = AddendumSaveReceipt.objects.get(addendum_id=addendum_pk)
        with pytest.raises(DatabaseError), transaction.atomic():
            AddendumSaveReceipt.objects.filter(pk=receipt.pk).update(revision=7)
        with pytest.raises(DatabaseError), transaction.atomic():
            EncounterAddendum.objects.filter(pk=addendum_pk).delete()


def owner_inserts_refused(
    graph: RbacGraph, row: dict[str, object], changes: tuple[dict[str, object], ...]
) -> None:
    for change in changes:
        with (
            owner_context(graph.organization_a),
            pytest.raises(DatabaseError),
            transaction.atomic(),
        ):
            EncounterAddendum.objects.create(**{**row, **change})


def test_addendum_scope_holds_in_python_and_sql(rbac_graph: RbacGraph) -> None:
    version = draft_world(rbac_graph)
    encounter = version.document.encounter
    enrolled = enrollment_of(rbac_graph, encounter.patient_id, rbac_graph.clinic_a)
    colleague = clinician(rbac_graph, "physician", enrolled)
    # The colleague has the same standing for the same patient in clinic B:
    # only the record's own clinic may decide.
    enrolled_b = enrollment_of(rbac_graph, encounter.patient_id, rbac_graph.clinic_b)
    join(rbac_graph, colleague, "physician", rbac_graph.clinic_b, enrolled_b)
    other = clinician(rbac_graph, "physician", enrolled)
    receptionist = clinician(rbac_graph, "receptionist", enrolled)
    with as_actor(rbac_graph, colleague):
        addendum = open_addendum(
            clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk
        )
        write(rbac_graph, addendum.pk, 1)
    audit_before = audit_count(rbac_graph)
    with as_actor(rbac_graph, colleague):
        with pytest.raises(ClinicalAccessDeniedError):
            open_addendum(clinic_id=rbac_graph.clinic_b, encounter_id=encounter.pk)
        with pytest.raises(ClinicalAccessDeniedError):
            write(rbac_graph, addendum.pk, 2, "Fora", clinic=rbac_graph.clinic_b)
        with pytest.raises(ClinicalAccessDeniedError):
            view_addendum(clinic_id=rbac_graph.clinic_b, addendum_id=addendum.pk)
    with as_actor(rbac_graph, other):
        # Another clinician neither sees nor writes this author's addendum.
        assert not EncounterAddendum.objects.filter(pk=addendum.pk).exists()
        with pytest.raises(ClinicalAccessDeniedError):
            write(rbac_graph, addendum.pk, 2, "Intruso")
    # Addendum refusals write nothing, not even an audit row.
    assert audit_count(rbac_graph) == audit_before
    base = {
        "organization_id": rbac_graph.organization_a,
        "clinic_id": rbac_graph.clinic_a,
        "encounter_id": encounter.pk,
        "patient_id": encounter.patient_id,
    }
    with owner_context(rbac_graph.organization_a):
        stranger = Patient.objects.create(
            organization_id=rbac_graph.organization_a,
            full_name="Sintetico Outro Paciente",
            birth_date=timezone.now().date() - timedelta(days=9000),
        ).pk
    enrollment_of(rbac_graph, stranger, rbac_graph.clinic_a)
    colleague_direct_writes_refused(
        rbac_graph, colleague, other, base, stranger, (addendum.pk, version.pk)
    )
    with (
        permission_context(rbac_graph, receptionist),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        EncounterAddendum.objects.create(**base, author_id=receptionist)
    registration(rbac_graph, rbac_graph.physician, rbac_graph.clinic_a)
    with (
        permission_context(rbac_graph, rbac_graph.physician),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        EncounterAddendum.objects.create(**base, author_id=rbac_graph.physician)
    # The binding trigger alone (owner writes skip runtime policies) keeps the
    # encounter's clinic and patient and refuses the assignee.
    owner_inserts_refused(
        rbac_graph,
        {**base, "author_id": other},
        (
            {"clinic_id": rbac_graph.clinic_b},
            {"patient_id": stranger},
            {"author_id": rbac_graph.physician},
        ),
    )
    assert count(rbac_graph, EncounterAddendum) == 1
    assert main_draft(rbac_graph, version.pk)[0] == 1


def test_closed_encounter_refuses_every_new_save_before_its_receipt(
    rbac_graph: RbacGraph,
) -> None:
    """Round-2 B2: an unchanged save after closure created a new receipt.

    A finalized main note leaves the encounter open, and addenda still save
    (record contract). Once the encounter is closed, every new command is
    refused before its receipt, changed or unchanged, in the service, through
    the API and at the database guard; replaying an acknowledged command stays
    a read. The main draft's own commands are refused as non-draft.
    """
    version = draft_world(rbac_graph)
    encounter = version.document.encounter
    enrolled = enrollment_of(rbac_graph, encounter.patient_id, rbac_graph.clinic_a)
    colleague = clinician(rbac_graph, "physician", enrolled)
    request = verified_request(rbac_graph.physician)
    with as_actor(rbac_graph, rbac_graph.physician):
        save(rbac_graph, version.pk, 1)
        finalize_version(
            clinic_id=rbac_graph.clinic_a,
            version_id=version.pk,
            expected_revision=2,
            request=request,
        )
    acknowledged = uuid4()
    with as_actor(rbac_graph, colleague):
        addendum = open_addendum(
            clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk
        )
        saved = write(rbac_graph, addendum.pk, 1, command=acknowledged)
    assert (saved.status, saved.revision) == (AutosaveStatus.SAVED, 2)
    with as_actor(rbac_graph, rbac_graph.physician):
        close_encounter(clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk)
    receipts = (
        count(rbac_graph, AddendumSaveReceipt),
        count(rbac_graph, DraftSaveReceipt),
    )
    with owner_context(rbac_graph.organization_a):
        mark = AuditEvent.objects.order_by("-pk").values_list("pk", flat=True)[0]
    with as_actor(rbac_graph, colleague):
        for text in (TEXT, "Depois do encerramento"):
            with pytest.raises(ClinicalConflictError, match="encounter_closed"):
                write(rbac_graph, addendum.pk, 2, text)
        replay = write(rbac_graph, addendum.pk, 1, command=acknowledged)
    assert (replay.status, replay.revision) == (AutosaveStatus.REPLAYED, 2)
    with as_actor(rbac_graph, rbac_graph.physician):
        for sections in (SECTIONS, {**SECTIONS, "plan": "Depois"}):
            with pytest.raises(ClinicalConflictError, match="precondition_failed"):
                save(rbac_graph, version.pk, 2, sections=sections)
    with signed_in(colleague) as client:
        unchanged = client.post(
            "/api/ui/v1/ehr/addendum/autosave/",
            json.dumps(
                {
                    "clinic_id": str(rbac_graph.clinic_a),
                    "addendum_id": str(addendum.pk),
                    "expected_revision": 2,
                    "editor_command_id": str(uuid4()),
                    "text": TEXT,
                }
            ),
            "application/json",
        )
    assert unchanged.status_code == 412
    # The database decides the same binding when the service is bypassed.
    with (
        as_actor(rbac_graph, colleague),
        pytest.raises(DatabaseError, match="invalid addendum receipt"),
        transaction.atomic(),
    ):
        AddendumSaveReceipt.objects.create(
            organization_id=rbac_graph.organization_a,
            addendum_id=addendum.pk,
            command_id=uuid4(),
            request_sha256="0" * 64,
            base_revision=2,
            revision=2,
            saved_at=timezone.now(),
        )
    with as_actor(rbac_graph, colleague):
        closed_row = EncounterAddendum.objects.get(pk=addendum.pk)
    closed_row.text = "Depois do encerramento"
    closed_row.text_sha256 = "1" * 64
    closed_row.revision = 3
    with (
        as_actor(rbac_graph, colleague),
        pytest.raises(DatabaseError, match="invalid addendum binding"),
        transaction.atomic(),
    ):
        closed_row.save(update_fields=("text", "text_sha256", "revision"))
    assert (
        count(rbac_graph, AddendumSaveReceipt),
        count(rbac_graph, DraftSaveReceipt),
    ) == receipts
    with owner_context(rbac_graph.organization_a):
        added = list(
            AuditEvent.objects.filter(pk__gt=mark)
            .order_by("pk")
            .values_list("event_type", flat=True)
        )
    # Nothing: no receipt, no saved event and no read audit on either draft.
    assert added == []
    with owner_context(rbac_graph.organization_a):
        assert EncounterAddendum.objects.get(pk=addendum.pk).revision == 2
    third = clinician(rbac_graph, "physician", enrolled)
    with (
        owner_context(rbac_graph.organization_a),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        EncounterAddendum.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            encounter_id=encounter.pk,
            patient_id=encounter.patient_id,
            author_id=third,
        )


# --------------------------------------------------------------------------
# HTTP: the UI API contract and identical refusals.
# --------------------------------------------------------------------------


@contextmanager
def signed_in(user_id: UUID) -> Iterator[Client]:
    username = User.objects.get(pk=user_id).username
    device = create_totp_device(user_id, confirmed=True)
    client = Client()
    with http_runtime_role(), fixed_otp_time():
        assert (
            client.post(
                "/auth/login/", {"username": username, "password": RBAC_RAW_CREDENTIAL}
            ).status_code
            == 302
        )
        assert (
            client.post(
                "/auth/verify/",
                {"otp_device": device.persistent_id, "otp_token": token_for(device)},
            ).status_code
            == 302
        )
        yield client


def test_addendum_api_contract_and_identical_refusals(rbac_graph: RbacGraph) -> None:
    version = draft_world(rbac_graph)
    encounter = version.document.encounter
    enrolled = enrollment_of(rbac_graph, encounter.patient_id, rbac_graph.clinic_a)
    colleague = clinician(rbac_graph, "physician", enrolled)
    with signed_in(colleague) as client:

        def post(path: str, body: dict[str, object]) -> _MonkeyPatchedWSGIResponse:
            return client.post(
                f"/api/ui/v1/ehr/addendum/{path}/", json.dumps(body), "application/json"
            )

        opened = post(
            "open",
            {"clinic_id": str(rbac_graph.clinic_a), "encounter_id": str(encounter.pk)},
        )
        assert opened.status_code == 200
        assert set(opened.json()) == {"addendum_id", "revision"}
        addendum = opened.json()["addendum_id"]
        body = {
            "clinic_id": str(rbac_graph.clinic_a),
            "addendum_id": addendum,
            "expected_revision": 1,
            "editor_command_id": str(uuid4()),
            "text": TEXT,
        }
        saved = post("autosave", body)
        assert saved.status_code == 200
        assert "no-store" in saved.headers["Cache-Control"]
        assert set(saved.json()) == {"revision", "saved_at"}
        assert saved.json()["revision"] == 2
        assert saved.json()["saved_at"].endswith("-03:00")
        stale = post(
            "autosave", {**body, "editor_command_id": str(uuid4()), "text": "X"}
        )
        assert stale.status_code == 409
        assert set(stale.json()) == {"current_revision", "diff"}
        denial = b'{"code":"access_denied","message_key":"api.error.access_denied"}'
        refusals = [
            post("autosave", {**body, "addendum_id": str(uuid4())}),
            post("autosave", {**body, "clinic_id": str(rbac_graph.clinic_b)}),
            post("autosave", {**body, "clinic_id": str(rbac_graph.clinic_c)}),
            post(
                "open",
                {"clinic_id": str(rbac_graph.clinic_a), "encounter_id": str(uuid4())},
            ),
            post(
                "open",
                {
                    "clinic_id": str(rbac_graph.clinic_b),
                    "encounter_id": str(encounter.pk),
                },
            ),
        ]
        assert {(r.status_code, r.content) for r in refusals} == {(403, denial)}
    with owner_context(rbac_graph.organization_a):
        assert EncounterAddendum.objects.get(pk=addendum).revision == 2
    assert count(rbac_graph, AddendumSaveReceipt) == 1


# --------------------------------------------------------------------------
# Exact posture of the addendum tables (the schema-policy test defers here).
# --------------------------------------------------------------------------

ADDENDUM_TABLES = {"ehr_encounteraddendum", "ehr_addendumsavereceipt"}


def test_addendum_tables_have_exact_rls_grants_and_triggers() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity, "
            "relowner::regrole::text FROM pg_class WHERE relname = ANY(%s)",
            [list(ADDENDUM_TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, True, True, "clinic_owner") for table in ADDENDUM_TABLES
        }
        cursor.execute(
            "SELECT tablename, policyname, cmd, roles::text FROM pg_policies "
            "WHERE schemaname = 'clinic_app' AND tablename = ANY(%s)",
            [list(ADDENDUM_TABLES)],
        )
        assert set(cursor.fetchall()) == {
            *((t, "setup_tenant", "ALL", "{clinic_owner}") for t in ADDENDUM_TABLES),
            ("ehr_encounteraddendum", "addendum_read", "SELECT", "{clinic_app}"),
            ("ehr_encounteraddendum", "addendum_insert", "INSERT", "{clinic_app}"),
            ("ehr_encounteraddendum", "addendum_write", "UPDATE", "{clinic_app}"),
            (
                "ehr_addendumsavereceipt",
                "addendum_receipt_read",
                "SELECT",
                "{clinic_app}",
            ),
            (
                "ehr_addendumsavereceipt",
                "addendum_receipt_insert",
                "INSERT",
                "{clinic_app}",
            ),
        }
        cursor.execute(
            "SELECT table_name, privilege_type "
            "FROM information_schema.role_table_grants WHERE grantee = 'clinic_app' "
            "AND table_schema = 'clinic_app' AND table_name = ANY(%s)",
            [list(ADDENDUM_TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (t, p) for t in ADDENDUM_TABLES for p in ("SELECT", "INSERT")
        }
        cursor.execute(
            "SELECT table_name, column_name FROM information_schema.role_column_grants "
            "WHERE grantee = 'clinic_app' AND privilege_type = 'UPDATE' "
            "AND table_schema = 'clinic_app' AND table_name = ANY(%s)",
            [list(ADDENDUM_TABLES)],
        )
        assert set(cursor.fetchall()) == {
            ("ehr_encounteraddendum", column)
            for column in ("text", "text_sha256", "revision", "updated_at")
        }
        cursor.execute(
            "SELECT c.relname, t.tgname, p.proname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_proc p ON p.oid = t.tgfoid "
            "WHERE NOT t.tgisinternal AND c.relname = ANY(%s)",
            [list(ADDENDUM_TABLES)],
        )
        assert set(cursor.fetchall()) == {
            *(
                (t, "ehr_addendum_binding", "ehr_addendum_guard")
                for t in ADDENDUM_TABLES
            ),
            *(
                (t, "ehr_addendum_immutable", "questionnaire_immutable")
                for t in ADDENDUM_TABLES
            ),
        }
