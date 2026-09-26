"""Executed differential probes that back every census exemption.

A census row labelled nonstaff, provider, infrastructure, presentation or
data_operation claims "no staff permission gate here". Static analysis cannot
prove that claim, so each such row must carry a probe here. The probe calls
the real function on real PostgreSQL as ``clinic_app`` once per staff role
state: the 10 permission-bundle roles, no role here (member elsewhere) and
all roles at once. Every actor also holds an other-clinic role. The outcome
(normalized result or exception type) must be identical across all 12
states, the function body must actually run, and the outcome class must
match the probe's declaration. A 13th state is the encounter's assigned
physician, so a gate keyed on assignment rather than role (care scope)
also shows up as a divergence. A gate in any form (partial, table, instance
``__call__``, trigger, unqualified SQL call) changes the executed outcome
for some state, so no spelling or indirection can hide it.

Contexts:
- ``staff``: inside ``tenant_context(actor, organization)``.
- ``patient``: inside the seeded patient session, with the actor's
  user/tenant GUCs injected, so a function that consulted staff authority
  would diverge. The call also runs once as deployed (no staff GUC), and
  that baseline must reach the declared outcome class.
- ``entry``: sessionless callers (redemption, provider callbacks, commands)
  with the actor's GUCs set at session level.
- ``entry_bare``: like ``entry`` but outside any savepoint, for context
  managers that refuse to nest in an atomic block.
Every call runs in a savepoint that is rolled back, except ``entry_bare``.
"""

from __future__ import annotations

import hashlib
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID, uuid4

from apps.audit.events import build_phase1_audit_event
from apps.audit.services import _record_system_event, record_event
from apps.billing import presentation as billing_presentation
from apps.billing import reconciliation
from apps.billing import services as billing_services
from apps.billing.adapters import (
    SYNTHETIC_PROVIDER,
    ProviderChargeStatus,
    require_synthetic_pix,
)
from apps.consent import services as consent
from apps.consent.models import ConsentAcceptance, ConsentText
from apps.core.fairness import _organization_quotas
from apps.ehr import finalization
from apps.ehr.management.commands.encrypt_attachment_objects import (
    Command as EncryptAttachmentObjects,
)
from apps.ehr.models import ClinicalDocumentVersion, Encounter
from apps.identity.management.base import owner_tty
from apps.identity.management.bootstrap import BootstrapRequest, bootstrap_clinic
from apps.identity.models import Clinic, User, UserClinicRole
from apps.identity.permissions import BUNDLES_V1
from apps.identity.phase1a_identity_acl_migration import remove_runtime_identity_acl
from apps.intake import patient_access, questionnaire_views, questionnaires
from apps.intake.patient_access import PATIENT_SESSION_KEY
from apps.prescription import signing, verification
from apps.prescription import views as prescription_views
from apps.retention import services as retention
from apps.scheduling import (
    patient_authority,
    patient_booking,
    patient_views,
    waitlist,
    waitlist_views,
)
from apps.teleconsult import services as teleconsult
from apps.teleconsult import views as teleconsult_views
from apps.teleconsult.models import TeleconsultCredential
from apps.tenancy import envelope
from apps.tenancy.db import clear_connection_tenant_gucs, tenant_context
from apps.tenancy.middleware import TenantMiddleware
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection, transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.test import RequestFactory

from identity.legacy_parity_support import target_code
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_encounters import setup_context

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from identity.legacy_operational_boundaries import OperationalSubjects
    from identity.legacy_parity_support import LegacyWorld
    from identity.legacy_prescription_boundaries import PrescriptionSubjects
    from identity.legacy_teleconsult_boundaries import TeleconsultSubjects
    from rbac_fixtures import RbacGraph

type Context = Literal["staff", "patient", "entry", "entry_bare"]
type Reaches = Literal["success", "refusal"]
_PLAINTEXT: Final = b"synthetic-probe"
_PURPOSE: Final = "intake.patientdemographics.occupation"


