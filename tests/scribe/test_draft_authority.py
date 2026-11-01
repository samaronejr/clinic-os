"""Execute the real note reader and new SQL guards under current authority."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.scribe.draft_contracts import DraftAccessDeniedError, ReviewInput
from apps.scribe.draft_sources import note_frame
from apps.scribe.models import DraftSectionSuggestion, ReviewDecision
from apps.scribe.reviews import review_suggestion
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, IntegrityError, connection, transaction

from patient_service_support import runtime_role
from scribe.draft_support import generate
from scribe.test_draft_runtime import draft_world
from scribe.test_ingest import media
from scribe.test_refusals import fixture_connection

if TYPE_CHECKING:
    from scribe.draft_support import DraftWorld

__all__ = ("draft_world", "media")
pytestmark = pytest.mark.django_db(transaction=True)


def test_note_scope_and_sql_readers_keep_assigned_and_current_authority(
    draft_world: DraftWorld,
) -> None:
    scope, graph = draft_world, draft_world.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert (
            note_frame(clinic_id=graph.clinic_a, version_id=scope.version).version.pk
            == scope.version
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT clinic_app.scribe_note_author(%s),"
                "clinic_app.scribe_note_visible(%s)",
                [scope.version, scope.version],
            )
            assert cursor.fetchone() == (True, True)
    for user, organization in (
        (graph.shared_user, graph.organization_a),
        (graph.physician, graph.organization_b),
        (uuid4(), graph.organization_a),
    ):
        with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true)", [str(organization)]
            )
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true)", [str(user)]
            )
            with pytest.raises(DraftAccessDeniedError):
                note_frame(clinic_id=graph.clinic_a, version_id=scope.version)
            cursor.execute(
                "SELECT clinic_app.scribe_note_author(%s),"
                "clinic_app.scribe_note_visible(%s)",
                [scope.version, scope.version],
            )
            assert cursor.fetchone() == (False, False)
    with fixture_connection() as database:
        database.execute(
            "UPDATE clinic_app.identity_user SET is_active=false WHERE id=%s",
            [graph.physician],
        )
    try:
        with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true)",
                [str(graph.organization_a)],
            )
            cursor.execute(
                "SELECT set_config('app.current_user_id',%s,true)",
                [str(graph.physician)],
            )
            with pytest.raises(DraftAccessDeniedError):
                note_frame(clinic_id=graph.clinic_a, version_id=scope.version)
            cursor.execute(
                "SELECT clinic_app.scribe_note_author(%s),"
                "clinic_app.scribe_note_visible(%s)",
                [scope.version, scope.version],
            )
            assert cursor.fetchone() == (False, False)
    finally:
        with fixture_connection() as database:
            database.execute(
                "UPDATE clinic_app.identity_user SET is_active=true WHERE id=%s",
                [graph.physician],
            )
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT clinic_app.scribe_note_author(%s),"
            "clinic_app.scribe_note_visible(%s)",
            [scope.version, scope.version],
        )
        assert cursor.fetchone() == (True, True)


def test_raw_citation_and_history_guards_reject_foreign_references(
    draft_world: DraftWorld,
) -> None:
    scope, graph = draft_world, draft_world.graph
    queued = generate(scope)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        suggestion = DraftSectionSuggestion.objects.filter(draft=queued).first()
        assert suggestion is not None
        with (
            pytest.raises(DatabaseError),
            transaction.atomic(),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "INSERT INTO clinic_app.scribe_draftsectionsuggestion "
                "(id,organization_id,version_id,draft_id,section,text,citations,"
                "assertion_class,created_at) "
                "SELECT %s,organization_id,version_id,draft_id,'objective',text,"
                "jsonb_build_array(jsonb_build_object('kind','transcript','id',%s)),"
                "assertion_class,created_at "
                "FROM clinic_app.scribe_draftsectionsuggestion WHERE id=%s",
                [uuid4(), str(uuid4()), suggestion.pk],
            )


def test_raw_review_guard_enforces_applied_text_by_decision_kind(
    draft_world: DraftWorld,
) -> None:
    scope, graph = draft_world, draft_world.graph
    queued = generate(scope)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        frame = note_frame(clinic_id=graph.clinic_a, version_id=scope.version)
        suggestion = DraftSectionSuggestion.objects.get(
            draft=queued, section="subjective"
        )
        decision = review_suggestion(
            clinic_id=graph.clinic_a,
            version_id=scope.version,
            review=ReviewInput(
                suggestion.pk,
                frame.version.revision,
                uuid4(),
                "rejected",
                frame.version.soap,
            ),
        )
        decision.refresh_from_db()
        assert decision.applied_text is None
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT applied_text IS NULL FROM clinic_app.scribe_reviewdecision "
                "WHERE id=%s",
                [decision.pk],
            )
            assert cursor.fetchone() == (True,)
        for action, payload in (
            ("accepted", None),
            ("modified", None),
            ("accepted", b""),
            ("modified", b""),
            ("accepted", b"x" * 262145),
            ("modified", b"x" * 262145),
            ("rejected", b"x"),
        ):
            # All copied bindings already passed the real review writer. The
            # named BEFORE trigger must refuse the payload before uniqueness.
            with (
                pytest.raises(IntegrityError, match="invalid clinician review"),
                transaction.atomic(),
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    "INSERT INTO clinic_app.scribe_reviewdecision "
                    "(id,organization_id,version_id,suggestion_id,reviewed_by_id,"
                    "command_id,request_digest,action,section,expected_revision,"
                    "result_revision,result_sha256,applied_text,"
                    "capability_snapshot,created_at) "
                    "SELECT %s,organization_id,version_id,suggestion_id,"
                    "reviewed_by_id,%s,request_digest,%s,section,expected_revision,"
                    "result_revision,result_sha256,%s,capability_snapshot,"
                    "created_at FROM clinic_app.scribe_reviewdecision WHERE id=%s",
                    [uuid4(), uuid4(), action, payload, decision.pk],
                )
        assert ReviewDecision.objects.filter(version_id=scope.version).count() == 1
