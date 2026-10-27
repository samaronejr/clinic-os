"""Manual classification and date-aware refusal under the real runtime role."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.prescription.class_policy import class_eligibility
from apps.prescription.class_values import PrescriptionClass
from apps.prescription.document_rendering import default_renderer
from apps.prescription.models import PrescriptionDraft, SignatureOperation
from apps.prescription.services import (
    create_document_draft,
    create_draft,
    render_document,
    save_draft,
)
from apps.prescription.signing import initiate_signature
from apps.tenancy.db import tenant_context
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.utils import translation
from django.utils.translation import gettext

from ehr.test_autosave import registration
from patient_service_support import runtime_role
from renewal.test_encounters import physician_client
from renewal.test_prescription_drafts import ITEM, payload, seed

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
WHITE_CLASSES = ("controle_especial", "antimicrobial_retention", "glp1_retention")
NOTIFICATIONS = (
    "notificacao_a",
    "notificacao_b",
    "notificacao_b2",
    "retinoid_systemic",
    "thalidomide",
)


@pytest.mark.parametrize("category", WHITE_CLASSES)
@pytest.mark.parametrize(
    ("on_date", "reason"),
    [
        (date(2026, 10, 29), "class_electronic_unavailable"),
        (date(2026, 10, 30), "sncr_required"),
        (date(2026, 10, 31), "sncr_required"),
    ],
)
def test_white_electronic_gate_changes_at_the_recorded_date(
    rbac_graph: RbacGraph, category: str, on_date: date, reason: str
) -> None:
    # Given: the seeded policy, not a caller's claimed SNCR approval.
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        # When: electronic eligibility is evaluated at the legal boundary.
        result = class_eligibility(category=category, electronic=True, on_date=on_date)
        # Then: both sides refuse, with the date-specific reason.
        assert not result.allowed
        assert result.reason_code == reason


@pytest.mark.parametrize("category", [*WHITE_CLASSES, *NOTIFICATIONS])
def test_regulated_paper_path_is_explicitly_labeled(
    rbac_graph: RbacGraph, category: str
) -> None:
    # Given: no SNCR or EG-6 activation.
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        # When: the physician chooses the paper preparation path.
        result = class_eligibility(
            category=category, electronic=False, on_date=date(2026, 10, 30)
        )
        # Then: preparation is allowed, never represented as electronic issuance.
        assert result.allowed
        assert result.mode == "paper"
        assert result.label == gettext("Paper preparation only; not issued.")


@pytest.mark.parametrize("category", NOTIFICATIONS)
def test_notifications_always_refuse_without_sncr(
    rbac_graph: RbacGraph, category: str
) -> None:
    # Given / When: a notification is requested electronically after 30/09/2026.
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        result = class_eligibility(
            category=category, electronic=True, on_date=date(2026, 9, 30)
        )
        # Then: the stored policy cannot be bypassed with a class selector.
        assert (result.allowed, result.reason_code) == (False, "sncr_required")


@pytest.mark.parametrize("document_facade", [False, True])
def test_new_draft_is_unset_and_only_explicit_selection_persists(
    rbac_graph: RbacGraph, document_facade: bool
) -> None:
    # Given: a newly opened encounter, without a remembered class.
    scope = seed(rbac_graph)
    if document_facade:
        registration(rbac_graph, rbac_graph.physician, rbac_graph.clinic_a)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = (
            create_document_draft(
                clinic_id=scope["clinic_id"],
                encounter_id=scope["encounter_id"],
                kind="prescription",
            )
            if document_facade
            else create_draft(**scope)
        )
        assert draft.category is None
        # When: the physician explicitly selects a class during a versioned save.
        saved = save_draft(
            **scope,
            draft_id=draft.pk,
            category="simple",
            expected_version=1,
            items=[ITEM],
        )
        # Then: the choice is retained on this draft alone.
        assert saved.category == "simple"
        assert saved.contract_version == "prescription-classes-v1"


@pytest.mark.parametrize("category", PrescriptionClass.values)
def test_each_known_class_can_be_drafted_without_inference(
    rbac_graph: RbacGraph, category: str
) -> None:
    # Given: medication text has no authority to classify the draft.
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        # When: the physician explicitly chooses a known class.
        draft = create_draft(**scope, category=category)
        # Then: the exact choice survives the database CHECK.
        assert PrescriptionDraft.objects.get(pk=draft.pk).category == category


def test_unset_class_refuses_the_signing_service_before_any_operation(
    rbac_graph: RbacGraph,
) -> None:
    # Given: an unset draft can be saved and previewed without choosing for its author.
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope)
        save_draft(
            **scope,
            draft_id=draft.pk,
            category=None,
            expected_version=1,
            items=[ITEM],
        )
        document = render_document(**scope, draft_id=draft.pk, expected_version=2)
        # When: issuance is attempted through the real signing service.
        with pytest.raises(ValidationError) as refusal:
            initiate_signature(clinic_id=scope["clinic_id"], document_id=document.pk)
        # Then: no provider operation or implicit class exists.
        assert refusal.value.code == "class_required"
        assert not SignatureOperation.objects.exists()
        assert document.frozen_input["category"] is None


def test_crafted_post_and_direct_database_class_values_are_refused(
    rbac_graph: RbacGraph,
) -> None:
    # Given: a saved, unset draft in the physician's workspace.
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope)
    url = f"/prescription/clinics/{rbac_graph.clinic_a}/draft/"
    with physician_client(rbac_graph) as client:
        client.post(url, {"action": "open", "encounter_id": scope["encounter_id"]})
        # When: a crafted selector bypasses the native choice widget.
        response = client.post(url, {**payload(scope, draft), "category": "forged"})
        # Then: the HTTP boundary refuses and the database stays unchanged.
        assert response.status_code == 400
        session = dict(client.session)
        wrong_clinic = client.post(
            url.replace(str(rbac_graph.clinic_a), str(rbac_graph.clinic_b)),
            {**payload(scope, draft), "category": "forged"},
        )
        unknown = client.post(
            url.replace(str(rbac_graph.clinic_a), str(uuid4())),
            {**payload(scope, draft), "category": "forged"},
        )
        assert wrong_clinic.status_code == unknown.status_code == 403
        assert wrong_clinic.content == unknown.content
        for field in ("encounter_id", "patient_id", "issuer_id", "draft_id"):
            denied = client.post(
                url,
                {**payload(scope, draft), field: str(uuid4()), "category": "forged"},
            )
            assert denied.status_code == 403
            assert denied.content == unknown.content
            assert dict(client.session) == session
            assert not denied.cookies
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        assert PrescriptionDraft.objects.get(pk=draft.pk).category is None
        with pytest.raises(DatabaseError), transaction.atomic():
            PrescriptionDraft.objects.filter(pk=draft.pk).update(
                category="forged",
                version=2,
                contract_version="prescription-classes-v1",
            )


def test_paper_and_structured_labels_are_frozen_not_renderer_locale(
    rbac_graph: RbacGraph,
) -> None:
    # Given: the exact source fields and presentation labels have been frozen.
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope)
        save_draft(
            **scope,
            draft_id=draft.pk,
            category="simple",
            expected_version=1,
            items=[
                {
                    **ITEM,
                    "dose": "1",
                    "dose_unit": "tablet",
                    "frequency": "2",
                    "duration": "3",
                    "quantity": "",
                }
            ],
        )
        document = render_document(**scope, draft_id=draft.pk, expected_version=2)
        # When: the same immutable input is rendered under two active languages.
        with translation.override("en"):
            english = default_renderer().render(document.frozen_input)
        with translation.override("pt-br"):
            portuguese = default_renderer().render(document.frozen_input)
        # Then: a language change cannot change the approved bytes or digest.
        assert english.pdf_bytes == portuguese.pdf_bytes == bytes(document.pdf_bytes)
        assert english.pdf_digest == portuguese.pdf_digest == document.pdf_digest
