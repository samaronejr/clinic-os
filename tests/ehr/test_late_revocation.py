"""A permitted revocation committed between a writer's authority decision and its write.

An Event holds the request at an exact point: just before the first
``ehr.record.viewed`` append of an audit-first action, at the model write of a
write-first action (after any row lock it takes), or just before the lookup that
reuses, replays or retries an earlier result (``first_read``, ``get_read``), or
at a video note action's exit decision (``teleconsult_exit_decision``). While it
is held the role is retired in its own transaction: by the shipped owner
CLI, which waits for a request that has already appended an audit row
(``decided``), or by the same row update it makes (``superuser_revoker``) where
that command refuses or would wait. The late response must be the response to
the same request made once the revocation has committed: the same refusal and
body, the same denial record (or none), no other clinical audit event, and
unchanged rows, stored objects and session. Every wait is a bounded Event;
nothing sleeps.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from threading import Event
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.audit import services as audit_services
from apps.core.integration import register_send_adapter
from apps.ehr.addenda import autosave_addendum, open_addendum
from apps.ehr.attachments import AttachmentInput, scan_attachment, upload_attachment
from apps.ehr.autosave import autosave_draft
from apps.ehr.episodes import close_episode, link_encounter, open_episode
from apps.ehr.finalization import close_encounter, discard_draft, finalize_version
from apps.ehr.models import (
    AddendumSaveReceipt,
    ClinicalAttachment,
    ClinicalDocumentVersion,
    DraftEditState,
    DraftSaveReceipt,
    Encounter,
    EncounterAddendum,
    Episode,
    EpisodeEncounter,
    HistoryAssessment,
    SpecialtyTemplate,
)
from apps.ehr.services import (
    SOAP_FIELDS,
    create_draft,
    decided,
    open_encounter,
    publish_template,
    record_clinical_note,
    record_denial,
)
from apps.identity.current_context import CurrentActorError
from apps.identity.models import UserClinicRole
from apps.teleconsult.adapters import SyntheticRoomAdapter
from apps.teleconsult.services import assigned_session
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.models.query import QuerySet
from django.test import override_settings

from auth.stepup_test_support import verified_request
from database_urls import database_url_for_name
from ehr.test_addenda import clinician, signed_in
from ehr.test_autosave import (
    AUTOSAVE_URL,
    NONCE,
    SECTIONS,
    TAB_A,
    as_actor,
    enrollment,
    registration,
)
from ehr.test_write_ordering import (
    DENIAL,
    audit_mark,
    audit_since,
    clinic_owner,
    late_revocation,
    wall_clock_code,
)
from identity.permission_support import owner_context
from infra.management_command_support import run_management_command
from renewal.test_attachments import PDF_BYTES
from renewal.test_encounters import seed as encounter_seed
from renewal.test_teleconsult_sessions import _create
from renewal.test_teleconsult_sessions import seed as teleconsult_seed

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from uuid import UUID

    from apps.teleconsult.models import TeleconsultSession
    from django.db.models import Model
    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse
    from pytest_django.fixtures import SettingsWrapper

    from ehr.test_write_ordering import LateRevocation
    from rbac_fixtures import RbacGraph

    type Seam = Callable[[pytest.MonkeyPatch, Callable[[], None]], None]

pytestmark = pytest.mark.django_db(transaction=True)
# The teleconsult route's own fixed denial (apps/teleconsult/services._denied).
TELECONSULT_DENIAL = "teleconsult.access.denied"
CLINICAL = ("ehr.", "teleconsult.")


@pytest.fixture
def storage_root(tmp_path: Path) -> Iterator[Path]:
    """Attachment objects under this test's own directory."""
    root = tmp_path / "objects"
    with override_settings(EHR_ATTACHMENT_ROOT=root):
        yield root


@pytest.fixture
def synthetic_room(settings: SettingsWrapper) -> None:
    """Video sessions enqueue their room through the synthetic provider."""
    settings.TELECONSULT_SYNTHETIC_PROVIDER = True
    register_send_adapter(SyntheticRoomAdapter())


def revoker(graph: RbacGraph, target: UUID, role: str) -> Callable[[], int]:
    """The shipped owner CLI retiring ``target``'s ``role``, in its own process."""
    operator, device = clinic_owner(graph)
    database_url = database_url_for_name(
        os.environ["MIGRATION_DATABASE_URL"], str(connection.settings_dict["NAME"])
    )

    def revoke() -> int:
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
                str(target),
                "--role",
                role,
            ),
            database_url=database_url,
            hidden_responses=(("Authentication code: ", wall_clock_code(device)),),
        ).exit_code

    return revoke


