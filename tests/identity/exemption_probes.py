"""Executed probes that back every census exemption.

A census row labelled nonstaff, provider, infrastructure, presentation or
data_operation claims "no staff permission gate here". Static analysis cannot
prove that claim, so each such row must carry a probe here, run on real
PostgreSQL as ``clinic_app``.

Primary rule, sound by construction: an exempt function never observes the
actor. The actor observer (identity/actor_channels.py) watches every channel
through which code can see the staff actor (actor settings, actor-reading
SQL functions by exact call counts, relations whose row policies read the
actor, Python accessors, a staff request's user) while the function runs in
its deployed context. Code that never observes the actor cannot decide
anything about it, however many role, grant, flag or assignment states
exist. A function that does observe it is not exempt: the census classifies
it by an executed or named boundary instead (``RECLASSIFIED``).

Cross-check, defence in depth: the probe also runs once per state of the
staff-state matrix (identity/probe_states.py), whose dimensions are derived
from the inputs the live permission decision reads, and certifies only when,
across every state:
- the outcome is exactly identical: the full canonical result or the raised
  type and message (``canonical``); a per-call value may be masked by an
  explicit, justified ``normalize``, never its presence or type;
- every line of the body holding a call executed in at least one state
  (``gate_lines``, observed through ``sys.monitoring`` LINE events), so a
  gate behind an input the probe never supplies cannot pass as absent;
- the body started in every state, and the primary input reaches the
  declared class (success or refusal).

Contexts:
- ``staff``: inside ``tenant_context(actor, organization)``.
- ``patient``: inside the seeded patient session, with the actor's
  user/tenant GUCs injected, so a function that consulted staff authority
  would diverge. The call also runs once as deployed (no staff GUC is bound,
  which ``baseline`` asserts); that run must reach the declared outcome
  class, and the lines it executes count towards ``gate_lines``.
- ``entry``: sessionless callers (redemption, provider callbacks, commands)
  with the actor's GUCs set at session level; also run once as deployed
  (no actor bound) under the actor observer.
- ``entry_bare``: like ``entry`` but outside any savepoint, for context
  managers that refuse to nest in an atomic block.
Every call runs in a savepoint that is rolled back, except ``entry_bare``.
"""

from __future__ import annotations

import base64
import dataclasses
import dis
import hashlib
import io
import os
import re
import sys
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from time import perf_counter
from types import CodeType, SimpleNamespace
from typing import TYPE_CHECKING, Final, Literal, Self
from uuid import UUID, uuid4

import coverage
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
from apps.ehr.attachment_migration import ObjectMigrationReceipt
from apps.ehr.management.commands import (
    encrypt_attachment_objects as attachment_command,
)
from apps.ehr.management.commands.encrypt_attachment_objects import (
    Command as EncryptAttachmentObjects,
)
from apps.ehr.models import ClinicalDocumentVersion, Encounter
from apps.ehr.services import (
    SOAP_FIELDS,
    create_draft,
    open_encounter,
    record_clinical_note,
)
from apps.identity.management.base import owner_tty
from apps.identity.management.bootstrap import BootstrapRequest, bootstrap_clinic
from apps.identity.models import Clinic, User, UserClinicRole
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
from apps.scheduling.models import WaitlistEntry, WaitlistOffer
from apps.scheduling.services import (
    AppointmentLocalRange,
    SlotConflict,
    create_appointment,
    create_availability,
)
from apps.teleconsult import services as teleconsult
from apps.teleconsult import views as teleconsult_views
from apps.teleconsult.models import TeleconsultCredential, TeleconsultSession
from apps.tenancy import envelope
from apps.tenancy.db import clear_connection_tenant_gucs, tenant_context
from apps.tenancy.middleware import TenantMiddleware
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.backends.db import SessionStore
from django.core import signing as signing_core
from django.db import connection, transaction
from django.db.models import Model
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.test import RequestFactory
from PIL import Image

from auth.stepup_test_support import STEP_UP_NOW, verified_request
from identity import actor_channels, probe_states
from identity import permission_gate_census as census
from identity.legacy_parity_support import target_code
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.test_encounters import setup_context

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from identity.legacy_operational_boundaries import OperationalSubjects
    from identity.legacy_parity_support import LegacyWorld
    from identity.legacy_prescription_boundaries import PrescriptionSubjects
    from identity.legacy_teleconsult_boundaries import TeleconsultSubjects

type Context = Literal["staff", "patient", "entry", "entry_bare"]
type Outcome = tuple[object, ...]
type Reaches = Literal["success", "refusal"]
_PLAINTEXT: Final = b"synthetic-probe"
_PURPOSE: Final = "intake.patientdemographics.occupation"


@dataclass(frozen=True, slots=True)
class ProbeWorld:
    """The seeded parity world plus the staff-state matrix."""

    w: LegacyWorld
    op: OperationalSubjects
    rx: PrescriptionSubjects
    tc: TeleconsultSubjects
    matrix: probe_states.Matrix
    invitation_code: str
    authority: object
    offer: str
    slot_token: str
    patient_credential: TeleconsultCredential
    text: ConsentText
    acceptance_id: UUID
    released_version: UUID
    clinic: Clinic
    encounter: Encounter
    envelope: bytes
    sealed: bytes
    fresh_request: HttpRequest
    charge_row: tuple[object, ...]
    offer_id: UUID
    observer: actor_channels.ActorObserver

    @property
    def organization(self) -> UUID:
        return self.w.graph.organization_a

    @property
    def actors(self) -> dict[str, UUID]:
        """State label -> actor, in execution order."""
        return {state.label: state.actor for state in self.matrix.states}


@dataclass(frozen=True, slots=True)
class ExemptionProbe:
    """One exempt symbol, the context it runs in, the call and its outcome.

    Outcomes are compared exactly. ``normalize`` may map an outcome before
    the comparison only where a part of it changes on every call for a
    non-permission reason; ``why`` must say what and why, and the
    normalized part must actually differ between two runs of one state
    (``test_every_exemption_probe_is_staff_independent`` checks it).
    ``code`` overrides the target code (a probe of a mutated copy).
    """

    symbol: str
    context: Context
    reaches: Reaches
    invoke: Callable[[ProbeWorld], object]
    normalize: Callable[[Outcome], Outcome] | None = None
    why: str = ""
    code: CodeType | None = None


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
        charge_row = _charge_row(w, op)
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
        offer_id = _waitlist_offer(w, op)
    with runtime_role(), patient_access.patient_session_context(op.patient_session):
        authority = consent.patient_authority()
        _, offer = consent.prepare_acceptance(text_id=text.pk)
        slots = patient_booking.patient_slots(date(2035, 6, 3))
        join = teleconsult.request_patient_join(session_id=tc.session.pk)
    with setup_context(w.graph.organization_a):
        credential = TeleconsultCredential.objects.select_related("session").get(
            token_digest=hashlib.sha256(join.token.encode()).hexdigest()
        )
    actor_channels.observe_request_user(w.request)
    return ProbeWorld(
        w=w,
        op=op,
        rx=rx,
        tc=tc,
        matrix=probe_states.build_states(
            w.graph, w.graph.physician, closed_encounter_physician(w, op)
        ),
        authority=authority,
        offer=offer,
        slot_token=slots[0].token if slots else "no-slot",
        patient_credential=credential,
        invitation_code=invitation.secret,
        text=text,
        acceptance_id=acceptance.pk,
        released_version=released.pk,
        clinic=clinic,
        encounter=encounter,
        envelope=protected,
        sealed=sealed,
        fresh_request=actor_channels.observe_request_user(_fresh_request(w)),
        charge_row=charge_row,
        offer_id=offer_id,
        observer=actor_observer(w),
    )