@dataclass(frozen=True, slots=True)
class ProbeWorld:
    """The seeded parity world plus the 12 staff role states."""

    w: LegacyWorld
    op: OperationalSubjects
    rx: PrescriptionSubjects
    tc: TeleconsultSubjects
    actors: dict[str, UUID]
    invitation_code: str
    authority: object
    offer: str
    slot_token: str
    patient_credential: TeleconsultCredential
    text_id: UUID
    acceptance_id: UUID
    released_version: UUID
    clinic: Clinic
    encounter: Encounter
    envelope: bytes
    sealed: bytes

    @property
    def organization(self) -> UUID:
        return self.w.graph.organization_a


@dataclass(frozen=True, slots=True)
class ExemptionProbe:
    """One exempt symbol, the context it runs in, the call and its outcome."""

    symbol: str
    context: Context
    reaches: Reaches
    invoke: Callable[[ProbeWorld], object]


def role_states(graph: RbacGraph) -> dict[str, UUID]:
    """Create 12 actors: each bundle role, none here, all roles here.

    ``build_world`` adds the 13th state, the encounter's assigned physician.
    """
    states: dict[str, tuple[UserClinicRole.Role, ...]] = {
        role: (UserClinicRole.Role(role),) for role in sorted(BUNDLES_V1)
    }
    states["none"] = ()
    states["all"] = tuple(UserClinicRole.Role(role) for role in BUNDLES_V1)
    actors: dict[str, UUID] = {}
    for label, roles in states.items():
        actor = User.objects.create(username=f"probe-{label}-{uuid4().hex}")
        with owner_context(graph.organization_a):
            UserClinicRole.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_b,
                user_id=actor.pk,
                role=UserClinicRole.Role.RECEPTIONIST,
            )
            for role in roles:
                UserClinicRole.objects.create(
                    organization_id=graph.organization_a,
                    clinic_id=graph.clinic_a,
                    user_id=actor.pk,
                    role=role,
                )
        actors[label] = actor.pk
    return actors


def build_world(
    w: LegacyWorld,
    op: OperationalSubjects,
    rx: PrescriptionSubjects,
    tc: TeleconsultSubjects,
) -> ProbeWorld:
    """Add the objects the probes need to the seeded parity world."""
    with runtime_role(), tenant_context(w.graph.shared_user, w.graph.organization_a):
        invitation = patient_access.issue_invitation(
            clinic_id=w.clinic, enrollment_id=op.enrollment
        )
        sealed = envelope.encrypt(purpose=_PURPOSE, plaintext=_PLAINTEXT)
        protected = envelope.protect(purpose=_PURPOSE, plaintext=_PLAINTEXT)
    with setup_context(w.graph.organization_a):
        text = ConsentText.objects.filter(clinic_id=w.clinic).latest("created_at")
        acceptance = ConsentAcceptance.objects.filter(clinic_id=w.clinic).latest(
            "accepted_at"
        )
        released = (
            ClinicalDocumentVersion.objects.filter(document_id=w.version.document_id)
            .order_by("-created_at")
            .first()
        )
        assert released is not None
        clinic = Clinic.objects.get(pk=w.clinic)
        encounter = Encounter.objects.get(pk=w.encounter)
    with runtime_role(), patient_access.patient_session_context(op.patient_session):
        authority = consent.patient_authority()
        _, offer = consent.prepare_acceptance(text_id=text.pk)
        slots = patient_booking.patient_slots(date(2035, 6, 3))
        join = teleconsult.request_patient_join(session_id=tc.session.pk)
    with setup_context(w.graph.organization_a):
        credential = TeleconsultCredential.objects.select_related("session").get(
            token_digest=hashlib.sha256(join.token.encode()).hexdigest()
        )
    actors = role_states(w.graph)
    actors["assigned"] = w.graph.physician
    return ProbeWorld(
        w=w,
        op=op,
        rx=rx,
        tc=tc,
        actors=actors,
        authority=authority,
        offer=offer,
        slot_token=slots[0].token if slots else "no-slot",
        patient_credential=credential,
        invitation_code=invitation.secret,
        text_id=text.pk,
        acceptance_id=acceptance.pk,
        released_version=released.pk,
        clinic=clinic,
        encounter=encounter,
        envelope=protected,
        sealed=sealed,
    )


