"""Real-role runtime acceptance; authored here but not run in the source-only lane."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.ai.artifacts import create_input
from apps.ai.contracts import Purpose
from apps.ai.models import AIArtifact
from apps.audit.services import verify_chain
from apps.core.telemetry import METRICS
from apps.ehr.attachment_storage import AttachmentStorageError
from apps.ehr.finalization import finalize_version
from apps.ehr.models import ClinicalDocumentVersion, SpecialtyTemplate
from apps.ehr.services import create_draft, record_clinical_note
from apps.identity.clinic_configuration import (
    ConfigurationContent,
    latest_configuration,
    publish_configuration,
)
from apps.identity.models import ServicePrincipal, ServicePrincipalGrant, User
from apps.retention import execution
from apps.retention.models import ArtifactPurge
from apps.retention.services import (
    apply_retention_policy,
    approve_policy,
    place_hold,
    propose_policy,
    release_hold,
)
from apps.retention.storage import SyntheticPurgeStorage
from apps.scribe.contracts import AudioPurgedError
from apps.scribe.models import AudioChunk, RecordingSession
from apps.scribe.services import control_recording
from apps.scribe.storage import open_audio
from apps.tenancy.db import service_principal_context, tenant_context
from django.db import IntegrityError, connection, connections, transaction
from ops.testing.restore_purge import CURRENT_TOMBSTONES_SQL
from psycopg import sql

from ai.support import seed
from auth.stepup_test_support import verified_request
from identity.permission_support import owner_context
from identity.test_service_principals import agent_password
from patient_service_support import runtime_role
from renewal.test_encounters import CONTENT
from scribe.test_ingest import chunk, receive, world

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from datetime import datetime
    from pathlib import Path
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph
    from scribe.test_ingest import World

pytestmark = pytest.mark.django_db(transaction=True, databases={"default", "agent"})
__all__ = ("agent_password",)


@pytest.mark.usefixtures("agent_password")
def test_native_retention_authority_and_restore_keep_independent_subjects(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = rbac_graph
    test_legal_hold_receipt_and_crypto_shred_execute_with_the_real_principal(
        graph, settings, tmp_path, monkeypatch, storage_failure=False
    )
    with owner_context(graph.organization_a):
        principal = ServicePrincipal.objects.get(
            clinic_id=graph.clinic_a, purpose="retention"
        )
        receipt = ArtifactPurge.objects.get(clinic_id=graph.clinic_a)
        with connection.cursor() as cursor:
            cursor.execute(CURRENT_TOMBSTONES_SQL)
            tombstones = cursor.fetchone()
            assert tombstones is not None
            cursor.execute(
                "SELECT clinic_app.retention_restore_tombstones(%s::jsonb)",
                [tombstones[0]],
            )
            assert cursor.fetchone()[0]
    with transaction.atomic(using="agent"), connections["agent"].cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_user_id','',true),"
            "set_config('app.current_patient_session','',true)"
        )
        cursor.execute(
            "SELECT clinic_app.retention_principal_scope(%s,%s)",
            [principal.pk, graph.clinic_a],
        )
        assert cursor.fetchone() == (graph.organization_a,)
        cursor.execute(
            "SELECT set_config('app.current_user_id',%s,true)", [str(graph.physician)]
        )
        cursor.execute(
            "SELECT clinic_app.retention_principal_scope(%s,%s)",
            [principal.pk, graph.clinic_a],
        )
        assert cursor.fetchone() == (None,)
    try:
        for active in (True, False):
            with owner_context(graph.organization_a):
                User.objects.filter(pk=graph.physician).update(is_active=active)
            with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.current_user_id',%s,true),"
                    "set_config('app.current_tenant',%s,true)",
                    [str(graph.physician), str(graph.organization_a)],
                )
                cursor.execute(
                    "SELECT clinic_app.retention_purge_visible(%s)", [receipt.pk]
                )
                assert cursor.fetchone() == (active,)
    finally:
        with owner_context(graph.organization_a):
            User.objects.filter(pk=graph.physician).update(is_active=True)


def _retention_principal(graph: RbacGraph) -> ServicePrincipal:
    with owner_context(graph.organization_a):
        principal = ServicePrincipal.objects.create(
            organization_id=graph.organization_a,
            clinic_id=graph.clinic_a,
            db_identity=str(connections["agent"].settings_dict["USER"]),
            name="synthetic-retention",
            purpose="retention",
            grant_set_version=3,
        )
        ServicePrincipalGrant.objects.create(
            organization_id=graph.organization_a,
            principal=principal,
            permission="retention.execute",
            grant_set_version=3,
        )
    return principal


def _stopped_audio(
    graph: RbacGraph, *, retention_days: int = 0, finalize_note: bool = False
) -> tuple[World, UUID]:
    scope = world(graph)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert receive(graph, scope, chunk(scope)) == "received"
        stored = AudioChunk.objects.get(session_id=scope.session)
        control_recording(
            clinic_id=graph.clinic_a,
            session_id=scope.session,
            encounter_token=scope.token,
            command_id=uuid4(),
            action="stop",
            expected_epoch=1,
            last_seq=0,
        )
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        prior = latest_configuration(graph.clinic_a)
        publish_configuration(
            clinic_id=graph.clinic_a,
            expected_version=prior.version if prior else 0,
            content=ConfigurationContent(
                display_name="Synthetic retention",
                raw_audio_retention_days=retention_days,
                retention_dry_run=False,
            ),
        )
    if finalize_note:
        _finalize_audio_note(graph, scope)
    return scope, stored.pk


def _finalize_audio_note(graph: RbacGraph, scope: World) -> ClinicalDocumentVersion:
    request = verified_request(graph.physician)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        template = SpecialtyTemplate.objects.get(clinic_id=graph.clinic_a, key="geral")
        version = create_draft(
            clinic_id=graph.clinic_a,
            encounter_id=scope.encounter,
            template_id=template.pk,
        )
        version = record_clinical_note(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=1,
            content=CONTENT,
        )
        return finalize_version(
            clinic_id=graph.clinic_a,
            version_id=version.pk,
            expected_revision=version.revision,
            request=request,
        )


@pytest.fixture
def retention_clock(
    superuser_database_url: str,
    settings: SettingsWrapper,
    tmp_path: Path,
) -> Iterator[Callable[[datetime], None]]:
    """Advance only retention clocks, preserving every predicate and restoring SQL."""
    settings.SCRIBE_SYNTHETIC_CAPTURE = True
    settings.SCRIBE_MEDIA_ROOT = tmp_path / "media"
    signatures = (
        "clinic_app.retention_schedule(uuid,text)",
        "clinic_app.retention_prepare(uuid,uuid)",
        "clinic_app.retention_purge_guard()",
        "clinic_app.retention_complete(uuid,uuid)",
    )
    with psycopg.connect(superuser_database_url, autocommit=True) as owner:
        originals: list[tuple[str, str]] = []
        for signature in signatures:
            row = owner.execute(
                "SELECT pg_get_functiondef(%s::regprocedure)", [signature]
            ).fetchone()
            assert row is not None
            originals.append((signature, row[0]))

        def advance(reference: datetime) -> None:
            literal = sql.Literal(reference).as_string(owner)
            for _signature, original in originals:
                owner.execute(
                    original.replace("statement_timestamp()", literal).encode()
                )

        try:
            yield advance
        finally:
            for signature, original in originals:
                owner.execute(original.encode())
                assert owner.execute(
                    "SELECT pg_get_functiondef(%s::regprocedure)", [signature]
                ).fetchone() == (original,)


@pytest.mark.parametrize("approved_policy", [False, True])
@pytest.mark.usefixtures("agent_password")
def test_audio_ttl_starts_only_after_note_finalization(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
    retention_clock: Callable[[datetime], None],
    approved_policy: bool,
) -> None:
    graph = rbac_graph
    scope, chunk_id = _stopped_audio(graph, retention_days=7)
    principal = _retention_principal(graph)
    monkeypatch.setattr("apps.retention.execution.publish_purge", lambda **_: None)
    if approved_policy:
        with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
            policy = propose_policy(
                clinic_id=graph.clinic_a, record_class="raw_audio", retention_days=7
            )
            approve_policy(clinic_id=graph.clinic_a, policy_id=policy.pk)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        stopped = RecordingSession.objects.get(pk=scope.session).stopped_at
        stored = AudioChunk.objects.get(pk=chunk_id)
        key, ciphertext = stored.storage_key, bytes(stored.encrypted_data)
    assert stopped is not None
    storage = SyntheticPurgeStorage(settings.SCRIBE_MEDIA_ROOT)
    storage.put(key, ciphertext)
    retention_clock(stopped + timedelta(days=8))
    with service_principal_context(principal_id=principal.pk, clinic_id=graph.clinic_a):
        unfinalized = apply_retention_policy(
            clinic_id=graph.clinic_a, record_class="raw_audio"
        )
    assert (unfinalized.due, unfinalized.purge_ids) == (0, ())
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert not ArtifactPurge.objects.filter(record_id=scope.session).exists()
        assert AudioChunk.objects.get(pk=chunk_id).wrapped_key
        assert not storage.absent(key)
    finalized = _finalize_audio_note(graph, scope)
    assert finalized.finalized_at is not None
    retention_clock(finalized.finalized_at + timedelta(days=6))
    with service_principal_context(principal_id=principal.pk, clinic_id=graph.clinic_a):
        assert (
            apply_retention_policy(
                clinic_id=graph.clinic_a, record_class="raw_audio"
            ).due
            == 0
        )
    retention_clock(finalized.finalized_at + timedelta(days=7))
    with service_principal_context(principal_id=principal.pk, clinic_id=graph.clinic_a):
        due = apply_retention_policy(clinic_id=graph.clinic_a, record_class="raw_audio")
    assert due.due == 1
    execution.run_purge(
        clinic_id=graph.clinic_a, principal_id=principal.pk, purge_id=due.purge_ids[0]
    )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        receipt = ArtifactPurge.objects.get(pk=due.purge_ids[0])
        assert receipt.due_at == finalized.finalized_at + timedelta(days=7)
        assert receipt.state == AudioChunk.objects.get(pk=chunk_id).state == "purged"
        assert storage.absent(key)


@pytest.mark.parametrize("storage_failure", [False, True])
@pytest.mark.usefixtures("agent_password")
def test_legal_hold_receipt_and_crypto_shred_execute_with_the_real_principal(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    storage_failure: bool,
) -> None:
    graph = rbac_graph
    settings.SCRIBE_SYNTHETIC_CAPTURE = True
    settings.SCRIBE_MEDIA_ROOT = tmp_path / "media"
    scope, chunk_id = _stopped_audio(graph, finalize_note=True)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        stored = AudioChunk.objects.get(pk=chunk_id)
        key, ciphertext = stored.storage_key, bytes(stored.encrypted_data)
    storage = SyntheticPurgeStorage(settings.SCRIBE_MEDIA_ROOT)
    storage.put(key, ciphertext)
    principal = _retention_principal(graph)
    # Control only broker publication; all policy, hold, RLS and audit checks run.
    published: list[UUID] = []

    def publish(*, clinic_id: UUID, principal_id: UUID, purge_id: UUID) -> None:
        published.append(purge_id)

    monkeypatch.setattr("apps.retention.execution.publish_purge", publish)
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        hold = place_hold(
            clinic_id=graph.clinic_a,
            record_class="raw_audio",
            record_id=scope.session,
            authority="Synthetic retention authority",
            reason="Synthetic hold",
        )
    with service_principal_context(principal_id=principal.pk, clinic_id=graph.clinic_a):
        held = apply_retention_policy(
            clinic_id=graph.clinic_a, record_class="raw_audio"
        )
    assert held.blocked_hold == 1
    execution.run_purge(
        clinic_id=graph.clinic_a, principal_id=principal.pk, purge_id=held.purge_ids[0]
    )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert AudioChunk.objects.get(pk=chunk_id).wrapped_key
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        release_hold(
            clinic_id=graph.clinic_a,
            hold_id=hold.pk,
            authority="Synthetic retention authority",
            reason="Synthetic release",
        )
    with service_principal_context(principal_id=principal.pk, clinic_id=graph.clinic_a):
        ready = apply_retention_policy(
            clinic_id=graph.clinic_a, record_class="raw_audio"
        )
    if storage_failure:
        before_failures = METRICS._retention_failures

        def refuse_delete(self: SyntheticPurgeStorage, key: str) -> None:
            raise AttachmentStorageError

        with monkeypatch.context() as failure:
            failure.setattr(
                "apps.retention.storage.SyntheticPurgeStorage.delete", refuse_delete
            )
            execution.run_purge(
                clinic_id=graph.clinic_a,
                principal_id=principal.pk,
                purge_id=ready.purge_ids[0],
            )
        _assert_failed_purge(graph, ready.purge_ids[0])
        assert METRICS._retention_failures == before_failures + 1
        assert not storage.absent(key)
    execution.run_purge(
        clinic_id=graph.clinic_a,
        principal_id=principal.pk,
        purge_id=ready.purge_ids[0],
    )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        stored = AudioChunk.objects.get(pk=chunk_id)
        receipt = ArtifactPurge.objects.get(pk=ready.purge_ids[0])
        assert (stored.wrapped_key, stored.encrypted_data) == (b"", b"")
        assert stored.state == receipt.state == "purged"
        assert receipt.purged_at is not None
        assert receipt.backup_copy_expires_at is not None
        assert receipt.backup_copy_expires_at > receipt.purged_at
        assert storage.absent(key)
        with pytest.raises(AudioPurgedError) as denied:
            open_audio(clinic_id=graph.clinic_a, chunk_id=chunk_id)
        assert denied.value.receipt_date == receipt.purged_at
    _assert_receipt_retained(graph, receipt)


def _assert_failed_purge(graph: RbacGraph, purge_id: UUID) -> None:
    with runtime_role(), tenant_context(graph.clinic_admin, graph.organization_a):
        failed = ArtifactPurge.objects.get(pk=purge_id)
        assert (failed.state, failed.failure_code, failed.purged_at) == (
            "purging",
            "storage_unavailable",
            None,
        )
        assert failed.failure_task_id is not None


def _assert_receipt_retained(graph: RbacGraph, receipt: ArtifactPurge) -> None:
    with owner_context(graph.organization_a):
        with (
            pytest.raises(IntegrityError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "DELETE FROM clinic_app.retention_artifactpurge WHERE id=%s",
                [receipt.pk],
            )
        with (
            pytest.raises(IntegrityError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute("TRUNCATE clinic_app.retention_artifactpurge")
        with (
            pytest.raises(IntegrityError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "DELETE FROM clinic_app.scribe_audiochunk WHERE session_id=%s",
                [receipt.record_id],
            )
        assert ArtifactPurge.objects.filter(pk=receipt.pk).exists()
        assert verify_chain(graph.organization_a).valid


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.usefixtures("agent_password")
def test_held_backlog_does_not_starve_runnable_artifacts(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    graph = rbac_graph
    gateway = seed(graph, monkeypatch)
    monkeypatch.setattr("apps.retention.execution.publish_purge", lambda **_: None)
    principal = _retention_principal(graph)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        sources = [
            create_input(
                clinic_id=graph.clinic_a,
                purpose=Purpose.FRONT_DESK,
                content="Synthetic retention batching fixture",
            )
            for _ in range(102)
        ]
    with runtime_role(), tenant_context(gateway.administrator, graph.organization_a):
        prior = latest_configuration(graph.clinic_a)
        assert prior is not None
        publish_configuration(
            clinic_id=graph.clinic_a,
            expected_version=prior.version,
            content=ConfigurationContent(
                "Synthetic backlog", retention_dry_run=dry_run
            ),
        )
        policy = propose_policy(
            clinic_id=graph.clinic_a, record_class="ai_artifact", retention_days=0
        )
        approve_policy(clinic_id=graph.clinic_a, policy_id=policy.pk)
        for source in sources[:-1]:
            place_hold(
                clinic_id=graph.clinic_a,
                record_class="ai_artifact",
                record_id=source.pk,
                authority="Synthetic authority",
                reason="Synthetic batching hold",
            )
    with service_principal_context(principal_id=principal.pk, clinic_id=graph.clinic_a):
        result = apply_retention_policy(
            clinic_id=graph.clinic_a, record_class="ai_artifact"
        )
    assert result.dry_run is dry_run
    assert (result.due, result.blocked_hold) == (101, 100)
    with owner_context(graph.organization_a):
        receipts = ArtifactPurge.objects.filter(clinic_id=graph.clinic_a)
        if dry_run:
            assert not result.purge_ids
            assert not receipts.exists()
            return
        runnable = receipts.get(record_id=sources[-1].pk)
        assert runnable.state == "purge_due"
        assert receipts.filter(state="blocked_hold").count() == 100
    execution.run_purge(
        clinic_id=graph.clinic_a, principal_id=principal.pk, purge_id=runnable.pk
    )
    with owner_context(graph.organization_a):
        runnable.refresh_from_db()
        assert runnable.state == "purged"
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT content IS NULL FROM clinic_app.ai_aiartifact WHERE id=%s",
                [sources[-1].pk],
            )
            assert cursor.fetchone() == (True,)
        assert (
            AIArtifact.objects.filter(
                pk__in=[source.pk for source in sources[:-1]], purged_at__isnull=True
            ).count()
            == 101
        )