def actor_observer(w: LegacyWorld) -> actor_channels.ActorObserver:
    """The actor channels of the live system (identity/actor_channels.py)."""
    with runtime_role():
        settings = actor_channels.actor_settings(
            lambda: tenant_context(w.graph.physician, w.graph.organization_a),
            w.graph.physician,
        )
    with connection.cursor() as cursor:
        catalog = actor_channels.actor_catalog(cursor, settings)
        graph = census.build_graph(census.live_decisions(cursor))
    accessors = actor_channels.python_accessors(graph, catalog)
    codes: dict[CodeType, str] = {}
    for symbol in accessors:
        code = target_code(symbol)
        assert code is not None, symbol
        codes[code] = symbol
    return actor_channels.ActorObserver(catalog, codes)


def closed_encounter_physician(w: LegacyWorld, op: OperationalSubjects) -> UUID:
    """A physician whose only encounter is closed through the real path.

    Booked by reception, opened, documented and finalized (with step-up) by
    the physician, then closed by close_encounter, which refuses while a
    draft is in progress.
    """
    physician = User.objects.create(username=f"probe-closed-{uuid4().hex}")
    with owner_context(w.graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=w.graph.organization_a,
            clinic_id=w.clinic,
            user_id=physician.pk,
            role=UserClinicRole.Role.PHYSICIAN,
        )
    with runtime_role(), tenant_context(w.graph.shared_user, w.graph.organization_a):
        create_availability(
            clinic_id=w.clinic,
            practitioner_id=physician.pk,
            start_local="2035-06-04T10:00",
            end_local="2035-06-04T11:00",
            idempotency_key=uuid4(),
        )
        appointment = create_appointment(
            clinic_id=w.clinic,
            enrollment_id=op.enrollment,
            practitioner_id=physician.pk,
            local_range=AppointmentLocalRange("2035-06-04T10:00", "2035-06-04T10:30"),
            idempotency_key=uuid4(),
        )
    request = verified_request(physician.pk, verified_at=STEP_UP_NOW)
    with runtime_role(), tenant_context(physician.pk, w.graph.organization_a):
        assert request.user.is_verified()  # type: ignore[union-attr]
        encounter = open_encounter(clinic_id=w.clinic, appointment_id=appointment.pk)
        version = create_draft(
            clinic_id=w.clinic, encounter_id=encounter.pk, template_id=w.template.pk
        )
        version = record_clinical_note(
            clinic_id=w.clinic,
            version_id=version.pk,
            expected_revision=version.revision,
            content=dict.fromkeys(SOAP_FIELDS, "Sintetico encerrado"),
        )
        finalization.finalize_version(
            clinic_id=w.clinic,
            version_id=version.pk,
            expected_revision=version.revision,
            request=request,
        )
        closed = finalization.close_encounter(
            clinic_id=w.clinic, encounter_id=encounter.pk
        )
    assert closed.state == Encounter.State.CLOSED
    return physician.pk


def _fresh_request(w: LegacyWorld) -> HttpRequest:
    """A step-up-fresh request; its lazy OTP device resolves as that user."""
    request = verified_request(w.graph.physician, verified_at=STEP_UP_NOW)
    with runtime_role(), tenant_context(w.graph.physician, w.graph.organization_a):
        assert request.user.is_verified()  # type: ignore[union-attr]
    return request


def _charge_row(w: LegacyWorld, op: OperationalSubjects) -> tuple[object, ...]:
    """A released Pix charge row, as billing_patient_charge would return it."""
    buffer = io.BytesIO()
    Image.new("RGB", (2, 3)).save(buffer, format="PNG")
    qr = base64.b64encode(buffer.getvalue())
    return (
        op.invoice.pk,
        "SINTETICO-REF",
        100,
        "BRL",
        "issued",
        datetime(2035, 1, 1, tzinfo=UTC),
        None,
        None,
        envelope.protect(purpose="billing.pixcharge.copy_code", plaintext=b"0002SINT"),
        envelope.protect(purpose="billing.pixcharge.qr_base64", plaintext=qr),
        datetime(2035, 1, 2, tzinfo=UTC),
        False,
        "America/Sao_Paulo",
    )


def _waitlist_offer(w: LegacyWorld, op: OperationalSubjects) -> UUID:
    """A pending offer to the probe patient inside the seeded availability."""
    starts = datetime(2035, 6, 3, 11, 30, tzinfo=UTC)
    ends = datetime(2035, 6, 3, 12, 0, tzinfo=UTC)
    entry = WaitlistEntry.objects.create(
        organization_id=w.graph.organization_a,
        clinic_id=w.clinic,
        patient_id=w.appointment.patient_id,
        enrollment_id=op.enrollment,
        practitioner_id=w.graph.physician,
        practitioner_label="Sintetico",
        start_at=starts,
        end_at=ends,
        state=WaitlistEntry.State.OFFERED,
    )
    return WaitlistOffer.objects.create(
        organization_id=w.graph.organization_a,
        clinic_id=w.clinic,
        practitioner_id=w.graph.physician,
        entry=entry,
        start_at=starts,
        end_at=ends,
        expires_at=datetime(2035, 6, 1, tzinfo=UTC),
    ).pk


def _request(pw: ProbeWorld, method: str = "GET", data: object = None) -> HttpRequest:
    factory = RequestFactory()
    request = factory.post("/", data or {}) if method == "POST" else factory.get("/")
    request.session = SessionStore()
    request.session[PATIENT_SESSION_KEY] = str(pw.op.patient_session)
    request.user = AnonymousUser()
    MessageMiddleware(lambda _request: HttpResponse()).process_request(request)
    return actor_channels.observe_request_user(request)


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


class Each(tuple[Outcome, ...]):
    """Outcomes of several inputs to one function; the first is primary.

    Each input runs in its own savepoint (except in ``entry_bare``), so
    one input's writes never reach the next. Extra inputs exist to drive
    every call line of the body in some state (``gate_lines``).
    """

    __slots__ = ()


def _each(*calls: Callable[[], object]) -> Each:
    return Each(_outcome(call, savepoint=True) for call in calls)