def _request(pw: ProbeWorld, method: str = "GET", data: object = None) -> HttpRequest:
    factory = RequestFactory()
    request = factory.post("/", data or {}) if method == "POST" else factory.get("/")
    request.session = SessionStore()
    request.session[PATIENT_SESSION_KEY] = str(pw.op.patient_session)
    request.user = AnonymousUser()
    MessageMiddleware(lambda _request: HttpResponse()).process_request(request)
    return request


def _charge_state(pw: ProbeWorld) -> reconciliation._ChargeState:
    invoice = pw.op.invoice
    return reconciliation._ChargeState(
        operation_id=uuid4(),
        invoice_id=invoice.pk,
        organization_id=pw.organization,
        clinic_id=pw.w.clinic,
        actor_id=pw.w.graph.shared_user,
        invoice_state="issued",
        invoice_amount_minor=100,
        invoice_currency="BRL",
        operation_amount_minor=100,
        operation_currency="BRL",
        invoice_reference=invoice.pk,
        expires_at=datetime(2035, 1, 1, tzinfo=UTC),
        settled_amount_minor=None,
        settled_currency=None,
    )


_PAID: Final = ProviderChargeStatus(status="settled", amount_minor=100, currency="BRL")
_RANGE: Final = (
    datetime(2035, 6, 3, 11, 0, tzinfo=UTC),
    datetime(2035, 6, 3, 11, 30, tzinfo=UTC),
)


def _audit(pw: ProbeWorld) -> object:
    event = build_phase1_audit_event(
        "intake.patient.searched",
        clinic_id=pw.w.clinic,
        affected_record_id=pw.w.clinic,
    )
    return record_event(event.event, payload=event.payload)


def _system_audit(pw: ProbeWorld) -> object:
    event = build_phase1_audit_event(
        "intake.patient.searched",
        clinic_id=pw.w.clinic,
        affected_record_id=pw.w.clinic,
    )
    return _record_system_event(event.event, payload=event.payload)


def _bootstrap(pw: ProbeWorld) -> object:
    request = BootstrapRequest(
        organization_id=uuid4(),
        organization_name="Probe",
        cnpj="00000000000191",
        clinic_id=uuid4(),
        clinic_name="Probe",
        crm_uf="SP",
        timezone="America/Sao_Paulo",
        owner_user_id=uuid4(),
        owner_username=f"probe-{uuid4().hex}",
        owner_email="probe@example.invalid",
    )
    del pw
    bootstrap_clinic(request, "synthetic-probe-password")
    return None


def _record_consent(pw: ProbeWorld) -> object:
    return consent.record_consent(
        offer=pw.offer, purpose="teleconsultation", accepted=True
    )


def _owner_tty(pw: ProbeWorld) -> object:
    del pw
    with owner_tty() as tty:
        return tty.writable()


def _session_entry(pw: ProbeWorld) -> object:
    with patient_access.patient_session_context(pw.op.patient_session) as binding:
        return binding is not None


def _middleware_patient(pw: ProbeWorld) -> object:
    middleware = TenantMiddleware(lambda _request: HttpResponse(status=204))
    return middleware._patient(_request(pw))


