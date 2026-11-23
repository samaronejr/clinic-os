"""Patient choice still governs the review DTO, native form and direct artifact."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from apps.ai.artifacts import read_artifact
from apps.ai.contracts import GatewayRefusedError
from apps.consent.models import ConsentPurpose
from apps.consent.services import (
    prepare_acceptance,
    publish_text,
    record_consent,
    record_refusal,
    revoke_consent,
)
from apps.ehr.models import ClinicalDocumentVersion
from apps.intake.patient_access import patient_session_context
from apps.scribe import gateway
from apps.scribe.draft_contracts import synthetic_reply
from apps.scribe.models import DraftSectionSuggestion
from apps.scribe.services import read_review
from apps.tenancy.db import tenant_context
from django.utils.html import escape

from patient_service_support import runtime_role
from renewal.test_encounters import physician_client
from scribe.draft_support import generate
from scribe.test_draft_runtime import accept, draft_world
from scribe.test_draft_views import post
from scribe.test_ingest import media
from scribe.test_transcript_views import bind

if TYPE_CHECKING:
    from uuid import UUID

    from scribe.draft_support import DraftWorld

__all__ = ("draft_world", "media")
pytestmark = pytest.mark.django_db(transaction=True)


def two_source_reply(raw: str) -> str:
    """Make the pending text distinct from its separately displayed human sources."""
    answer = json.loads(synthetic_reply(raw))
    sources = json.loads(raw)["sources"]
    first = sources[0]
    second = next(item for item in sources[1:] if item["text"] != first["text"])
    chosen = (first, second)
    assertion = (
        first["assertion_class"]
        if first["assertion_class"] == second["assertion_class"]
        and not any(item["uncertain"] for item in chosen)
        else "hypothesis"
    )
    pending = next(
        item for item in answer["suggestions"] if item["section"] == "subjective"
    )
    pending.update(
        text="\n".join(item["text"] for item in chosen),
        citations=[item["reference"] for item in chosen],
        assertion_class=assertion,
    )
    return json.dumps(answer)


def change_patient_choice(scope: DraftWorld, prior: UUID, choice: str) -> None:
    g = scope.graph
    if choice == "withdraw":
        with patient_session_context(scope.recording.patient_session):
            revoke_consent(acceptance_id=prior)
        return
    # A visit disclosure is immutable. A patient can refuse a new offer, or
    # accept it, without reviving anything generated under the earlier receipt.
    with tenant_context(g.clinic_admin, g.organization_a):
        text = publish_text(
            clinic_id=g.clinic_a,
            purpose=ConsentPurpose.AI_ASSISTANCE,
            text="Escolha sintética de assistência por IA para novos usos.",
        )
    with patient_session_context(scope.recording.patient_session):
        _, offer = prepare_acceptance(text_id=text.pk)
        if choice == "refuse":
            record_refusal(offer=offer, purpose=ConsentPurpose.AI_ASSISTANCE)
        else:
            record_consent(
                offer=offer, purpose=ConsentPurpose.AI_ASSISTANCE, accepted=True
            )


@pytest.mark.parametrize("reader", ["native", "service", "artifact"])
@pytest.mark.parametrize("choice", ["withdraw", "refuse", "new_receipt"])
def test_withdrawal_hides_pending_drafts_but_preserves_clinician_text(
    draft_world: DraftWorld, monkeypatch: pytest.MonkeyPatch, reader: str, choice: str
) -> None:
    scope, g = draft_world, draft_world.graph
    monkeypatch.setattr(gateway, "synthetic_reply", two_source_reply)
    draft = generate(scope)
    with runtime_role(), tenant_context(g.physician, g.organization_a):
        accepted = DraftSectionSuggestion.objects.get(draft=draft, section="assessment")
        pending = DraftSectionSuggestion.objects.get(draft=draft, section="subjective")
        decision = accept(scope, accepted)
        version = ClinicalDocumentVersion.objects.get(pk=scope.version)
        assert version.soap[accepted.section] == accepted.text
        assert pending.text
        assert pending.text not in version.soap.values()
        output = draft.invocation.output.pk
        assert read_artifact(clinic_id=g.clinic_a, artifact_id=output)
    with physician_client(g) as client:
        bind(g, scope.recording, client, monkeypatch)
        before = post(client, scope)
        assert before.status_code == 200
        assert str(pending.pk).encode() in before.content
        text = escape(pending.text).encode()
        assert text in before.content
        change_patient_choice(scope, draft.consent_id, choice)
        if reader == "native":
            withdrawn = post(client, scope)
            assert withdrawn.status_code == 200
            if choice != "new_receipt":
                assert b"data-review-unavailable" in withdrawn.content
            assert text not in withdrawn.content
            assert str(pending.pk).encode() not in withdrawn.content
            assert b"data-ai-use-record" in withdrawn.content
            assert b'name="action" value="save"' in withdrawn.content
            assert escape(accepted.text).encode() in withdrawn.content
        else:
            with tenant_context(g.physician, g.organization_a):
                if reader == "service":
                    panel = read_review(clinic_id=g.clinic_a, version_id=scope.version)
                    assert not panel.rows
                    assert not panel.proposals
                    assert panel.latest is None
                    assert panel.version.soap[accepted.section] == decision.applied_text
                else:
                    with pytest.raises(GatewayRefusedError):
                        read_artifact(clinic_id=g.clinic_a, artifact_id=output)