def superuser_revoker(graph: RbacGraph, target: UUID, role: str) -> Callable[[], int]:
    """Retire ``target``'s ``role`` with the row update the owner CLI makes.

    The CLI's revoke_staff_role refuses a physician who holds live availability
    or an occupying appointment, which every booked visit and video session
    binds. The test superuser makes the same state change in its own
    transaction: ``revoked_at`` on the role row, whose trigger revokes the
    user's sessions exactly as the command's save does. Only the command's
    ``identity.staff_role.revoked`` event is absent.
    """
    database_url = database_url_for_name(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], str(connection.settings_dict["NAME"])
    )

    def revoke() -> int:
        with psycopg.connect(database_url) as database:
            retired = database.execute(
                "UPDATE clinic_app.identity_userclinicrole"
                " SET revoked_at = statement_timestamp()"
                " WHERE organization_id = %s AND clinic_id = %s AND user_id = %s"
                " AND role = %s AND revoked_at IS NULL",
                [graph.organization_a, graph.clinic_a, target, role],
            ).rowcount
        return 0 if retired == 1 else 1

    return revoke


def member(graph: RbacGraph, user: UUID) -> None:
    """Clinic membership that outlives a revoked clinical or admin role."""
    with owner_context(graph.organization_a):
        UserClinicRole.objects.get_or_create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=user,
            role=UserClinicRole.Role.RECEPTIONIST,
        )


def viewed_append(patch: pytest.MonkeyPatch, hold: Callable[[], None]) -> None:
    """Hold view_version before its ``ehr.record.viewed`` append."""
    append = audit_services.record_phase1_event

    def viewed(event_type: str, *, clinic_id: UUID, affected_record_id: UUID) -> int:
        # The action's checks all passed; this append is its first write.
        if event_type == "ehr.record.viewed":
            hold()
        return append(
            event_type, clinic_id=clinic_id, affected_record_id=affected_record_id
        )

    patch.setattr("apps.ehr.services.record_phase1_event", viewed)


def model_write(model: type[Model]) -> Seam:
    """Hold ``model``'s first save: a write-first action's INSERT or UPDATE."""

    def install(patch: pytest.MonkeyPatch, hold: Callable[[], None]) -> None:
        save: Callable[..., None] = model.save

        def held(*args: object, **kwargs: object) -> None:
            hold()
            save(*args, **kwargs)

        patch.setattr(model, "save", held)

    return install


def first_read(model: type[Model], *, locking: bool = False) -> Seam:
    """Hold before the first ``first()`` of ``model``: a reuse or replay lookup."""

    def install(patch: pytest.MonkeyPatch, hold: Callable[[], None]) -> None:
        first: Callable[..., object] = QuerySet.first

        def held(queryset: QuerySet[Model], *args: object, **kwargs: object) -> object:
            locked = bool(queryset.query.select_for_update)
            if queryset.model is model and locked == locking:
                hold()
            return first(queryset, *args, **kwargs)

        patch.setattr(QuerySet, "first", held)

    return install


def get_read(model: type[Model], *, locking: bool = False) -> Seam:
    """Hold before the first ``get()`` of ``model``: a locked retry lookup."""

    def install(patch: pytest.MonkeyPatch, hold: Callable[[], None]) -> None:
        get: Callable[..., object] = QuerySet.get

        def held(queryset: QuerySet[Model], *args: object, **kwargs: object) -> object:
            locked = bool(queryset.query.select_for_update)
            if queryset.model is model and locked == locking:
                hold()
            return get(queryset, *args, **kwargs)

        patch.setattr(QuerySet, "get", held)

    return install


def teleconsult_exit_decision(
    patch: pytest.MonkeyPatch, hold: Callable[[], None]
) -> None:
    """Hold a video note action's exit decision (its workspace ``assigned_session``).

    The route decides through its own binding and no handler asks again, so the
    first call through the workspace's binding is the action's last decision.
    """
    decide = assigned_session

    def held(*, clinic_id: UUID, session_id: UUID) -> TeleconsultSession:
        hold()
        return decide(clinic_id=clinic_id, session_id=session_id)

    patch.setattr("apps.teleconsult.workspace.assigned_session", held)