def _probes() -> tuple[ExemptionProbe, ...]:
    staff: Context = "staff"
    patient: Context = "patient"
    entry: Context = "entry"
    ok: Reaches = "success"
    no: Reaches = "refusal"
    probe = ExemptionProbe
    return (
        # Infrastructure: audit, fairness, envelope, owner-only commands.
        probe("apps.audit.services.record_event", staff, ok, _audit),
        probe("apps.audit.services._record_system_event", staff, no, _system_audit),
        probe(
            "apps.core.fairness._organization_quotas",
            staff,
            ok,
            lambda pw: _organization_quotas(pw.organization),
        ),
        probe(
            "apps.ehr.management.commands.encrypt_attachment_objects.Command.handle",
            entry,
            no,
            lambda pw: EncryptAttachmentObjects().handle(),
        ),
        probe("apps.identity.management.base.owner_tty", entry, no, _owner_tty),
        probe(
            "apps.identity.management.bootstrap.bootstrap_clinic", entry, no, _bootstrap
        ),
        probe(
            "apps.identity.phase1a_identity_acl_migration.remove_runtime_identity_acl",
            entry,
            no,
            lambda pw: remove_runtime_identity_acl(
                None,  # type: ignore[arg-type]
                SimpleNamespace(connection=connection),  # type: ignore[arg-type]
            ),
        ),
        probe(
            "apps.tenancy.envelope.decrypt",
            staff,
            ok,
            lambda pw: (
                envelope.decrypt(purpose=_PURPOSE, envelope=pw.sealed) == _PLAINTEXT
            ),
        ),
        probe(
            "apps.tenancy.envelope.encrypt",
            staff,
            ok,
            lambda pw: (
                envelope.decrypt(
                    purpose=_PURPOSE,
                    envelope=envelope.encrypt(purpose=_PURPOSE, plaintext=_PLAINTEXT),
                )
                == _PLAINTEXT
            ),
        ),
        probe(
            "apps.tenancy.envelope.issue_tenant_key",
            staff,
            no,
            lambda pw: envelope.issue_tenant_key(),
        ),
        probe(
            "apps.tenancy.envelope.protect",
            staff,
            ok,
            lambda pw: (
                envelope.reveal(
                    purpose=_PURPOSE,
                    envelope=envelope.protect(purpose=_PURPOSE, plaintext=_PLAINTEXT),
                )
                == _PLAINTEXT
            ),
        ),
        probe(
            "apps.tenancy.envelope.reencrypt",
            staff,
            no,
            lambda pw: (
                envelope.decrypt(
                    purpose=_PURPOSE,
                    envelope=envelope.reencrypt(purpose=_PURPOSE, envelope=pw.sealed),
                )
                == _PLAINTEXT
            ),
        ),
        probe(
            "apps.tenancy.envelope.reveal",
            staff,
            ok,
            lambda pw: (
                envelope.reveal(purpose=_PURPOSE, envelope=pw.envelope) == _PLAINTEXT
            ),
        ),
        probe(
            "apps.tenancy.envelope.rewrap_tenant_keys",
            staff,
            no,
            lambda pw: envelope.rewrap_tenant_keys(new_kek="ab" * 32),
        ),
        probe(
            "apps.tenancy.envelope.tenant_key_status",
            staff,
            no,
            lambda pw: envelope.tenant_key_status(),
        ),
        # Provider callbacks: sessionless, resolved by stored references.
        probe(
            "apps.billing.adapters.require_synthetic_pix",
            entry,
            ok,
            lambda pw: require_synthetic_pix(),
        ),
        probe(
            "apps.billing.reconciliation._event_seen",
            entry,
            ok,
            lambda pw: reconciliation._event_seen(SYNTHETIC_PROVIDER, "probe-event"),
        ),
        probe(
            "apps.billing.reconciliation._recovered",
            entry,
            ok,
            lambda pw: reconciliation._recovered(
                SYNTHETIC_PROVIDER, _charge_state(pw), "probe-event", _PAID
            ),
        ),
        probe(
            "apps.billing.reconciliation._resolve_charge",
            entry,
            ok,
            lambda pw: reconciliation._resolve_charge(
                SYNTHETIC_PROVIDER, "probe-reference"
            ),
        ),
        probe(
            "apps.billing.reconciliation._superseded",
            entry,
            ok,
            lambda pw: reconciliation._superseded(
                _charge_state(pw), datetime(2035, 1, 1, tzinfo=UTC), _PAID
            ),
        ),
        probe(
            "apps.prescription.signing._resolve_callback_scope",
            entry,
            ok,
            lambda pw: signing._resolve_callback_scope(
                pw.rx.operation.provider, str(pw.rx.operation.pk)
            ),
        ),
        probe(
            "apps.prescription.signing._resolve_operation_scope",
            entry,
            ok,
            lambda pw: signing._resolve_operation_scope(pw.rx.operation.pk),
        ),
        probe(
            "apps.prescription.verification._allowance",
            entry,
            ok,
            lambda pw: verification._allowance(b"\x00" * 32),
        ),
        probe(
            "apps.prescription.verification._verify_row",
            entry,
            ok,
            lambda pw: verification._verify_row("probe-handle"),
        ),
        # Data operations and presentation helpers.
        probe(
            "apps.ehr.finalization._next_version",
            staff,
            ok,
            lambda pw: finalization._next_version(pw.w.version.document),
        ),
        probe(
            "apps.teleconsult.services._fail",
            staff,
            ok,
            lambda pw: teleconsult._fail(pw.tc.session.pk, "synthetic probe"),
        ),
        probe(
            "apps.prescription.views._identity_state",
            staff,
            ok,
            lambda pw: sorted(prescription_views._identity_state(pw.w.request)),
        ),
        # Patient-session functions: the actor's staff GUCs are injected.
        probe(
            "apps.billing.presentation.patient_charge",
            patient,
            ok,
            lambda pw: billing_presentation.patient_charge(invoice_id=pw.op.invoice.pk),
        ),
        probe(
            "apps.billing.services.patient_charges",
            patient,
            ok,
            lambda pw: billing_services.patient_charges(),
        ),
        probe(
            "apps.consent.services._patient_audit",
            patient,
            ok,
            lambda pw: consent._patient_audit(
                "consent.accepted",
                pw.acceptance_id,
                pw.authority,  # type: ignore[arg-type]
            ),
        ),
        probe(
            "apps.consent.services.patient_authority",
            patient,
            ok,
            lambda pw: consent.patient_authority(),
        ),
        probe(
            "apps.consent.services.prepare_acceptance",
            patient,
            ok,
            lambda pw: consent.prepare_acceptance(text_id=pw.text_id),
        ),
        probe("apps.consent.services.record_consent", patient, ok, _record_consent),
        probe(
            "apps.consent.services.revoke_consent",
            patient,
            ok,
            lambda pw: consent.revoke_consent(acceptance_id=pw.acceptance_id),
        ),
        probe(
            "apps.intake.patient_access.end_patient_session",
            patient,
            ok,
            lambda pw: patient_access.end_patient_session(pw.op.patient_session),
        ),
        probe(
            "apps.intake.patient_access.patient_session_context",
            "entry_bare",
            ok,
            _session_entry,
        ),
        probe(
            "apps.intake.patient_access.patient_session_overview",
            patient,
            ok,
            lambda pw: patient_access.patient_session_overview(),
        ),
        probe(
            "apps.intake.patient_access.redeem_invitation",
            entry,
            ok,
            lambda pw: (
                patient_access.redeem_invitation(pw.w.clinic, pw.invitation_code)
                is not None
            ),
        ),
        probe(
            "apps.intake.questionnaire_views.patient_questionnaires",
            patient,
            ok,
            lambda pw: questionnaire_views.patient_questionnaires(_request(pw)),
        ),
        probe(
            "apps.intake.questionnaires.patient_response",
            patient,
            ok,
            lambda pw: questionnaires.patient_response(pw.op.response.pk),
        ),
        probe(
            "apps.intake.questionnaires.patient_responses",
            patient,
            ok,
            lambda pw: questionnaires.patient_responses(),
        ),
        probe(
            "apps.prescription.verification._patient_session_scope",
            patient,
            ok,
            lambda pw: verification._patient_session_scope(),
        ),
        probe(
            "apps.prescription.verification._record_patient_download",
            patient,
            no,
            lambda pw: verification._record_patient_download(pw.rx.document.pk),
        ),
        probe(
            "apps.prescription.verification.patient_document_download",
            patient,
            no,
            lambda pw: verification.patient_document_download(
                document_id=pw.rx.document.pk
            ),
        ),
        probe(
            "apps.prescription.verification.patient_documents",
            patient,
            ok,
            lambda pw: verification.patient_documents(),
        ),
        probe(
            "apps.retention.services._record_patient_view",
            patient,
            ok,
            lambda pw: retention._record_patient_view(
                session_id=pw.op.patient_session,
                organization_id=pw.organization,
                clinic_id=pw.w.clinic,
                version_id=pw.released_version,
            ),
        ),
        probe(
            "apps.retention.services._session_scope",
            patient,
            ok,
            lambda pw: retention._session_scope(),
        ),
        probe(
            "apps.retention.services.patient_released_records",
            patient,
            ok,
            lambda pw: retention.patient_released_records(),
        ),
        probe(
            "apps.scheduling.patient_authority.lock_patient_availability",
            patient,
            ok,
            lambda pw: patient_authority.lock_patient_availability(
                (pw.w.graph.physician,), (_RANGE,)
            ),
        ),
        probe(
            "apps.scheduling.patient_authority.patient_booking_scope",
            patient,
            ok,
            lambda pw: patient_authority.patient_booking_scope(),
        ),
        probe(
            "apps.scheduling.patient_authority.patient_enrollment",
            patient,
            ok,
            lambda pw: patient_authority.patient_enrollment(
                pw.clinic, pw.op.enrollment
            ),
        ),
        probe(
            "apps.scheduling.patient_authority.patient_practitioner_active",
            patient,
            ok,
            lambda pw: patient_authority.patient_practitioner_active(
                pw.w.clinic, pw.w.graph.physician
            ),
        ),
        probe(
            "apps.scheduling.patient_authority.require_patient_booking_scope",
            patient,
            ok,
            lambda pw: patient_authority.require_patient_booking_scope(),
        ),
        probe(
            "apps.scheduling.patient_booking._slot",
            patient,
            ok,
            lambda pw: patient_booking._slot(pw.slot_token),
        ),
        probe(
            "apps.scheduling.patient_booking.patient_appointment",
            patient,
            ok,
            lambda pw: patient_booking.patient_appointment(pw.w.appointment.pk),
        ),
        probe(
            "apps.scheduling.patient_booking.patient_slots",
            patient,
            ok,
            lambda pw: patient_booking.patient_slots(date(2035, 6, 3)),
        ),
        probe(
            "apps.scheduling.patient_booking.reschedule_patient_appointment",
            patient,
            ok,
            lambda pw: patient_booking.reschedule_patient_appointment(
                pw.w.appointment.pk, pw.slot_token
            ),
        ),
        probe(
            "apps.scheduling.patient_views._submit",
            patient,
            ok,
            lambda pw: patient_views._submit(
                _request(
                    pw,
                    "POST",
                    {"action": "cancel", "appointment_id": str(pw.w.appointment.pk)},
                ),
                str(uuid4()),
            ),
        ),
        probe(
            "apps.scheduling.waitlist.respond_to_offer",
            patient,
            no,
            lambda pw: waitlist.respond_to_offer(uuid4(), accept=True),
        ),
        probe(
            "apps.scheduling.waitlist_views._patient_submit",
            patient,
            no,
            lambda pw: waitlist_views._patient_submit(
                _request(pw, "POST", {"action": "accept", "offer_id": str(uuid4())})
            ),
        ),
        probe(
            "apps.teleconsult.services._enter_as_patient",
            patient,
            ok,
            lambda pw: teleconsult._enter_as_patient(pw.patient_credential),
        ),
        probe(
            "apps.teleconsult.services.patient_session_events",
            patient,
            ok,
            lambda pw: teleconsult.patient_session_events(session_id=pw.tc.session.pk),
        ),
        probe(
            "apps.teleconsult.services.request_patient_join",
            patient,
            ok,
            lambda pw: teleconsult.request_patient_join(session_id=pw.tc.session.pk),
        ),
        probe(
            "apps.teleconsult.views._patient_status",
            patient,
            ok,
            lambda pw: teleconsult_views._patient_status(pw.tc.session.pk),
        ),
        probe(
            "apps.tenancy.middleware.TenantMiddleware._patient",
            "entry_bare",
            ok,
            _middleware_patient,
        ),
    )


