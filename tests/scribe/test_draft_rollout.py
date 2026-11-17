"""Retained note acceptance rechecks its original evaluated invocation."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ai.contracts import GatewayRefusedError, Purpose
from apps.ehr.models import ClinicalDocumentVersion
from apps.ehr.services import record_clinical_note
from apps.scribe.draft_contracts import (
    DraftAccessDeniedError,
    DraftConflictError,
    ReviewInput,
)
from apps.scribe.models import DraftSectionSuggestion, ReviewDecision
from apps.scribe.reviews import review_suggestion
from django.db import DatabaseError, transaction

from ai.retained_rollout_support import (
    INVALIDATIONS,
    Invalidation,
    authorize_native,
    invalidate,
)
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from scribe.draft_support import DraftWorld, generate, seed_draft_world
from scribe.test_draft_runtime import accept
from scribe.test_ingest import media

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

__all__ = ("media",)
pytestmark = pytest.mark.django_db(transaction=True)


def native_accept(
    scope: DraftWorld, suggestion: DraftSectionSuggestion, *, clinic_id: UUID
) -> ReviewDecision:
    """Bypass scribe review Python and submit real CAS/provenance writes."""
    authorize_native(
        invocation_id=suggestion.draft.invocation_id,
        clinic_id=clinic_id,
        purpose=Purpose.SCRIBE_DRAFT,
    )
    version = ClinicalDocumentVersion.objects.get(pk=scope.version)
    saved = record_clinical_note(
        clinic_id=scope.graph.clinic_a,
        version_id=version.pk,
        expected_revision=version.revision,
        content={**version.soap, suggestion.section: suggestion.text},
    )
    invocation = suggestion.draft.invocation
    return ReviewDecision.objects.create(
        organization_id=saved.organization_id,
        version=saved,
        suggestion=suggestion,
        reviewed_by_id=scope.graph.physician,
        command_id=uuid4(),
        request_digest="a" * 64,
        action="accepted",
        section=suggestion.section,
        expected_revision=version.revision,
        result_revision=saved.revision,
        result_sha256=saved.content_sha256,
        applied_text=saved.soap[suggestion.section],
        capability_snapshot={
            "purpose": "scribe_draft",
            "capability": invocation.configuration.capability_key,
            "configuration_id": str(invocation.configuration_id),
            "version": invocation.config_version,
        },
    )


@pytest.mark.parametrize("mode", INVALIDATIONS)
@pytest.mark.parametrize("native", [False, True], ids=["python", "native"])
def test_retained_draft_cannot_apply_after_rollout_invalidation(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    mode: Invalidation,
    native: bool,
) -> None:
    scope = seed_draft_world(rbac_graph, monkeypatch)
    draft = generate(scope)
    with as_actor(rbac_graph, rbac_graph.physician):
        suggestion = DraftSectionSuggestion.objects.get(
            draft=draft, section="subjective"
        )
        original = ClinicalDocumentVersion.objects.get(pk=scope.version)
        with transaction.atomic():
            decision = (
                native_accept(scope, suggestion, clinic_id=rbac_graph.clinic_a)
                if native
                else accept(scope, suggestion)
            )
            assert decision.action == "accepted"
            assert ClinicalDocumentVersion.objects.get(pk=scope.version).revision == (
                original.revision + 1
            )
            transaction.set_rollback(True)
    clinic_id = invalidate(rbac_graph, draft.invocation_id, mode, monkeypatch)
    with as_actor(rbac_graph, rbac_graph.physician):
        refused = (
            (DatabaseError,)
            if native
            else (
                GatewayRefusedError,
                DraftAccessDeniedError,
                DraftConflictError,
            )
        )
        attempt = (
            partial(native_accept, scope, suggestion, clinic_id=clinic_id)
            if native
            else partial(
                review_suggestion,
                clinic_id=clinic_id,
                version_id=scope.version,
                review=ReviewInput(
                    suggestion.pk,
                    original.revision,
                    uuid4(),
                    "accepted",
                    original.soap,
                ),
            )
        )
        with pytest.raises(refused), transaction.atomic():
            attempt()
    with owner_context(rbac_graph.organization_a):
        current = ClinicalDocumentVersion.objects.get(pk=scope.version)
        assert (current.revision, current.content_sha256) == (
            original.revision,
            original.content_sha256,
        )
        assert not ReviewDecision.objects.filter(suggestion=suggestion).exists()
