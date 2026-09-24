"""HTTP acceptance of the prescribing screens: author, review, identity, sign.

These tests drive the real views through the tenant middleware as the
assigned physician and read the rendered machine attributes (``data-*``)
rather than prose. They prove the pieces a browser cannot force
deterministically: a stale step-up that routes through the identity
challenge and back to the same review, a delayed callback that expires the
authorization, and repeated submits that resolve to one operation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from unittest.mock import patch
from urllib.parse import quote
from uuid import uuid4

import pytest
from apps.ehr.services import open_encounter
from apps.identity.models import PhysicianProfile
from apps.identity.stepup import STEP_UP_INTENT_SESSION_KEY
from apps.intake.services import create_patient
from apps.prescription import signature_provider, signing
from apps.prescription.models import PrescriptionDocument, SignatureOperation
from apps.prescription.policy import SYNTHETIC_CATEGORY
from apps.prescription.services import create_draft, save_draft
from apps.prescription.signature_provider import (
    SIGNATURE_HEADER,
    SYNTHETIC_PROVIDER,
    SignatureRequest,
    SyntheticSignatureProvider,
)
from apps.tenancy.db import tenant_context
from django.test import Client, override_settings
from django.utils.timezone import now as real_now
from django_otp.oath import TOTP

from appointment_service_support import (
    AppointmentSetup,
    create_synthetic_appointment,
)
from otp_test_support import OTP_FIXED_TIME, get_totp_device, runtime_role
from renewal.test_document_artifacts import ITEM, seed, seed_rendered
from renewal.test_encounters import physician_client
from renewal.test_signatures import owner_scope, physician_anonymous_post
from stepup_test_support import clear_freshness

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
STEP = 30


class _Attributes(HTMLParser):
    """Collect ``data-*`` attributes and the text of elements carrying them."""

    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, list[str]] = {}
        self.text: dict[str, str] = {}
        self.links: list[str] = []
        self.forms: list[tuple[dict[str, str | None], dict[str, str]]] = []
        self._form: dict[str, str] | None = None
        self._open: list[tuple[str, str]] = []
        self._actions: list[str] = []

    @property
    def actions(self) -> list[str]:
        return self._actions

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = dict(attrs)
        if tag == "form":
            self._form = {}
            self.forms.append((fields, self._form))
        if tag in {"input", "button"} and self._form is not None:
            name, value = fields.get("name"), fields.get("value")
            if name and value is not None:
                self._form[name] = value
        if tag == "a" and (href := fields.get("href")):
            self.links.append(href)
        if tag == "button" and fields.get("name") == "action":
            self._actions.append(str(fields.get("value")))
        for name, value in fields.items():
            if name.startswith("data-"):
                self.values.setdefault(name, []).append(value or "")
                if name in {"data-field", "data-context", "data-frozen"}:
                    self._open.append((f"{name}={value}", ""))

    def handle_data(self, data: str) -> None:
        self._open = [(key, text + data) for key, text in self._open]

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None
        if tag == "dd" and self._open:
            key, text = self._open.pop()
            self.text[key] = text

    def first(self, name: str) -> str:
        values = self.values.get(name)
        assert values, f"{name} is not rendered"
        return values[0]


def _page(client: Client, url: str) -> tuple[int, _Attributes]:
    response = client.get(url)
    parser = _Attributes()
    parser.feed(response.content.decode())
    return response.status_code, parser


def _provision(graph: RbacGraph) -> None:
    with owner_scope(graph.organization_a):
        PhysicianProfile.objects.create(
            organization_id=graph.organization_a,
            user_id=graph.physician,
            jurisdiction="SP",
            registration_number="SYNTHETIC-CRM-36",
            signing_subject=f"synthetic:physician:{graph.physician}",
        )


def _urls(graph: RbacGraph, document_id: UUID) -> tuple[str, str]:
    draft = f"/prescription/clinics/{graph.clinic_a}/draft/"
    review = f"/prescription/clinics/{graph.clinic_a}/documents/{document_id}/review/"
    return draft, review


def _open(client: Client, url: str, encounter_id: UUID) -> None:
    opened = client.post(url, {"action": "open", "encounter_id": encounter_id})
    assert opened.status_code == 302


def _signed_callback(
    graph: RbacGraph, document: PrescriptionDocument, *, status: str = "signed"
) -> tuple[str, bytes]:
    """Build the provider's authenticated callback for the stored operation."""
    provider = SyntheticSignatureProvider()
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        operation = SignatureOperation.objects.get(document=document, state="signing")
        request = SignatureRequest(
            operation_id=operation.pk,
            issuer_id=operation.issuer_id,
            signer_subject=operation.signer_subject,
            content_digest=operation.content_digest,
            content=bytes(document.pdf_bytes),
        )
        if status == "signed":
            headers, body = provider.sign(request, operation.operation_id)
            return headers[SIGNATURE_HEADER], body
        body = json.dumps(
            {
                "event_id": "event-failed",
                "operation_id": operation.operation_id,
                "status": "failed",
            }
        ).encode()
        secret = signature_provider.SYNTHETIC_SECRET
        return hmac.new(secret, body, hashlib.sha256).hexdigest(), body