PROBES: Final[Mapping[str, ExemptionProbe]] = {item.symbol: item for item in _probes()}


def normalize(result: object) -> object:
    """Reduce a result to what must match across states (no fresh ids)."""
    if isinstance(result, HttpResponseBase):
        return ("http", result.status_code)
    if result is None or isinstance(result, bool | str):
        return result
    if isinstance(result, int | float | bytes | UUID | datetime | date):
        return type(result).__name__
    if isinstance(result, list | tuple | set | frozenset | dict):
        return (type(result).__name__, len(result))
    return type(result).__name__


@contextmanager
def _inject_actor(pw: ProbeWorld, actor: UUID, *, local: bool) -> Iterator[None]:
    with connection.cursor() as cursor:
        for setting, value in (
            ("app.current_user_id", str(actor)),
            ("app.current_tenant", str(pw.organization)),
        ):
            cursor.execute(
                "SELECT pg_catalog.set_config(%s, %s, %s)", [setting, value, local]
            )
    yield


def _outcome(invoke: Callable[[], object], *, savepoint: bool) -> tuple[str, object]:
    try:
        if savepoint:
            with transaction.atomic():
                result = normalize(invoke())
                transaction.set_rollback(True)
        else:
            result = normalize(invoke())
    except Exception as error:  # noqa: BLE001 - the refusal type is the outcome
        return ("raise", type(error).__name__)
    return ("ok", result)


