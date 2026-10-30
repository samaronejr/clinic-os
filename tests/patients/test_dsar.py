"""Todo 50 data-subject requests (IS-19): LGPD rights as an auditable workflow.

Test-first contract for ``apps.intake.data_requests`` and the
``intake_datasubjectrequest`` table, written before either exists. A request
moves received -> in_progress -> fulfilled | rejected, or received -> rejected
for a duplicate. The due date and the overdue flag come from the database
clock (``clinic_app.data_request_now``) and equality counts as overdue. Every
refusal is side-effect free and identical for unknown, foreign and
out-of-scope selectors. Access and portability reuse the deterministic
released-record export, so a delegate never receives a record restricted from
delegates. Correction never edits clinical content: it opens a review task and
the clinical change, if any, goes through an amendment.
"""

from __future__ import annotations

import dataclasses
import io
import json
import zipfile
from contextlib import contextmanager
from datetime import timedelta
from queue import Queue
from threading import Barrier, Thread
from typing import TYPE_CHECKING
from unittest.mock import Mock
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.consent.models import ConsentRevocation
from apps.consent.services import prepare_acceptance, publish_text, record_consent
from apps.ehr.finalization import amend_document, finalize_version
from apps.ehr.services import record_clinical_note
from apps.identity.delegates import switch_delegate
from apps.identity.models import UserClinicRole
from apps.intake import data_request_views
from apps.intake.access import PatientAccessDeniedError
from apps.intake.data_requests import (
    DATA_REQUEST_DETAILS_MAX,
    DATA_REQUEST_DUE,
    DATA_REQUEST_KINDS,
    DATA_REQUEST_REJECTION_REASONS,
    DataRequestConflictError,
    DataRequestInputError,
    DataSubjectRequestReceipt,
    export_data_subject_copy,
    fulfill_data_subject_request,
    patient_data_subject_requests,
    reject_data_subject_request,
    sharing_information,
    staff_data_subject_requests,
    start_data_subject_request,
    submit_data_subject_request,
)
from apps.intake.models import DataSubjectRequest
from apps.intake.patient_access import (
    PATIENT_SESSION_KEY,
    end_patient_session,
    patient_session_context,
)
from apps.providers.models import ProviderCapability
from apps.retention.services import (
    patient_released_records,
    release_version,
    verify_export_package,
)
from apps.tenancy.db import tenant_context
from apps.workflows.models import Task
from django.db import (
    DatabaseError,
    close_old_connections,
    connection,
    connections,
    transaction,
)
from django.test import Client

from auth.stepup_test_support import verified_request
from identity.permission_support import permission_actor, permission_context
from patient_http_support import audit_event_types, audit_payloads
from patient_service_support import runtime_role
from patients.delegate_support import active_grant, delegate_world
from provider_gate_support import activate_capability, seed_capabilities
from renewal.test_amendments import AMENDED
from renewal.test_encounters import setup_context
from renewal.test_patient_access import _issue, _redeem

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime
    from pathlib import Path

    from apps.identity.delegates import DelegateReceipt

    from patients.delegate_support import DelegateWorld
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

SENTINEL = "SINTETICO-SENTINELA-DSAR endereco antigo da Rua Sintetica 12"
AUDIT_KEYS = frozenset(
    {
        "clinic_id",
        "http_method",
        "http_status",
        "object_verb",
        "reason_code",
        "request_id",
    }
)
# Management roles answer data-subject requests; every other staff role is
# refused exactly like an unknown request. The partition covers the full
# role vocabulary so a new role cannot slip through unclassified.
ANSWERING_ROLES = frozenset({"clinic_manager", "clinic_admin", "org_admin", "owner"})
REFUSED_ROLES = frozenset(UserClinicRole.Role.values) - ANSWERING_ROLES


@dataclasses.dataclass(frozen=True)
class Releases:
    restricted_version_id: UUID
    shared_version_id: UUID


@dataclasses.dataclass(frozen=True)
class DsarWorld:
    delegate: DelegateWorld
    patient_session_id: UUID


def _world(graph: RbacGraph, root: Path) -> DsarWorld:
    world = delegate_world(graph, root)
    _, secret = _issue(graph, world.notes.enrollment_id)
    session = _redeem(graph.clinic_a, secret)
    assert session is not None
    return DsarWorld(world, session)


