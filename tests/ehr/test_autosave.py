"""Plan item 27: durable autosave, conflict compare, episodes, unscheduled visits.

Everything runs as ``clinic_app`` against real policies and triggers. Clocks
are monkeypatched (``apps.ehr.autosave.utc_now``); races use ``Barrier``.
Permission expectations derive from ``BUNDLES_V2`` over the full stored role
catalog, and a statement spy pins the exact permission each guard asks.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.ehr import autosave as autosave_module
from apps.ehr import services
from apps.ehr.autosave import (
    LOCK_TTL,
    AutosaveIdempotencyError,
    AutosaveStatus,
    autosave_draft,
    section_diff,
    section_edit_epochs,
)
from apps.ehr.episodes import close_episode, link_encounter, open_episode
from apps.ehr.finalization import close_encounter, finalize_version
from apps.ehr.models import (
    ClinicalDocumentVersion,
    DraftEditState,
    DraftSaveReceipt,
    Encounter,
    Episode,
    EpisodeEncounter,
)
from apps.ehr.services import (
    SOAP_FIELDS,
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    create_draft,
    open_unscheduled_encounter,
    resume_encounter,
)
from apps.identity.current_context import CurrentActorError
from apps.identity.models import (
    CareTeamMembership,
    ProfessionalRegistration,
    RoleGrant,
    User,
    UserClinicRole,
)
from apps.identity.permissions import BUNDLES_V2
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.prescription.services import authorize_encounter
from apps.teleconsult.services import TeleconsultAccessDeniedError, create_session
from apps.tenancy.db import tenant_context
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, connections, transaction
from django.utils import timezone

from auth.stepup_test_support import verified_request
from identity.permission_support import (
    owner_context,
    permission_actor,
    permission_context,
)
from patient_service_support import runtime_role
from renewal.test_encounters import draft, physician_client, seed

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from datetime import datetime

    from django.db.models import Model
    from django.test.client import _MonkeyPatchedWSGIResponse

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

SENTINEL = "SINTETICO-SENTINELA-27"
SECTIONS: dict[str, str] = dict(
    zip(
        SOAP_FIELDS,
        (
            f"Relato sintético {SENTINEL}",
            "Exame sintético",
            "Avaliação sintética",
            "Plano sintético",
        ),
        strict=True,
    )
)
TAB_A = UUID(int=0xA27)
TAB_B = UUID(int=0xB27)
UNSCHEDULED = "encounter.open_unscheduled"
ROLES = tuple(UserClinicRole.Role.values)


# --------------------------------------------------------------------------
# Shared worlds and probes.
# --------------------------------------------------------------------------


@contextmanager
def as_actor(graph: RbacGraph, actor: UUID) -> Iterator[None]:
    with runtime_role(), tenant_context(actor, graph.organization_a):
        yield


def draft_world(graph: RbacGraph) -> ClinicalDocumentVersion:
    """A scheduled-bound SOAP draft (revision 1) of the legacy physician."""
    appointment, template = seed(graph)
    with as_actor(graph, graph.physician):
        return draft(graph, appointment, template)


def save(  # noqa: PLR0913 - test shorthand for the command's keyword contract
    graph: RbacGraph,
    version: UUID,
    expected: int,
    *,
    session: UUID = TAB_A,
    command: UUID | None = None,
    sections: dict[str, str] | None = None,
    handover: bool = False,
) -> autosave_module.AutosaveResult:
    return autosave_draft(
        clinic_id=graph.clinic_a,
        version_id=version,
        expected_revision=expected,
        editor_command_id=command or uuid4(),
        editor_session_id=session,
        sections=dict(sections or SECTIONS),
        request_handover=handover,
    )


def stored(graph: RbacGraph, version: UUID) -> tuple[int, dict[str, str]]:
    with owner_context(graph.organization_a):
        row = ClinicalDocumentVersion.objects.get(pk=version)
        return row.revision, row.soap


def count(graph: RbacGraph, model: type[Model], **filters: object) -> int:
    with owner_context(graph.organization_a):
        return model._default_manager.filter(**filters).count()


def audit_types(graph: RbacGraph) -> list[str]:
    with owner_context(graph.organization_a):
        return list(
            AuditEvent.objects.filter(event_type__startswith="ehr.")
            .order_by("pk")
            .values_list("event_type", flat=True)
        )


def assert_no_phi_in_audit(graph: RbacGraph) -> None:
    with owner_context(graph.organization_a):
        payloads = list(AuditEvent.objects.values_list("payload", flat=True))
    assert payloads
    assert all(SENTINEL not in json.dumps(payload) for payload in payloads)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[timedelta], datetime]:
    """Freeze the autosave clock; ``clock(delta)`` moves it from the origin."""
    origin = timezone.now().replace(microsecond=0)
    current = [origin]
    monkeypatch.setattr(autosave_module, "utc_now", lambda: current[0])

    def move(delta: timedelta) -> datetime:
        current[0] = origin + delta
        return current[0]

    return move


@contextmanager
def asked_permissions() -> Iterator[list[str]]:
    """Statement spy: every permission name sent to has_permission."""
    asked: list[str] = []

    def spy(
        execute: Callable[..., object],
        sql: str,
        params: Sequence[object] | None,
        many: bool,
        context: dict[str, object],
    ) -> object:
        if "clinic_app.has_permission(" in sql and params:
            asked.append(str(params[0]))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(spy):
        yield asked


def registration(graph: RbacGraph, user: UUID, clinic: UUID) -> None:
    """A current synthetic registration matching the clinic's CRM UF."""
    with owner_context(graph.organization_a):
        ProfessionalRegistration.objects.create(
            organization_id=graph.organization_a,
            clinic_id=clinic,
            user_id=user,
            role="physician",
            council="CRM",
            number="SINTETICO-027",
            jurisdiction="SP" if clinic == graph.clinic_a else "RJ",
            specialty="Sintetico",
            status="regular",
            valid_from=timezone.now() - timedelta(days=1),
            valid_to=timezone.now() + timedelta(days=1),
        )


