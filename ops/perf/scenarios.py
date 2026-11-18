"""Load scenarios; each returns raw latencies in milliseconds.

``measured`` scenarios run the real application code as ``clinic_app`` on real
PostgreSQL/Redis; ``component`` scenarios measure one real primitive of a path
whose full fixtures belong to another suite; ``modeled`` scenarios are
seeded arithmetic over assumed provider timings and never count as met.
"""

from __future__ import annotations

import multiprocessing
import random
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid4, uuid5

from ops.perf import dataset as data
from ops.perf.children import child_setup, context_for
from ops.perf.latency_model import (
    ModelParams,
    draft_generation_ms,
    transcript_partial_ms,
)
from ops.perf.report import ScenarioResult
from ops.perf.runtime import runtime_role
from ops.perf.staff_http import StaffLogin, http_worker, provision_logins
from ops.perf.threads import WorkerResult, run_workers

if TYPE_CHECKING:
    from collections.abc import Callable

    from ops.perf.dataset import Dataset, Draft

HTTP_PROCESSES = 4
AUTOSAVE_RATE_PER_SECOND = 12.0
CHUNK_BYTES = 8 * 1024
FANOUT_PUBLISHES = 30
REGISTRY_FULL_SAMPLE_LIMIT = 5000
REGISTRY_LARGE_SAMPLES = 5


@dataclass(slots=True)
class Prepared:
    """Everything seeded on top of the dataset for the scenarios."""

    dataset: Dataset
    drafts: list[Draft] = field(default_factory=list)
    logins: list[StaffLogin] = field(default_factory=list)
    booked: int = 0


def prepare(dataset: Dataset, *, editors: int) -> Prepared:
    """Seed drafts, a booked agenda day and staff sign-ins on the focus tenant."""
    focus = dataset.focus
    drafts = data.seed_drafts(focus, editors)
    # The neighbour tenant needs its own drafts only for the fairness checks.
    booked = data.seed_agenda(
        focus, visits_per_clinician=dataset.profile.visits_per_clinician_day
    )
    logins = provision_logins(focus, dataset.profile.staff_sessions)
    return Prepared(dataset=dataset, drafts=drafts, logins=logins, booked=booked)


def _timed(call: Callable[[], object]) -> float:
    started = time.perf_counter()
    call()
    return (time.perf_counter() - started) * 1000.0


def patient_search_blind_index(prepared: Prepared) -> ScenarioResult:
    """Exact-name search through HMAC blind indexes (ADR-015 trigger check)."""
    from apps.intake.patient_search import search_patients_exact  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    focus = prepared.dataset.focus
    rng = random.Random(prepared.dataset.seed)  # noqa: S311 - reproducible
    result = WorkerResult()
    with runtime_role():
        for _ in range(prepared.dataset.profile.samples):
            name = rng.choice(focus.sample_names)

            def call(name: str = name) -> None:
                with tenant_context(focus.admin_id, focus.organization_id):
                    page = search_patients_exact(
                        clinic_id=focus.clinic_id, query=name, limit=25
                    )
                if page.total < 1:
                    result.errors += 1

            result.samples_ms.append(_timed(call))
    return ScenarioResult(
        "patient_search_blind_index",
        "measured",
        result.samples_ms,
        result.errors,
        f"search_patients_exact over {focus.patient_count} patients, one tenant "
        "transaction per query including the audit write",
    )


def patient_search_registry(prepared: Prepared) -> ScenarioResult:
    """Substring registry search the staff patient list uses."""
    from apps.intake.patient_search import search_patients  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    focus = prepared.dataset.focus
    rng = random.Random(prepared.dataset.seed + 1)  # noqa: S311 - reproducible
    result = WorkerResult()
    # Each query scans every patient of the tenant: tens of seconds at 100k.
    count = (
        min(prepared.dataset.profile.samples, 60)
        if focus.patient_count <= REGISTRY_FULL_SAMPLE_LIMIT
        else REGISTRY_LARGE_SAMPLES
    )
    with runtime_role():
        for _ in range(count):
            token = rng.choice(focus.sample_names).rsplit(" ", 1)[-1]

            def call(token: str = token) -> None:
                with tenant_context(focus.admin_id, focus.organization_id):
                    page = search_patients(
                        clinic_id=focus.clinic_id, query=token, page=1
                    )
                if page.total < 1:
                    result.errors += 1

            result.samples_ms.append(_timed(call))
    return ScenarioResult(
        "patient_search_registry",
        "measured",
        result.samples_ms,
        result.errors,
        f"search_patients (normalized substring inside the database boundary) "
        f"over {focus.patient_count} patients; {count} queries; legacy substring "
        "registry workflow with no SLO assigned; follow-up: owner-approved "
        "performance contract",
    )


