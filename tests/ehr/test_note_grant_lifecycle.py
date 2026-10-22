"""Real restricted-note grants cannot survive loss of recipient eligibility."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import psycopg
import pytest
from apps.ehr.models import ClinicalDocumentVersion, NoteAccessGrant
from apps.ehr.note_access import grant_note_access, restrict_note
from apps.ehr.services import ClinicalAccessDeniedError
from apps.identity.models import User, UserClinicRole
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from ehr.longitudinal_support import care_reader, chart_world, finalized
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context, permission_context

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _recipient_state(
    graph: RbacGraph, recipient: UUID, loss: str, *, eligible: bool
) -> None:
    with owner_context(graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_user_id', %s, true)",
            [str(graph.clinic_admin)],
        )
        if loss == "inactive":
            User.objects.filter(pk=recipient).update(is_active=eligible)
        elif eligible:
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                user_id=recipient,
                role="physician",
            )
        else:
            assert (
                UserClinicRole.objects.filter(
                    clinic_id=graph.clinic_a, user_id=recipient, role="physician"
                ).update(revoked_at=timezone.now())
                == 1
            )


@pytest.mark.parametrize("loss", ["inactive", "revoked"])
@pytest.mark.parametrize("channel", ["service", "sql"])
def test_ineligible_recipient_cannot_receive_note_grant(
    rbac_graph: RbacGraph, loss: str, channel: Literal["service", "sql"]
) -> None:
    graph = rbac_graph
    world = chart_world(graph)
    source = finalized(graph, world.version)
    recipient = care_reader(graph, world)
    with as_actor(graph, graph.physician):
        note = restrict_note(clinic_id=graph.clinic_a, version_id=source.pk)
    _recipient_state(graph, recipient, loss, eligible=False)
    with as_actor(graph, graph.physician):
        if channel == "service":
            with pytest.raises(ClinicalAccessDeniedError):
                grant_note_access(
                    clinic_id=graph.clinic_a,
                    version_id=source.pk,
                    grantee_id=recipient,
                )
        else:
            with (
                pytest.raises(DatabaseError, match="invalid note grant scope"),
                transaction.atomic(),
            ):
                NoteAccessGrant.objects.create(
                    organization_id=graph.organization_a,
                    clinic_id=graph.clinic_a,
                    note=note,
                    grantee_id=recipient,
                    granted_by_id=graph.physician,
                )
    with owner_context(graph.organization_a):
        assert not NoteAccessGrant.objects.filter(
            note=note, grantee_id=recipient
        ).exists()


@pytest.mark.parametrize("loss", ["inactive", "revoked"])
def test_recipient_loss_terminally_revokes_existing_note_grant(
    rbac_graph: RbacGraph, loss: str
) -> None:
    graph = rbac_graph
    world = chart_world(graph)
    source = finalized(graph, world.version)
    recipient = care_reader(graph, world)
    with as_actor(graph, graph.physician):
        restrict_note(clinic_id=graph.clinic_a, version_id=source.pk)
        original = grant_note_access(
            clinic_id=graph.clinic_a, version_id=source.pk, grantee_id=recipient
        )
    _recipient_state(graph, recipient, loss, eligible=False)
    with owner_context(graph.organization_a):
        original.refresh_from_db()
        revoked_at = original.revoked_at
        assert revoked_at is not None
    with permission_context(graph, recipient), connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.ehr_note_visible(%s)", [source.document_id])
        assert cursor.fetchone() == (False,)
        assert not ClinicalDocumentVersion.objects.filter(pk=source.pk).exists()
    _recipient_state(graph, recipient, loss, eligible=True)
    with permission_context(graph, recipient), connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.ehr_note_visible(%s)", [source.document_id])
        assert cursor.fetchone() == (False,)
        assert not ClinicalDocumentVersion.objects.filter(pk=source.pk).exists()
    with as_actor(graph, graph.physician):
        replacement = grant_note_access(
            clinic_id=graph.clinic_a, version_id=source.pk, grantee_id=recipient
        )
        assert replacement.pk != original.pk
    with owner_context(graph.organization_a):
        original.refresh_from_db()
        assert original.revoked_at == revoked_at
    with permission_context(graph, recipient):
        assert ClinicalDocumentVersion.objects.filter(pk=source.pk).exists()


@pytest.mark.parametrize("loss", ["inactive", "revoked"])
def test_grant_and_recipient_loss_hold_the_same_transaction_fence(
    rbac_graph: RbacGraph, superuser_database_url: str, loss: str
) -> None:
    graph = rbac_graph
    world = chart_world(graph)
    source = finalized(graph, world.version)
    recipient = care_reader(graph, world)
    with as_actor(graph, graph.physician):
        restrict_note(clinic_id=graph.clinic_a, version_id=source.pk)
    # A nonblocking second backend observes the actual lock, not timing luck.
    statement = (
        "SELECT pg_try_advisory_xact_lock("
        "hashtextextended('ehr-note-recipient:' || %s,0))"
    )
    with psycopg.connect(superuser_database_url, autocommit=True) as peer:
        with as_actor(graph, graph.physician):
            grant_note_access(
                clinic_id=graph.clinic_a, version_id=source.pk, grantee_id=recipient
            )
            assert peer.execute(statement, [str(recipient)]).fetchone() == (False,)
        assert peer.execute(statement, [str(recipient)]).fetchone() == (True,)
        with transaction.atomic():
            _recipient_state(graph, recipient, loss, eligible=False)
            assert peer.execute(statement, [str(recipient)]).fetchone() == (False,)
        assert peer.execute(statement, [str(recipient)]).fetchone() == (True,)


@pytest.mark.parametrize("loss", ["inactive", "revoked"])
def test_recipient_loss_rollback_preserves_the_existing_grant(
    rbac_graph: RbacGraph, loss: str
) -> None:
    graph = rbac_graph
    world = chart_world(graph)
    source = finalized(graph, world.version)
    recipient = care_reader(graph, world)
    with as_actor(graph, graph.physician):
        restrict_note(clinic_id=graph.clinic_a, version_id=source.pk)
        grant = grant_note_access(
            clinic_id=graph.clinic_a, version_id=source.pk, grantee_id=recipient
        )
    with owner_context(graph.organization_a):
        with transaction.atomic():
            _recipient_state(graph, recipient, loss, eligible=False)
            grant.refresh_from_db()
            assert grant.revoked_at is not None
            transaction.set_rollback(True)
        grant.refresh_from_db()
        assert grant.revoked_at is None
    with permission_context(graph, recipient):
        assert ClinicalDocumentVersion.objects.filter(pk=source.pk).exists()