@pytest.fixture
def _synthetic_signing() -> Iterator[None]:
    with override_settings(
        PHYSICIAN_SYNTHETIC_REGISTRY=True, PRESCRIPTION_SYNTHETIC_SIGNING=True
    ):
        yield


@pytest.mark.usefixtures("_synthetic_signing")
def test_render_opens_the_review_and_a_repeat_render_reopens_the_same_document(
    rbac_graph: RbacGraph,
) -> None:
    scope = seed(rbac_graph)
    _provision(rbac_graph)
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        draft = create_draft(**scope, category=SYNTHETIC_CATEGORY)
        save_draft(
            **scope,
            draft_id=draft.pk,
            category=SYNTHETIC_CATEGORY,
            expected_version=1,
            items=[ITEM],
        )
    url = f"/prescription/clinics/{rbac_graph.clinic_a}/draft/"
    with physician_client(rbac_graph) as client:
        _open(client, url, scope["encounter_id"])
        status, author = _page(client, url)
        assert status == 200
        assert author.first("data-step") == "author"
        assert "patient" in "".join(author.values["data-context"])
        assert "review_document" not in author.actions
        rendered = client.post(
            url,
            {
                "action": "render_document",
                "encounter_id": scope["encounter_id"],
                "expected_version": 2,
            },
        )
        assert rendered.status_code == 302
        review_url = rendered.headers["Location"]
        assert review_url.endswith("/review/")
        # The same click again (a double submit or a reload of the POST) does
        # not produce a second artifact: it reopens the one that exists.
        repeated = client.post(
            url,
            {
                "action": "render_document",
                "encounter_id": scope["encounter_id"],
                "expected_version": 2,
            },
        )
        assert repeated.status_code == 302
        assert repeated.headers["Location"] == review_url
        # A stale expected version is a 409 that keeps the author screen whole.
        stale = client.post(
            url,
            {
                "action": "render_document",
                "encounter_id": scope["encounter_id"],
                "expected_version": 1,
            },
        )
        assert stale.status_code == 409
        parser = _Attributes()
        parser.feed(stale.content.decode())
        assert parser.first("data-outcome") == "draft_moved"
        assert parser.first("data-step") == "author"
        assert parser.first("data-version") == "2"
        assert parser.first("data-state") == "saved"
        assert "render_document" in parser.actions
        status, review = _page(client, review_url)
        assert status == 200
        assert review.first("data-step") == "review"
        assert review.first("data-version") == "2"
        assert review.first("data-signature") == "ready"
        assert review.first("data-identity") == "fresh"
        assert review.text["data-field=dose"] == ITEM["dose"]
        assert (
            review.text["data-field=medication_description"]
            == (ITEM["medication_description"])
        )
        assert "sign_document" in review.actions
        status, author = _page(client, url)
        assert author.first("data-signature") == "none"
        assert "review_document" in author.actions
    with owner_scope(rbac_graph.organization_a):
        assert PrescriptionDocument.objects.count() == 1