def enrollment(graph: RbacGraph, clinic: UUID) -> UUID:
    with owner_context(graph.organization_a):
        patient = Patient.objects.create(
            organization_id=graph.organization_a,
            full_name="Sintetico Paciente Vinte Sete",
            birth_date=timezone.now().date() - timedelta(days=12000),
        )
        return PatientClinicEnrollment.objects.create(
            organization_id=graph.organization_a,
            clinic_id=clinic,
            patient=patient,
            idempotency_key=uuid4(),
            create_fingerprint=b"s" * 32,
        ).pk


def care_team(graph: RbacGraph, user: UUID, enrolled: UUID) -> None:
    with owner_context(graph.organization_a):
        CareTeamMembership.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            patient_enrollment_id=enrolled,
            user_id=user,
            role="physician",
            valid_from=timezone.now() - timedelta(days=1),
            valid_to=timezone.now() + timedelta(days=1),
        )


# --------------------------------------------------------------------------
# Autosave: CAS, acknowledgement, epochs, idempotency.
# --------------------------------------------------------------------------


def test_autosave_acknowledges_revisions_and_counts_section_epochs(
    rbac_graph: RbacGraph, clock: Callable[[timedelta], datetime]
) -> None:
    version = draft_world(rbac_graph)
    now = clock(timedelta(0))
    with as_actor(rbac_graph, rbac_graph.physician):
        first = save(rbac_graph, version.pk, 1)
        assert (first.status, first.revision, first.saved_at) == (
            AutosaveStatus.SAVED,
            2,
            now,
        )
        second = save(rbac_graph, version.pk, 2, sections={**SECTIONS, "plan": "Novo"})
        assert (second.status, second.revision) == (AutosaveStatus.SAVED, 3)
        # An unchanged autosave is acknowledged without a new revision.
        same = save(rbac_graph, version.pk, 3, sections={**SECTIONS, "plan": "Novo"})
        assert (same.status, same.revision) == (AutosaveStatus.SAVED, 3)
        assert section_edit_epochs(
            clinic_id=rbac_graph.clinic_a, version_id=version.pk
        ) == {"subjective": 1, "objective": 1, "assessment": 1, "plan": 2}
    assert stored(rbac_graph, version.pk) == (3, {**SECTIONS, "plan": "Novo"})
    with owner_context(rbac_graph.organization_a):
        receipts = list(
            DraftSaveReceipt.objects.order_by("base_revision").values_list(
                "base_revision", "revision"
            )
        )
        state = DraftEditState.objects.get(version_id=version.pk)
        assert (state.lock_holder, state.lock_expires_at) == (TAB_A, now + LOCK_TTL)
    assert receipts == [(1, 2), (2, 3), (3, 3)]
    assert audit_types(rbac_graph).count("ehr.document.saved") == 2
    assert_no_phi_in_audit(rbac_graph)


def test_replay_writes_once_and_a_reused_key_is_refused(rbac_graph: RbacGraph) -> None:
    version = draft_world(rbac_graph)
    command = uuid4()
    with as_actor(rbac_graph, rbac_graph.physician):
        first = save(rbac_graph, version.pk, 1, command=command)
        replay = save(rbac_graph, version.pk, 1, command=command)
        assert replay.status == AutosaveStatus.REPLAYED
        assert (replay.revision, replay.saved_at) == (first.revision, first.saved_at)
        later = save(rbac_graph, version.pk, 2, sections={**SECTIONS, "plan": "Depois"})
        assert later.revision == 3
        # A late duplicate of the first command still replays; never a conflict.
        assert save(rbac_graph, version.pk, 1, command=command).revision == 2
        with pytest.raises(AutosaveIdempotencyError):
            save(
                rbac_graph,
                version.pk,
                1,
                command=command,
                sections={**SECTIONS, "subjective": "Outro"},
            )
    assert stored(rbac_graph, version.pk)[0] == 3
    assert count(rbac_graph, DraftSaveReceipt, command_id=command) == 1
    assert audit_types(rbac_graph).count("ehr.document.saved") == 2


def test_concurrent_replay_of_one_command_increments_once(
    rbac_graph: RbacGraph,
) -> None:
    version = draft_world(rbac_graph)
    command = uuid4()
    barrier = Barrier(2, timeout=15)

    def attempt() -> tuple[str, int]:
        try:
            with as_actor(rbac_graph, rbac_graph.physician):
                barrier.wait()
                result = save(rbac_graph, version.pk, 1, command=command)
                return result.status.value, result.revision
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(lambda _: attempt(), range(2)))
    assert results == [("replayed", 2), ("saved", 2)]
    assert stored(rbac_graph, version.pk)[0] == 2
    assert count(rbac_graph, DraftSaveReceipt) == 1


def test_two_tabs_racing_one_revision_save_exactly_once(rbac_graph: RbacGraph) -> None:
    version = draft_world(rbac_graph)
    barrier = Barrier(2, timeout=15)
    texts = {TAB_A: "Aba A", TAB_B: "Aba B"}

    def attempt(session: UUID) -> tuple[str, int, UUID]:
        try:
            with as_actor(rbac_graph, rbac_graph.physician):
                barrier.wait()
                result = save(
                    rbac_graph,
                    version.pk,
                    1,
                    session=session,
                    sections={**SECTIONS, "subjective": texts[session]},
                )
                return result.status.value, result.revision, session
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(attempt, (TAB_A, TAB_B)))
    assert [row[:2] for row in results] == [("locked_by_other", 2), ("saved", 2)]
    winner = results[1][2]
    revision, content = stored(rbac_graph, version.pk)
    assert (revision, content["subjective"]) == (2, texts[winner])
    assert count(rbac_graph, DraftSaveReceipt) == 1