def _releases(graph: RbacGraph, world: DsarWorld) -> Releases:
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        original = finalize_version(
            clinic_id=graph.clinic_a,
            version_id=world.delegate.notes.first_version_id,
            expected_revision=2,
            request=request,
        )
        amendment = amend_document(
            clinic_id=graph.clinic_a,
            version_id=original.pk,
            reason="Synthetic data-subject request acceptance",
        )
        saved = record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=amendment.pk,
            expected_revision=1,
            content=AMENDED,
        )
        amended = finalize_version(
            clinic_id=graph.clinic_a,
            version_id=saved.pk,
            expected_revision=2,
            request=request,
        )
        restricted = release_version(clinic_id=graph.clinic_a, version_id=original.pk)
        shared = release_version(
            clinic_id=graph.clinic_a,
            version_id=amended.pk,
            restricted_from_delegates=False,
        )
    return Releases(restricted.version_id, shared.version_id)


def _act_for(graph: RbacGraph, world: DsarWorld, grant: DelegateReceipt) -> None:
    with runtime_role(), patient_session_context(world.delegate.session_id):
        switch_delegate(clinic_id=graph.clinic_a, grant_id=grant.grant_id)


def _rows(graph: RbacGraph) -> list[tuple[object, ...]]:
    with setup_context(graph.organization_a):
        return list(
            DataSubjectRequest.objects.order_by("received_at", "id").values_list(
                "id", "kind", "state", "revision"
            )
        )


def _dsar_events(graph: RbacGraph) -> list[str]:
    return [
        event
        for event in audit_event_types(graph, graph.clinic_admin)
        if event.startswith("intake.data_request.")
    ]


def _exported_record_ids(data: bytes) -> list[UUID]:
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        manifest = json.loads(package.read("manifest.json"))
    return sorted(UUID(entry["record_id"]) for entry in manifest["files"])


def _members(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        return "\n".join(
            package.read(name).decode("utf-8", errors="replace")
            for name in package.namelist()
        )


@contextmanager
def data_request_clock(at: datetime) -> Iterator[None]:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(
            "CREATE OR REPLACE FUNCTION clinic_app.data_request_now() "
            "RETURNS timestamptz LANGUAGE sql STABLE SECURITY DEFINER "
            "SET search_path=pg_catalog,clinic_app,pg_temp AS "
            "$f$ SELECT %s::timestamptz $f$".replace("%s", "'" + at.isoformat() + "'")
        )
        cursor.execute("RESET ROLE")
    try:
        yield
    finally:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_resolver")
            cursor.execute(
                "CREATE OR REPLACE FUNCTION clinic_app.data_request_now() "
                "RETURNS timestamptz LANGUAGE sql STABLE SECURITY DEFINER "
                "SET search_path=pg_catalog,clinic_app,pg_temp AS "
                "$f$ SELECT statement_timestamp() $f$"
            )
            cursor.execute("RESET ROLE")


def _submit(  # noqa: PLR0913 - the contract's full keyword surface
    graph: RbacGraph,
    session_id: UUID,
    kind: str,
    *,
    details: str = "",
    purpose: str = "",
    key: UUID | None = None,
) -> DataSubjectRequestReceipt:
    with runtime_role(), patient_session_context(session_id):
        return submit_data_subject_request(
            clinic_id=graph.clinic_a,
            kind=kind,
            details=details,
            purpose=purpose,
            idempotency_key=key or uuid4(),
        )


def _manager(graph: RbacGraph) -> UUID:
    actor, _ = permission_actor(graph, "clinic_manager")
    return actor


def _set_active(actor: UUID, *, active: bool) -> None:
    # Raw SQL as the owner: no model hook or Python actor check is involved.
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE clinic_app.identity_user SET is_active=%s WHERE id=%s",
            [active, actor],
        )
        assert cursor.rowcount == 1