@pytest.mark.usefixtures("_synthetic_signing")
def test_stale_step_up_routes_through_identity_with_context_and_back_to_review(
    rbac_graph: RbacGraph,
) -> None:
    scope, document = seed_rendered(rbac_graph)
    _provision(rbac_graph)
    draft_url, review_url = _urls(rbac_graph, document.pk)
    with physician_client(rbac_graph) as client:
        _open(client, draft_url, scope["encounter_id"])
        clear_freshness(client)
        status, review = _page(client, review_url)
        assert status == 200
        assert review.first("data-identity") == "required"
        challenged = client.post(
            review_url, {"action": "sign_document", "document_id": document.pk}
        )
        assert challenged.status_code == 302
        assert challenged.headers["Location"] == (
            f"/auth/step-up/?next={quote(review_url, safe='')}"
        )
        intent = client.session[STEP_UP_INTENT_SESSION_KEY]
        assert intent["target"] == review_url
        assert intent["action"] == "Assinar o documento versão 2"
        assert intent["facts"][0] == [
            "Paciente",
            document.frozen_input["patient_label"],
        ]
        challenge = client.get(challenged.headers["Location"])
        assert challenge.status_code == 200
        parser = _Attributes()
        parser.feed(challenge.content.decode())
        assert "data-step-up-intent" in parser.values
        assert document.frozen_input["patient_label"].encode() in challenge.content
        assert b"Assinar o documento vers" in challenge.content
        # Another continuation target never inherits this intent.
        elsewhere = client.get(f"/auth/step-up/?next={quote(draft_url, safe='')}")
        assert elsewhere.status_code == 200
        assert b"data-step-up-intent" not in elsewhere.content
        device = get_totp_device(rbac_graph.physician, confirmed=True)
        generator = TOTP(
            device.bin_key, device.step, device.t0, device.digits, device.drift
        )
        generator.time = OTP_FIXED_TIME + STEP
        # One TOTP step later: a fresh code, and the freshness clock (which
        # shares ``time.time``) advances with it for the rest of the journey.
        with patch(
            "django_otp.plugins.otp_totp.models.time.time",
            return_value=OTP_FIXED_TIME + STEP,
        ):
            confirmed = client.post(
                "/auth/step-up/",
                {
                    "next": review_url,
                    "otp_device": device.persistent_id,
                    "otp_token": f"{generator.token():0{device.digits}d}",
                },
            )
            assert confirmed.status_code == 302
            assert confirmed.headers["Location"] == review_url
            assert STEP_UP_INTENT_SESSION_KEY not in client.session
            status, review = _page(client, review_url)
            assert status == 200
            assert review.first("data-identity") == "fresh"
            # The interrupted attempt is not in flight; the review still signs.
            assert review.first("data-signature") == "ready"
            assert "sign_document" in review.actions
            started = client.post(
                review_url, {"action": "sign_document", "document_id": document.pk}
            )
            assert started.status_code == 302
            status, signing_page = _page(client, started.headers["Location"])
            assert status == 200
            assert signing_page.first("data-state") == "signing"
            assert signing_page.first("data-step") == "sign"
            assert signing_page.values["data-attempt-state"] == ["failed", "signing"]
    with owner_scope(rbac_graph.organization_a):
        operations = SignatureOperation.objects.values_list("state", "failure_reason")
        assert sorted(operations) == [("failed", "superseded"), ("signing", "")]


