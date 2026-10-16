"""A permitted revocation committed between a writer's authority decision and its write.

The shipped owner CLI retires the role in its own transaction while an Event
holds the request at an exact point: just before the first ``ehr.record.viewed``
append of an audit-first action, or at the model write of a write-first action
(after any row lock it takes). Each is before the request's first audit append,
after which the revocation waits for the request to commit (``decided``).
The late response must be the response to the
same request made once the revocation has committed: the same refusal and body,
the same denial record (or none), no other clinical audit event, and unchanged
rows, stored objects and session. Every wait is a bounded Event; nothing sleeps.

Opening a scheduled encounter is guarded the same way and has no case here: the
owner CLI refuses to retire a physician who holds live availability or an
occupying appointment, which the arrived-appointment fixtures create.
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

import pytest
from apps.audit import services as audit_services
from apps.ehr.addenda import open_addendum
from apps.ehr.attachments import AttachmentInput, upload_attachment
from apps.ehr.episodes import link_encounter, open_episode
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
from apps.ehr.services import SOAP_FIELDS, publish_template, record_clinical_note
from apps.identity.current_context import CurrentActorError
from apps.identity.models import UserClinicRole
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings

from auth.stepup_test_support import verified_request
from database_urls import database_url_for_name
from ehr.test_addenda import clinician, signed_in
from ehr.test_autosave import NONCE, SECTIONS, as_actor, enrollment
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

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from uuid import UUID

    from django.db.models import Model
    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse

    from ehr.test_write_ordering import LateRevocation
    from rbac_fixtures import RbacGraph

    type Seam = Callable[[pytest.MonkeyPatch, Callable[[], None]], None]

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def storage_root(tmp_path: Path) -> Iterator[Path]:
    """Attachment objects under this test's own directory."""
    root = tmp_path / "objects"
    with override_settings(EHR_ATTACHMENT_ROOT=root):
        yield root


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


def note_rows(graph: RbacGraph, world: LateRevocation) -> object:
    """The encounter's versions, their edit states and their save receipts."""
    encounter = world.encounter
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
        partial(note_rows, graph, world),
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
        partial(note_rows, graph, world),
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
    with as_actor(graph, world.physician):
        finalized = finalize_version(
            clinic_id=graph.clinic_a,
            version_id=draft.pk,
            expected_revision=draft.revision,
            request=verified_request(world.physician),
        )
    body = {"action": "amend", "version_id": finalized.pk, "reason": "Correção"}
    return Scene(
        world.physician,
        world.revoke,
        viewed_append,
        posting(workspace(graph), body),
        partial(note_rows, graph, world),
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
        partial(note_rows, graph, world),
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


def template_scene(graph: RbacGraph, root: Path) -> Scene:
    """Starting a note from a template, held at the version INSERT."""
    world = late_revocation(graph)
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
        model_write(ClinicalDocumentVersion),
        posting(workspace(graph), {"action": "template", "template_id": template}),
        partial(note_rows, graph, world),
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
    member = clinician(graph, "physician", world.enrollment)
    with owner_context(graph.organization_a):
        # Tenant membership outlives the physician role.
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=member,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    return member, revoker(graph, member, UserClinicRole.Role.PHYSICIAN)


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
}


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
    early_trail = [e for e in audit_since(graph, before) if e.startswith("ehr.")]
    late_trail = [e for e in trail if e.startswith("ehr.")]
    # Lost authority is the ordinary refusal: no 409, 412 or 5xx.
    assert late.status_code == early.status_code == 403
    if late.headers["Content-Type"].startswith("application/json"):
        assert late.json() == early.json()
    else:
        assert NONCE.sub(b"", late.content) == NONCE.sub(b"", early.content)
    # Only the fixed denial event may remain, exactly as the early refusal leaves.
    assert set(late_trail) <= {DENIAL}
    assert late_trail == early_trail
    assert trail.count("identity.staff_role.revoked") == 1
    assert scene.rows() == unchanged


def test_template_publish_after_late_admin_revocation_writes_nothing(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    admin = clinician(graph, "clinic_admin", enrollment(graph, graph.clinic_a))
    with owner_context(graph.organization_a):
        # Tenant membership outlives the clinic-admin role.
        UserClinicRole.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            user_id=admin,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
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