def _each_bare(*calls: Callable[[], object]) -> Each:
    return Each(_outcome(call, savepoint=False) for call in calls)


def reached(outcome: Outcome) -> str:
    """The outcome class (ok/raise) of the primary input."""
    value = outcome[1] if outcome[0] == "ok" else None
    if isinstance(value, tuple) and value[:1] == ("Each",):
        primary = value[1][0]
        assert isinstance(primary, tuple)
        return str(primary[0])
    return str(outcome[0])


@contextmanager
def _as_owner() -> Iterator[None]:
    """Run as clinic_owner, the deployed role of owner-only code paths.

    SET ROLE is transactional: an error rolls the savepoint back and with
    it the role; success switches back to the runtime role explicitly.
    """
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("RESET ROLE")
        yield
        cursor.execute("SET ROLE clinic_app")


@contextmanager
def _answer(fragment: str, sql: str, params: Sequence[object] = ()) -> Iterator[None]:
    """Answer every statement containing ``fragment`` with ``sql`` instead.

    Drives a body past a resolver that returns nothing (or forces a
    defensive empty result); the real resolver still runs in the primary
    input under every state.
    """

    def wrapper(
        execute: Callable[..., object],
        statement: str,
        parameters: object,
        many: bool,
        context: object,
    ) -> object:
        if fragment in statement:
            return execute(sql, list(params), many, context)
        return execute(statement, parameters, many, context)

    with connection.execute_wrapper(wrapper):
        yield


@contextmanager
def _forced(owner: object, name: str, value: object) -> Iterator[None]:
    """Replace one module global for one input (a forced branch)."""
    original = getattr(owner, name)
    setattr(owner, name, value)
    try:
        yield
    finally:
        setattr(owner, name, original)


def _raiser(error: BaseException) -> Callable[..., object]:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise error

    return fail


_NOTHING: Final = "SELECT 1 WHERE false"


def _with(*contexts: object) -> Callable[[Callable[[], object]], Callable[[], object]]:
    """Bind context managers around a thunk."""

    def bind(call: Callable[[], object]) -> Callable[[], object]:
        def run() -> object:
            with ExitStack() as stack:
                for context in contexts:
                    stack.enter_context(context)  # type: ignore[arg-type]
                return call()

        return run

    return bind


def _audit_event(pw: ProbeWorld) -> object:
    return build_phase1_audit_event(
        "intake.patient.searched",
        clinic_id=pw.w.clinic,
        affected_record_id=pw.w.clinic,
    )


def _audit(pw: ProbeWorld) -> object:
    def append() -> object:
        event = _audit_event(pw)
        return record_event(event.event, payload=event.payload)  # type: ignore[attr-defined]

    return _each(
        append,
        _with(_answer("current_setting('app.current_tenant'", _NOTHING))(append),
        _with(_answer("clinic_app.audit_append(", _NOTHING))(append),
    )


def _system_audit(pw: ProbeWorld) -> object:
    def append() -> object:
        event = _audit_event(pw)
        return _record_system_event(event.event, payload=event.payload)  # type: ignore[attr-defined]

    def owner() -> object:
        with _as_owner():
            return append()

    def owner_empty() -> object:
        with _as_owner(), _answer("audit_append_system(", _NOTHING):
            return append()

    return _each(append, owner, owner_empty)