@pytest.mark.usefixtures("_synthetic_signing")
def test_repeated_sign_submits_resume_one_operation(rbac_graph: RbacGraph) -> None:
    scope, document = seed_rendered(rbac_graph)
    _provision(rbac_graph)
    draft_url, review_url = _urls(rbac_graph, document.pk)
    with physician_client(rbac_graph) as client:
        _open(client, draft_url, scope["encounter_id"])
        first = client.post(
            review_url, {"action": "sign_document", "document_id": document.pk}
        )
        second = client.post(
            review_url, {"action": "sign_document", "document_id": document.pk}
        )
        third = client.post(
            draft_url,
            {
                "action": "sign_document",
                "encounter_id": scope["encounter_id"],
                "document_id": document.pk,
            },
        )
        assert first.status_code == second.status_code == third.status_code == 302
        assert first.headers["Location"] == second.headers["Location"]
        assert first.headers["Location"] == third.headers["Location"]
        status, review = _page(client, review_url)
        assert status == 200
        assert review.first("data-signature") == "live"
        assert "sign_document" not in review.actions
    with owner_scope(rbac_graph.organization_a):
        assert SignatureOperation.objects.count() == 1


@pytest.mark.usefixtures("_synthetic_signing")
def test_provider_failure_is_terminal_and_restart_keeps_the_history(
    rbac_graph: RbacGraph,
) -> None:
    scope, document = seed_rendered(rbac_graph)
    _provision(rbac_graph)
    draft_url, review_url = _urls(rbac_graph, document.pk)
    with physician_client(rbac_graph) as client:
        _open(client, draft_url, scope["encounter_id"])
        started = client.post(
            review_url, {"action": "sign_document", "document_id": document.pk}
        )
        status_url = started.headers["Location"]
    signature, body = _signed_callback(rbac_graph, document, status="failed")
    with runtime_role():
        assert (
            physician_anonymous_post(
                f"/prescription/signing/callback/{SYNTHETIC_PROVIDER}/", body, signature
            ).status_code
            == 200
        )
    with physician_client(rbac_graph) as client:
        _open(client, draft_url, scope["encounter_id"])
        status, failed = _page(client, status_url)
        assert status == 200
        assert failed.first("data-state") == "failed"
        assert failed.first("data-failure") == "provider_reported"
        assert failed.first("data-step") == "result"
        assert failed.values["data-progress-state"] == ["done", "done", "failed"]
        assert "restart_signature" in failed.actions
        status, author = _page(client, draft_url)
        assert author.first("data-signature") == "failed"
        assert "review_document" in author.actions
        status, review = _page(client, review_url)
        assert review.first("data-last-failure") == "provider_reported"
        assert review.first("data-signature") == "ready"
        restarted = client.post(status_url, {"action": "restart_signature"})
        assert restarted.status_code == 302
        assert restarted.headers["Location"] != status_url
        status, fresh = _page(client, restarted.headers["Location"])
        assert fresh.first("data-state") == "signing"
        assert fresh.values["data-attempt-state"] == ["failed", "signing"]
        # The terminal attempt is still readable, unchanged, from its own page.
        status, still_failed = _page(client, status_url)
        assert still_failed.first("data-state") == "failed"
        assert still_failed.first("data-failure") == "provider_reported"
    with owner_scope(rbac_graph.organization_a):
        assert SignatureOperation.objects.filter(state="failed").count() == 1
        assert SignatureOperation.objects.filter(state="signing").count() == 1