@contextmanager
def held_until_revoked(seam: Seam, revoke: Callable[[], int]) -> Iterator[None]:
    """Hold the first call at ``seam`` until ``revoke`` has committed."""
    reached, revoked = Event(), Event()

    def hold() -> None:
        if not reached.is_set():
            reached.set()
            assert revoked.wait(timeout=120), "the revocation did not finish"

    def run() -> int:
        try:
            assert reached.wait(timeout=120), "the request never reached its seam"
            return revoke()
        finally:
            revoked.set()

    with (
        pytest.MonkeyPatch.context() as patch,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        seam(patch, hold)
        revocation = pool.submit(run)
        yield
        assert revocation.result(timeout=180) == 0


def rows(
    graph: RbacGraph, model: type[Model], *fields: str, **filters: object
) -> list[tuple[object, ...]]:
    with owner_context(graph.organization_a):
        return sorted(
            model._default_manager.filter(**filters).values_list(*fields), key=repr
        )


def files(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
    )


@dataclass(frozen=True)
class Scene:
    """One held request: who sends it, where it is held, and what must not change."""

    actor: UUID
    revoke: Callable[[], int]
    seam: Seam
    send: Callable[[Client], _MonkeyPatchedWSGIResponse]
    rows: Callable[[], object]
    select: Callable[[Client], None] | None = None
    # Retired by the owner CLI, which records identity.staff_role.revoked.
    cli: bool = True


def posting(
    url: str, body: dict[str, object]
) -> Callable[[Client], _MonkeyPatchedWSGIResponse]:
    def send(client: Client) -> _MonkeyPatchedWSGIResponse:
        return client.post(url, body)

    return send


def workspace(graph: RbacGraph) -> str:
    return f"/ehr/clinics/{graph.clinic_a}/encounter/"


def draft_of(graph: RbacGraph, world: LateRevocation) -> ClinicalDocumentVersion:
    with owner_context(graph.organization_a):
        return ClinicalDocumentVersion.objects.get(
            document__encounter_id=world.encounter, state="draft"
        )


def note_rows(graph: RbacGraph, encounter: UUID) -> object:
    """The encounter's versions, their edit states and their save receipts."""
    return (
        rows(
            graph,
            ClinicalDocumentVersion,
            "pk",
            "state",
            "revision",
            "content_sha256",
            document__encounter_id=encounter,
        ),
        rows(
            graph,
            DraftEditState,
            "version_id",
            "lock_holder",
            "updated_at",
            version__document__encounter_id=encounter,
        ),
        rows(
            graph,
            DraftSaveReceipt,
            "command_id",
            "revision",
            version__document__encounter_id=encounter,
        ),
    )


def save_scene(graph: RbacGraph, root: Path, *, action: str) -> Scene:
    """Native save or explicit merge, held before its audited read."""
    world = late_revocation(graph)
    draft = draft_of(graph, world)
    body: dict[str, object] = {
        "action": action,
        "version_id": draft.pk,
        "revision": draft.revision,
        **SECTIONS,
    }
    if action == "merge":
        body["merge_revision"] = draft.revision
    return Scene(
        world.physician,
        world.revoke,
        viewed_append,
        posting(workspace(graph), body),
        partial(note_rows, graph, world.encounter),
    )


def discard_scene(graph: RbacGraph, root: Path) -> Scene:
    """Native discard, held before its audited read."""
    world = late_revocation(graph)
    draft = draft_of(graph, world)
    return Scene(
        world.physician,
        world.revoke,
        viewed_append,
        posting(workspace(graph), {"action": "discard", "version_id": draft.pk}),
        partial(note_rows, graph, world.encounter),
    )


def saved(graph: RbacGraph, world: LateRevocation) -> ClinicalDocumentVersion:
    """The world's draft with every SOAP section written by its author."""
    draft = draft_of(graph, world)
    with as_actor(graph, world.physician):
        return record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=draft.pk,
            expected_revision=draft.revision,
            content=dict(SECTIONS),
        )


def amend_scene(graph: RbacGraph, root: Path) -> Scene:
    """Native amendment of the finalized note, held before its audited read."""
    world = late_revocation(graph)
    draft = saved(graph, world)
    # The step-up request reads the user row, which the runtime role may not.
    request = verified_request(world.physician)
    with as_actor(graph, world.physician):
        finalized = finalize_version(
            clinic_id=graph.clinic_a,
            version_id=draft.pk,
            expected_revision=draft.revision,
            request=request,
        )
    body = {"action": "amend", "version_id": finalized.pk, "reason": "Correção"}
    return Scene(
        world.physician,
        world.revoke,
        viewed_append,
        posting(workspace(graph), body),
        partial(note_rows, graph, world.encounter),
    )


def finalize_scene(graph: RbacGraph, root: Path) -> Scene:
    """Native finalize, held at the locked version's UPDATE."""
    world = late_revocation(graph)
    draft = saved(graph, world)
    body = {"action": "finalize", "version_id": draft.pk, "revision": draft.revision}
    return Scene(
        world.physician,
        world.revoke,
        model_write(ClinicalDocumentVersion),
        posting(workspace(graph), body),
        partial(note_rows, graph, world.encounter),
    )


def discarded(graph: RbacGraph, world: LateRevocation) -> None:
    draft = draft_of(graph, world)
    with as_actor(graph, world.physician):
        discard_draft(clinic_id=graph.clinic_a, version_id=draft.pk)


def encounter_rows(graph: RbacGraph) -> object:
    return rows(graph, Encounter, "pk", "state", "closed_at", clinic_id=graph.clinic_a)


def close_scene(graph: RbacGraph, root: Path) -> Scene:
    """Closing the visit, held at the locked encounter's UPDATE."""
    world = late_revocation(graph)
    discarded(graph, world)
    return Scene(
        world.physician,
        world.revoke,
        model_write(Encounter),
        posting(workspace(graph), {"action": "close", "encounter_id": world.encounter}),
        partial(encounter_rows, graph),
    )