def _start_as(graph: RbacGraph, actor: UUID, request_id: UUID) -> str:
    """Start one request through the resolver alone; return its state or SQLSTATE."""
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true),"
            "set_config('app.current_user_id',%s,true)",
            [str(graph.organization_a), str(actor)],
        )
        try:
            with transaction.atomic():
                cursor.execute(
                    "SELECT state FROM clinic_app.transition_data_request("
                    "%s,%s,1,'in_progress','')",
                    [graph.clinic_a, request_id],
                )
                row = cursor.fetchone()
        except DatabaseError as error:
            cause = error.__cause__
            if not isinstance(cause, psycopg.Error):
                raise
            return str(cause.sqlstate)
    assert row is not None
    return str(row[0])


def _direct_start(graph: RbacGraph, actor: UUID, request_id: UUID) -> None:
    """Move one request straight through the row guard as ``actor``."""
    with setup_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.current_user_id',%s,true)", [str(actor)])
        cursor.execute(
            "UPDATE clinic_app.intake_datasubjectrequest SET state='in_progress',"
            "revision=revision+1,updated_by_id=%s WHERE id=%s",
            [actor, request_id],
        )


def test_request_table_is_forced_rls_append_only_and_transition_guarded(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = _world(rbac_graph, tmp_path)
    receipt = _submit(rbac_graph, world.patient_session_id, "access")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.relrowsecurity, c.relforcerowsecurity, "
            "has_table_privilege('clinic_app', c.oid, 'DELETE'), "
            "has_table_privilege('clinic_app', c.oid, 'TRUNCATE') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='clinic_app' AND c.relname='intake_datasubjectrequest'"
        )
        assert cursor.fetchone() == (True, True, False, False)
        cursor.execute(
            "SELECT policyname, roles::text[], cmd FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename='intake_datasubjectrequest' "
            "ORDER BY policyname"
        )
        assert cursor.fetchall() == [
            ("privacy_staff_read", ["clinic_app"], "SELECT"),
            ("setup_tenant", ["clinic_owner"], "ALL"),
        ]
        cursor.execute(
            "SELECT grantee, privilege_type FROM information_schema.role_table_grants "
            "WHERE table_schema='clinic_app' "
            "AND table_name='intake_datasubjectrequest' "
            "AND grantee IN ('clinic_app','clinic_resolver','clinic_agent','PUBLIC') "
            "ORDER BY 1, 2"
        )
        assert cursor.fetchall() == [
            ("clinic_app", "SELECT"),
            ("clinic_resolver", "INSERT"),
            ("clinic_resolver", "SELECT"),
        ]
    with pytest.raises(DatabaseError), setup_context(rbac_graph.organization_a):
        DataSubjectRequest.objects.filter(pk=receipt.request_id).delete()
    with pytest.raises(DatabaseError), setup_context(rbac_graph.organization_a):
        DataSubjectRequest.objects.filter(pk=receipt.request_id).update(
            state="fulfilled"
        )
    with runtime_role(), connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM clinic_app.intake_datasubjectrequest")
        assert cursor.fetchone() == (0,)
    assert _rows(rbac_graph) == [(receipt.request_id, "access", "received", 1)]