@pytest.mark.usefixtures("_synthetic_signing")
def test_delayed_callback_after_step_up_expiry_names_the_expired_authorization(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope, document = seed_rendered(rbac_graph)
    _provision(rbac_graph)
    draft_url, review_url = _urls(rbac_graph, document.pk)
    with physician_client(rbac_graph) as client:
        _open(client, draft_url, scope["encounter_id"])
        started = client.post(
            review_url, {"action": "sign_document", "document_id": document.pk}
        )
        status_url = started.headers["Location"]
    signature, body = _signed_callback(rbac_graph, document)
    # The provider answers after the step-up/evidence deadline has passed.
    monkeypatch.setattr(signing, "utc_now", lambda: real_now() + timedelta(minutes=10))
    with runtime_role():
        assert (
            physician_anonymous_post(
                f"/prescription/signing/callback/{SYNTHETIC_PROVIDER}/", body, signature
            ).status_code
            == 200
        )
    with physician_client(rbac_graph) as client:
        _open(client, draft_url, scope["encounter_id"])
        status, page = _page(client, status_url)
        assert status == 200
        assert page.first("data-state") == "failed"
        assert page.first("data-failure") == "authorization_expired"
        assert "data-result" not in page.values
        assert "restart_signature" in page.actions
        assert "download_signed" not in page.actions
    with owner_scope(rbac_graph.organization_a):
        operation = SignatureOperation.objects.get()
        assert operation.signed_bytes is None


@pytest.mark.usefixtures("_synthetic_signing")
def test_completed_rehearsal_shows_synthetic_result_and_verification_evidence(
    rbac_graph: RbacGraph,
) -> None:
    scope, document = seed_rendered(rbac_graph)
    _provision(rbac_graph)
    draft_url, review_url = _urls(rbac_graph, document.pk)
    with physician_client(rbac_graph) as client:
        _open(client, draft_url, scope["encounter_id"])
        started = client.post(
            review_url, {"action": "sign_document", "document_id": document.pk}
        )
        status_url = started.headers["Location"]
    signature, body = _signed_callback(rbac_graph, document)
    with runtime_role():
        assert (
            physician_anonymous_post(
                f"/prescription/signing/callback/{SYNTHETIC_PROVIDER}/", body, signature
            ).status_code
            == 200
        )
    with physician_client(rbac_graph) as client:
        _open(client, draft_url, scope["encounter_id"])
        response = client.get(status_url)
        assert response.status_code == 200
        page = _Attributes()
        page.feed(response.content.decode())
        assert page.first("data-state") == "rehearsal_complete"
        assert page.first("data-result") == "rehearsal_complete"
        assert page.first("data-step") == "result"
        assert page.values["data-progress-state"] == ["done", "done", "done"]
        verify_path = f"/prescription/verify/{document.qr_handle}/"
        assert verify_path.encode() in response.content
        assert "download_signed" in page.actions
        assert "restart_signature" not in page.actions
        assert b"sem validade" in response.content
        _, review = _page(client, review_url)
        assert review.first("data-signature") == "complete"
        assert "sign_document" not in review.actions
        _, author = _page(client, draft_url)
        assert author.first("data-signature") == "rehearsal_complete"
        assert "release_document" in author.actions
        # Lifecycle conflicts re-render the author screen with the true reason.
        undelivered = client.post(
            draft_url,
            {
                "action": "deliver_document",
                "encounter_id": scope["encounter_id"],
                "document_id": document.pk,
            },
        )
        assert undelivered.status_code == 409
        parser = _Attributes()
        parser.feed(undelivered.content.decode())
        assert parser.first("data-outcome") == "not_released"
        assert parser.first("data-step") == "author"
        assert parser.first("data-document") == str(document.pk)
        for _ in range(2):
            released = client.post(
                draft_url,
                {
                    "action": "release_document",
                    "encounter_id": scope["encounter_id"],
                    "document_id": document.pk,
                },
            )
            assert released.status_code == 302
        for _ in range(2):
            revoked = client.post(
                draft_url,
                {
                    "action": "revoke_release",
                    "encounter_id": scope["encounter_id"],
                    "document_id": document.pk,
                },
            )
            assert revoked.status_code == 302


def _assert_encounter_return_after_switch(
    client: Client,
    author: _Attributes,
    encounter_url: str,
    other: UUID,
    expected: UUID,
) -> None:
    returns = [
        (attrs, fields)
        for attrs, fields in author.forms
        if attrs.get("action") == encounter_url
    ]
    assert len(returns) == 1
    control, payload = returns[0]
    assert control["method"] == "post"
    # Move the EHR selector again AFTER A's return control was rendered.
    # Updating a shared session on workspace load cannot satisfy this case.
    switched = client.post(encounter_url, {"action": "show", "encounter_id": other})
    assert switched.status_code == 200
    status, selected = _page(client, encounter_url)
    assert status == 200
    assert selected.first("data-encounter") == str(other)
    action_url = control["action"]
    assert action_url is not None
    response = client.post(action_url, payload)
    assert response.status_code == 200
    returned = _Attributes()
    returned.feed(response.content.decode())
    assert returned.first("data-encounter") == str(expected)


@pytest.mark.usefixtures("_synthetic_signing")
@pytest.mark.parametrize("surface", ["review", "result"])
def test_return_link_keeps_document_patient(
    rbac_graph: RbacGraph, surface: str
) -> None:
    """Review/result -> author -> encounter retains A despite another tab's B."""
    scope, document = seed_rendered(rbac_graph)
    _provision(rbac_graph)
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        patient = create_patient(
            clinic_id=graph.clinic_a,
            full_name="Different synthetic patient B",
            birth_date=date(1990, 1, 1),
            idempotency_key=uuid4(),
        )
        setup = AppointmentSetup(
            graph.organization_a,
            graph.clinic_a,
            graph.shared_user,
            graph.physician,
            patient.enrollment.pk,
            patient.patient.pk,
        )
        appointment = create_synthetic_appointment(
            setup, start_local="2035-06-02T10:00", end_local="2035-06-02T11:00"
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        other = open_encounter(clinic_id=graph.clinic_a, appointment_id=appointment.pk)
    draft_url, review_url = _urls(graph, document.pk)
    encounter_url = f"/ehr/clinics/{graph.clinic_a}/encounter/"
    with physician_client(graph) as client:
        assert (
            client.post(
                encounter_url, {"action": "show", "encounter_id": scope["encounter_id"]}
            ).status_code
            == 200
        )
        _open(client, draft_url, scope["encounter_id"])
        source_url = review_url
        if surface == "result":
            started = client.post(review_url, {"action": "sign_document"})
            assert started.status_code == 302
            source_url = started.headers["Location"]
            signature, body = _signed_callback(graph, document)
            assert (
                physician_anonymous_post(
                    f"/prescription/signing/callback/{SYNTHETIC_PROVIDER}/",
                    body,
                    signature,
                ).status_code
                == 200
            )
        assert (
            client.post(
                encounter_url, {"action": "show", "encounter_id": other.pk}
            ).status_code
            == 200
        )
        _open(client, draft_url, other.pk)
        status, selected = _page(client, draft_url)
        assert status == 200
        assert selected.first("data-subject") == str(other.pk)
        status, source = _page(client, source_url)
        assert status == 200
        assert source.first("data-subject") == str(scope["encounter_id"])
        links = [href for href in source.links if href.endswith("/draft/")]
        assert len(links) == 1
        status, returned = _page(client, links[0])
        assert status == 200
        assert returned.first("data-subject") == str(scope["encounter_id"])
        assert "Different synthetic patient B" not in returned.text.get(
            "data-context=patient", ""
        )
        # The document's own history row is on the returned workspace.
        assert str(document.pk) in returned.values.get("data-document", [])
        _assert_encounter_return_after_switch(
            client, returned, encounter_url, other.pk, scope["encounter_id"]
        )
        # An explicit path cannot be redirected to another patient by its body.
        denied = client.post(links[0], {"action": "open", "encounter_id": other.pk})
        assert denied.status_code == 403
        assert (
            client.get(
                links[0].replace(str(graph.clinic_a), str(graph.clinic_c))
            ).status_code
            == 403
        )


@pytest.mark.usefixtures("_synthetic_signing")
def test_completed_history_remains_after_draft_discard(
    rbac_graph: RbacGraph,
) -> None:
    """Discard keeps history and evidence reachable, never restores draft editing."""
    scope, document = seed_rendered(rbac_graph)
    _provision(rbac_graph)
    draft_url, review_url = _urls(rbac_graph, document.pk)
    with physician_client(rbac_graph) as client:
        _open(client, draft_url, scope["encounter_id"])
        started = client.post(review_url, {"action": "sign_document"})
        assert started.status_code == 302
        result_url = started.headers["Location"]
    signature, body = _signed_callback(rbac_graph, document)
    with runtime_role():
        assert (
            physician_anonymous_post(
                f"/prescription/signing/callback/{SYNTHETIC_PROVIDER}/", body, signature
            ).status_code
            == 200
        )
    with physician_client(rbac_graph) as client:
        _open(client, draft_url, scope["encounter_id"])
        status, before = _page(client, draft_url)
        assert status == 200
        assert str(document.pk) in before.values["data-document"]
        assert before.first("data-signature") == "rehearsal_complete"
        discarded = client.post(
            draft_url,
            {
                "action": "discard",
                "category": SYNTHETIC_CATEGORY,
                **scope,
                "draft_id": document.draft_id,
                "expected_version": 2,
            },
        )
        assert discarded.status_code == 302
        workspace_url = discarded.headers["Location"]
        status, after = _page(client, workspace_url)
        assert status == 200
        # The immutable history and its evidence links survive the discard.
        assert str(document.pk) in after.values.get("data-document", [])
        assert after.first("data-signature") == "rehearsal_complete"
        assert "release_document" in after.actions
        assert "download_document" in after.actions
        # The discarded draft itself stays terminal: no editing or rendering.
        assert "save" not in after.actions
        assert "discard" not in after.actions
        assert "render_document" not in after.actions
        links = [href for href in after.links if "/signing/" in href]
        assert links == [result_url]
        status, result = _page(client, links[0])
        assert status == 200
        assert result.first("data-result") == "rehearsal_complete"
        assert "signed_digest" in result.values["data-evidence"]
        downloaded = client.post(links[0], {"action": "download_signed"})
        assert downloaded.status_code == 200
        assert downloaded.content
        verify_url = next(href for href in result.links if "/verify/" in href)
        status, verified = _page(Client(), verify_url)
        assert status == 200
        assert verified.first("data-status") == "rehearsal_complete"
        released = client.post(
            workspace_url,
            {
                "action": "release_document",
                "encounter_id": scope["encounter_id"],
                "document_id": document.pk,
            },
        )
        assert released.status_code == 302
        assert released.headers["Location"] == workspace_url
        # The frozen review remains readable but no longer offers signing.
        status, review = _page(client, review_url)
        assert status == 200
        assert review.first("data-signature") == "complete"
        assert "sign_document" not in review.actions


def test_review_is_issuer_only_and_names_a_moved_draft(rbac_graph: RbacGraph) -> None:
    scope, document = seed_rendered(rbac_graph)
    draft_url, review_url = _urls(rbac_graph, document.pk)
    graph = rbac_graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        save_draft(
            **scope,
            draft_id=document.draft_id,
            category=SYNTHETIC_CATEGORY,
            expected_version=2,
            items=[{**ITEM, "dose": "Dose alterada depois da renderização"}],
        )
    with physician_client(rbac_graph) as client:
        _open(client, draft_url, scope["encounter_id"])
        status, review = _page(client, review_url)
        assert status == 200
        assert review.first("data-stale") == "true"
        # The frozen content is the rendered one, not the newer draft text.
        assert review.text["data-field=dose"] == ITEM["dose"]
        assert client.get(review_url).headers["Cache-Control"].find("no-store") >= 0
        unknown = client.get(
            f"/prescription/clinics/{rbac_graph.clinic_a}/documents/"
            f"{scope['encounter_id']}/review/"
        )
        assert unknown.status_code == 403
    assert Client().get(review_url).status_code == 403