def template_scene(graph: RbacGraph, root: Path, *, resume: bool = False) -> Scene:
    """Starting a note: held at the version INSERT, or before the resume lookup."""
    world = late_revocation(graph)
    if not resume:
        discarded(graph, world)
    with owner_context(graph.organization_a):
        template = (
            SpecialtyTemplate.objects.filter(clinic_id=graph.clinic_a)
            .values_list("pk", flat=True)
            .first()
        )
    assert template is not None

    def select(client: Client) -> None:
        # The resumed workspace acts on the session-selected encounter.
        session = client.session
        session[f"ehr.encounter.{graph.clinic_a}"] = str(world.encounter)
        session.save()

    return Scene(
        world.physician,
        world.revoke,
        first_read(ClinicalDocumentVersion)
        if resume
        else model_write(ClinicalDocumentVersion),
        posting(workspace(graph), {"action": "template", "template_id": template}),
        partial(note_rows, graph, world.encounter),
        select=select,
    )


def unscheduled_scene(graph: RbacGraph, root: Path) -> Scene:
    """Starting another walk-in, held at the encounter INSERT."""
    world = late_revocation(graph)
    discarded(graph, world)
    with as_actor(graph, world.physician):
        close_encounter(clinic_id=graph.clinic_a, encounter_id=world.encounter)
    body = {
        "action": "open_unscheduled",
        "enrollment_id": world.enrollment,
        "reason": "walk_in",
    }
    return Scene(
        world.physician,
        world.revoke,
        model_write(Encounter),
        posting(workspace(graph), body),
        partial(encounter_rows, graph),
    )


def episode_rows(graph: RbacGraph) -> object:
    return (
        rows(graph, Episode, "pk", "state", "closed_at", clinic_id=graph.clinic_a),
        rows(
            graph,
            EpisodeEncounter,
            "episode_id",
            "encounter_id",
            episode__clinic_id=graph.clinic_a,
        ),
    )


def episode_scene(graph: RbacGraph, root: Path, *, action: str) -> Scene:
    """Episode open, link and close, held at the episode or membership write."""
    world = late_revocation(graph)
    body: dict[str, object] = {"action": action, "encounter_id": world.encounter}
    seam = model_write(Episode)
    if action == "episode_open":
        body["title"] = "Episódio sintético"
    else:
        with as_actor(graph, world.physician):
            episode = open_episode(
                clinic_id=graph.clinic_a,
                enrollment_id=world.enrollment,
                title="Episódio sintético",
            )
            if action == "episode_close":
                link_encounter(
                    clinic_id=graph.clinic_a,
                    encounter_id=world.encounter,
                    episode_id=episode.pk,
                )
        body["episode_id"] = episode.pk
        if action == "episode_link":
            seam = model_write(EpisodeEncounter)
    return Scene(
        world.physician,
        world.revoke,
        seam,
        posting(workspace(graph), body),
        partial(episode_rows, graph),
    )


def history_scene(graph: RbacGraph, root: Path) -> Scene:
    """A history save, held at the assessment INSERT."""
    world = late_revocation(graph)
    body = {
        "encounter_id": world.encounter,
        "action": "save",
        "kind": "problem",
        "revision": 1,
        "state": "documented",
        "description": "Problema sintético novo",
        "status": "active",
        "reason": "Atualização",
    }
    return Scene(
        world.physician,
        world.revoke,
        model_write(HistoryAssessment),
        posting(world.url, body),
        partial(rows, graph, HistoryAssessment, "pk", "kind", "revision"),
    )


def colleague(
    graph: RbacGraph, world: LateRevocation
) -> tuple[UUID, Callable[[], int]]:
    """A registered care-team physician of the patient whom the owner CLI may revoke."""
    physician = clinician(graph, "physician", world.enrollment)
    member(graph, physician)
    return physician, revoker(graph, physician, UserClinicRole.Role.PHYSICIAN)


def addendum_api(
    path: str, body: Callable[[], dict[str, object]]
) -> Callable[[Client], _MonkeyPatchedWSGIResponse]:
    def send(client: Client) -> _MonkeyPatchedWSGIResponse:
        return client.post(
            f"/api/ui/v1/ehr/addendum/{path}/", json.dumps(body()), "application/json"
        )

    return send


def addendum_rows(graph: RbacGraph) -> object:
    return (
        rows(
            graph,
            EncounterAddendum,
            "pk",
            "state",
            "revision",
            "text_sha256",
            clinic_id=graph.clinic_a,
        ),
        rows(
            graph,
            AddendumSaveReceipt,
            "command_id",
            "revision",
            addendum__clinic_id=graph.clinic_a,
        ),
    )


def addendum_open_scene(graph: RbacGraph, root: Path) -> Scene:
    """Another clinician's addendum open, held at the addendum INSERT."""
    world = late_revocation(graph)
    member, revoke = colleague(graph, world)
    return Scene(
        member,
        revoke,
        model_write(EncounterAddendum),
        addendum_api(
            "open",
            lambda: {
                "clinic_id": str(graph.clinic_a),
                "encounter_id": str(world.encounter),
            },
        ),
        partial(addendum_rows, graph),
    )


