"""Owner-channel domain-service steps of the todo 75 flagship browser suites.

Some journey and scenario legs have no browser or HTTP surface by design: the
PSP callback adapter and its lookup-only recovery run in the integration
worker, the governed import (todo 66) is a service API, and an agent's source
can be superseded by any later definition publication. The suites run those
legs here, through the same production services the worker or operator calls,
as the runtime role ``clinic_app`` inside a tenant transaction (the owner DSN
only supplies role switching and synthetic provider records). Every result the
browser asserts afterwards is read back through the product's screens.

Input is one JSON object on stdin (``action`` plus opaque ids); output is one
JSON line of ids, counts and decisions only: never names, texts or secrets.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID, uuid4

import django

if TYPE_CHECKING:
    from apps.billing.psp_contracts import PaymentRequest, PaymentStatus
    from apps.billing.psp_fixtures import RecordedPaymentProvider

    from interop.support import ImportWorld

FIXTURE_SECRET: Final = b"synthetic-flagship-psp"
SIGNATURE: Final = "x-fixture-psp-signature"
AMOUNT: Final = 18000


def _scribe_ambiguous(data: dict[str, str]) -> dict[str, object]:
    """Seed a final transcript whose dose was heard ambiguously (fixture 2)."""
    from apps.consent.services import (  # noqa: PLC0415
        consent_for_future_use,
        prepare_acceptance,
        publish_text,
        record_consent,
    )
    from apps.ehr.models import ClinicalDocumentVersion, Encounter  # noqa: PLC0415
    from apps.intake.patient_access import patient_session_context  # noqa: PLC0415
    from apps.scribe.authority import encounter_scope  # noqa: PLC0415
    from apps.scribe.contracts import ChunkInput  # noqa: PLC0415
    from apps.scribe.models import AudioChunk, CaptureEpoch  # noqa: PLC0415
    from apps.scribe.services import (  # noqa: PLC0415
        control_recording,
        create_recording,
        finalize_transcript,
        ingest_chunk,
        issue_encounter_token,
        read_transcript,
    )
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    from patient_service_support import runtime_role  # noqa: PLC0415
    from renewal.scribe_review_fixture import configure, recognize  # noqa: PLC0415

    actor, clinic, organization, appointment = (
        UUID(data[key]) for key in ("actor", "clinic", "organization", "appointment")
    )
    administrator = configure(clinic=clinic, organization=organization)
    with runtime_role(), tenant_context(actor, organization):
        encounter = Encounter.objects.get(
            clinic_id=clinic, physician_id=actor, appointment_id=appointment
        )
        version = ClinicalDocumentVersion.objects.get(
            document__encounter=encounter, state="draft"
        )
        scope = encounter_scope(clinic_id=clinic, encounter_id=encounter.pk)
        acceptance = consent_for_future_use(
            clinic_id=clinic,
            enrollment_id=scope.enrollment.pk,
            purpose="consultation_recording",
        )
        assert acceptance is not None
        patient_session = acceptance.patient_session_id
    with runtime_role(), tenant_context(administrator, organization):
        text = publish_text(
            clinic_id=clinic,
            purpose="ai_assistance",
            text="Consentimento sintético específico para assistência de IA.",
        )
    with runtime_role(), patient_session_context(patient_session):
        _text, offer = prepare_acceptance(text_id=text.pk)
        record_consent(offer=offer, purpose="ai_assistance", accepted=True)
    with runtime_role(), tenant_context(actor, organization):
        token = issue_encounter_token(
            clinic_id=clinic,
            encounter_id=encounter.pk,
            browser_binding="synthetic-flagship-fixture",
        )
        recording = create_recording(
            clinic_id=clinic, encounter_token=token, command_id=uuid4()
        )
        control_recording(
            clinic_id=clinic,
            session_id=recording.pk,
            encounter_token=token,
            command_id=uuid4(),
            action="start",
            expected_epoch=0,
        )
        epoch = CaptureEpoch.objects.get(session=recording, number=1)
    if data.get("outage") == "asr-timeout":
        return _asr_timeout(
            actor=actor,
            clinic=clinic,
            organization=organization,
            token=token,
            recording=recording.pk,
            epoch=epoch.pk,
        )
    # Fixture 0 is a clear phrase; fixture 2 is "5 ou 15 mg" at 0.42 confidence.
    for sequence, fixture_index in enumerate((0, 2)):
        content = f"SINTETICO-ASR-FIXTURE:{fixture_index}".encode("ascii")
        with runtime_role(), tenant_context(actor, organization):
            assert (
                ingest_chunk(
                    clinic_id=clinic,
                    encounter_token=token,
                    chunk=ChunkInput(
                        session_id=recording.pk,
                        epoch=epoch.pk,
                        track="microphone",
                        seq=sequence,
                        sha256=sha256(content).hexdigest(),
                        duration_ms=2000,
                        codec="audio/webm;codecs=opus",
                        data=content,
                    ),
                ).ack
                == "received"
            )
            chunk_id = AudioChunk.objects.get(
                session=recording, epoch=epoch, seq=sequence
            ).pk
        recognize(
            chunk_id=chunk_id, clinic=clinic, actor=actor, organization=organization
        )
    with runtime_role(), tenant_context(actor, organization):
        transcript = read_transcript(clinic_id=clinic, session_id=recording.pk)
        assert transcript is not None
        uncertain = sum(1 for segment in transcript.segments if segment.uncertainty)
        control_recording(
            clinic_id=clinic,
            session_id=recording.pk,
            encounter_token=token,
            command_id=uuid4(),
            action="stop",
            expected_epoch=1,
            last_seq=1,
        )
        finalize_transcript(
            clinic_id=clinic,
            session_id=recording.pk,
            expected_revision=transcript.revision,
            acknowledge_incomplete=False,
        )
    return {
        "version": str(version.pk),
        "segments": len(transcript.segments),
        "uncertain_segments": uncertain,
    }


def _asr_timeout(  # noqa: PLR0913 - one recording's full scope
    *,
    actor: UUID,
    clinic: UUID,
    organization: UUID,
    token: str,
    recording: UUID,
    epoch: UUID,
) -> dict[str, object]:
    """Run the real worker stages while the synthetic ASR adapter times out.

    The adapter is down for this worker process only: its transcribe call
    raises the provider timeout a network adapter would. Everything after it
    is the product's own failure handling (invocation failure, circuit, no
    fallback, retained audio).
    """
    from apps.ai.contracts import ProviderTimeoutError  # noqa: PLC0415
    from apps.ai.ledger_models import AICircuit  # noqa: PLC0415
    from apps.core.integration import execute_operation  # noqa: PLC0415
    from apps.scribe import gateway  # noqa: PLC0415
    from apps.scribe.contracts import ChunkInput  # noqa: PLC0415
    from apps.scribe.models import (  # noqa: PLC0415
        AudioChunk,
        ChunkTranscription,
        TranscriptVersion,
    )
    from apps.scribe.services import control_recording, ingest_chunk  # noqa: PLC0415
    from apps.scribe.transcription import (  # noqa: PLC0415
        prepare_audio,
        request_transcription,
    )
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    from patient_service_support import runtime_role  # noqa: PLC0415

    def timed_out(*_args: object, **_kwargs: object) -> object:
        raise ProviderTimeoutError

    for adapter in (gateway.ScribeSyntheticProvider, gateway.ScribeRecordedProvider):
        setattr(adapter, "transcribe", timed_out)  # noqa: B010 - adapter down
    outcomes: list[str] = []
    for sequence in (0, 1):
        content = b"SINTETICO-ASR-FIXTURE:0"
        with runtime_role(), tenant_context(actor, organization):
            assert (
                ingest_chunk(
                    clinic_id=clinic,
                    encounter_token=token,
                    chunk=ChunkInput(
                        session_id=recording,
                        epoch=epoch,
                        track="microphone",
                        seq=sequence,
                        sha256=sha256(content).hexdigest(),
                        duration_ms=2000,
                        codec="audio/webm;codecs=opus",
                        data=content,
                    ),
                ).ack
                == "received"
            )
            chunk = AudioChunk.objects.get(
                session_id=recording, epoch_id=epoch, seq=sequence
            )
        assert chunk.operation_id is not None
        with runtime_role():
            assert execute_operation(chunk.operation_id) == "succeeded"
        with runtime_role(), tenant_context(actor, organization):
            prepared = prepare_audio(clinic_id=clinic, chunk_id=chunk.pk)
        ciphertext = prepared.storage.get(prepared.key)
        with runtime_role(), tenant_context(actor, organization):
            status = request_transcription(
                clinic_id=clinic, chunk_id=chunk.pk, ciphertext=ciphertext
            )
            operation = (
                ChunkTranscription.objects.filter(chunk_id=chunk.pk)
                .values_list("invocation__operation_id", flat=True)
                .first()
            )
        if status == "queued" and operation is not None:
            with runtime_role():
                outcomes.append(execute_operation(operation))
        else:
            outcomes.append(status)
    # The physician stops the capture; the pending audio stays retained.
    with runtime_role(), tenant_context(actor, organization):
        control_recording(
            clinic_id=clinic,
            session_id=recording,
            encounter_token=token,
            command_id=uuid4(),
            action="stop",
            expected_epoch=1,
            last_seq=1,
        )
    with runtime_role(), tenant_context(actor, organization):
        first = ChunkTranscription.objects.select_related("invocation").get(
            chunk__session_id=recording, chunk__seq=0
        )
        return {
            "outcomes": outcomes,
            "reason": first.invocation.reason_code,
            "configuration": str(first.invocation.configuration_id),
            "open_circuits": AICircuit.objects.filter(
                configuration_id=first.invocation.configuration_id,
                closed_at__isnull=True,
            ).count(),
            "transcript_versions": TranscriptVersion.objects.filter(
                session_id=recording
            ).count(),
            "retained_chunks": AudioChunk.objects.filter(
                session_id=recording, wrapped_key__isnull=False
            ).count(),
        }


def _scribe_outage(data: dict[str, str]) -> dict[str, object]:
    """A consented recording whose ASR provider times out during the work."""
    return _scribe_ambiguous({**data, "outage": "asr-timeout"})


def _circuit_reset(data: dict[str, str]) -> dict[str, object]:
    """Close the open circuit exactly as ``ai_gateway circuit-reset`` does."""
    from apps.ai.owner import reset_circuit  # noqa: PLC0415
    from django.db import connection, transaction  # noqa: PLC0415

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [data["organization"]]
        )
        closed = reset_circuit(
            clinic_id=UUID(data["clinic"]),
            configuration_id=UUID(data["configuration"]),
        )
    return {"closed": closed}


def _care_plan(data: dict[str, str]) -> dict[str, object]:
    """Turn the day's finalized note version into its care plan (one return).

    ``create_care_plan_from_version`` has no screen of its own; the physician
    runs it on the finalized version the browser just produced.
    """
    from apps.tenancy.db import tenant_context  # noqa: PLC0415
    from apps.workflows.care import services as care  # noqa: PLC0415
    from apps.workflows.care.validation import (  # noqa: PLC0415
        CareActionSpec,
        return_window,
    )
    from django.utils import timezone  # noqa: PLC0415

    from patient_service_support import runtime_role  # noqa: PLC0415

    start, end = return_window(now=timezone.now(), weeks=2)
    with (
        runtime_role(),
        tenant_context(UUID(data["actor"]), UUID(data["organization"])),
    ):
        plan = care.create_care_plan_from_version(
            clinic_id=UUID(data["clinic"]),
            version_id=UUID(data["version"]),
            actions=(
                CareActionSpec(
                    kind="return_window", window_start=start, window_end=end
                ),
            ),
            idempotency_key=uuid4(),
        )
    return {"plan_id": str(plan.pk)}


def _release_for_delegates(data: dict[str, str]) -> dict[str, object]:
    """Release a finalized version that verified delegates may also read.

    The retention screen always releases delegate-restricted (the service's
    default), so a delegate-readable release runs ``release_version`` itself.
    """
    from apps.retention.services import release_version  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    from patient_service_support import runtime_role  # noqa: PLC0415

    with (
        runtime_role(),
        tenant_context(UUID(data["actor"]), UUID(data["organization"])),
    ):
        release = release_version(
            clinic_id=UUID(data["clinic"]),
            version_id=UUID(data["version"]),
            restricted_from_delegates=False,
        )
    return {"released": release.pk is not None}


def _delegate_grant(data: dict[str, str]) -> dict[str, object]:
    """Request, verify and activate a guardian grant through its services.

    The clinic's verifier staff attests the legal evidence; no staff screen
    exists for these transitions, so the services run as the runtime role.
    """
    from datetime import timedelta  # noqa: PLC0415

    from apps.identity.delegates import (  # noqa: PLC0415
        DelegateGrantRequest,
        activate,
        request_delegate_grant,
        verify_delegate_grant,
    )
    from apps.identity.models import PatientDelegateGrant  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415
    from django.utils import timezone  # noqa: PLC0415

    from patient_service_support import runtime_role  # noqa: PLC0415

    clinic = UUID(data["clinic"])
    with (
        runtime_role(),
        tenant_context(UUID(data["actor"]), UUID(data["organization"])),
    ):
        receipt = request_delegate_grant(
            clinic_id=clinic,
            grant=DelegateGrantRequest(
                represented_enrollment_id=UUID(data["represented"]),
                delegate_enrollment_id=UUID(data["guardian"]),
                authority_kind=PatientDelegateGrant.AuthorityKind.GUARDIAN,
                evidence_attachment_id=UUID(data["evidence"]),
                purposes=("care_processing",),
                operations=("enrollment_view", "records"),
                expires_at=timezone.now() + timedelta(days=30),
                idempotency_key=uuid4(),
            ),
        )
        receipt = verify_delegate_grant(
            clinic_id=clinic,
            grant_id=receipt.grant_id,
            expected_revision=receipt.revision,
            verified=True,
        )
        receipt = activate(
            clinic_id=clinic,
            grant_id=receipt.grant_id,
            expected_revision=receipt.revision,
        )
    return {
        "grant": str(receipt.grant_id),
        "revision": receipt.revision,
        "state": receipt.state,
    }


def _delegate_revoke(data: dict[str, str]) -> dict[str, object]:
    """Revoke a guardian grant through ``identity.delegates.revoke`` as staff.

    The product has no staff screen for this transition; the service is the
    one its offboarding and patient flows call, run as the runtime role.
    """
    from apps.identity.delegates import revoke  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    from patient_service_support import runtime_role  # noqa: PLC0415

    with (
        runtime_role(),
        tenant_context(UUID(data["actor"]), UUID(data["organization"])),
    ):
        receipt = revoke(
            clinic_id=UUID(data["clinic"]),
            grant_id=UUID(data["grant"]),
            expected_revision=int(data["revision"]),
        )
    return {"state": receipt.state, "revision": receipt.revision}


def _agent_supersede(data: dict[str, str]) -> dict[str, object]:
    """Publish a newer revision of the definition an agent proposal cites."""
    from apps.ai.models import ActionProposal  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415
    from apps.workflows.models import WorkflowDefinitionVersion  # noqa: PLC0415
    from apps.workflows.services import publish_definition  # noqa: PLC0415

    from patient_service_support import runtime_role  # noqa: PLC0415

    actor, clinic, organization, proposal_id = (
        UUID(data[key]) for key in ("actor", "clinic", "organization", "proposal")
    )
    with runtime_role(), tenant_context(actor, organization):
        proposal = ActionProposal.objects.get(pk=proposal_id, clinic_id=clinic)
        subject = json.loads(proposal.payload)["subject"]
        definition = WorkflowDefinitionVersion.objects.get(pk=UUID(subject["id"]))
        newer = publish_definition(
            clinic_id=clinic,
            key=definition.key,
            steps=[{"handler": "timer", "seconds": 2}],
            idempotency_key=uuid4(),
        )
    return {"superseded_revision": int(subject["revision"]), "new": str(newer.pk)}


def _psp_prepare(data: dict[str, str]) -> dict[str, object]:
    """Issue a released invoice and a PIX intent whose send is still queued."""
    from apps.billing.compensation import prepare_invoice_terms  # noqa: PLC0415
    from apps.billing.journal import create_book  # noqa: PLC0415
    from apps.billing.money_types import NO_ADJUSTMENTS  # noqa: PLC0415
    from apps.billing.psp_contracts import FIXTURE_PROVIDER  # noqa: PLC0415
    from apps.billing.psp_services import (  # noqa: PLC0415
        prepare_psp_charge,
        set_psp_policy,
    )
    from apps.billing.services import (  # noqa: PLC0415
        create_invoice,
        issue_invoice,
        release_invoice,
    )
    from apps.comms.models import IntegrationOperation  # noqa: PLC0415
    from apps.identity.models import UserClinicRole  # noqa: PLC0415
    from apps.providers import lifecycle  # noqa: PLC0415
    from apps.providers.services import current_version  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    from identity.permission_support import owner_context  # noqa: PLC0415
    from patient_service_support import runtime_role  # noqa: PLC0415
    from provider_gate_support import APPROVER  # noqa: PLC0415

    actor, clinic, organization = (
        UUID(data[key]) for key in ("actor", "clinic", "organization")
    )
    with owner_context(organization):
        for role in ("finance", "owner"):
            UserClinicRole.objects.get_or_create(
                organization_id=organization, clinic_id=clinic, user_id=actor, role=role
            )
        version = current_version("pix", clinic_id=clinic)
        if version is None or version.provider != FIXTURE_PROVIDER:
            version = lifecycle.propose_version(
                "pix",
                clinic_id=clinic,
                version_input=lifecycle.VersionInput(
                    provider=FIXTURE_PROVIDER, environment="sandbox", account="fixture"
                ),
            )
            lifecycle.approve_version("pix", clinic_id=clinic, decision=APPROVER)
            version.refresh_from_db()
            version.state = "sandbox"
            version.save(update_fields=("state", "updated_at"))
    with runtime_role(), tenant_context(actor, organization):
        create_book(clinic_id=clinic)
        set_psp_policy(clinic_id=clinic, max_installments=3, command_id=uuid4())
        invoice = create_invoice(
            clinic_id=clinic,
            patient_id=UUID(data["patient"]),
            amount_minor=AMOUNT,
            idempotency_key=uuid4(),
        )
        issue_invoice(
            clinic_id=clinic, invoice_id=invoice.pk, expected_revision=invoice.revision
        )
        release_invoice(clinic_id=clinic, invoice_id=invoice.pk)
        prepare_invoice_terms(
            clinic_id=clinic,
            invoice_id=invoice.pk,
            payer="patient",
            adjustments=NO_ADJUSTMENTS,
            payout_rule_id=None,
        )
        intent = prepare_psp_charge(
            clinic_id=clinic, invoice_id=invoice.pk, method="pix", installments=1
        )
        operation = IntegrationOperation.objects.get(subject_id=intent.pk).pk
    return {
        "invoice": str(invoice.pk),
        "intent": str(intent.pk),
        "operation": str(operation),
    }


def _recorded_provider(root: Path) -> RecordedPaymentProvider:
    """A recorded PSP whose remote facts survive a killed worker process."""
    from apps.billing.psp_contracts import PaymentStatus  # noqa: PLC0415
    from apps.billing.psp_fixtures import RecordedPaymentProvider  # noqa: PLC0415

    class DurableRecordedProvider(RecordedPaymentProvider):
        def __init__(self) -> None:
            super().__init__(secret=FIXTURE_SECRET, records={})

        def lookup(self, *, request: PaymentRequest) -> PaymentStatus | None:
            path = root / f"{request.idempotency_key.hex}.json"
            if not path.exists():
                return None
            fields = json.loads(path.read_text(encoding="utf-8"))
            return PaymentStatus(
                fields["provider_reference"],
                "settled",
                fields["amount_minor"],
                "BRL",
                2,
            )

        def create_charge(self, *, request: PaymentRequest) -> PaymentStatus:
            with (root / "remote-create-attempts").open("a", encoding="utf-8") as log:
                log.write(f"{request.idempotency_key.hex}\n")
                log.flush()
                os.fsync(log.fileno())
            with (root / f"{request.idempotency_key.hex}.json").open(
                "x", encoding="utf-8"
            ) as effect:
                json.dump(
                    {
                        "provider_reference": f"fixture-{request.idempotency_key.hex}",
                        "amount_minor": request.amount_minor,
                    },
                    effect,
                )
                effect.flush()
                os.fsync(effect.fileno())
            if os.environ.get("FLAGSHIP_KILL_AFTER_EFFECT") == "1":
                # The worker dies after the remote effect, before recording it.
                os.kill(os.getpid(), signal.SIGKILL)
            return super().create_charge(request=request)

    return DurableRecordedProvider()


def _psp_send(data: dict[str, str]) -> dict[str, object]:
    """Run the real outbox send of the queued intent (may die after the effect)."""
    from apps.billing.psp_worker import register_payment_provider  # noqa: PLC0415
    from apps.core.integration import execute_operation  # noqa: PLC0415

    from patient_service_support import runtime_role  # noqa: PLC0415

    register_payment_provider(_recorded_provider(Path(data["root"])))
    with runtime_role():
        outcome = execute_operation(UUID(data["operation"]))
    return {"outcome": outcome}


def _psp_recover(data: dict[str, str]) -> dict[str, object]:
    """Recover by the original idempotency key: lookup only, never a resend."""
    from apps.billing.psp_models import PspEvidence, PspIntent  # noqa: PLC0415
    from apps.billing.psp_posting import apply_psp_evidence  # noqa: PLC0415
    from apps.billing.psp_reconciliation import reconcile_psp_intent  # noqa: PLC0415
    from apps.billing.psp_worker import register_payment_provider  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    from patient_service_support import runtime_role  # noqa: PLC0415

    actor, clinic, organization, intent_id = (
        UUID(data[key]) for key in ("actor", "clinic", "organization", "intent")
    )
    register_payment_provider(_recorded_provider(Path(data["root"])))
    with runtime_role():
        decision = reconcile_psp_intent(intent_id=intent_id)
        again = reconcile_psp_intent(intent_id=intent_id)
    with runtime_role(), tenant_context(actor, organization):
        intent = PspIntent.objects.get(pk=intent_id)
        evidence = PspEvidence.objects.get(intent=intent, decision="verified")
        receipt = apply_psp_evidence(clinic_id=clinic, evidence_id=evidence.pk)
        replayed = apply_psp_evidence(clinic_id=clinic, evidence_id=evidence.pk)
        verified = PspEvidence.objects.filter(
            intent=intent, decision="verified"
        ).count()
    attempts = (Path(data["root"]) / "remote-create-attempts").read_text(
        encoding="utf-8"
    )
    return {
        "decision": decision,
        "again": again,
        "same_receipt": receipt == replayed,
        "verified_evidence": verified,
        "remote_creates": len(attempts.splitlines()),
    }


def _signed(secret: bytes, **fields: object) -> tuple[dict[str, str], bytes]:
    import hmac  # noqa: PLC0415

    body = json.dumps(fields, sort_keys=True).encode()
    return {SIGNATURE: hmac.digest(secret, body, "sha256").hex()}, body


def _psp_callbacks(data: dict[str, str]) -> dict[str, object]:
    """Deliver forged, valid, replayed and out-of-order callbacks to the adapter."""
    from apps.billing.psp_contracts import (  # noqa: PLC0415
        PaymentAuthenticationError,
        PaymentStatus,
    )
    from apps.billing.psp_fixtures import RecordedPaymentProvider  # noqa: PLC0415
    from apps.billing.psp_models import PspEvidence, PspIntent  # noqa: PLC0415
    from apps.billing.psp_posting import apply_psp_evidence  # noqa: PLC0415
    from apps.billing.psp_reconciliation import receive_psp_event  # noqa: PLC0415
    from apps.billing.psp_worker import register_payment_provider  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    from patient_service_support import runtime_role  # noqa: PLC0415

    actor, clinic, organization, intent_id = (
        UUID(data[key]) for key in ("actor", "clinic", "organization", "intent")
    )
    reference = f"fixture-{intent_id.hex}"
    settled = PaymentStatus(reference, "settled", AMOUNT, "BRL", 2)
    provider = RecordedPaymentProvider(
        secret=FIXTURE_SECRET, records={intent_id: settled}
    )
    register_payment_provider(provider)
    event = {
        "event_id": f"flagship-{intent_id.hex}-settled",
        "provider_reference": reference,
        "status": "settled",
        "amount_minor": AMOUNT,
        "currency": "BRL",
        "version": 2,
        # Tenant claims in a payload are never read.
        "clinic_id": str(uuid4()),
        "organization_id": str(uuid4()),
    }
    decisions: dict[str, str] = {}
    with runtime_role():
        headers, body = _signed(b"forged-secret", **event)
        try:
            receive_psp_event(provider=provider.provider, headers=headers, body=body)
            decisions["forged"] = "accepted"
        except PaymentAuthenticationError:
            decisions["forged"] = "refused"
        headers, body = _signed(FIXTURE_SECRET, **event)
        decisions["tampered"] = "refused"
        try:
            receive_psp_event(
                provider=provider.provider, headers=headers, body=body + b" "
            )
            decisions["tampered"] = "accepted"
        except PaymentAuthenticationError:
            pass
        decisions["valid"] = receive_psp_event(
            provider=provider.provider, headers=headers, body=body
        )
        decisions["replayed"] = receive_psp_event(
            provider=provider.provider, headers=headers, body=body
        )
        older = {
            **event,
            "event_id": f"flagship-{intent_id.hex}-pending",
            "status": "pending",
            "version": 1,
        }
        headers, body = _signed(FIXTURE_SECRET, **older)
        decisions["out_of_order"] = receive_psp_event(
            provider=provider.provider, headers=headers, body=body
        )
    with runtime_role(), tenant_context(actor, organization):
        intent = PspIntent.objects.get(pk=intent_id)
        evidence = PspEvidence.objects.get(intent=intent, decision="verified")
        receipt = apply_psp_evidence(clinic_id=clinic, evidence_id=evidence.pk)
        replayed = apply_psp_evidence(clinic_id=clinic, evidence_id=evidence.pk)
        verified = PspEvidence.objects.filter(
            intent=intent, decision="verified"
        ).count()
    return {
        **decisions,
        "same_receipt": receipt == replayed,
        "verified_evidence": verified,
    }


@dataclass(frozen=True, slots=True)
class _World:
    graph: SimpleNamespace
    operator: UUID
    session: str


def _import_rehearsal(data: dict[str, str]) -> dict[str, object]:
    """Dry-run, sign off and commit a partner spreadsheet through todo 66."""
    import pytest  # noqa: PLC0415
    from apps.interop import services  # noqa: PLC0415

    from interop.support import (  # noqa: PLC0415
        approve,
        operating,
        part,
        patient_row,
        patients_csv,
        run_batches,
        to_dry_run,
    )
    from renewal.import_reconciliation_fixture import reconcile_source  # noqa: PLC0415

    # The interop helpers read only graph.organization_a/clinic_a, operator, session.
    world = cast(
        "ImportWorld",
        _World(
            graph=SimpleNamespace(
                organization_a=UUID(data["organization"]),
                clinic_a=UUID(data["clinic"]),
            ),
            operator=UUID(data["actor"]),
            session=data["session"],
        ),
    )
    offset = int(data["offset"])
    count = int(data["count"])
    malformed = [
        patient_row(offset + count + 1, CPF="111.111.111-11"),
        patient_row(offset + count + 2, Nascimento="31/02/1990"),
        patient_row(offset + 1, Codigo=f"SIN-{offset + count + 3:06d}"),
    ]
    rows = [patient_row(number) for number in range(offset + 1, offset + count + 1)]
    source = patients_csv(0, extra=[*rows, *malformed])
    report = to_dry_run(world, source)
    datasets = part(report.report, "datasets")
    reasons = sorted({item[3] for item in part(report.report, "rejections")})
    result: dict[str, object] = {
        "job": str(report.job.job_id),
        "state": report.job.state,
        "rows": report.job.row_count,
        "valid": report.job.valid_count,
        "rejected": report.job.rejected_count,
        "duplicates": report.job.duplicate_count,
        "datasets": datasets,
        "reasons": reasons,
        "source_sha256": sha256(source).hexdigest(),
    }
    if data.get("commit") != "1":
        return result
    approved = approve(world, report)
    with operating(world):
        receipt = services.commit_import(
            clinic_id=world.graph.clinic_a,
            job_id=report.job.job_id,
            expected_sequence=approved.sequence,
            session_key=world.session,
        )
    with pytest.MonkeyPatch.context() as patch:
        outcomes = run_batches(world, report.job.job_id, patch)
    with operating(world):
        job = services.import_job(
            clinic_id=world.graph.clinic_a, job_id=report.job.job_id
        )
    result.update(
        {
            "batches": len(receipt.batch_ids),
            "outcomes": outcomes,
            "committed_state": job.state,
            "imported": job.valid_count,
        }
    )
    result["reconciliation"] = reconcile_source(world, report.job.job_id, source, rows)
    return result


def _financial_documents(data: dict[str, str]) -> dict[str, object]:
    """Exercise the service-only fiscal and payer-return legs of the close."""
    from renewal import financial_close_fixture  # noqa: PLC0415

    if data.get("fixture") == "denial":
        return financial_close_fixture.manual_denial(data)
    return financial_close_fixture.reconcile_documents(data)


def _approve_without_dispatch(data: dict[str, str]) -> dict[str, object]:
    """Retain a real approval using the browser's persisted verified session.

    The browser A1 handler immediately dispatches after approval. Calling its
    approval service separately gives the scenario a deterministic mutation
    point before the actual executor, without patching either service.
    """
    from apps.ai.approvals import approve_action  # noqa: PLC0415
    from apps.ai.attestations import step_up_evidence  # noqa: PLC0415
    from apps.ai.models import ActionProposal  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415
    from django.contrib.auth import get_user  # noqa: PLC0415
    from django.contrib.sessions.backends.db import SessionStore  # noqa: PLC0415
    from django.http import HttpRequest, HttpResponse  # noqa: PLC0415
    from django_otp.middleware import OTPMiddleware  # noqa: PLC0415

    from patient_service_support import runtime_role  # noqa: PLC0415

    request = HttpRequest()
    request.session = SessionStore(session_key=data["session"])
    clinic, actor, organization, proposal_id = (
        UUID(data[key]) for key in ("clinic", "actor", "organization", "proposal")
    )
    with runtime_role(), tenant_context(actor, organization):
        request.user = get_user(request)
        OTPMiddleware(lambda _request: HttpResponse())(request)
        assert request.user.pk == actor
        proposal = ActionProposal.objects.get(pk=proposal_id, clinic_id=clinic)
        approve_action(
            clinic_id=clinic,
            proposal_id=proposal.pk,
            expected_digest=data["digest"],
            evidence=step_up_evidence(request, clinic_id=clinic, digest=data["digest"]),
        )
        proposal.refresh_from_db()
        return {"state": proposal.state}


def _execute_approved(data: dict[str, str]) -> dict[str, object]:
    """Call the actual executor after the independently committed authority change."""
    from apps.ai.execution import execute_action  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    from patient_service_support import runtime_role  # noqa: PLC0415

    with (
        runtime_role(),
        tenant_context(UUID(data["actor"]), UUID(data["organization"])),
    ):
        result = execute_action(
            clinic_id=UUID(data["clinic"]), proposal_id=UUID(data["proposal"])
        )
        return {
            "state": result.state,
            "reason": result.reason.value if result.reason else None,
            "execution_created": result.execution_id is not None,
            "receipt_created": result.receipt_id is not None,
        }


ACTIONS: Final = {
    "approve-without-dispatch": _approve_without_dispatch,
    "execute-approved": _execute_approved,
    "financial-documents": _financial_documents,
    "scribe-ambiguous": _scribe_ambiguous,
    "scribe-outage": _scribe_outage,
    "delegate-grant": _delegate_grant,
    "release-for-delegates": _release_for_delegates,
    "delegate-revoke": _delegate_revoke,
    "care-plan": _care_plan,
    "scribe-circuit-reset": _circuit_reset,
    "agent-supersede": _agent_supersede,
    "psp-prepare": _psp_prepare,
    "psp-send": _psp_send,
    "psp-recover": _psp_recover,
    "psp-callbacks": _psp_callbacks,
    "import-rehearsal": _import_rehearsal,
}


def main() -> None:
    """Require the owner's synthetic fixture channel; emit metadata only."""
    django.setup()
    from config.settings.contracts import (  # noqa: PLC0415
        SYNTHETIC_DATA_MODE,
        resolved_data_mode,
    )
    from django.db import connection  # noqa: PLC0415

    assert resolved_data_mode() == SYNTHETIC_DATA_MODE
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_owner",)
    data = json.loads(sys.stdin.read())
    result = ACTIONS[data.pop("action")](data)
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