def test_every_kind_is_received_with_database_due_date_task_and_idempotent_replay(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = _world(rbac_graph, tmp_path)
    assert DATA_REQUEST_KINDS == (
        "access",
        "correction",
        "portability",
        "sharing_information",
        "objection",
    )
    assert timedelta(days=15) == DATA_REQUEST_DUE
    arguments: dict[str, dict[str, str]] = {
        "access": {},
        "correction": {"details": SENTINEL},
        "portability": {},
        "sharing_information": {},
        "objection": {"purpose": "research_model_improvement"},
    }
    for kind in DATA_REQUEST_KINDS:
        key = uuid4()
        first = _submit(
            rbac_graph, world.patient_session_id, kind, key=key, **arguments[kind]
        )
        replay = _submit(
            rbac_graph, world.patient_session_id, kind, key=key, **arguments[kind]
        )
        assert replay == first
        assert (first.kind, first.state, first.revision) == (kind, "received", 1)
        assert first.delegate_acted is False
        assert first.due_at - first.received_at == DATA_REQUEST_DUE
        other = "sharing_information" if kind != "sharing_information" else "access"
        with pytest.raises(DataRequestConflictError):
            _submit(rbac_graph, world.patient_session_id, other, key=key)
        with setup_context(rbac_graph.organization_a):
            task = Task.objects.get(pk=first.task_id)
        assert (task.kind, task.state, task.owner_role) == (
            "review",
            "open",
            "clinic_manager",
        )
        assert task.subject_ref == {
            "kind": "enrollment",
            "id": str(world.delegate.notes.enrollment_id),
        }
        assert task.due_at == first.due_at
        assert task.created_by_id is None
    assert _dsar_events(rbac_graph) == ["intake.data_request.received"] * len(
        DATA_REQUEST_KINDS
    )
    payloads = audit_payloads(
        rbac_graph, rbac_graph.clinic_admin, "intake.data_request.received"
    )
    assert {payload["key"] for payload in payloads} <= AUDIT_KEYS
    assert all(SENTINEL not in payload["value"] for payload in payloads)
    with setup_context(rbac_graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT details::text FROM clinic_app.intake_datasubjectrequest "
            "WHERE kind='correction'"
        )
        raw = cursor.fetchone()
    assert raw is not None
    assert SENTINEL not in str(raw[0])


def test_concurrent_submissions_of_one_key_store_one_request_and_one_task(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = _world(rbac_graph, tmp_path)
    _, secret = _issue(rbac_graph, world.delegate.notes.enrollment_id)
    second_session = _redeem(rbac_graph.clinic_a, secret)
    assert second_session is not None
    key = uuid4()
    barrier = Barrier(2)
    outcomes: Queue[DataSubjectRequestReceipt | Exception] = Queue()

    def worker(session_id: UUID) -> None:
        close_old_connections()
        try:
            with runtime_role(), patient_session_context(session_id):
                # Two live sessions of one patient meet at the same command.
                barrier.wait(timeout=10)
                outcomes.put(
                    submit_data_subject_request(
                        clinic_id=rbac_graph.clinic_a,
                        kind="access",
                        details="",
                        purpose="",
                        idempotency_key=key,
                    )
                )
        except (DataRequestConflictError, PatientAccessDeniedError) as error:
            outcomes.put(error)
        finally:
            connections.close_all()

    threads = [
        Thread(target=worker, args=(session_id,))
        for session_id in (world.patient_session_id, second_session)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    first, other = outcomes.get_nowait(), outcomes.get_nowait()
    assert isinstance(first, DataSubjectRequestReceipt), first
    assert other == first
    assert len(_rows(rbac_graph)) == 1
    assert _dsar_events(rbac_graph) == ["intake.data_request.received"]
    with setup_context(rbac_graph.organization_a):
        assert list(
            Task.objects.filter(pk=first.task_id).values_list("state", flat=True)
        ) == ["open"]


@pytest.mark.parametrize(
    ("kind", "details", "purpose"),
    [
        ("erasure", "", ""),
        ("", "", ""),
        ("correction", "", ""),
        ("correction", "x" * (DATA_REQUEST_DETAILS_MAX + 1), ""),
        ("access", SENTINEL, ""),
        ("objection", "", ""),
        ("objection", "", "care_processing"),
        ("objection", "", "unknown_purpose"),
        ("portability", "", "marketing"),
    ],
)
def test_malformed_requests_refuse_before_any_write(
    rbac_graph: RbacGraph, tmp_path: Path, kind: str, details: str, purpose: str
) -> None:
    world = _world(rbac_graph, tmp_path)
    with pytest.raises(DataRequestInputError):
        _submit(
            rbac_graph,
            world.patient_session_id,
            kind,
            details=details,
            purpose=purpose,
        )
    assert _rows(rbac_graph) == []
    assert _dsar_events(rbac_graph) == []
    with setup_context(rbac_graph.organization_a):
        assert not Task.objects.filter(kind="review").exists()


def test_scope_selectors_fail_identically_and_revoked_sessions_cannot_submit(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = _world(rbac_graph, tmp_path)
    for clinic_id in (rbac_graph.clinic_b, uuid4()):
        with (
            pytest.raises(PatientAccessDeniedError),
            runtime_role(),
            patient_session_context(world.patient_session_id),
        ):
            submit_data_subject_request(
                clinic_id=clinic_id,
                kind="access",
                details="",
                purpose="",
                idempotency_key=uuid4(),
            )
    with runtime_role(), patient_session_context(world.patient_session_id):
        end_patient_session(world.patient_session_id)
    with (
        pytest.raises(PatientAccessDeniedError),
        runtime_role(),
        patient_session_context(world.patient_session_id),
    ):
        submit_data_subject_request(
            clinic_id=rbac_graph.clinic_a,
            kind="access",
            details="",
            purpose="",
            idempotency_key=uuid4(),
        )
    assert _rows(rbac_graph) == []
    assert _dsar_events(rbac_graph) == []


def test_access_and_portability_reuse_the_released_record_export(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = _world(rbac_graph, tmp_path)
    releases = _releases(rbac_graph, world)
    access = _submit(rbac_graph, world.patient_session_id, "access")
    portability = _submit(rbac_graph, world.patient_session_id, "portability")
    correction = _submit(
        rbac_graph, world.patient_session_id, "correction", details=SENTINEL
    )
    with runtime_role(), patient_session_context(world.patient_session_id):
        released = sorted(record.version_id for record in patient_released_records())
        assert released == sorted(
            [releases.restricted_version_id, releases.shared_version_id]
        )
        for receipt in (access, portability):
            package = export_data_subject_copy(
                clinic_id=rbac_graph.clinic_a, request_id=receipt.request_id
            )
            assert verify_export_package(package.data).ok
            assert _exported_record_ids(package.data) == released
        with pytest.raises(DataRequestInputError):
            export_data_subject_copy(
                clinic_id=rbac_graph.clinic_a, request_id=correction.request_id
            )
        for unknown in (uuid4(), rbac_graph.clinic_a):
            with pytest.raises(PatientAccessDeniedError):
                export_data_subject_copy(
                    clinic_id=rbac_graph.clinic_a, request_id=unknown
                )
    assert _dsar_events(rbac_graph).count("intake.data_request.exported") == 2


def test_delegate_export_excludes_restricted_records_and_foreign_requests(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = _world(rbac_graph, tmp_path)
    releases = _releases(rbac_graph, world)
    own = _submit(rbac_graph, world.patient_session_id, "access")
    grant = active_grant(rbac_graph, world.delegate)
    _act_for(rbac_graph, world, grant)
    represented = _submit(rbac_graph, world.delegate.session_id, "access")
    assert represented.delegate_acted is True
    with runtime_role(), patient_session_context(world.delegate.session_id):
        visible = [row.request_id for row in patient_data_subject_requests()]
        assert visible == [represented.request_id]
        package = export_data_subject_copy(
            clinic_id=rbac_graph.clinic_a, request_id=represented.request_id
        )
        assert _exported_record_ids(package.data) == [releases.shared_version_id]
        assert str(releases.restricted_version_id) not in _members(package.data)
        for selected in (own.request_id, uuid4()):
            with pytest.raises(PatientAccessDeniedError):
                export_data_subject_copy(
                    clinic_id=rbac_graph.clinic_a, request_id=selected
                )
    with runtime_role(), patient_session_context(world.patient_session_id):
        everyone = {row.request_id for row in patient_data_subject_requests()}
        assert everyone == {own.request_id, represented.request_id}
        own_package = export_data_subject_copy(
            clinic_id=rbac_graph.clinic_a, request_id=own.request_id
        )
        assert releases.restricted_version_id in _exported_record_ids(own_package.data)
    client = Client()
    session = client.session
    session[PATIENT_SESSION_KEY] = str(world.delegate.session_id)
    session.save()
    before = _rows(rbac_graph)
    events = _dsar_events(rbac_graph)
    with runtime_role():
        attempts = [
            client.post(
                "/patient/data-requests/",
                {"action": "export", "request_id": str(selected)},
            )
            for selected in (own.request_id, uuid4())
        ]
    assert [response.status_code for response in attempts] == [403, 403]
    assert attempts[0].content == attempts[1].content
    assert _rows(rbac_graph) == before
    assert _dsar_events(rbac_graph) == events


def test_delegate_operations_and_purposes_bound_their_requests(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = _world(rbac_graph, tmp_path)
    grant = active_grant(
        rbac_graph,
        world.delegate,
        operations=("enrollment_view", "consent"),
        purposes=("teleconsultation",),
    )
    _act_for(rbac_graph, world, grant)
    for kind, purpose in (
        ("access", ""),
        ("portability", ""),
        ("objection", "marketing"),
    ):
        with pytest.raises(PatientAccessDeniedError):
            _submit(rbac_graph, world.delegate.session_id, kind, purpose=purpose)
    allowed = _submit(
        rbac_graph, world.delegate.session_id, "objection", purpose="teleconsultation"
    )
    assert allowed.delegate_acted is True
    assert [row[1] for row in _rows(rbac_graph)] == ["objection"]


def test_objection_withdraws_only_the_named_accepted_purpose(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = _world(rbac_graph, tmp_path)
    manager = _manager(rbac_graph)
    with permission_context(rbac_graph, manager):
        texts = {
            purpose: publish_text(
                clinic_id=rbac_graph.clinic_a,
                purpose=purpose,
                text=f"Synthetic {purpose} text",
            )
            for purpose in ("marketing", "teleconsultation")
        }
    with runtime_role(), patient_session_context(world.patient_session_id):
        accepted = {}
        for purpose, text in texts.items():
            _, offer = prepare_acceptance(text_id=text.pk)
            accepted[purpose] = record_consent(
                offer=offer, purpose=purpose, accepted=True
            )
    withdrawn = _submit(
        rbac_graph, world.patient_session_id, "objection", purpose="marketing"
    )
    assert withdrawn.state == "fulfilled"
    pending = _submit(
        rbac_graph,
        world.patient_session_id,
        "objection",
        purpose="research_model_improvement",
    )
    assert pending.state == "received"
    with setup_context(rbac_graph.organization_a):
        revoked = set(ConsentRevocation.objects.values_list("acceptance_id", flat=True))
    assert revoked == {accepted["marketing"].pk}
    assert _dsar_events(rbac_graph) == [
        "intake.data_request.received",
        "intake.data_request.fulfilled",
        "intake.data_request.received",
    ]


def test_sharing_information_projects_the_provider_registry_without_secrets(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = _world(rbac_graph, tmp_path)
    # Transactional tests flush the migration seed; rebuild it, then drive one
    # capability to ``activated`` through the real owner path: under the
    # synthetic data mode it must still never be reported as live.
    seed_capabilities()
    activated = activate_capability("llm_inference")
    assert activated.state == "activated"
    with setup_context(rbac_graph.organization_a):
        keys = set(ProviderCapability.objects.values_list("key", flat=True))
    with runtime_role(), patient_session_context(world.patient_session_id):
        entries = sharing_information()
    assert entries
    assert {entry.capability_key for entry in entries} == keys
    assert all(
        tuple(field.name for field in dataclasses.fields(entry))
        == ("capability_key", "state", "live")
        for entry in entries
    )
    assert not any(entry.live for entry in entries)
    states = {entry.capability_key: entry.state for entry in entries}
    assert states["llm_inference"] == "activated"
    with pytest.raises(PatientAccessDeniedError), runtime_role():
        sharing_information()


def test_staff_lifecycle_is_exact_closes_the_task_and_never_edits_records(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = _world(rbac_graph, tmp_path)
    releases = _releases(rbac_graph, world)
    with runtime_role(), patient_session_context(world.patient_session_id):
        digests = sorted(
            (record.version_id, record.content_digest)
            for record in patient_released_records()
        )
    correction = _submit(
        rbac_graph, world.patient_session_id, "correction", details=SENTINEL
    )
    duplicate = _submit(
        rbac_graph, world.patient_session_id, "correction", details=SENTINEL
    )
    manager = _manager(rbac_graph)
    clinic = rbac_graph.clinic_a
    with permission_context(rbac_graph, manager):
        queue = {
            row.request_id: row for row in staff_data_subject_requests(clinic_id=clinic)
        }
        assert set(queue) == {correction.request_id, duplicate.request_id}
        assert queue[correction.request_id].details == SENTINEL
        started = start_data_subject_request(
            clinic_id=clinic, request_id=correction.request_id, expected_revision=1
        )
        assert (started.state, started.revision) == ("in_progress", 2)
        with pytest.raises(DataRequestConflictError):
            start_data_subject_request(
                clinic_id=clinic, request_id=correction.request_id, expected_revision=1
            )
        done = fulfill_data_subject_request(
            clinic_id=clinic, request_id=correction.request_id, expected_revision=2
        )
        assert (done.state, done.revision) == ("fulfilled", 3)
        for command in (start_data_subject_request, fulfill_data_subject_request):
            with pytest.raises(DataRequestConflictError):
                command(
                    clinic_id=clinic,
                    request_id=correction.request_id,
                    expected_revision=3,
                )
        with pytest.raises(DataRequestInputError):
            reject_data_subject_request(
                clinic_id=clinic,
                request_id=duplicate.request_id,
                expected_revision=1,
                reason_code="because",
            )
        assert "duplicate" in DATA_REQUEST_REJECTION_REASONS
        rejected = reject_data_subject_request(
            clinic_id=clinic,
            request_id=duplicate.request_id,
            expected_revision=1,
            reason_code="duplicate",
        )
        assert (rejected.state, rejected.revision) == ("rejected", 2)
    with setup_context(rbac_graph.organization_a):
        states = set(
            Task.objects.filter(
                pk__in=[correction.task_id, duplicate.task_id]
            ).values_list("state", flat=True)
        )
    assert states == {"done"}
    with runtime_role(), patient_session_context(world.patient_session_id):
        after = sorted(
            (record.version_id, record.content_digest)
            for record in patient_released_records()
        )
        rows = {row.request_id: row for row in patient_data_subject_requests()}
    assert after == digests
    assert releases.shared_version_id in {version for version, _ in after}
    assert rows[duplicate.request_id].rejection_reason == "duplicate"
    assert rows[correction.request_id].closed_at is not None
    assert _dsar_events(rbac_graph) == [
        "intake.data_request.received",
        "intake.data_request.received",
        "intake.data_request.started",
        "intake.data_request.fulfilled",
        "intake.data_request.rejected",
    ]


def test_only_management_roles_answer_and_other_roles_match_unknown_requests(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    assert frozenset(UserClinicRole.Role.values) >= ANSWERING_ROLES
    world = _world(rbac_graph, tmp_path)
    receipt = _submit(rbac_graph, world.patient_session_id, "sharing_information")
    before = _rows(rbac_graph)
    for role in sorted(REFUSED_ROLES):
        actor, _ = permission_actor(rbac_graph, role)
        for selected in (receipt.request_id, uuid4()):
            with (
                pytest.raises(PatientAccessDeniedError),
                permission_context(rbac_graph, actor),
            ):
                start_data_subject_request(
                    clinic_id=rbac_graph.clinic_a,
                    request_id=selected,
                    expected_revision=1,
                )
        with (
            pytest.raises(PatientAccessDeniedError),
            permission_context(rbac_graph, actor),
        ):
            staff_data_subject_requests(clinic_id=rbac_graph.clinic_a)
    assert _rows(rbac_graph) == before
    for role in sorted(ANSWERING_ROLES):
        actor, _ = permission_actor(rbac_graph, role)
        with permission_context(rbac_graph, actor):
            listed = staff_data_subject_requests(clinic_id=rbac_graph.clinic_a)
        assert [row.request_id for row in listed] == [receipt.request_id], role
    manager = _manager(rbac_graph)
    with (
        pytest.raises(PatientAccessDeniedError),
        permission_context(rbac_graph, manager),
    ):
        staff_data_subject_requests(clinic_id=rbac_graph.clinic_b)
    # Both staff SQL paths delegate the active-account check to
    # clinic_app.has_permission: an inactive manager is refused by the row
    # guard and by the resolver, and admitted again once the account is active.
    _set_active(manager, active=False)
    with pytest.raises(DatabaseError, match="invalid data subject request transition"):
        _direct_start(rbac_graph, manager, receipt.request_id)
    assert _start_as(rbac_graph, manager, receipt.request_id) == "42501"
    _set_active(manager, active=True)
    assert _start_as(rbac_graph, manager, receipt.request_id) == "in_progress"
    assert _dsar_events(rbac_graph) == ["intake.data_request.received"]


def test_overdue_is_database_time_and_equality_is_overdue(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = _world(rbac_graph, tmp_path)
    receipt = _submit(rbac_graph, world.patient_session_id, "access")
    manager = _manager(rbac_graph)
    observed = []
    for at in (
        receipt.due_at - timedelta(microseconds=1),
        receipt.due_at,
        receipt.due_at + timedelta(days=1),
    ):
        with data_request_clock(at), permission_context(rbac_graph, manager):
            (row,) = staff_data_subject_requests(clinic_id=rbac_graph.clinic_a)
            observed.append(row.overdue)
    assert observed == [False, True, True]
    pinned = receipt.due_at + timedelta(days=2)
    with data_request_clock(pinned):
        later = _submit(rbac_graph, world.patient_session_id, "portability")
    assert later.received_at == pinned
    assert later.due_at == pinned + DATA_REQUEST_DUE


def test_other_tenants_and_other_sessions_see_no_requests(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = _world(rbac_graph, tmp_path)
    _submit(rbac_graph, world.patient_session_id, "access")
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_b),
    ):
        assert DataSubjectRequest.objects.count() == 0
    with runtime_role(), transaction.atomic():
        assert DataSubjectRequest.objects.count() == 0
    with runtime_role(), patient_session_context(world.delegate.session_id):
        assert patient_data_subject_requests() == ()


def test_patient_page_submits_in_the_body_and_refuses_without_side_effects(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = _world(rbac_graph, tmp_path)
    client = Client()
    session = client.session
    session[PATIENT_SESSION_KEY] = str(world.patient_session_id)
    session.save()
    with runtime_role():
        page = client.get("/patient/data-requests/")
        created = client.post(
            "/patient/data-requests/",
            {
                "action": "submit",
                "kind": "correction",
                "details": SENTINEL,
                "idempotency_key": str(uuid4()),
            },
        )
    assert page.status_code == 200
    assert b"data-data-request-form" in page.content
    assert created.status_code == 200
    assert b'data-data-request-state="received"' in created.content
    before = _rows(rbac_graph)
    events = _dsar_events(rbac_graph)
    with runtime_role():
        refusals = [
            client.post("/patient/data-requests/", body)
            for body in (
                {"action": "export", "request_id": str(uuid4())},
                {"action": "export", "request_id": "not-a-uuid"},
                {"action": "erase"},
            )
        ]
    assert [response.status_code for response in refusals] == [403, 403, 403]
    assert len({response.content for response in refusals}) == 1
    assert all("Set-Cookie" not in response.headers for response in refusals)
    assert _rows(rbac_graph) == before
    assert _dsar_events(rbac_graph) == events


def test_patient_page_keeps_input_and_conflict_refusals_on_the_page(
    rbac_graph: RbacGraph, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(rbac_graph, tmp_path)
    client = Client()
    session = client.session
    session[PATIENT_SESSION_KEY] = str(world.patient_session_id)
    session.save()
    url = "/patient/data-requests/"
    key = str(uuid4())
    with runtime_role():
        malformed = [
            client.post(
                url, {"action": "submit", "kind": "access", "idempotency_key": "x"}
            ),
            client.post(
                url,
                {"action": "submit", "kind": "erasure", "idempotency_key": key},
            ),
        ]
        first = client.post(
            url, {"action": "submit", "kind": "access", "idempotency_key": key}
        )
        replayed = client.post(
            url, {"action": "submit", "kind": "portability", "idempotency_key": key}
        )
    # A copy too large for a synchronous download is the export's conflict.
    monkeypatch.setattr(
        data_request_views,
        "export_data_subject_copy",
        Mock(side_effect=DataRequestConflictError),
    )
    with runtime_role():
        too_large = client.post(url, {"action": "export", "request_id": str(uuid4())})
    assert [response.status_code for response in malformed] == [400, 400]
    assert first.status_code == 200
    assert replayed.status_code == 409
    assert too_large.status_code == 409
    assert all(
        b"data-data-request-form" in response.content
        for response in (*malformed, replayed, too_large)
    )
    assert [row[1] for row in _rows(rbac_graph)] == ["access"]
    assert _dsar_events(rbac_graph) == ["intake.data_request.received"]