def addendum_autosave_scene(graph: RbacGraph, root: Path) -> Scene:
    """An addendum autosave, held at the locked addendum's UPDATE."""
    world = late_revocation(graph)
    member, revoke = colleague(graph, world)
    with as_actor(graph, member):
        addendum = open_addendum(clinic_id=graph.clinic_a, encounter_id=world.encounter)

    def body() -> dict[str, object]:
        return {
            "clinic_id": str(graph.clinic_a),
            "addendum_id": str(addendum.pk),
            "expected_revision": addendum.revision,
            "editor_command_id": str(uuid4()),
            "text": "Complemento sintético",
        }

    return Scene(
        member,
        revoke,
        model_write(EncounterAddendum),
        addendum_api("autosave", body),
        partial(addendum_rows, graph),
    )


def attachment_rows(graph: RbacGraph, root: Path) -> object:
    return (
        rows(
            graph,
            ClinicalAttachment,
            "pk",
            "state",
            "scan_attempts",
            "scan_reason",
            clinic_id=graph.clinic_a,
        ),
        files(root),
    )


def attachment_scene(graph: RbacGraph, root: Path, *, action: str) -> Scene:
    """Attachment upload and scan, held at the attachment INSERT or UPDATE."""
    world = late_revocation(graph)
    url = f"/ehr/clinics/{graph.clinic_a}/attachments/"
    body: dict[str, object] = {"action": action, "encounter_id": world.encounter}
    if action == "scan":
        with as_actor(graph, world.physician):
            attachment = upload_attachment(
                clinic_id=graph.clinic_a,
                encounter_id=world.encounter,
                upload=AttachmentInput(
                    file_name="exame-sintetico.pdf",
                    declared_type="application/pdf",
                    data=PDF_BYTES,
                ),
            )
        body["attachment_id"] = attachment.pk

    def send(client: Client) -> _MonkeyPatchedWSGIResponse:
        files_: dict[str, object] = {}
        if action == "upload":
            files_["attachment"] = SimpleUploadedFile(
                "exame.pdf", PDF_BYTES, content_type="application/pdf"
            )
        return client.post(url, body | files_)

    return Scene(
        world.physician,
        world.revoke,
        model_write(ClinicalAttachment),
        send,
        partial(attachment_rows, graph, root),
    )


def scheduled_open_scene(graph: RbacGraph, root: Path) -> Scene:
    """Opening a booked visit, held at the encounter INSERT after its lock."""
    appointment, _ = encounter_seed(graph)
    member(graph, graph.physician)
    return Scene(
        graph.physician,
        superuser_revoker(graph, graph.physician, UserClinicRole.Role.PHYSICIAN),
        model_write(Encounter),
        posting(workspace(graph), {"action": "open", "appointment_id": appointment.pk}),
        partial(encounter_rows, graph),
        cli=False,
    )


def teleconsult_scene(graph: RbacGraph, root: Path, *, action: str) -> Scene:
    """A video visit's note action, held where its native twin is held."""
    _, encounter, _, _, _ = teleconsult_seed(graph)
    registration(graph, graph.physician, graph.clinic_a)
    with owner_context(graph.organization_a):
        template = SpecialtyTemplate.objects.get(clinic_id=graph.clinic_a)
    with as_actor(graph, graph.physician):
        version = create_draft(
            clinic_id=graph.clinic_a, encounter_id=encounter.pk, template_id=template.pk
        )
    session = _create(graph, encounter)
    member(graph, graph.physician)
    body: dict[str, object] = {"action": action, "session_id": session.pk}
    seam: Seam = viewed_append
    if action == "note-template":
        with as_actor(graph, graph.physician):
            discard_draft(clinic_id=graph.clinic_a, version_id=version.pk)
        body["template_id"] = template.pk
        seam = model_write(ClinicalDocumentVersion)
    elif action == "note-template-resume":
        # The draft stays: the template action resumes it.
        body |= {"action": "note-template", "template_id": template.pk}
        seam = first_read(ClinicalDocumentVersion)
    elif action in {"note-finalize", "note-finalize-stale", "note-amend"}:
        with as_actor(graph, graph.physician):
            version = record_clinical_note(
                clinic_id=graph.clinic_a,
                version_id=version.pk,
                expected_revision=version.revision,
                content=dict(SECTIONS),
            )
        if action == "note-finalize":
            body |= {"version_id": version.pk, "revision": version.revision}
            seam = model_write(ClinicalDocumentVersion)
        elif action == "note-finalize-stale":
            # A stale revision is answered by the conflict render, a normal
            # return: the loss shows at the action's exit decision.
            body |= {
                "action": "note-finalize",
                "version_id": version.pk,
                "revision": version.revision - 1,
            }
            seam = teleconsult_exit_decision
        else:
            request = verified_request(graph.physician)
            with as_actor(graph, graph.physician):
                finalized = finalize_version(
                    clinic_id=graph.clinic_a,
                    version_id=version.pk,
                    expected_revision=version.revision,
                    request=request,
                )
            body |= {"version_id": finalized.pk, "reason": "Correção"}
    else:
        body["version_id"] = version.pk
        if action == "note-save":
            body |= {"revision": version.revision, **SECTIONS}
    return Scene(
        graph.physician,
        superuser_revoker(graph, graph.physician, UserClinicRole.Role.PHYSICIAN),
        seam,
        posting(f"/teleconsult/clinics/{graph.clinic_a}/", body),
        partial(note_rows, graph, encounter.pk),
        cli=False,
    )