def _bootstrap_request() -> BootstrapRequest:
    return BootstrapRequest(
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


def _bootstrap(pw: ProbeWorld) -> object:
    del pw

    def run() -> object:
        bootstrap_clinic(_bootstrap_request(), "synthetic-probe-password")
        return None

    def owner() -> object:
        with _as_owner():
            return run()

    return _each(run, owner)


def _record_consent(pw: ProbeWorld) -> object:
    def record(offer: str, *, accepted: bool = True) -> Callable[[], object]:
        return lambda: consent.record_consent(
            offer=offer, purpose="teleconsultation", accepted=accepted
        )

    stale = signing_core.dumps(
        {"text": str(pw.text.pk), "purpose": "teleconsultation", "digest": "stale"},
        salt=f"consent.offer:{pw.op.patient_session}",
    )

    def revoked() -> object:
        consent.revoke_consent(acceptance_id=pw.acceptance_id)
        return consent.record_consent(
            offer=pw.offer, purpose="teleconsultation", accepted=True
        )

    def fresh_text() -> object:
        # A newer text version has no acceptance yet: the created branch.
        # The publication guard binds the publisher as the acting user.
        text = pw.text
        with _as_owner(), _bound(pw, text.published_by_id):
            newer = ConsentText.objects.create(
                organization_id=text.organization_id,
                clinic_id=text.clinic_id,
                purpose=text.purpose,
                version=text.version + 1,
                text="Sintetico nova",
                language=text.language,
                digest=hashlib.sha256(b"Sintetico nova").hexdigest(),
                published_by_id=text.published_by_id,
            )
        _, offer = consent.prepare_acceptance(text_id=newer.pk)
        return consent.record_consent(
            offer=offer, purpose="teleconsultation", accepted=True
        )

    return _each(
        record(pw.offer),
        record(pw.offer, accepted=False),
        record(stale),
        revoked,
        fresh_text,
    )


_GUCS: Final = ("app.current_tenant", "app.current_user_id")


@contextmanager
def _bound(pw: ProbeWorld, user: UUID) -> Iterator[None]:
    """Bind tenant and user for one staff-side write, then restore both.

    Runs inside ``_as_owner``'s savepoint: an error rolls the settings back
    with it, success restores them explicitly.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.current_setting(%s, true), "
            "pg_catalog.current_setting(%s, true)",
            list(_GUCS),
        )
        saved = cursor.fetchone() or ("", "")
        for setting, value in zip(_GUCS, (pw.organization, user), strict=True):
            cursor.execute(
                "SELECT pg_catalog.set_config(%s, %s, true)", [setting, str(value)]
            )
    yield
    with connection.cursor() as cursor:
        for setting, value in zip(_GUCS, saved, strict=True):
            cursor.execute(
                "SELECT pg_catalog.set_config(%s, %s, true)", [setting, value or ""]
            )


def _owner_tty(pw: ProbeWorld) -> object:
    del pw

    def enter() -> object:
        with owner_tty() as tty:
            return tty.writable()

    def with_terminal() -> object:
        # The deployed shape: a controlling terminal and the owner role.
        leader, follower = os.openpty()
        terminal = os.ttyname(follower)
        original = Path.open

        def opener(self: Path, *args: object, **kwargs: object) -> object:
            target = Path(terminal) if str(self) == "/dev/tty" else self
            return original(target, *args, **kwargs)  # type: ignore[call-overload]

        try:
            with _forced(Path, "open", opener), _as_owner():
                return enter()
        finally:
            os.close(leader)
            os.close(follower)

    return _each(enter, with_terminal)


def _session_entry(pw: ProbeWorld) -> object:
    with patient_access.patient_session_context(pw.op.patient_session) as binding:
        return binding is not None


def _middleware_patient(pw: ProbeWorld) -> object:
    def patient(status: int, request: HttpRequest) -> Callable[[], object]:
        middleware = TenantMiddleware(lambda _request: HttpResponse(status=status))
        return lambda: middleware._patient(request)

    anonymous = _request(pw)
    anonymous.session = SessionStore()
    unknown = _request(pw)
    unknown.session[PATIENT_SESSION_KEY] = str(uuid4())
    return _each_bare(
        patient(204, _request(pw)),
        patient(204, anonymous),
        patient(204, unknown),
        patient(500, _request(pw)),
    )


def _attachments(pw: ProbeWorld) -> object:
    def handle(**options: object) -> Callable[[], object]:
        def run() -> object:
            output = io.StringIO()
            EncryptAttachmentObjects(stdout=output).handle(**options)
            return output.getvalue()

        return run

    organization = [str(pw.organization)]
    failing = ObjectMigrationReceipt(
        organizations=1, already_enveloped=0, migrated=0, failed=1
    )
    return _each(
        handle(),
        handle(organization_id=["not-a-uuid"]),
        handle(organization_id=organization),
        _with(_as_owner())(handle(organization_id=organization)),
        _with(
            _as_owner(),
            _forced(
                attachment_command, "migrate_attachment_objects", lambda _ids: failing
            ),
        )(handle(organization_id=organization)),
    )


def _acl_reverse(pw: ProbeWorld) -> object:
    del pw

    def reverse(editor_factory: Callable[[], object]) -> Callable[[], object]:
        def run() -> object:
            with editor_factory() as editor:  # type: ignore[attr-defined]
                remove_runtime_identity_acl(None, editor)  # type: ignore[arg-type]
            return None

        return run

    def owner() -> object:
        with _as_owner():
            return reverse(lambda: connection.schema_editor(atomic=False))()

    return _each(
        reverse(lambda: nullcontext(SimpleNamespace(connection=connection))),
        owner,
    )


def _owner_only(call: Callable[[], object]) -> object:
    """An owner-only function as the runtime role (refused) and as owner."""

    def owner() -> object:
        with _as_owner():
            return call()

    return _each(call, owner)


def _charge_sql(width: int) -> str:
    return "SELECT " + ", ".join(["%s"] * width)


def _patient_charge(pw: ProbeWorld) -> object:
    call = lambda: billing_presentation.patient_charge(invoice_id=pw.op.invoice.pk)  # noqa: E731
    row = pw.charge_row
    return _each(
        call,
        _with(_answer("billing_patient_charge(", _charge_sql(len(row)), row))(call),
    )


def _resolve_charge(pw: ProbeWorld) -> object:
    call = lambda: reconciliation._resolve_charge(SYNTHETIC_PROVIDER, "probe-reference")  # noqa: E731
    state = dataclasses.replace(_charge_state(pw), operation_id=_OPERATION)
    row = tuple(getattr(state, item.name) for item in fields(state))
    return _each(
        call,
        _with(_answer("billing_payment_event_scope(", _charge_sql(len(row)), row))(
            call
        ),
    )


def _callback_scope(pw: ProbeWorld) -> object:
    call = lambda: signing._resolve_callback_scope(  # noqa: E731
        pw.rx.operation.provider, str(pw.rx.operation.pk)
    )
    row = (
        pw.rx.operation.pk,
        pw.organization,
        pw.w.clinic,
        pw.w.graph.physician,
        pw.rx.operation.provider,
    )
    return _each(
        call,
        _with(_answer("prescription_signature_callback_scope(", _charge_sql(5), row))(
            call
        ),
    )


_ISSUED: Final = datetime(2035, 1, 1, tzinfo=UTC)
_OPERATION: Final = UUID("00000000-0000-4000-8000-00000000c0de")


def _patient_documents(pw: ProbeWorld) -> object:
    call = verification.patient_documents
    row = (pw.rx.document.pk, 1, "released", _ISSUED, "https://verify.invalid/x")
    return _each(
        call,
        _with(_answer("prescription_patient_documents(", _charge_sql(5), row))(call),
    )


def _patient_download(pw: ProbeWorld) -> object:
    call = lambda: verification.patient_document_download(  # noqa: E731
        document_id=pw.rx.document.pk
    )
    return _each(
        call,
        _with(
            _answer("prescription_patient_document_bytes(", "SELECT %s", [b"%PDF"]),
            _answer("prescription_document_viewed(", "SELECT 1"),
        )(call),
    )


def _record_download(pw: ProbeWorld) -> object:
    call = lambda: verification._record_patient_download(pw.rx.document.pk)  # noqa: E731
    return _each(
        call,
        _with(_answer("prescription_document_viewed(", "SELECT 1"))(call),
        _with(_answer("prescription_document_viewed(", _NOTHING))(call),
    )


def _identity_state(pw: ProbeWorld) -> object:
    return _each(
        lambda: prescription_views._identity_state(pw.w.request),
        lambda: prescription_views._identity_state(pw.fresh_request),
    )


def _questionnaires(pw: ProbeWorld) -> object:
    response = str(pw.op.response.pk)
    revision = str(pw.op.response.revision)

    def post(**data: str) -> Callable[[], object]:
        return lambda: questionnaire_views.patient_questionnaires(
            _request(pw, "POST", data)
        )

    return _each(
        lambda: questionnaire_views.patient_questionnaires(_request(pw)),
        post(response_id=response, action="open"),
        post(response_id=response, action="submit", revision=revision, q_synthetic="a"),
        post(response_id=response, action="save", revision="99"),
        post(response_id=response, action="other"),
        post(response_id="not-a-uuid", action="open"),
    )


def _slots(pw: ProbeWorld) -> object:
    return _each(
        lambda: patient_booking.patient_slots(date(2035, 6, 3)),
        lambda: patient_booking.patient_slots(
            date(2035, 6, 3), appointment_id=pw.w.appointment.pk
        ),
    )


def _patient_submit(pw: ProbeWorld) -> object:
    def submit(**data: str) -> Callable[[], object]:
        return lambda: patient_views._submit(_request(pw, "POST", data), str(uuid4()))

    # Book and reschedule only need their call lines executed: an unknown
    # slot token is refused by the callee after the line ran.
    appointment = str(pw.w.appointment.pk)
    return _each(
        submit(action="cancel", appointment_id=appointment),
        submit(action="book", slot="unknown-slot"),
        submit(action="reschedule", appointment_id=appointment, slot="unknown-slot"),
    )


def _respond(pw: ProbeWorld) -> object:
    def respond(*, accept: bool) -> Callable[[], object]:
        return lambda: waitlist.respond_to_offer(pw.offer_id, accept=accept)

    def booked(*_args: object) -> object:
        return pw.w.appointment

    # Each accepting input forces _book_offer's result, so every branch
    # after the booking call runs without a real booking per state.
    return _each(
        lambda: waitlist.respond_to_offer(uuid4(), accept=True),
        respond(accept=False),
        _with(
            _forced(
                waitlist, "_book_offer", _raiser(waitlist._ExpiredDuringBookingError())
            )
        )(respond(accept=True)),
        _with(_forced(waitlist, "_book_offer", _raiser(SlotConflict())))(
            respond(accept=True)
        ),
        _with(_forced(waitlist, "_book_offer", booked))(respond(accept=True)),
    )


def _patient_join(pw: ProbeWorld) -> object:
    call = lambda: teleconsult.request_patient_join(session_id=pw.tc.session.pk)  # noqa: E731
    states = tuple(TeleconsultSession.State.values)
    return _each(
        call,
        _with(_forced(teleconsult, "_TERMINAL_STATES", states))(call),
        _with(_forced(teleconsult, "_room_ready", lambda _session: False))(call),
        _with(
            _forced(
                teleconsult, "_liveness_failure", lambda _session: "consent_revoked"
            )
        )(call),
    )


type _Normalizer = Callable[[Outcome], Outcome]
_CSRF: Final = re.compile(rb'(name="csrfmiddlewaretoken" value=")[^"]*')
_SIGNED_AT: Final = re.compile(r"^(eyJ[^:]*):[0-9A-Za-z]+:[-_0-9A-Za-z]+$")


def _rewrite(node: object, change: Callable[[object], object]) -> object:
    node = change(node)
    if isinstance(node, tuple):
        return tuple(_rewrite(item, change) for item in node)
    return node


def _fields(*names: str, why: str) -> tuple[_Normalizer, str]:
    """Blank the value of the named fields, never their presence or type.

    A field's canonical value is ``(type name, value)``; the type name
    stays, so ``None`` and a value, or two different types, still compare
    unequal. Only the content of a present value is masked.
    """

    def change(node: object) -> object:
        if (
            isinstance(node, tuple)
            and len(node) == 2
            and node[0] in names
            and isinstance(node[1], tuple)
            and node[1]
            and isinstance(node[1][0], str)
            and node[1][0] != "NoneType"
        ):
            return (node[0], (node[1][0], "<volatile>"))
        return node

    return (lambda outcome: _rewrite(outcome, change), why)  # type: ignore[return-value]


def _leaves(
    pattern: re.Pattern[bytes] | re.Pattern[str], *, why: str
) -> tuple[_Normalizer, str]:
    """Blank the per-call part of matching text or bytes leaves."""

    def change(node: object) -> object:
        if isinstance(node, bytes) and isinstance(pattern.pattern, bytes):
            return pattern.sub(rb"\1<volatile>", node)
        if isinstance(node, str) and isinstance(pattern.pattern, str):
            return pattern.sub(r"\1:<volatile>", node)
        return node

    return (lambda outcome: _rewrite(outcome, change), why)  # type: ignore[return-value]


def _fresh_ids(*inputs: int) -> tuple[_Normalizer, str]:
    """Blank the audit row id the given inputs return (a sequence value)."""

    def change(outcome: Outcome) -> Outcome:
        kind, value = outcome[0], outcome[1] if len(outcome) > 1 else None
        if kind != "ok" or not isinstance(value, tuple) or value[:1] != ("Each",):
            return outcome
        subs = list(value[1])
        for index in inputs:
            sub = subs[index]
            if sub[0] == "ok" and sub[1][0] == "int":
                subs[index] = ("ok", ("int", "fresh sequence value"))
        return ("ok", ("Each", tuple(subs)))

    return (change, "audit_append returns the next audit sequence value per call")


def _probes() -> tuple[ExemptionProbe, ...]:
    staff: Context = "staff"
    patient: Context = "patient"
    entry: Context = "entry"
    ok: Reaches = "success"
    no: Reaches = "refusal"
    probe = ExemptionProbe
    return (
        # Infrastructure: audit, fairness, envelope, owner-only commands.
        probe(
            "apps.audit.services.record_event",
            staff,
            ok,
            _audit,
            *_fresh_ids(0),
        ),
        probe(
            "apps.audit.services._record_system_event",
            staff,
            no,
            _system_audit,
            *_fresh_ids(1),
        ),
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
            _attachments,
        ),
        probe("apps.identity.management.base.owner_tty", entry, no, _owner_tty),
        probe(
            "apps.identity.management.bootstrap.bootstrap_clinic", entry, no, _bootstrap
        ),
        probe(
            "apps.identity.phase1a_identity_acl_migration.remove_runtime_identity_acl",
            entry,
            no,
            _acl_reverse,
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
            lambda pw: _owner_only(envelope.issue_tenant_key),
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
            lambda pw: _owner_only(
                lambda: (
                    envelope.decrypt(
                        purpose=_PURPOSE,
                        envelope=envelope.reencrypt(
                            purpose=_PURPOSE, envelope=pw.sealed
                        ),
                    )
                    == _PLAINTEXT
                )
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
            lambda pw: _owner_only(
                lambda: envelope.rewrap_tenant_keys(new_kek="ab" * 32)
            ),
        ),
        probe(
            "apps.tenancy.envelope.tenant_key_status",
            staff,
            no,
            lambda pw: _owner_only(envelope.tenant_key_status),
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
            _resolve_charge,
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
            _callback_scope,
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
            _identity_state,
        ),
        # Patient-session functions: the actor's staff GUCs are injected.
        probe(
            "apps.billing.presentation.patient_charge",
            patient,
            ok,
            _patient_charge,
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
            lambda pw: consent.prepare_acceptance(text_id=pw.text.pk),
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
            *_fields(
                "idle_expires_at",
                why="touch_patient_session renews the idle deadline on every call",
            ),
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
            _questionnaires,
            *_leaves(
                _CSRF,
                why="Django renders a fresh CSRF token into every form",
            ),
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
            _record_download,
        ),
        probe(
            "apps.prescription.verification.patient_document_download",
            patient,
            no,
            _patient_download,
        ),
        probe(
            "apps.prescription.verification.patient_documents",
            patient,
            ok,
            _patient_documents,
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
            _slots,
            *_leaves(
                _SIGNED_AT,
                why="slot tokens carry the TimestampSigner issue time and its MAC",
            ),
        ),
        probe(
            "apps.scheduling.patient_booking.reschedule_patient_appointment",
            patient,
            ok,
            lambda pw: patient_booking.reschedule_patient_appointment(
                pw.w.appointment.pk, pw.slot_token
            ),
            *_fields("updated_at", why="auto_now stamps each reschedule write"),
        ),
        probe(
            "apps.scheduling.patient_views._submit",
            patient,
            ok,
            _patient_submit,
        ),
        probe(
            "apps.scheduling.waitlist.respond_to_offer",
            patient,
            no,
            _respond,
            *_fields(
                "responded_at",
                "appointment_id",
                why="an answered offer stamps timezone.now(); an accepted one "
                "books a new appointment with a fresh uuid4 key",
            ),
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
            _patient_join,
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


# Exemptions the actor observer refused in fix round 5: each observes the
# actor, so the census classifies it by its executed boundary instead. The
# probes stay, as the executed evidence (test_reclassified_functions_observe
# _the_actor).
RECLASSIFIED: Final = frozenset(
    {
        "apps.audit.services._record_system_event",
        "apps.audit.services.record_event",
        "apps.prescription.views._identity_state",
        "apps.teleconsult.services._fail",
    }
)
ALL_PROBES: Final[Mapping[str, ExemptionProbe]] = {
    item.symbol: item for item in _probes()
}
PROBES: Final[Mapping[str, ExemptionProbe]] = {
    symbol: item for symbol, item in ALL_PROBES.items() if symbol not in RECLASSIFIED
}


_SCALARS: Final = (
    bool,
    int,
    float,
    str,
    bytes,
    UUID,
    Decimal,
    date,
    time,
    timedelta,
)
_MAX_DEPTH: Final = 16


def canonical(value: object, depth: int = 0) -> object:
    """Return a hashable, exact image of ``value`` (type-tagged, recursive).

    Fails closed: a value with no known structure raises ``TypeError``, so
    a probe can never compare less than the whole result.
    """
    if depth > _MAX_DEPTH:
        message = "result nests too deeply to compare exactly"
        raise TypeError(message)
    if value is None or isinstance(value, _SCALARS):
        return (type(value).__qualname__, value)
    if isinstance(value, Enum):
        return (type(value).__qualname__, canonical(value.value, depth + 1))
    if isinstance(value, memoryview):
        return ("bytes", value.tobytes())
    if isinstance(value, Each):
        return ("Each", tuple(value))
    if isinstance(value, HttpResponseBase):
        return _canonical_http(value)
    return _canonical_structure(value, depth + 1)


def _canonical_structure(value: object, inner: int) -> object:
    """Models, dataclasses, containers and plain objects, field by field."""
    name = type(value).__qualname__
    if isinstance(value, Model):
        return (
            value._meta.label,
            tuple(
                (field.attname, canonical(getattr(value, field.attname), inner))
                for field in value._meta.concrete_fields
            ),
        )
    if is_dataclass(value) and not isinstance(value, type):
        return (
            name,
            tuple(
                (item.name, canonical(getattr(value, item.name), inner))
                for item in fields(value)
            ),
        )
    if isinstance(value, list | tuple):
        return (name, tuple(canonical(v, inner) for v in value))
    if isinstance(value, dict):
        pairs = ((canonical(k, inner), canonical(v, inner)) for k, v in value.items())
        return (name, tuple(sorted(pairs, key=repr)))
    if isinstance(value, set | frozenset):
        return (name, tuple(sorted((canonical(v, inner) for v in value), key=repr)))
    state = _attributes(value)
    if state is None:
        message = f"no exact comparison for {name}"
        raise TypeError(message)
    return (name, canonical(state, inner))


def _attributes(value: object) -> dict[str, object] | None:
    names = [
        name
        for klass in type(value).__mro__
        for name in getattr(klass, "__slots__", ())
        if not name.startswith("__")
    ]
    found = {name: getattr(value, name) for name in names if hasattr(value, name)}
    own = getattr(value, "__dict__", None)
    if isinstance(own, dict):
        found.update(own)
    return found if (names or isinstance(own, dict)) else None


def _canonical_http(response: HttpResponseBase) -> object:
    body = (
        b"".join(response.streaming_content)  # type: ignore[attr-defined]
        if getattr(response, "streaming", False)
        else response.content  # type: ignore[attr-defined]
    )
    cookies = tuple(
        sorted(
            (key, morsel.value, tuple(sorted((k, str(v)) for k, v in morsel.items())))
            for key, morsel in response.cookies.items()
        )
    )
    return (
        "http",
        response.status_code,
        tuple(sorted(response.items())),
        cookies,
        body,
    )


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


type Watch = tuple[actor_channels.ActorObserver, bool, list[str]]


def _outcome(
    invoke: Callable[[], object], *, savepoint: bool, watch: Watch | None = None
) -> Outcome:
    if watch is not None:
        return _watched(invoke, savepoint=savepoint, watch=watch)
    try:
        if savepoint:
            with transaction.atomic():
                result = canonical(invoke())
                transaction.set_rollback(True)
        else:
            result = canonical(invoke())
    except Exception as error:  # noqa: BLE001 - the refusal is the outcome
        return ("raise", type(error).__qualname__, str(error))
    return ("ok", result)


def _watched(invoke: Callable[[], object], *, savepoint: bool, watch: Watch) -> Outcome:
    """Execute once while the actor observer records every channel.

    The statistics snapshots must share the execution's transaction, so the
    input runs in a savepoint inside one more rolled-back block; without a
    savepoint (a deployed context that must be outermost) only the unbound
    checks apply.
    """
    observer, bound, sink = watch
    if not savepoint:
        assert not bound, "a bound actor needs the statistics transaction"
        observer.begin(bound=False)
        outcome = _outcome(invoke, savepoint=False)
        sink.extend(observer.end())
        return outcome
    with transaction.atomic():
        observer.begin(bound=bound)
        outcome = _outcome(invoke, savepoint=True)
        sink.extend(observer.end())
        transaction.set_rollback(True)
    return outcome


@contextmanager
def _state_scope(context: Context, pw: ProbeWorld, actor: UUID) -> Iterator[None]:
    """Bind one staff state for a context; each input then takes a savepoint.

    ``staff``: one tenant transaction. ``patient``: one patient-session
    transaction with the actor's GUCs injected. ``entry``: the actor's GUCs
    at session level (each input is its own rolled-back transaction).
    """
    if context == "staff":
        with runtime_role(), tenant_context(actor, pw.organization):
            yield
    elif context == "patient":
        with (
            runtime_role(),
            patient_access.patient_session_context(pw.op.patient_session),
            _inject_actor(pw, actor, local=True),
        ):
            yield
    else:
        try:
            with runtime_role(), _inject_actor(pw, actor, local=False):
                yield
        finally:
            clear_connection_tenant_gucs()


def execute(probe: ExemptionProbe, pw: ProbeWorld, actor: UUID) -> Outcome:
    """Run one probe once as ``actor`` in its declared context."""
    call = lambda: probe.invoke(pw)  # noqa: E731 - bound once per state
    if probe.context == "entry_bare":
        try:
            with runtime_role(), _inject_actor(pw, actor, local=False):
                return _outcome(call, savepoint=False)
        finally:
            clear_connection_tenant_gucs()
    with _state_scope(probe.context, pw, actor):
        return _outcome(call, savepoint=True)


def baseline(
    probe: ExemptionProbe, pw: ProbeWorld, watch: Watch | None = None
) -> Outcome:
    """Run a patient probe as deployed: in the session, no staff GUC."""
    call = lambda: probe.invoke(pw)  # noqa: E731 - bound once
    with runtime_role(), patient_access.patient_session_context(pw.op.patient_session):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.current_setting('app.current_user_id', true)"
            )
            bound = cursor.fetchone()
        assert bound in ((None,), ("",)), (
            "staff actor bound in patient context",
            bound,
        )
        return _outcome(call, savepoint=True, watch=watch)


def deployed_entry(probe: ExemptionProbe, pw: ProbeWorld, watch: Watch) -> Outcome:
    """Run a sessionless probe as deployed: runtime role, no actor bound."""
    call = lambda: probe.invoke(pw)  # noqa: E731 - bound once
    try:
        with runtime_role():
            clear_connection_tenant_gucs()
            return _outcome(call, savepoint=probe.context == "entry", watch=watch)
    finally:
        clear_connection_tenant_gucs()


def probe_code(probe: ExemptionProbe) -> CodeType:
    """The code object the probe certifies."""
    code = probe.code or target_code(probe.symbol)
    assert code is not None, probe.symbol
    return code


def _nested(code: CodeType) -> tuple[CodeType, ...]:
    found = [code]
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            found.extend(_nested(constant))
    return tuple(found)


def gate_lines(code: CodeType) -> frozenset[int]:
    """Every line holding a call in the body (nested code included).

    A permission gate is a call, however it is spelled or bound (a partial,
    a table entry, an instance, a helper), so a probe can certify a body
    only if its inputs execute every such line in at least one state.
    Interpreter intrinsics (``CALL_INTRINSIC_*``) are not calls.
    """
    return frozenset(
        instruction.positions.lineno
        for nested in _nested(code)
        for instruction in dis.get_instructions(nested)
        if instruction.opname.startswith("CALL")
        and not instruction.opname.startswith("CALL_INTRINSIC")
        and instruction.positions is not None
        and instruction.positions.lineno is not None
    )


class _Monitor:
    """``sys.monitoring`` LINE and PY_START events on the probed code only."""

    def __init__(
        self,
        codes: Mapping[str, CodeType],
        observer: actor_channels.ActorObserver | None = None,
    ) -> None:
        self.observer = observer
        self.accessors = {} if observer is None else dict(observer.accessors)
        self.entry = {code: symbol for symbol, code in codes.items()}
        self.owner = {
            nested: symbol for symbol, code in codes.items() for nested in _nested(code)
        }
        self.current: str | None = None
        self.entered = False
        self.lines: dict[str, set[int]] = {symbol: set() for symbol in codes}
        self.tool = -1

    def __enter__(self) -> Self:
        monitoring = sys.monitoring
        self.tool = next(i for i in range(6) if monitoring.get_tool(i) is None)
        monitoring.use_tool_id(self.tool, "exemption-probes")
        monitoring.register_callback(self.tool, monitoring.events.PY_START, self._start)
        monitoring.register_callback(self.tool, monitoring.events.LINE, self._line)
        for code in {*self.owner, *self.accessors}:
            events = 0
            if code in self.owner:
                events |= monitoring.events.LINE
            if code in self.entry or code in self.accessors:
                events |= monitoring.events.PY_START
            monitoring.set_local_events(self.tool, code, events)
        return self

    def __exit__(self, *_exc: object) -> None:
        monitoring = sys.monitoring
        for code in {*self.owner, *self.accessors}:
            monitoring.set_local_events(self.tool, code, 0)
        monitoring.register_callback(self.tool, monitoring.events.PY_START, None)
        monitoring.register_callback(self.tool, monitoring.events.LINE, None)
        monitoring.free_tool_id(self.tool)

    def _start(self, code: CodeType, _offset: int) -> None:
        if self.current is not None and self.entry.get(code) == self.current:
            self.entered = True
        if self.observer is not None and code in self.accessors:
            self.observer.on_start(code)

    def _line(self, code: CodeType, line: int) -> None:
        if self.current is not None and self.owner.get(code) == self.current:
            self.lines[self.current].add(line)

    def begin(self, symbol: str) -> None:
        self.current = symbol
        self.entered = False

    def end(self) -> bool:
        self.current = None
        return self.entered


@dataclass(slots=True)
class ProbeRun:
    """What one probe did across the matrix (and, for patient code, once
    as deployed)."""

    outcomes: dict[str, Outcome]
    required: frozenset[int]
    reached: set[int]
    not_entered: list[str]
    baseline: Outcome | None = None
    seconds: float = 0.0
    observed: dict[str, list[str]] = field(default_factory=dict)


def run_matrix(
    probes: Sequence[ExemptionProbe],
    pw: ProbeWorld,
    states: Sequence[probe_states.ProbeState] | None = None,
    *,
    observe: bool = True,
) -> dict[str, ProbeRun]:
    """Execute every probe under every state, in state order.

    Phase states apply their shared-row setup once, before their turn, so
    each world runs the matrix once. Patient-context probes also run once
    as deployed (patient session, no staff actor bound), before the
    states; the lines that run reaches count too, because there no staff
    actor exists for a permission decision to vary with (``baseline``
    asserts it). Sessionless (entry) probes also run once as deployed.

    With ``observe`` the actor observer (identity/actor_channels.py) watches
    every deployed run and, for staff probes, the first state and every
    state that changes shared rows (``observed_state``). Until code first
    observes the actor it cannot tell two states over the same rows apart,
    so whether it observes the actor is the same in all of them.
    """
    assert not pw.matrix.applied, "a probe world runs its matrix once"
    pw.matrix.applied.add("run")
    codes = {probe.symbol: probe_code(probe) for probe in probes}
    runs = {
        probe.symbol: ProbeRun({}, gate_lines(codes[probe.symbol]), set(), [])
        for probe in probes
    }
    grouped: dict[Context, list[ExemptionProbe]] = {}
    for probe in probes:
        grouped.setdefault(probe.context, []).append(probe)
    chosen = list(pw.matrix.states if states is None else states)
    observer = pw.observer if observe else None
    capture: AbstractContextManager[None] = (
        nullcontext()
        if observer is None
        else actor_channels.statements_captured(observer)
    )
    with _Monitor(codes, observer) as monitor, capture:
        # Deployed runs first: they use world tokens with a lifetime (a
        # consent offer lasts 30 minutes), so they never depend on the
        # matrix's wall time.
        for probe in probes:
            if probe.context != "staff":
                _run_deployed(probe, runs[probe.symbol], monitor, pw, observer)
        # Every state after the first repeats code coverage.py already
        # traced; pausing its tracer once for all of them keeps the matrix
        # affordable under --cov (the reach check and the actor observer use
        # their own sys.monitoring tool and statistics).
        for index, state in enumerate(chosen[:1]):
            watching = observer if observed_state(index, state) else None
            _run_state(state, grouped, runs, monitor, pw, watching)
        with _untraced():
            for index, state in enumerate(chosen[1:], start=1):
                watching = observer if observed_state(index, state) else None
                _run_state(state, grouped, runs, monitor, pw, watching)
        for symbol, lines in monitor.lines.items():
            runs[symbol].reached = lines
    return runs


def _run_deployed(
    probe: ExemptionProbe,
    run: ProbeRun,
    monitor: _Monitor,
    pw: ProbeWorld,
    observer: actor_channels.ActorObserver | None,
) -> None:
    """A patient or sessionless probe once as deployed (no actor bound)."""
    watch: Watch | None = None
    if observer is not None:
        watch = (observer, False, run.observed.setdefault("deployed", []))
    if probe.context == "patient":
        run.baseline = _observed(
            monitor, probe, "baseline", run, _deployed(probe, pw, watch)
        )
    elif watch is not None:
        _observed(monitor, probe, "deployed", run, _deployed_entry(probe, pw, watch))


def _deployed_entry(
    probe: ExemptionProbe, pw: ProbeWorld, watch: Watch
) -> Callable[[], Outcome]:
    return lambda: deployed_entry(probe, pw, watch)


def observed_state(index: int, state: probe_states.ProbeState) -> bool:
    """The first state, and every state whose shared rows differ."""
    return index == 0 or state.setup is not None or state.scoped is not None


def _run_state(  # noqa: PLR0913 - one state needs the whole run context
    state: probe_states.ProbeState,
    grouped: Mapping[Context, list[ExemptionProbe]],
    runs: Mapping[str, ProbeRun],
    monitor: _Monitor,
    pw: ProbeWorld,
    observer: actor_channels.ActorObserver | None,
) -> None:
    """One state: one bound scope per context, one savepoint per probe.

    A scoped state change (a removal that must not outlive its state) is
    applied inside each scope and rolled back with it.
    """
    if state.setup is not None:
        state.setup()
    for context, group in grouped.items():
        if context == "entry_bare":
            scope: AbstractContextManager[None] = nullcontext()
        else:
            scope = _state_scope(context, pw, state.actor)
        with scope, _scoped(state, context):
            for probe in group:
                run = runs[probe.symbol]
                watch: Watch | None = None
                if observer is not None and context == "staff":
                    watch = (observer, True, run.observed.setdefault(state.label, []))
                started = perf_counter()
                outcome = _observed(
                    monitor,
                    probe,
                    state.label,
                    run,
                    _thunk(probe, pw, state.actor, watch),
                )
                run.seconds += perf_counter() - started
                run.outcomes[state.label] = outcome


@contextmanager
def _scoped(state: probe_states.ProbeState, context: Context) -> Iterator[None]:
    """Apply a state's scoped change inside one context scope, then undo it.

    Sessionless-bare probes run outside any transaction a scoped change
    could live in; they see the state without it (their deployed context
    binds no actor at all).
    """
    if state.scoped is None or context == "entry_bare":
        yield
        return
    with transaction.atomic():
        state.scoped()
        yield
        transaction.set_rollback(True)


def _deployed(
    probe: ExemptionProbe, pw: ProbeWorld, watch: Watch | None
) -> Callable[[], Outcome]:
    return lambda: baseline(probe, pw, watch)


def _thunk(
    probe: ExemptionProbe, pw: ProbeWorld, actor: UUID, watch: Watch | None = None
) -> Callable[[], Outcome]:
    """One input inside an already bound state scope (``entry_bare``: alone)."""
    if probe.context == "entry_bare":
        return lambda: execute(probe, pw, actor)
    return lambda: _outcome(lambda: probe.invoke(pw), savepoint=True, watch=watch)


def _observed(
    monitor: _Monitor,
    probe: ExemptionProbe,
    label: str,
    run: ProbeRun,
    thunk: Callable[[], Outcome],
) -> Outcome:
    """Execute once under the monitor; the body must start."""
    monitor.begin(probe.symbol)
    outcome = thunk()
    if not monitor.end():
        run.not_entered.append(label)
    return outcome if probe.normalize is None else probe.normalize(outcome)


@contextmanager
def _untraced() -> Iterator[None]:
    """Pause coverage.py's collector, if one is running, for repeated code."""
    current = coverage.Coverage.current()
    if current is None:
        yield
        return
    current.stop()
    try:
        yield
    finally:
        current.start()


def differential(probe: ExemptionProbe, pw: ProbeWorld) -> dict[str, Outcome]:
    """Execute ``probe`` under every state; the body must run each time."""
    run = run_matrix([probe], pw)[probe.symbol]
    assert not run.not_entered, (probe.symbol, run.not_entered, "body never ran")
    return run.outcomes


def staff_independent(outcomes: Mapping[str, Outcome]) -> bool:
    """Every state produced exactly the same outcome."""
    return len(set(outcomes.values())) == 1


def actor_problems(run: ProbeRun) -> list[str]:
    """The primary rule: an exempt function never observes the actor."""
    return [
        f"observes the actor ({label}): {findings}"
        for label, findings in sorted(run.observed.items())
        if findings
    ]


def problems(
    probe: ExemptionProbe, run: ProbeRun, *, observed: bool = True
) -> list[str]:
    """Why ``run`` cannot certify ``probe`` (empty when it can)."""
    found: list[str] = actor_problems(run) if observed else []
    if observed and not run.observed:
        found.append("the actor observer never ran")
    if run.not_entered:
        found.append(f"body never ran in {len(run.not_entered)} states")
    unreached = sorted(run.required - run.reached)
    if unreached:
        found.append(f"call lines never executed in any state: {unreached}")
    if probe.normalize is not None and not probe.why:
        found.append("normalized without a reason")
    if not staff_independent(run.outcomes):
        groups: dict[Outcome, list[str]] = {}
        for state, outcome in run.outcomes.items():
            groups.setdefault(outcome, []).append(state)
        ranked = sorted(groups.items(), key=lambda item: -len(item[1]))
        found.append(
            f"outcome differs across states ({len(groups)} outcomes): "
            + "; ".join(
                f"{len(states)} states (e.g. {states[:3]})"
                + (
                    ""
                    if index == 0
                    else f" differ at {difference(ranked[0][0], outcome)}"
                )
                for index, (outcome, states) in enumerate(ranked[:6])
            )
        )
    return found


def difference(left: object, right: object, path: str = "") -> str:
    """The first path where two canonical outcomes differ, with both values."""
    if (
        isinstance(left, tuple)
        and isinstance(right, tuple)
        and len(left) == len(right)
        and left != right
    ):
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            if a != b:
                label = (
                    a[0]
                    if isinstance(a, tuple)
                    and len(a) == 2
                    and isinstance(a[0], str)
                    and isinstance(b, tuple)
                    and a[0] == b[0]
                    else index
                )
                return difference(a, b, f"{path}/{label}")
    return f"{path or '/'}: {str(left)[:120]} != {str(right)[:120]}"