def test_stale_revision_compares_and_never_overwrites(
    rbac_graph: RbacGraph, clock: Callable[[timedelta], datetime]
) -> None:
    version = draft_world(rbac_graph)
    theirs = {**SECTIONS, "assessment": "Linha comum\nLinha da aba A"}
    mine = {**SECTIONS, "assessment": "Linha comum\nLinha da aba B"}
    clock(timedelta(0))
    with as_actor(rbac_graph, rbac_graph.physician):
        save(rbac_graph, version.pk, 1, sections=theirs)
        clock(LOCK_TTL)
        stale = save(rbac_graph, version.pk, 1, session=TAB_B, sections=mine)
        assert (stale.status, stale.revision, stale.saved_at) == (
            AutosaveStatus.CONFLICT,
            2,
            None,
        )
        assert [(d.section, d.theirs) for d in stale.diff] == [
            ("assessment", theirs["assessment"])
        ]
        assert [(line.kind, line.text) for line in stale.diff[0].lines] == [
            ("same", "Linha comum"),
            ("removed", "Linha da aba A"),
            ("added", "Linha da aba B"),
        ]
        assert stored(rbac_graph, version.pk) == (2, theirs)
        assert count(rbac_graph, DraftSaveReceipt) == 1
        # The conflicting tab now holds the lock; its explicit merge saves.
        assert save(rbac_graph, version.pk, 2).status == AutosaveStatus.LOCKED_BY_OTHER
        merged = save(rbac_graph, version.pk, 2, session=TAB_B, sections=mine)
        assert (merged.status, merged.revision) == (AutosaveStatus.SAVED, 3)
    assert stored(rbac_graph, version.pk) == (3, mine)


def test_section_diff_marks_saved_only_and_editor_only_lines() -> None:
    stored_text = dict.fromkeys(SOAP_FIELDS, "igual")
    diff = section_diff(
        {**stored_text, "plan": "a\nb\nc"}, {**stored_text, "plan": "a\nx\nc\nd"}
    )
    assert [(d.section, [(x.kind, x.text) for x in d.lines]) for d in diff] == [
        (
            "plan",
            [
                ("same", "a"),
                ("removed", "b"),
                ("added", "x"),
                ("same", "c"),
                ("added", "d"),
            ],
        )
    ]
    assert section_diff(stored_text, stored_text) == ()


# --------------------------------------------------------------------------
# Author lock: expiry by the monkeypatched clock and handover.
# --------------------------------------------------------------------------


def test_lock_refuses_other_tabs_until_it_expires(
    rbac_graph: RbacGraph, clock: Callable[[timedelta], datetime]
) -> None:
    version = draft_world(rbac_graph)
    clock(timedelta(0))
    with as_actor(rbac_graph, rbac_graph.physician):
        save(rbac_graph, version.pk, 1)
        # The holder's own autosave renews the two-minute lease.
        clock(timedelta(minutes=1))
        save(rbac_graph, version.pk, 2, sections={**SECTIONS, "plan": "Renovado"})
        clock(timedelta(minutes=1) + LOCK_TTL - timedelta(microseconds=1))
        locked = save(rbac_graph, version.pk, 3, session=TAB_B)
        assert (locked.status, locked.revision) == (AutosaveStatus.LOCKED_BY_OTHER, 3)
        assert stored(rbac_graph, version.pk)[1]["plan"] == "Renovado"
        assert count(rbac_graph, DraftSaveReceipt) == 2
        # The refusal wrote nothing: the holder and its lease are unchanged.
        with owner_context(rbac_graph.organization_a):
            state = DraftEditState.objects.get(version_id=version.pk)
            assert (state.lock_holder, state.handover_requested_by) == (TAB_A, None)
        clock(timedelta(minutes=1) + LOCK_TTL)
        acquired = save(rbac_graph, version.pk, 3, session=TAB_B)
        assert (acquired.status, acquired.revision) == (AutosaveStatus.SAVED, 4)
        assert save(rbac_graph, version.pk, 4).status == AutosaveStatus.LOCKED_BY_OTHER
    with owner_context(rbac_graph.organization_a):
        assert DraftEditState.objects.get(version_id=version.pk).lock_holder == TAB_B


def test_handover_moves_the_lock_after_the_holder_saves(
    rbac_graph: RbacGraph, clock: Callable[[timedelta], datetime]
) -> None:
    version = draft_world(rbac_graph)
    clock(timedelta(0))
    with as_actor(rbac_graph, rbac_graph.physician):
        save(rbac_graph, version.pk, 1)
        asked = save(rbac_graph, version.pk, 1, session=TAB_B, handover=True)
        assert asked.status == AutosaveStatus.HANDOVER_REQUESTED
        holder = save(rbac_graph, version.pk, 2, sections={**SECTIONS, "plan": "Final"})
        assert (holder.status, holder.revision, holder.handed_over) == (
            AutosaveStatus.SAVED,
            3,
            True,
        )
        assert save(rbac_graph, version.pk, 3).status == AutosaveStatus.LOCKED_BY_OTHER
        # The requester's stale text is compared, never written over.
        stale = save(rbac_graph, version.pk, 1, session=TAB_B)
        assert stale.status == AutosaveStatus.CONFLICT
        assert stored(rbac_graph, version.pk)[1]["plan"] == "Final"
    assert audit_types(rbac_graph).count("ehr.draft.handed_over") == 1
    with owner_context(rbac_graph.organization_a):
        state = DraftEditState.objects.get(version_id=version.pk)
        assert (state.lock_holder, state.handover_requested_by) == (TAB_B, None)