def unscheduled_reuse_scene(graph: RbacGraph, root: Path) -> Scene:
    """A walk-in start that reuses the open visit, held before that lookup (c6 B1)."""
    world = late_revocation(graph)
    body = {
        "action": "open_unscheduled",
        "enrollment_id": world.enrollment,
        "reason": "walk_in",
    }
    return Scene(
        world.physician,
        world.revoke,
        first_read(Encounter),
        posting(workspace(graph), body),
        partial(encounter_rows, graph),
    )


def scheduled_reuse_scene(graph: RbacGraph, root: Path) -> Scene:
    """Opening a booked visit that is already open, held before the reuse lookup."""
    appointment, _ = encounter_seed(graph)
    with as_actor(graph, graph.physician):
        open_encounter(clinic_id=graph.clinic_a, appointment_id=appointment.pk)
    member(graph, graph.physician)
    return Scene(
        graph.physician,
        superuser_revoker(graph, graph.physician, UserClinicRole.Role.PHYSICIAN),
        first_read(Encounter),
        posting(workspace(graph), {"action": "open", "appointment_id": appointment.pk}),
        partial(encounter_rows, graph),
        cli=False,
    )


def close_retry_scene(graph: RbacGraph, root: Path) -> Scene:
    """Closing a visit already closed, held before the locked retry read."""
    world = late_revocation(graph)
    discarded(graph, world)
    with as_actor(graph, world.physician):
        close_encounter(clinic_id=graph.clinic_a, encounter_id=world.encounter)
    return Scene(
        world.physician,
        world.revoke,
        get_read(Encounter, locking=True),
        posting(workspace(graph), {"action": "close", "encounter_id": world.encounter}),
        partial(encounter_rows, graph),
    )


def episode_retry_scene(graph: RbacGraph, root: Path, *, action: str) -> Scene:
    """Re-linking or re-closing an episode, held before its reuse or locked read."""
    world = late_revocation(graph)
    with as_actor(graph, world.physician):
        episode = open_episode(
            clinic_id=graph.clinic_a,
            enrollment_id=world.enrollment,
            title="Episódio sintético",
        )
        link_encounter(
            clinic_id=graph.clinic_a,
            encounter_id=world.encounter,
            episode_id=episode.pk,
        )
        if action == "episode_close":
            close_episode(clinic_id=graph.clinic_a, episode_id=episode.pk)
    body = {"action": action, "encounter_id": world.encounter, "episode_id": episode.pk}
    return Scene(
        world.physician,
        world.revoke,
        first_read(Episode, locking=True)
        if action == "episode_close"
        else first_read(EpisodeEncounter),
        posting(workspace(graph), body),
        partial(episode_rows, graph),
    )


def addendum_reuse_scene(graph: RbacGraph, root: Path) -> Scene:
    """Reopening another clinician's addendum draft, held before the reuse lookup."""
    world = late_revocation(graph)
    author, revoke = colleague(graph, world)
    with as_actor(graph, author):
        open_addendum(clinic_id=graph.clinic_a, encounter_id=world.encounter)
    return Scene(
        author,
        revoke,
        first_read(EncounterAddendum),
        addendum_api(
            "open",
            lambda: {
                "clinic_id": str(graph.clinic_a),
                "encounter_id": str(world.encounter),
            },
        ),
        partial(addendum_rows, graph),
    )


def addendum_replay_scene(graph: RbacGraph, root: Path) -> Scene:
    """Replaying an addendum autosave command, held before the receipt lookup."""
    world = late_revocation(graph)
    author, revoke = colleague(graph, world)
    command, text = uuid4(), "Complemento sintético"
    with as_actor(graph, author):
        addendum = open_addendum(clinic_id=graph.clinic_a, encounter_id=world.encounter)
        autosave_addendum(
            clinic_id=graph.clinic_a,
            addendum_id=addendum.pk,
            expected_revision=addendum.revision,
            editor_command_id=command,
            text=text,
        )
    body: dict[str, object] = {
        "clinic_id": str(graph.clinic_a),
        "addendum_id": str(addendum.pk),
        "expected_revision": addendum.revision,
        "editor_command_id": str(command),
        "text": text,
    }
    return Scene(
        author,
        revoke,
        first_read(AddendumSaveReceipt),
        addendum_api("autosave", lambda: body),
        partial(addendum_rows, graph),
    )