def _http_scenario(
    prepared: Prepared, scenario_id: str, paths: list[str], notes: str
) -> ScenarioResult:
    samples = prepared.dataset.profile.samples
    workers = min(HTTP_PROCESSES, len(prepared.logins))
    per_worker = max(1, samples // workers)
    context = context_for(prepared.dataset.focus)
    jobs = [
        (prepared.logins[index::workers], paths, per_worker) for index in range(workers)
    ]
    spawn = multiprocessing.get_context("spawn")
    with spawn.Pool(workers, initializer=child_setup, initargs=(context,)) as pool:
        parts = pool.starmap(http_worker, jobs)
    merged = WorkerResult()
    for part_samples, part_errors in parts:
        merged.samples_ms.extend(part_samples)
        merged.errors += part_errors
    return ScenarioResult(
        scenario_id,
        "measured",
        merged.samples_ms,
        merged.errors,
        f"{notes}; {workers} processes over {len(prepared.logins)} signed-in "
        "sessions (password + TOTP), closed loop; server-side time through the "
        "full middleware stack, no browser or network",
    )


def staff_navigation(prepared: Prepared) -> ScenarioResult:
    """Staff page navigation across the workspace shell."""
    focus = prepared.dataset.focus
    clinic = focus.clinic_id
    paths = [
        "/workspace/",
        f"/intake/clinics/{clinic}/patients/",
        f"/scheduling/clinics/{clinic}/agenda/",
        f"/scheduling/clinics/{clinic}/agenda/board/day/{data.AGENDA_DAY}/",
    ]
    return _http_scenario(
        prepared,
        "staff_navigation",
        paths,
        "workspace entry (follows its redirect), patients, agenda, day board",
    )


def agenda_interaction(prepared: Prepared) -> ScenarioResult:
    """Agenda board day/week/list views over a booked clinic day."""
    clinic = prepared.dataset.focus.clinic_id
    base = f"/scheduling/clinics/{clinic}/agenda/board"
    paths = [
        f"{base}/day/{data.AGENDA_DAY}/",
        f"{base}/week/{data.AGENDA_DAY}/",
        f"{base}/list/{data.AGENDA_DAY}/",
    ]
    return _http_scenario(
        prepared,
        "agenda_interaction",
        paths,
        f"{prepared.booked} appointments booked on the day",
    )


def autosave_run(
    prepared: Prepared,
    *,
    saves_per_editor: int,
) -> WorkerResult:
    """Run one concurrent editor per draft; return autosave ack latencies."""
    from apps.ehr.autosave import AutosaveStatus, autosave_draft  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    focus = prepared.dataset.focus
    drafts = prepared.drafts
    # Editors save at a fixed aggregate cadence instead of back to back, so the
    # measured time is the save, not queueing behind the harness's own threads.
    think_seconds = len(drafts) / AUTOSAVE_RATE_PER_SECOND

    def work(index: int, out: WorkerResult) -> None:
        draft = drafts[index]
        # The same simulated editor keeps its lock across alternating trials.
        session = uuid5(NAMESPACE_URL, f"clinic-perf-editor:{draft.version_id}")
        pause = threading.Event()
        revision = draft.revision
        for number in range(saves_per_editor):
            sections = {key: f"{value} {number}" for key, value in data.SOAP.items()}

            def call(
                sections: dict[str, str] = sections, expected: int = revision
            ) -> object:
                with tenant_context(draft.physician_id, focus.organization_id):
                    return autosave_draft(
                        clinic_id=focus.clinic_id,
                        version_id=draft.version_id,
                        expected_revision=expected,
                        editor_command_id=uuid4(),
                        editor_session_id=session,
                        sections=sections,
                    )

            started = time.perf_counter()
            outcome = call()
            out.samples_ms.append((time.perf_counter() - started) * 1000.0)
            if getattr(outcome, "status", None) != AutosaveStatus.SAVED:
                out.errors += 1
            else:
                revision = int(getattr(outcome, "revision", revision))
            pause.wait(think_seconds)
        draft.revision = revision

    return run_workers(len(drafts), work)


def autosave(prepared: Prepared) -> ScenarioResult:
    """Concurrent SOAP autosave acknowledgements."""
    editors = len(prepared.drafts)
    per_editor = max(1, prepared.dataset.profile.samples // editors)
    result = autosave_run(prepared, saves_per_editor=per_editor)
    return ScenarioResult(
        "autosave",
        "measured",
        result.samples_ms,
        result.errors,
        f"autosave_draft with {editors} concurrent editors, {per_editor} saves "
        f"each, paced to {AUTOSAVE_RATE_PER_SECOND:g} saves/s overall",
    )


def realtime_fanout(prepared: Prepared) -> ScenarioResult:
    """Commit-to-delivery latency of agenda hints over real Redis pub/sub."""
    from apps.realtime.transport import (  # noqa: PLC0415
        publish_on_commit,
        redis_client,
        topic_hash,
    )
    from django.db import connection, transaction  # noqa: PLC0415

    focus = prepared.dataset.focus
    topic = f"clinic:{focus.clinic_id}:agenda"
    subscribers = prepared.dataset.profile.staff_sessions
    ready = threading.Barrier(subscribers + 1)
    received: dict[int, list[float]] = {}
    stamps: dict[int, float] = {}
    done = threading.Event()
    lock = threading.Lock()

    def listen(index: int) -> None:
        with redis_client() as client, client.pubsub() as pubsub:
            pubsub.subscribe("rt:" + topic_hash(topic))
            pubsub.get_message(timeout=5)
            ready.wait()
            seen = 0
            while seen < FANOUT_PUBLISHES and not done.is_set():
                message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
                if message is None:
                    continue
                arrived = time.perf_counter()
                version = int(message["data"].split(b'"version":')[1].rstrip(b"}"))
                with lock:
                    received.setdefault(index, []).append(
                        (arrived - stamps[version]) * 1000.0
                    )
                seen += 1

    threads = [
        threading.Thread(target=listen, args=(index,)) for index in range(subscribers)
    ]
    for thread in threads:
        thread.start()
    ready.wait()
    with runtime_role():
        for version in range(1, FANOUT_PUBLISHES + 1):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                stamps[version] = time.perf_counter()
                publish_on_commit(topic=topic, kind="agenda", version=version)
    for thread in threads:
        thread.join(timeout=15)
    done.set()
    samples = [value for values in received.values() for value in values]
    missing = subscribers * FANOUT_PUBLISHES - len(samples)
    return ScenarioResult(
        "realtime_fanout",
        "measured",
        samples,
        max(missing, 0),
        f"publish_on_commit of an agenda hint to {subscribers} Redis subscribers, "
        f"{FANOUT_PUBLISHES} commits; one process, so client-side GIL time is "
        "included; a missed delivery counts as an error",
    )


def chunk_upload(prepared: Prepared) -> ScenarioResult:
    """Measure committed ingestion acknowledgements for synthetic Opus slices."""
    import secrets  # noqa: PLC0415
    from hashlib import sha256  # noqa: PLC0415

    from apps.scribe.contracts import ChunkInput  # noqa: PLC0415
    from apps.scribe.models import CaptureEpoch  # noqa: PLC0415
    from apps.scribe.services import (  # noqa: PLC0415
        control_recording,
        ingest_chunk,
    )
    from apps.tenancy.db import tenant_context  # noqa: PLC0415
    from django.test.utils import override_settings  # noqa: PLC0415

    focus = prepared.dataset.focus
    result = WorkerResult()
    body = secrets.token_bytes(CHUNK_BYTES)
    with override_settings(SCRIBE_SYNTHETIC_CAPTURE=True):
        recording = data.seed_recording(focus)
        with runtime_role():
            for number in range(prepared.dataset.profile.samples):
                with tenant_context(focus.physician_ids[0], focus.organization_id):
                    control_recording(
                        clinic_id=focus.clinic_id,
                        session_id=recording.session_id,
                        encounter_token=recording.token,
                        command_id=uuid4(),
                        action="start" if number == 0 else "resume",
                        expected_epoch=number,
                    )
                    epoch = CaptureEpoch.objects.get(
                        session_id=recording.session_id, number=number + 1
                    ).pk
                chunk = ChunkInput(
                    session_id=recording.session_id,
                    epoch=epoch,
                    track="microphone",
                    seq=0,
                    sha256=sha256(body).hexdigest(),
                    duration_ms=2000,
                    codec="audio/webm;codecs=opus",
                    data=body,
                )

                def call(chunk: ChunkInput = chunk) -> None:
                    with tenant_context(focus.physician_ids[0], focus.organization_id):
                        receipt = ingest_chunk(
                            clinic_id=focus.clinic_id,
                            encounter_token=recording.token,
                            chunk=chunk,
                        )
                    if receipt.ack != "received":
                        result.errors += 1

                result.samples_ms.append(_timed(call))
                with tenant_context(focus.physician_ids[0], focus.organization_id):
                    control_recording(
                        clinic_id=focus.clinic_id,
                        session_id=recording.session_id,
                        encounter_token=recording.token,
                        command_id=uuid4(),
                        action="pause",
                        expected_epoch=number + 1,
                        last_seq=0,
                    )
            with tenant_context(focus.physician_ids[0], focus.organization_id):
                control_recording(
                    clinic_id=focus.clinic_id,
                    session_id=recording.session_id,
                    encounter_token=recording.token,
                    command_id=uuid4(),
                    action="stop",
                    expected_epoch=prepared.dataset.profile.samples,
                )
    return ScenarioResult(
        "chunk_upload",
        "measured",
        result.samples_ms,
        result.errors,
        f"ingest_chunk acknowledgement through tenant commit for {CHUNK_BYTES}-byte "
        "synthetic two-second slices: authority/consent, encryption, AudioChunk, "
        "outbox and receipt audit; untimed setup/control supplies a fresh server "
        "epoch per sample, no clock changes; asynchronous object delivery excluded",
    )


def _modeled(
    prepared: Prepared,
    scenario_id: str,
    chunk: ScenarioResult,
    build: Callable[[float, random.Random], float],
) -> ScenarioResult:
    rng = random.Random(prepared.dataset.seed + 7)  # noqa: S311 - reproducible
    base = sorted(chunk.samples_ms)
    samples = [
        build(base[int(rng.random() * len(base))], rng)
        for _ in range(prepared.dataset.profile.samples)
    ]
    params = ModelParams().as_dict()
    return ScenarioResult(
        scenario_id,
        "modeled",
        samples,
        0,
        "modeled over assumed provider timings, not a measurement; chunk cost "
        f"sampled from measured chunk ingestion; assumptions={params}",
    )


def transcript_partial(prepared: Prepared, chunk: ScenarioResult) -> ScenarioResult:
    """Model the partial-transcript path with fake ASR timing."""
    params = ModelParams()
    return _modeled(
        prepared,
        "transcript_partial",
        chunk,
        lambda ingest, rng: transcript_partial_ms(
            params=params, ingest_ms=ingest, rng=rng
        ),
    )


def draft_generation(prepared: Prepared, chunk: ScenarioResult) -> ScenarioResult:
    """Model the final draft for the 30-minute reference encounter."""
    params = ModelParams()
    return _modeled(
        prepared,
        "draft_generation",
        chunk,
        lambda persist, rng: draft_generation_ms(
            params=params, persist_ms=persist, rng=rng
        ),
    )