def test_autosave_refuses_every_non_author_and_writes_nothing(
    rbac_graph: RbacGraph,
) -> None:
    version = draft_world(rbac_graph)
    for role in ROLES:
        actor, _ = permission_actor(rbac_graph, role)
        with (
            permission_context(rbac_graph, actor),
            pytest.raises((ClinicalAccessDeniedError, CurrentActorError)),
        ):
            save(rbac_graph, version.pk, 1)
    # Foreign clinic scope, unknown draft and no membership share the denial.
    with as_actor(rbac_graph, rbac_graph.physician):
        with pytest.raises(ClinicalAccessDeniedError):
            autosave_draft(
                clinic_id=rbac_graph.clinic_b,
                version_id=version.pk,
                expected_revision=1,
                editor_command_id=uuid4(),
                editor_session_id=TAB_A,
                sections=dict(SECTIONS),
            )
        with pytest.raises(ClinicalAccessDeniedError):
            save(rbac_graph, uuid4(), 1)
        with pytest.raises(ValidationError):
            save(rbac_graph, version.pk, 1, sections={**SECTIONS, "extra": "x"})
    outsider = User.objects.create(username=f"sintetico-outsider-{uuid4().hex}")
    with (
        permission_context(rbac_graph, outsider.pk),
        pytest.raises((ClinicalAccessDeniedError, CurrentActorError)),
    ):
        save(rbac_graph, version.pk, 1)
    User.objects.filter(pk=rbac_graph.physician).update(is_active=False)
    with (
        permission_context(rbac_graph, rbac_graph.physician),
        pytest.raises((ClinicalAccessDeniedError, CurrentActorError)),
    ):
        save(rbac_graph, version.pk, 1)
    assert stored(rbac_graph, version.pk)[0] == 1
    assert count(rbac_graph, DraftSaveReceipt) == 0
    assert count(rbac_graph, DraftEditState) == 0


def test_runtime_writes_to_draft_state_and_receipts_fail_closed(
    rbac_graph: RbacGraph,
) -> None:
    version = draft_world(rbac_graph)
    request = verified_request(rbac_graph.physician)
    with as_actor(rbac_graph, rbac_graph.physician):
        save(rbac_graph, version.pk, 1)
    other, _ = permission_actor(rbac_graph, "physician")
    with permission_context(rbac_graph, other):
        # Not the author/assignee: nothing is visible or writable.
        assert not DraftEditState.objects.filter(version_id=version.pk).exists()
        assert (
            DraftEditState.objects.filter(version_id=version.pk).update(
                lock_holder=TAB_B
            )
            == 0
        )
        with pytest.raises(DatabaseError), transaction.atomic():
            DraftSaveReceipt.objects.create(
                organization_id=rbac_graph.organization_a,
                version_id=version.pk,
                command_id=uuid4(),
                request_sha256="0" * 64,
                base_revision=1,
                revision=2,
                saved_at=timezone.now(),
            )
    with as_actor(rbac_graph, rbac_graph.physician):
        state = DraftEditState.objects.get(version_id=version.pk)
        for epochs in (
            {**state.section_edit_epochs, "plan": 0},
            {**state.section_edit_epochs, "extra": 1},
            {**state.section_edit_epochs, "plan": "1"},
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                DraftEditState.objects.filter(pk=state.pk).update(
                    section_edit_epochs=epochs
                )
        with pytest.raises(DatabaseError), transaction.atomic():
            DraftEditState.objects.filter(pk=state.pk).update(author_id=other)
        with pytest.raises(DatabaseError), transaction.atomic():
            DraftSaveReceipt.objects.create(
                organization_id=rbac_graph.organization_a,
                version_id=version.pk,
                command_id=uuid4(),
                request_sha256="0" * 64,
                base_revision=2,
                revision=3,
                saved_at=timezone.now(),
            )
        receipt = DraftSaveReceipt.objects.get()
        for change in ({"revision": 5}, {"request_sha256": "1" * 64}):
            with pytest.raises(DatabaseError), transaction.atomic():
                DraftSaveReceipt.objects.filter(pk=receipt.pk).update(**change)
        with pytest.raises(DatabaseError), transaction.atomic():
            DraftSaveReceipt.objects.filter(pk=receipt.pk).delete()
        with pytest.raises(DatabaseError), transaction.atomic():
            DraftEditState.objects.filter(pk=state.pk).delete()
        finalize_version(
            clinic_id=rbac_graph.clinic_a,
            version_id=version.pk,
            expected_revision=2,
            request=request,
        )
        # A finalized version's draft state is outside every write path.
        assert (
            DraftEditState.objects.filter(pk=state.pk).update(
                lock_holder=None, lock_expires_at=None
            )
            == 0
        )
        with pytest.raises(ClinicalConflictError):
            save(rbac_graph, version.pk, 2)


# --------------------------------------------------------------------------
# Unscheduled encounters: exact permission, actor states, record scope.
# --------------------------------------------------------------------------


def permitted(permission: str) -> set[str]:
    return {role for role in ROLES if permission in BUNDLES_V2[role]}


def open_walk_in(
    graph: RbacGraph, enrolled: UUID, reason: str = "walk_in"
) -> Encounter:
    return open_unscheduled_encounter(
        clinic_id=graph.clinic_a, enrollment_id=enrolled, reason=reason
    )


def test_unscheduled_open_is_decided_by_its_exact_permission(
    rbac_graph: RbacGraph,
) -> None:
    assert permitted(UNSCHEDULED) == {"physician"}
    for role in ROLES:
        actor, enrolled = permission_actor(rbac_graph, role)
        with permission_context(rbac_graph, actor), asked_permissions() as asked:
            if role in permitted(UNSCHEDULED):
                encounter = open_walk_in(rbac_graph, enrolled)
                assert (
                    encounter.appointment_id,
                    encounter.unscheduled_reason,
                    encounter.physician_id,
                ) == (None, "walk_in", actor)
            else:
                with pytest.raises(ClinicalAccessDeniedError):
                    open_walk_in(rbac_graph, enrolled)
        assert asked == [UNSCHEDULED], role
    assert count(rbac_graph, Encounter, appointment__isnull=True) == 1
    assert audit_types(rbac_graph).count("ehr.encounter.opened_unscheduled") == 1


def test_unscheduled_refusals_cover_actor_states_and_record_scope(
    rbac_graph: RbacGraph,
) -> None:
    physician, enrolled = permission_actor(rbac_graph, "physician")
    unregistered = User.objects.create(username=f"sintetico-unreg-{uuid4().hex}")
    inactive, _ = permission_actor(rbac_graph, "physician")
    User.objects.filter(pk=inactive).update(is_active=False)
    foreign = User.objects.create(username=f"sintetico-foreign-{uuid4().hex}")
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user=unregistered,
            role="physician",
        )
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            user=foreign,
            role="physician",
        )
    registration(rbac_graph, foreign.pk, rbac_graph.clinic_b)
    outsider = User.objects.create(username=f"sintetico-none-{uuid4().hex}")
    for actor in (unregistered.pk, inactive, foreign.pk, outsider.pk):
        with (
            permission_context(rbac_graph, actor),
            pytest.raises(ClinicalAccessDeniedError),
        ):
            open_walk_in(rbac_graph, enrolled)
    other_clinic = enrollment(rbac_graph, rbac_graph.clinic_b)
    with permission_context(rbac_graph, physician):
        for target in (other_clinic, uuid4()):
            with pytest.raises(ClinicalAccessDeniedError):
                open_walk_in(rbac_graph, target)
        with pytest.raises(ValidationError):
            open_walk_in(rbac_graph, enrolled, reason="scheduled")
    assert count(rbac_graph, Encounter) == 0
    # A clinic subtraction of the v2 permission refuses it while the same
    # actor keeps clinical.write: the guard asks for exactly this permission.
    with owner_context(rbac_graph.organization_a):
        RoleGrant.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            role="physician",
            permission=UNSCHEDULED,
            bundle_version=2,
            valid_from=timezone.now() - timedelta(days=1),
        )
    with permission_context(rbac_graph, physician):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.has_permission('clinical.write', %s, %s), "
                "clinic_app.has_permission(%s, %s, NULL)",
                [rbac_graph.clinic_a, enrolled, UNSCHEDULED, rbac_graph.clinic_a],
            )
            assert cursor.fetchone() == (True, False)
        with pytest.raises(ClinicalAccessDeniedError):
            open_walk_in(rbac_graph, enrolled)
    assert count(rbac_graph, Encounter) == 0