def autosave_replay_scene(graph: RbacGraph, root: Path) -> Scene:
    """Replaying a draft autosave command, held before the receipt lookup.

    The lookup follows the request's read audit, which holds the audit chain: the
    owner CLI would wait for the request, so the role row is updated instead.
    """
    world = late_revocation(graph)
    draft = draft_of(graph, world)
    command = uuid4()
    with as_actor(graph, world.physician):
        autosave_draft(
            clinic_id=graph.clinic_a,
            version_id=draft.pk,
            expected_revision=draft.revision,
            editor_command_id=command,
            editor_session_id=TAB_A,
            sections=dict(SECTIONS),
        )
    body = {
        "clinic_id": str(graph.clinic_a),
        "version_id": str(draft.pk),
        "expected_revision": draft.revision,
        "editor_session": str(TAB_A),
        "sections": SECTIONS,
        "handover": False,
        "editor_command_id": str(command),
    }

    def send(client: Client) -> _MonkeyPatchedWSGIResponse:
        return client.post(AUTOSAVE_URL, json.dumps(body), "application/json")

    return Scene(
        world.physician,
        superuser_revoker(graph, world.physician, UserClinicRole.Role.PHYSICIAN),
        first_read(DraftSaveReceipt),
        send,
        partial(note_rows, graph, world.encounter),
        cli=False,
    )


def scan_retry_scene(graph: RbacGraph, root: Path) -> Scene:
    """Scanning an attachment already scanned, held before the locked retry read."""
    world = late_revocation(graph)
    with as_actor(graph, world.physician):
        attachment = upload_attachment(
            clinic_id=graph.clinic_a,
            encounter_id=world.encounter,
            upload=AttachmentInput(
                file_name="exame-sintetico.pdf",
                declared_type="application/pdf",
                data=PDF_BYTES,
            ),
        )
        scan_attachment(clinic_id=graph.clinic_a, attachment_id=attachment.pk)
    body = {
        "action": "scan",
        "encounter_id": world.encounter,
        "attachment_id": attachment.pk,
    }
    return Scene(
        world.physician,
        world.revoke,
        first_read(ClinicalAttachment, locking=True),
        posting(f"/ehr/clinics/{graph.clinic_a}/attachments/", body),
        partial(attachment_rows, graph, root),
    )


def finalize_retry_scene(graph: RbacGraph, root: Path) -> Scene:
    """Finalizing a note already final, held before the locked retry read."""
    world = late_revocation(graph)
    draft = saved(graph, world)
    request = verified_request(world.physician)
    with as_actor(graph, world.physician):
        final = finalize_version(
            clinic_id=graph.clinic_a,
            version_id=draft.pk,
            expected_revision=draft.revision,
            request=request,
        )
    body = {"action": "finalize", "version_id": final.pk, "revision": final.revision}
    return Scene(
        world.physician,
        world.revoke,
        first_read(ClinicalDocumentVersion, locking=True),
        posting(workspace(graph), body),
        partial(note_rows, graph, world.encounter),
    )


SCENES: dict[str, Callable[[RbacGraph, Path], Scene]] = {
    "save": partial(save_scene, action="save"),
    "merge": partial(save_scene, action="merge"),
    "discard": discard_scene,
    "amend": amend_scene,
    "finalize": finalize_scene,
    "close": close_scene,
    "template": template_scene,
    "open_unscheduled": unscheduled_scene,
    "episode_open": partial(episode_scene, action="episode_open"),
    "episode_link": partial(episode_scene, action="episode_link"),
    "episode_close": partial(episode_scene, action="episode_close"),
    "history_save": history_scene,
    "addendum_open": addendum_open_scene,
    "addendum_autosave": addendum_autosave_scene,
    "attachment_upload": partial(attachment_scene, action="upload"),
    "attachment_scan": partial(attachment_scene, action="scan"),
    "scheduled_open": scheduled_open_scene,
    **{
        f"teleconsult_{action.replace('-', '_')}": partial(
            teleconsult_scene, action=action
        )
        for action in (
            "note-save",
            "note-discard",
            "note-amend",
            "note-finalize",
            "note-template",
            "note-template-resume",
            "note-finalize-stale",
        )
    },
    "reuse_unscheduled_open": unscheduled_reuse_scene,
    "reuse_scheduled_open": scheduled_reuse_scene,
    "retry_close": close_retry_scene,
    "resume_template": partial(template_scene, resume=True),
    "reuse_episode_link": partial(episode_retry_scene, action="episode_link"),
    "retry_episode_close": partial(episode_retry_scene, action="episode_close"),
    "reuse_addendum_open": addendum_reuse_scene,
    "replay_addendum_autosave": addendum_replay_scene,
    "replay_draft_autosave": autosave_replay_scene,
    "retry_attachment_scan": scan_retry_scene,
    "retry_finalize": finalize_retry_scene,
}