def execute(probe: ExemptionProbe, pw: ProbeWorld, actor: UUID) -> tuple[str, object]:
    """Run one probe once as ``actor`` in its declared context."""
    call = lambda: probe.invoke(pw)  # noqa: E731 - bound once per state
    if probe.context == "staff":
        with runtime_role(), tenant_context(actor, pw.organization):
            return _outcome(call, savepoint=True)
    if probe.context == "patient":
        with (
            runtime_role(),
            patient_access.patient_session_context(pw.op.patient_session),
            _inject_actor(pw, actor, local=True),
        ):
            return _outcome(call, savepoint=True)
    try:
        with runtime_role(), _inject_actor(pw, actor, local=False):
            return _outcome(call, savepoint=probe.context == "entry")
    finally:
        clear_connection_tenant_gucs()


def baseline(probe: ExemptionProbe, pw: ProbeWorld) -> tuple[str, object]:
    """Run a patient probe as deployed: in the session, no staff GUC."""
    call = lambda: probe.invoke(pw)  # noqa: E731 - bound once
    with runtime_role(), patient_access.patient_session_context(pw.op.patient_session):
        return _outcome(call, savepoint=True)


def differential(
    probe: ExemptionProbe, pw: ProbeWorld
) -> dict[str, tuple[str, object]]:
    """Execute ``probe`` under every role state; the body must run each time."""
    code = target_code(probe.symbol)
    outcomes: dict[str, tuple[str, object]] = {}
    for state, actor in pw.actors.items():
        entered = False

        def observe(frame: object, event: str, _arg: object) -> None:
            nonlocal entered
            if event == "call" and getattr(frame, "f_code", None) is code:
                entered = True

        previous = sys.getprofile()
        sys.setprofile(observe)
        try:
            outcomes[state] = execute(probe, pw, actor)
        finally:
            sys.setprofile(previous)
        assert entered, (probe.symbol, state, "the function body never ran")
    return outcomes


def staff_independent(outcomes: Mapping[str, tuple[str, object]]) -> bool:
    """Every role state produced the same decision."""
    return len(set(outcomes.values())) == 1