def test_runtime_encounter_inserts_admit_only_the_two_binding_shapes(
    rbac_graph: RbacGraph,
) -> None:
    appointment, _ = seed(rbac_graph)
    base = {
        "organization_id": rbac_graph.organization_a,
        "clinic_id": rbac_graph.clinic_a,
        "appointment": None,
        "unscheduled_reason": "documentation_only",
        "patient_id": appointment.patient_id,
    }
    for role in ROLES:
        actor, _ = permission_actor(rbac_graph, role)
        with permission_context(rbac_graph, actor):
            try:
                with transaction.atomic():
                    Encounter.objects.create(**base, physician_id=actor)
            except DatabaseError:
                created = False
            else:
                created = True
        assert created is (role in permitted(UNSCHEDULED)), role
    registered, _ = permission_actor(rbac_graph, "physician")
    with owner_context(rbac_graph.organization_a):
        outside = Patient.objects.create(
            organization_id=rbac_graph.organization_a,
            full_name="Sintetico Sem Matricula",
            birth_date=timezone.now().date() - timedelta(days=9000),
        )
    with permission_context(rbac_graph, registered):
        for change in (
            {"unscheduled_reason": ""},
            {"unscheduled_reason": "scheduled"},
            {"physician_id": rbac_graph.physician},
            {"patient_id": outside.pk},
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                Encounter.objects.create(
                    **{**base, "physician_id": registered, **change}
                )
    # The legacy physician holds the role but no registration: the insert
    # policy's has_permission refuses the unscheduled shape.
    with (
        permission_context(rbac_graph, rbac_graph.physician),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        Encounter.objects.create(**base, physician_id=rbac_graph.physician)
    with owner_context(rbac_graph.organization_a):
        RoleGrant.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            role="physician",
            permission=UNSCHEDULED,
            bundle_version=2,
            valid_from=timezone.now() - timedelta(days=1),
        )
    with (
        permission_context(rbac_graph, registered),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        # A clinic subtraction of exactly this permission refuses the insert.
        Encounter.objects.create(**base, physician_id=registered)
    with (
        permission_context(rbac_graph, rbac_graph.physician),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        Encounter.objects.create(
            **{**base, "appointment": appointment, "unscheduled_reason": "walk_in"},
            physician_id=rbac_graph.physician,
        )
    assert count(rbac_graph, Encounter) == 1


def test_parallel_unscheduled_starts_converge_on_one_open_visit(
    rbac_graph: RbacGraph,
) -> None:
    physician, enrolled = permission_actor(rbac_graph, "physician")
    barrier = Barrier(2, timeout=15)

    def start() -> UUID:
        try:
            with as_actor(rbac_graph, physician):
                barrier.wait()
                return open_walk_in(rbac_graph, enrolled).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(lambda _: start(), range(2))
    assert first == second
    with as_actor(rbac_graph, physician):
        close_encounter(clinic_id=rbac_graph.clinic_a, encounter_id=first)
        assert open_walk_in(rbac_graph, enrolled).pk != first
    assert count(rbac_graph, Encounter) == 2


def test_unscheduled_encounter_carries_the_draft_lifecycle(
    rbac_graph: RbacGraph,
) -> None:
    _, template = seed(rbac_graph)
    physician, enrolled = permission_actor(rbac_graph, "physician")
    other, _ = permission_actor(rbac_graph, "physician")
    care_team(rbac_graph, other, enrolled)
    request = verified_request(physician)
    with as_actor(rbac_graph, physician):
        encounter = open_walk_in(rbac_graph, enrolled, "phone_follow_up")
        assert (
            resume_encounter(clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk)
            == encounter
        )
        version = create_draft(
            clinic_id=rbac_graph.clinic_a,
            encounter_id=encounter.pk,
            template_id=template.pk,
        )
        assert save(rbac_graph, version.pk, 1).revision == 2
    with as_actor(rbac_graph, other):
        with pytest.raises(ClinicalAccessDeniedError):
            resume_encounter(clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk)
        with pytest.raises(ClinicalAccessDeniedError):
            create_draft(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=encounter.pk,
                template_id=template.pk,
            )
        with pytest.raises(ClinicalAccessDeniedError):
            save(rbac_graph, version.pk, 2)
        with pytest.raises(ClinicalAccessDeniedError):
            services._encounter_actor(rbac_graph.clinic_a, encounter)
    with owner_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            user_id=physician,
            role="physician",
        )
    with as_actor(rbac_graph, physician):
        # Record scope: the encounter's own clinic, never the caller's claim,
        # even for a physician of the claimed clinic.
        with pytest.raises(ClinicalAccessDeniedError):
            services._encounter_actor(rbac_graph.clinic_b, encounter)
        with connection.cursor() as cursor:
            cursor.execute("SELECT clinic_app.ehr_assigned(%s)", [encounter.pk])
            assert cursor.fetchone() == (True,)
        # Prescribing and video stay bound to a booked appointment.
        with pytest.raises(ClinicalAccessDeniedError):
            authorize_encounter(
                clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk
            )
        with pytest.raises(TeleconsultAccessDeniedError):
            create_session(clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk)
        finalized = finalize_version(
            clinic_id=rbac_graph.clinic_a,
            version_id=version.pk,
            expected_revision=2,
            request=request,
        )
        assert finalized.state == "finalized"
        closed = close_encounter(
            clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk
        )
        assert closed.state == "closed"
        assert closed.unscheduled_reason == "phone_follow_up"
    # The close guard keeps every non-closure column, including the reason.
    with (
        owner_context(rbac_graph.organization_a),
        pytest.raises(DatabaseError),
        transaction.atomic(),
    ):
        Encounter.objects.filter(pk=encounter.pk).update(unscheduled_reason="walk_in")
    assert_no_phi_in_audit(rbac_graph)


# --------------------------------------------------------------------------
# Episodes: clinical.write, the patient/clinic/assignee relations, both layers.
# --------------------------------------------------------------------------


def test_open_episode_is_decided_by_clinical_write(rbac_graph: RbacGraph) -> None:
    assert permitted("clinical.write") == {"physician"}
    for role in ROLES:
        actor, enrolled = permission_actor(rbac_graph, role)
        with permission_context(rbac_graph, actor), asked_permissions() as asked:
            if role in permitted("clinical.write"):
                episode = open_episode(
                    clinic_id=rbac_graph.clinic_a,
                    enrollment_id=enrolled,
                    title=f"Hipertensão {SENTINEL}",
                )
                assert (episode.state, episode.opened_by_id) == ("open", actor)
            else:
                with pytest.raises(ClinicalAccessDeniedError):
                    open_episode(
                        clinic_id=rbac_graph.clinic_a,
                        enrollment_id=enrolled,
                        title="Recusado",
                    )
        assert asked == ["clinical.write"], role
    assert count(rbac_graph, Episode) == 1
    assert_no_phi_in_audit(rbac_graph)


def test_episode_links_decide_patient_clinic_assignee_and_state(
    rbac_graph: RbacGraph,
) -> None:
    owner_physician, enrolled_x = permission_actor(rbac_graph, "physician")
    colleague, _ = permission_actor(rbac_graph, "physician")
    care_team(rbac_graph, colleague, enrolled_x)
    enrolled_y = enrollment(rbac_graph, rbac_graph.clinic_a)
    care_team(rbac_graph, owner_physician, enrolled_y)
    with as_actor(rbac_graph, owner_physician):
        first = open_walk_in(rbac_graph, enrolled_x)
        episode = open_episode(
            clinic_id=rbac_graph.clinic_a, enrollment_id=enrolled_x, title="Diabetes"
        )
        link = link_encounter(
            clinic_id=rbac_graph.clinic_a, encounter_id=first.pk, episode_id=episode.pk
        )
        assert (
            link_encounter(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=first.pk,
                episode_id=episode.pk,
            ).pk
            == link.pk
        )
        other_patient = open_episode(
            clinic_id=rbac_graph.clinic_a, enrollment_id=enrolled_y, title="Outro"
        )
        with pytest.raises(ClinicalAccessDeniedError):
            link_encounter(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=first.pk,
                episode_id=other_patient.pk,
            )
        second = open_episode(
            clinic_id=rbac_graph.clinic_a, enrollment_id=enrolled_x, title="Segundo"
        )
        with pytest.raises(ClinicalConflictError, match="already_linked"):
            link_encounter(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=first.pk,
                episode_id=second.pk,
            )
        visit_y = open_walk_in(rbac_graph, enrolled_y)
        with pytest.raises(DatabaseError), transaction.atomic():
            EpisodeEncounter.objects.create(
                organization_id=rbac_graph.organization_a,
                episode=episode,
                encounter=visit_y,
                linked_by_id=owner_physician,
            )
        closed = close_episode(clinic_id=rbac_graph.clinic_a, episode_id=second.pk)
        assert (
            close_episode(clinic_id=rbac_graph.clinic_a, episode_id=second.pk).closed_at
            == closed.closed_at
        )
        close_encounter(clinic_id=rbac_graph.clinic_a, encounter_id=first.pk)
        later = open_walk_in(rbac_graph, enrolled_x, "documentation_only")
        with pytest.raises(ClinicalConflictError, match="episode_closed"):
            link_encounter(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=later.pk,
                episode_id=second.pk,
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            EpisodeEncounter.objects.create(
                organization_id=rbac_graph.organization_a,
                episode=second,
                encounter=later,
                linked_by_id=owner_physician,
            )
    with owner_context(rbac_graph.organization_a):
        patient_x = PatientClinicEnrollment.objects.get(pk=enrolled_x).patient_id
        PatientClinicEnrollment.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            patient_id=patient_x,
            idempotency_key=uuid4(),
            create_fingerprint=b"s" * 32,
        )
        foreign = Episode.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            patient_id=patient_x,
            title="Outra clínica",
            opened_by_id=owner_physician,
        )
    with as_actor(rbac_graph, owner_physician):
        with pytest.raises(ClinicalAccessDeniedError):
            link_encounter(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=later.pk,
                episode_id=foreign.pk,
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            EpisodeEncounter.objects.create(
                organization_id=rbac_graph.organization_a,
                episode=foreign,
                encounter=later,
                linked_by_id=owner_physician,
            )
    with as_actor(rbac_graph, colleague):
        # Care-team clinical.read sees the episode; only the assignee links.
        assert Episode.objects.filter(pk=episode.pk).exists()
        with pytest.raises(ClinicalAccessDeniedError):
            link_encounter(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=later.pk,
                episode_id=episode.pk,
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            EpisodeEncounter.objects.create(
                organization_id=rbac_graph.organization_a,
                episode=episode,
                encounter=later,
                linked_by_id=colleague,
            )
    # The binding trigger alone (owner writes skip every runtime policy)
    # refuses another patient's, another clinic's or a closed episode.
    for target, visit in ((episode, visit_y), (foreign, later), (second, later)):
        with (
            owner_context(rbac_graph.organization_a),
            pytest.raises(DatabaseError),
            transaction.atomic(),
        ):
            EpisodeEncounter.objects.create(
                organization_id=rbac_graph.organization_a,
                episode=target,
                encounter=visit,
                linked_by_id=owner_physician,
            )
    receptionist, _ = permission_actor(rbac_graph, "receptionist")
    with permission_context(rbac_graph, receptionist):
        assert not Episode.objects.exists()
        assert not EpisodeEncounter.objects.exists()
    assert count(rbac_graph, EpisodeEncounter) == 1


# --------------------------------------------------------------------------
# HTTP: the UI API contract and the workspace's explicit merge and start.
# --------------------------------------------------------------------------

AUTOSAVE_URL = "/api/ui/v1/ehr/autosave/"
NONCE = re.compile(rb'(nonce|value|content)="[^"]*"')


def test_ui_api_autosave_contract_and_identical_refusals(
    rbac_graph: RbacGraph,
) -> None:
    version = draft_world(rbac_graph)
    body = {
        "clinic_id": str(rbac_graph.clinic_a),
        "version_id": str(version.pk),
        "expected_revision": 1,
        "editor_command_id": str(uuid4()),
        "editor_session": str(TAB_A),
        "sections": SECTIONS,
        "handover": False,
    }

    with physician_client(rbac_graph) as client:

        def post(**changes: object) -> _MonkeyPatchedWSGIResponse:
            payload = {**body, **changes}
            return client.post(AUTOSAVE_URL, json.dumps(payload), "application/json")

        saved = post()
        assert saved.status_code == 200
        assert "no-store" in saved.headers["Cache-Control"]
        data = saved.json()
        assert set(data) == {"revision", "saved_at", "lock"}
        assert (data["revision"], data["lock"]) == (2, "held")
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d-03:00", data["saved_at"])
        assert post().content == saved.content
        stale = post(
            editor_command_id=str(uuid4()),
            sections={**SECTIONS, "plan": "Plano antigo"},
        )
        assert stale.status_code == 409
        conflict = stale.json()
        assert set(conflict) == {"current_revision", "diff"}
        assert conflict["current_revision"] == 2
        assert [item["section"] for item in conflict["diff"]] == ["plan"]
        locked = post(
            editor_command_id=str(uuid4()),
            editor_session=str(TAB_B),
            expected_revision=2,
        )
        assert (locked.status_code, locked.json()) == (
            423,
            {"code": "locked_by_other", "message_key": "api.error.locked_by_other"},
        )
        handover = post(
            editor_command_id=str(uuid4()),
            editor_session=str(TAB_B),
            expected_revision=2,
            handover=True,
        )
        assert (handover.status_code, handover.json()["code"]) == (
            423,
            "handover_requested",
        )
        reused = post(sections={**SECTIONS, "plan": "Outro"})
        assert (reused.status_code, reused.json()["code"]) == (
            422,
            "idempotency_mismatch",
        )
        refusals = [
            post(editor_command_id=str(uuid4()), version_id=str(uuid4())),
            post(editor_command_id=str(uuid4()), clinic_id=str(rbac_graph.clinic_b)),
            post(editor_command_id=str(uuid4()), clinic_id=str(rbac_graph.clinic_c)),
        ]
        assert {(r.status_code, r.content) for r in refusals} == {
            (403, b'{"code":"access_denied","message_key":"api.error.access_denied"}')
        }
        invalid = post(sections={**SECTIONS, "extra": "x"})
        assert (invalid.status_code, invalid.json()["code"]) == (400, "invalid_input")
    assert stored(rbac_graph, version.pk)[0] == 2
    assert count(rbac_graph, DraftSaveReceipt) == 1


def test_explicit_stale_save_shows_the_compare_and_merges_explicitly(
    rbac_graph: RbacGraph,
) -> None:
    appointment, template = seed(rbac_graph)
    url = f"/ehr/clinics/{rbac_graph.clinic_a}/encounter/"
    with physician_client(rbac_graph) as client:
        client.post(url, {"action": "open", "appointment_id": appointment.pk})
        client.post(url, {"action": "template", "template_id": template.pk})
        page = client.get(url)
        html = page.content.decode()
        assert 'data-autosave="/api/ui/v1/ehr/autosave/"' in html
        assert 'data-text-retrying="Não salvo - tentando novamente"' in html
        version = page.context["version"]
        payload = {
            "action": "save",
            "version_id": version.pk,
            "revision": 1,
            **SECTIONS,
        }
        assert client.post(url, payload).status_code == 302
        injected = '<script src="/x.js"></script> Plano da aba antiga'
        stale = client.post(url, {**payload, "plan": injected})
        assert stale.status_code == 409
        # Typed markup is data: the compare renders it inert.
        assert "<script" not in stale.content.decode().split("</header>", 1)[-1]
        assert "&lt;script src=&quot;/x.js&quot;&gt;" in stale.content.decode()
        conflict = stale.context["conflict"]
        assert conflict["current_revision"] == 2
        assert [section["key"] for section in conflict["sections"]] == ["plan"]
        body = stale.content.decode()
        assert 'id="conflict-plan"' in body
        assert 'data-state="conflict"' in body
        kept = client.get(url).context["version"]
        assert (kept.revision, kept.soap) == (2, SECTIONS)
        merge = {**payload, "action": "merge", "merge_revision": 2}
        assert client.post(url, {**merge, "plan": "Plano combinado"}).status_code == 302
        assert client.post(url, {**merge, "plan": "Atrasado"}).status_code == 409
    assert stored(rbac_graph, version.pk) == (
        3,
        {**SECTIONS, "plan": "Plano combinado"},
    )


def test_unscheduled_start_through_the_workspace(rbac_graph: RbacGraph) -> None:
    registration(rbac_graph, rbac_graph.physician, rbac_graph.clinic_a)
    enrolled = enrollment(rbac_graph, rbac_graph.clinic_a)
    elsewhere = enrollment(rbac_graph, rbac_graph.clinic_b)
    url = f"/ehr/clinics/{rbac_graph.clinic_a}/encounter/"
    with physician_client(rbac_graph) as client:
        empty = client.get(url).content.decode()
        assert 'id="unscheduled-form"' in empty
        assert f'value="{enrolled}"' in empty
        refusals = [
            client.post(
                url,
                {
                    "action": "open_unscheduled",
                    "enrollment_id": target,
                    "reason": "walk_in",
                },
            )
            for target in (elsewhere, uuid4(), "invalid")
        ]
        assert {r.status_code for r in refusals} == {403}
        assert len({NONCE.sub(b"", r.content) for r in refusals}) == 1
        opened = client.post(
            url,
            {
                "action": "open_unscheduled",
                "enrollment_id": enrolled,
                "reason": "phone_follow_up",
            },
        )
        assert opened.status_code == 302
        page = client.get(url)
        html = page.content.decode()
        assert 'data-unscheduled-reason="phone_follow_up"' in html
        assert "/prescription/" not in html
        assert "data-teleconsult-link" not in html
        # Episodes from the workspace: bounded title, open with this visit,
        # an idempotent re-link, then closure.
        encounter = str(page.context["encounter"].pk)
        episode = {"encounter_id": encounter, "action": "episode_open"}
        blank = client.post(url, {**episode, "title": "  "})
        assert blank.status_code == 400
        assert blank.context["episode_error"]
        assert client.post(url, {**episode, "title": "Dor lombar"}).status_code == 302
        linked = client.get(url).content.decode()
        assert 'data-episode-state="open"' in linked
        episode_id = re.search(r'data-linked-episode="([0-9a-f-]+)"', linked)
        assert episode_id is not None
        for action in ("episode_link", "episode_close"):
            done = client.post(
                url,
                {
                    "encounter_id": encounter,
                    "episode_id": episode_id.group(1),
                    "action": action,
                },
            )
            assert done.status_code == 302
        assert 'data-episode-state="closed"' in client.get(url).content.decode()
    assert count(rbac_graph, Encounter, appointment__isnull=True) == 1
    assert count(rbac_graph, Episode, state="closed") == 1
    assert count(rbac_graph, EpisodeEncounter) == 1


# --------------------------------------------------------------------------
# Exact posture of the new tables (the schema-policy test defers here).
# --------------------------------------------------------------------------

NEW_TABLES = {
    "ehr_episode",
    "ehr_episodeencounter",
    "ehr_drafteditstate",
    "ehr_draftsavereceipt",
}


def test_new_tables_have_exact_rls_grants_and_triggers() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity, "
            "relowner::regrole::text FROM pg_class WHERE relname = ANY(%s)",
            [list(NEW_TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, True, True, "clinic_owner") for table in NEW_TABLES
        }
        cursor.execute(
            "SELECT tablename, policyname, cmd, roles::text FROM pg_policies "
            "WHERE schemaname = 'clinic_app' AND tablename = ANY(%s)",
            [list(NEW_TABLES)],
        )
        assert set(cursor.fetchall()) == {
            *((table, "setup_tenant", "ALL", "{clinic_owner}") for table in NEW_TABLES),
            ("ehr_episode", "episode_read", "SELECT", "{clinic_app}"),
            ("ehr_episode", "episode_insert", "INSERT", "{clinic_app}"),
            ("ehr_episode", "episode_close", "UPDATE", "{clinic_app}"),
            ("ehr_episodeencounter", "episode_member_read", "SELECT", "{clinic_app}"),
            ("ehr_episodeencounter", "episode_member_insert", "INSERT", "{clinic_app}"),
            ("ehr_drafteditstate", "draft_state_author", "ALL", "{clinic_app}"),
            ("ehr_draftsavereceipt", "draft_receipt_read", "SELECT", "{clinic_app}"),
            ("ehr_draftsavereceipt", "draft_receipt_insert", "INSERT", "{clinic_app}"),
        }
        cursor.execute(
            "SELECT table_name, privilege_type "
            "FROM information_schema.role_table_grants WHERE grantee = 'clinic_app' "
            "AND table_schema = 'clinic_app' AND table_name = ANY(%s)",
            [list(NEW_TABLES)],
        )
        assert set(cursor.fetchall()) == {
            (table, privilege)
            for table in NEW_TABLES
            for privilege in ("SELECT", "INSERT")
        }
        cursor.execute(
            "SELECT table_name, column_name FROM information_schema.role_column_grants "
            "WHERE grantee = 'clinic_app' AND privilege_type = 'UPDATE' "
            "AND table_schema = 'clinic_app' AND table_name = ANY(%s)",
            [list(NEW_TABLES)],
        )
        assert set(cursor.fetchall()) == {
            ("ehr_episode", "state"),
            ("ehr_episode", "closed_at"),
            ("ehr_episode", "closed_by_id"),
            *(
                ("ehr_drafteditstate", column)
                for column in (
                    "section_edit_epochs",
                    "lock_holder",
                    "lock_expires_at",
                    "handover_requested_by",
                    "updated_at",
                )
            ),
        }
        cursor.execute(
            "SELECT c.relname, t.tgname, p.proname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_proc p ON p.oid = t.tgfoid "
            "WHERE NOT t.tgisinternal AND c.relname = ANY(%s)",
            [list(NEW_TABLES)],
        )
        assert set(cursor.fetchall()) == {
            *(
                (table, "ehr_autosave_binding", "ehr_autosave_guard")
                for table in NEW_TABLES
            ),
            *(
                (table, "ehr_autosave_immutable", "questionnaire_immutable")
                for table in NEW_TABLES
            ),
        }