@pytest.mark.usefixtures("synthetic_room")
@pytest.mark.parametrize("name", sorted(SCENES))
def test_late_revocation_at_the_write_is_the_early_refusal(
    rbac_graph: RbacGraph, storage_root: Path, name: str
) -> None:
    graph = rbac_graph
    scene = SCENES[name](graph, storage_root)
    unchanged = scene.rows()
    before = audit_mark(graph)
    with signed_in(scene.actor) as client:
        if scene.select is not None:
            scene.select(client)
        session = dict(client.session)
        with held_until_revoked(scene.seam, scene.revoke):
            late = scene.send(client)
        assert dict(client.session) == session
    trail = audit_since(graph, before)
    assert scene.rows() == unchanged
    before = audit_mark(graph)
    with signed_in(scene.actor) as client:
        if scene.select is not None:
            scene.select(client)
        # The same request once the revocation has committed.
        early = scene.send(client)
    early_trail = [e for e in audit_since(graph, before) if e.startswith(CLINICAL)]
    late_trail = [e for e in trail if e.startswith(CLINICAL)]
    # Lost authority is the ordinary refusal: no 409, 412 or 5xx.
    assert late.status_code == early.status_code == 403
    if late.headers["Content-Type"].startswith("application/json"):
        assert late.json() == early.json()
    else:
        assert NONCE.sub(b"", late.content) == NONCE.sub(b"", early.content)
    # Only the fixed denial event may remain, exactly as the early refusal leaves.
    assert set(late_trail) <= {DENIAL, TELECONSULT_DENIAL}
    assert late_trail == early_trail
    # The owner CLI records the retirement; the row update alone records nothing.
    assert trail.count("identity.staff_role.revoked") == int(scene.cli)
    assert scene.rows() == unchanged


def test_template_publish_after_late_admin_revocation_writes_nothing(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    admin = clinician(graph, "clinic_admin", enrollment(graph, graph.clinic_a))
    member(graph, admin)
    revoke = revoker(graph, admin, UserClinicRole.Role.CLINIC_ADMIN)

    def publish() -> None:
        with as_actor(graph, admin):
            publish_template(
                clinic_id=graph.clinic_a,
                key="sintetico-tardio",
                title="Modelo sintético",
                prompts=dict.fromkeys(SOAP_FIELDS, "Pergunta sintética"),
            )

    templates = partial(rows, graph, SpecialtyTemplate, "pk", "key", "version")
    unchanged = templates()
    before = audit_mark(graph)
    with (
        held_until_revoked(model_write(SpecialtyTemplate), revoke),
        pytest.raises(CurrentActorError),
    ):
        publish()
    trail = audit_since(graph, before)
    assert templates() == unchanged
    before = audit_mark(graph)
    with pytest.raises(CurrentActorError):
        # The same call once the revocation has committed.
        publish()
    early_trail = [e for e in audit_since(graph, before) if e.startswith("ehr.")]
    late_trail = [e for e in trail if e.startswith("ehr.")]
    assert set(late_trail) <= {DENIAL}
    assert late_trail == early_trail
    assert trail.count("identity.staff_role.revoked") == 1
    assert templates() == unchanged


class _RefusalError(Exception):
    """A refusal of a type ``decided`` was not written for, as teleconsult's is."""


@pytest.mark.parametrize(
    "outcome", ["exit_refused", "block_failed", "failure_kept", "write_kept"]
)
def test_decided_decides_any_failure_again_outside_its_savepoint(
    rbac_graph: RbacGraph, outcome: str
) -> None:
    """Every failure of a decided block, its exit decision's own included, is
    decided again outside the savepoint, and nothing the block wrote survives it.

    Lost authority raises that decision's refusal with exactly one denial record;
    intact authority keeps the block's own failure, or its write, unchanged.
    """
    graph = rbac_graph
    lost = outcome in {"exit_refused", "block_failed"}

    def redecide() -> None:
        if lost:
            record_denial(graph.clinic_a, uuid4(), "not_assigned")
            raise _RefusalError

    def block() -> None:
        audit_services.record_phase1_event(
            "ehr.encounter.closed", clinic_id=graph.clinic_a, affected_record_id=uuid4()
        )
        if outcome in {"block_failed", "failure_kept"}:
            raise LookupError

    before = audit_mark(graph)
    with as_actor(graph, graph.physician):
        if outcome == "write_kept":
            with decided(redecide):
                block()
        else:
            with (
                pytest.raises(_RefusalError if lost else LookupError),
                decided(redecide),
            ):
                block()
    trail = [e for e in audit_since(graph, before) if e.startswith(CLINICAL)]
    assert (
        trail
        == {
            "exit_refused": [DENIAL],
            "block_failed": [DENIAL],
            "failure_kept": [],
            "write_kept": ["ehr.encounter.closed"],
        }[outcome]
    )
