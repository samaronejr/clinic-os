"""Every authorization a response depends on is decided before its first write.

The tenant middleware commits any response below 500, so a refusal raised
after a write would keep that write (todo 23's B1). These tests drive the
plan item 27 writers into their late refusals and assert that no domain row
and no audit row other than the fixed ``ehr.access.denied`` record survive.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.ehr import history, services
from apps.ehr.autosave import autosave_draft
from apps.ehr.episodes import open_episode_for_encounter
from apps.ehr.finalization import amend_document, finalize_version
from apps.ehr.history import HistoryChange, save_history
from apps.ehr.models import (
    DraftEditState,
    DraftSaveReceipt,
    Episode,
    EpisodeEncounter,
    Problem,
)
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    create_draft,
)
from apps.identity.models import User, UserClinicRole
from django.contrib.auth.hashers import make_password
from django.db import connection
from django.utils import timezone
from django_otp.oath import TOTP

from auth.stepup_test_support import verified_request
from database_urls import database_url_for_name
from ehr.test_addenda import clinician, enrollment_of, signed_in
from ehr.test_autosave import (
    NONCE,
    SECTIONS,
    TAB_A,
    as_actor,
    count,
    draft_world,
    enrollment,
    open_walk_in,
    registration,
    save,
    stored,
)
from identity.permission_support import owner_context
from infra.management_command_support import (
    generated_password,
    run_management_command,
)
from otp_test_support import create_totp_device
from renewal.test_encounters import physician_client, seed

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from uuid import UUID

    from apps.ehr.models import Encounter
    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse
    from django_otp.plugins.otp_totp.models import TOTPDevice

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
DENIAL = "ehr.access.denied"


def audit_since(graph: RbacGraph, before: int) -> list[str]:
    with owner_context(graph.organization_a):
        return list(
            AuditEvent.objects.filter(pk__gt=before)
            .order_by("pk")
            .values_list("event_type", flat=True)
        )


def audit_mark(graph: RbacGraph) -> int:
    with owner_context(graph.organization_a):
        last = AuditEvent.objects.order_by("-pk").values_list("pk", flat=True).first()
        return int(last or 0)


def test_workspace_episode_open_refuses_before_writing(rbac_graph: RbacGraph) -> None:
    registration(rbac_graph, rbac_graph.physician, rbac_graph.clinic_a)
    enrolled = enrollment_of(
        rbac_graph,
        draft_world(rbac_graph).document.encounter.patient_id,
        rbac_graph.clinic_a,
    )
    with as_actor(rbac_graph, rbac_graph.physician):
        encounter = open_walk_in(rbac_graph, enrolled)
    url = f"/ehr/clinics/{rbac_graph.clinic_a}/encounter/"
    body = {"encounter_id": encounter.pk, "action": "episode_open"}
    with physician_client(rbac_graph) as client:
        assert client.post(url, {**body, "title": "Primeiro"}).status_code == 302
        # The encounter is already linked: the second open is refused, and
        # the refusal must not leave its episode (or audit row) committed.
        again = client.post(url, {**body, "title": "Órfão"})
        assert again.status_code == 409
    with owner_context(rbac_graph.organization_a):
        assert Episode.objects.count() == 1
        assert EpisodeEncounter.objects.count() == 1
        opened = AuditEvent.objects.filter(event_type="ehr.episode.opened").count()
    assert opened == 1


def test_episode_open_with_encounter_refuses_before_writing(
    rbac_graph: RbacGraph,
) -> None:
    version = draft_world(rbac_graph)
    encounter = version.document.encounter
    enrolled = enrollment_of(rbac_graph, encounter.patient_id, rbac_graph.clinic_a)
    colleague = clinician(rbac_graph, "physician", enrolled)
    before = audit_mark(rbac_graph)
    # The assignee without clinical.write, and a colleague with clinical.write
    # who is not the assignee: both are refused and neither leaves a row.
    for actor in (rbac_graph.physician, colleague):
        with as_actor(rbac_graph, actor), pytest.raises(ClinicalAccessDeniedError):
            open_episode_for_encounter(
                clinic_id=rbac_graph.clinic_a,
                encounter_id=encounter.pk,
                title="Recusado",
            )
    assert count(rbac_graph, Episode) == 0
    assert count(rbac_graph, EpisodeEncounter) == 0
    assert set(audit_since(rbac_graph, before)) <= {DENIAL}
    registration(rbac_graph, rbac_graph.physician, rbac_graph.clinic_a)
    with as_actor(rbac_graph, rbac_graph.physician):
        link = open_episode_for_encounter(
            clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk, title="Asma"
        )
        with pytest.raises(ClinicalConflictError, match="already_linked"):
            open_episode_for_encounter(
                clinic_id=rbac_graph.clinic_a, encounter_id=encounter.pk, title="Outro"
            )
    assert count(rbac_graph, Episode) == 1
    assert count(rbac_graph, EpisodeEncounter, pk=link.pk) == 1


def test_care_reader_writes_are_refused_before_the_read_audit(
    rbac_graph: RbacGraph,
) -> None:
    """A care reader passes view_version on a finalized note but may not write.

    The read audit is a write; it must not be appended ahead of the refusal.
    """
    version = draft_world(rbac_graph)
    encounter = version.document.encounter
    request = verified_request(rbac_graph.physician)
    with as_actor(rbac_graph, rbac_graph.physician):
        save(rbac_graph, version.pk, 1)
        finalize_version(
            clinic_id=rbac_graph.clinic_a,
            version_id=version.pk,
            expected_revision=2,
            request=request,
        )
    enrolled = enrollment_of(rbac_graph, encounter.patient_id, rbac_graph.clinic_a)
    reader = clinician(rbac_graph, "physician", enrolled)
    template = version.template
    with as_actor(rbac_graph, reader):
        # Authoring any note for this patient makes the reader a care reader.
        visit = open_walk_in(rbac_graph, enrolled)
        services.create_draft(
            clinic_id=rbac_graph.clinic_a,
            encounter_id=visit.pk,
            template_id=template.pk,
        )
        assert (
            services.view_version(
                clinic_id=rbac_graph.clinic_a, version_id=version.pk
            ).state
            == "finalized"
        )
    before = audit_mark(rbac_graph)
    with as_actor(rbac_graph, reader):
        with pytest.raises(ClinicalAccessDeniedError):
            services.record_clinical_note(
                clinic_id=rbac_graph.clinic_a,
                version_id=version.pk,
                expected_revision=2,
                content={**SECTIONS, "plan": "Leitor"},
            )
        with pytest.raises(ClinicalAccessDeniedError):
            autosave_draft(
                clinic_id=rbac_graph.clinic_a,
                version_id=version.pk,
                expected_revision=2,
                editor_command_id=uuid4(),
                editor_session_id=TAB_A,
                sections={**SECTIONS, "plan": "Leitor"},
            )
        # Amending needs the assignee, not the author; still decided first.
        with pytest.raises(ClinicalAccessDeniedError):
            amend_document(
                clinic_id=rbac_graph.clinic_a,
                version_id=version.pk,
                reason="Leitor",
            )
    assert set(audit_since(rbac_graph, before)) <= {DENIAL}
    assert stored(rbac_graph, version.pk)[0] == 2
    assert count(rbac_graph, DraftSaveReceipt) == 1
    assert count(rbac_graph, DraftEditState, version_id=version.pk) == 1


def clinic_owner(graph: RbacGraph) -> tuple[UUID, TOTPDevice]:
    owner = User.objects.create(
        username=f"sintetico-owner-{uuid4().hex}",
        password=make_password(generated_password()),
    )
    with owner_context(graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user=owner,
            role=UserClinicRole.Role.OWNER,
        )
    return owner.pk, create_totp_device(owner.pk, confirmed=True)


def wall_clock_code(device: TOTPDevice) -> str:
    """The device's code on the wall clock.

    ``fixed_otp_time`` pins ``time.time`` for this whole process while a
    physician session is open; the owner command runs in its own process.
    """
    generator = TOTP(
        device.bin_key, device.step, device.t0, device.digits, device.drift
    )
    generator.time = timezone.now().timestamp()
    return f"{generator.token():0{device.digits}d}"


@dataclass(frozen=True)
class LateRevocation:
    """A physician's walk-in with one history entry, and the owner's revoke."""

    url: str
    physician: UUID
    encounter: UUID
    entry: UUID
    revoke: Callable[[], int]


def late_revocation(graph: RbacGraph) -> LateRevocation:
    _, template = seed(graph)
    enrolled = enrollment(graph, graph.clinic_a)
    physician = clinician(graph, "physician", enrolled)
    with as_actor(graph, physician):
        # Unscheduled: no appointment or availability blocks the revocation.
        encounter = open_walk_in(graph, enrolled)
        # Starting the note is the care relationship history writes need
        # (ehr_care: a version this physician authored for the patient).
        create_draft(
            clinic_id=graph.clinic_a,
            encounter_id=encounter.pk,
            template_id=template.pk,
        )
        save_history(
            clinic_id=graph.clinic_a,
            encounter_id=encounter.pk,
            change=HistoryChange(
                kind="problem",
                expected_revision=0,
                description="Problema sintético",
                status="active",
                reason="Registro inicial",
                state="documented",
            ),
        )
        (entry,) = Problem.objects.values_list("entry_id", flat=True)
    with owner_context(graph.organization_a):
        # Tenant membership outlives the physician role.
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=physician,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    operator, device = clinic_owner(graph)
    database_url = database_url_for_name(
        os.environ["MIGRATION_DATABASE_URL"], str(connection.settings_dict["NAME"])
    )

    def revoke() -> int:
        """The shipped owner CLI revokes the physician role."""
        return run_management_command(
            (
                "revoke_staff_role",
                "--operator-id",
                str(operator),
                "--organization-id",
                str(graph.organization_a),
                "--clinic-id",
                str(graph.clinic_a),
                "--target-user-id",
                str(physician),
                "--role",
                UserClinicRole.Role.PHYSICIAN,
            ),
            database_url=database_url,
            hidden_responses=(("Authentication code: ", wall_clock_code(device)),),
        ).exit_code

    return LateRevocation(
        f"/ehr/clinics/{graph.clinic_a}/history/",
        physician,
        encounter.pk,
        entry,
        revoke,
    )


@contextmanager
def revoked_at_last_history_check(world: LateRevocation) -> Iterator[None]:
    """Commit the real revocation between read_history's check and its read."""
    reached, revoked = Event(), Event()
    authorize = history.authorize_history

    def last_authorization(
        clinic_id: UUID, encounter_id: UUID, *, write: bool = False
    ) -> Encounter:
        # read_history's own check, unchanged; the committed revocation lands
        # between it and the audited, RLS-filtered read.
        found = authorize(clinic_id, encounter_id, write=write)
        reached.set()
        assert revoked.wait(timeout=120), "the revocation did not finish"
        return found

    def revoke() -> int:
        try:
            assert reached.wait(timeout=120), "the request never reached its check"
            return world.revoke()
        finally:
            revoked.set()

    with (
        pytest.MonkeyPatch.context() as patch,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        patch.setattr("apps.ehr.history.authorize_history", last_authorization)
        revocation = pool.submit(revoke)
        yield
        assert revocation.result(timeout=180) == 0


def test_history_edit_refused_after_late_role_revocation_keeps_no_view_audit(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    world = late_revocation(graph)
    form = {"action": "edit", "encounter_id": str(world.encounter), "kind": "problem"}
    before = audit_mark(graph)
    with signed_in(world.physician) as client:
        # The unknown entry is refused before the audited read.
        unknown = client.post(world.url, {**form, "entry_id": str(uuid4())})
    baseline = [e for e in audit_since(graph, before) if e.startswith("ehr.")]
    before = audit_mark(graph)
    with signed_in(world.physician) as client:
        session = dict(client.session)
        with revoked_at_last_history_check(world):
            late = client.post(world.url, {**form, "entry_id": str(world.entry)})
        assert dict(client.session) == session
    trail = audit_since(graph, before)
    assert unknown.status_code == late.status_code == 403
    assert NONCE.sub(b"", late.content) == NONCE.sub(b"", unknown.content)
    # The viewed audit rolled back with its savepoint: the late refusal leaves
    # what the early one leaves, beside the revocation's own event.
    assert [e for e in trail if e.startswith("ehr.")] == baseline
    assert "ehr.history.viewed" not in trail
    assert trail.count("identity.staff_role.revoked") == 1


@pytest.mark.parametrize("path", ["new", "view"])
def test_history_new_and_view_refused_after_late_role_revocation_keep_no_view_audit(
    rbac_graph: RbacGraph, path: str
) -> None:
    graph = rbac_graph
    world = late_revocation(graph)
    selected = {"encounter_id": str(world.encounter)}

    def send(client: Client) -> _MonkeyPatchedWSGIResponse:
        if path == "new":
            # The workspace's own new-entry form for the problem list.
            return client.post(
                world.url, {**selected, "action": "new", "kind": "problem"}
            )
        # The read-only view of the session-selected encounter.
        return client.get(world.url)

    before = audit_mark(graph)
    with signed_in(world.physician) as client:
        if path == "view":
            opened = client.post(world.url, {**selected, "action": "open"})
            assert opened.status_code == 302
        session = dict(client.session)
        with revoked_at_last_history_check(world):
            late = send(client)
        assert dict(client.session) == session
    trail = audit_since(graph, before)
    before = audit_mark(graph)
    with signed_in(world.physician) as client:
        # The same request once the revocation has committed.
        early = send(client)
    early_trail = [e for e in audit_since(graph, before) if e.startswith("ehr.")]
    late_trail = [e for e in trail if e.startswith("ehr.")]
    assert late.status_code == early.status_code == 403
    assert NONCE.sub(b"", late.content) == NONCE.sub(b"", early.content)
    # Every view audit before the late refusal rolled back with it: only the
    # fixed denial event may remain, exactly as the early refusal leaves.
    assert "ehr.history.viewed" not in trail
    assert set(late_trail) <= {DENIAL}
    assert late_trail == early_trail
    assert trail.count("identity.staff_role.revoked") == 1
