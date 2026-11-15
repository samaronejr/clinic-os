"""Deterministic synthetic dataset generator for the named load profiles.

Everything here is synthetic: tenants, users, patients, drafts and appointments
are created through the same models and services the application uses, as the
owner for tenant provisioning and as ``clinic_app`` for domain writes.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from collections.abc import Iterator

    from apps.identity.models import User

    from ops.perf.profiles import Profile

PASSWORD = "perf-synthetic-credential-not-a-secret"  # noqa: S105
AGENDA_DAY = "2035-06-04"
SLOT_MINUTES = 20
BATCH = 1000
NEIGHBOR_PATIENTS = 300
_GIVEN = (
    "Ana",
    "Bruno",
    "Carla",
    "Diego",
    "Elisa",
    "Fabio",
    "Gabriela",
    "Henrique",
    "Isabela",
    "Joao",
)
_FAMILY = (
    "Almeida",
    "Barros",
    "Cardoso",
    "Duarte",
    "Esteves",
    "Ferraz",
    "Gomes",
    "Lopes",
    "Moura",
    "Nogueira",
)


@dataclass(slots=True)
class Tenant:
    """One synthetic organization with a single clinic and its personas."""

    organization_id: UUID
    clinic_id: UUID
    admin_id: UUID
    front_desk_id: UUID
    physician_ids: list[UUID] = field(default_factory=list)
    patient_count: int = 0
    # A bounded sample of enrollments and exact names for lookups.
    sample_enrollments: list[UUID] = field(default_factory=list)
    sample_names: list[str] = field(default_factory=list)
    template_id: UUID | None = None


@dataclass(slots=True)
class Dataset:
    """The seeded tenants; the first one is the focus (large) tenant."""

    profile: Profile
    seed: int
    tenants: list[Tenant]

    @property
    def focus(self) -> Tenant:
        """Return the tenant every scenario measures."""
        return self.tenants[0]

    @property
    def neighbor(self) -> Tenant:
        """Return a second tenant used as the noisy neighbour."""
        return self.tenants[1]

    @property
    def patients(self) -> int:
        """Total patients across every tenant."""
        return sum(item.patient_count for item in self.tenants)


def patient_name(number: int) -> str:
    """Return a unique, synthetic, normalizable full name."""
    given = _GIVEN[number % len(_GIVEN)]
    family = _FAMILY[(number // len(_GIVEN)) % len(_FAMILY)]
    return f"{given} {family} Sintetico {number:07d}"


@contextmanager
def _owner_tenant(organization_id: UUID) -> Iterator[None]:
    """Open one owner transaction scoped to a tenant (provisioning only)."""
    from django.db import connection, transaction  # noqa: PLC0415

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        yield


def _make_user(label: str, password_hash: str) -> User:
    from apps.identity.models import User  # noqa: PLC0415

    return User.objects.create(
        username=f"perf-{label}-{uuid4().hex}", password=password_hash
    )


def seed_tenant(profile: Profile, *, index: int) -> Tenant:
    """Provision one organization, clinic, personas, clinicians and DEK."""
    from apps.identity.models import (  # noqa: PLC0415
        Clinic,
        Organization,
        ProfessionalRegistration,
        UserClinicRole,
    )
    from apps.tenancy.envelope import issue_tenant_key  # noqa: PLC0415
    from django.contrib.auth.hashers import make_password  # noqa: PLC0415
    from django.utils import timezone  # noqa: PLC0415

    password_hash = make_password(PASSWORD)
    organization_id = uuid4()
    admin = _make_user("admin", password_hash)
    front_desk = _make_user("front", password_hash)
    physicians = [
        _make_user(f"phys{number}", password_hash)
        for number in range(profile.clinicians_per_clinic)
    ]
    with _owner_tenant(organization_id):
        organization = Organization.objects.create(
            id=organization_id,
            name=f"Perf Org {index}",
            cnpj=f"{int(organization_id) % 10**14:014d}",
        )
        clinic = Clinic.objects.create(
            organization=organization,
            name=f"Perf Clinic {index}",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        issue_tenant_key()
        for user, role in (
            (admin, UserClinicRole.Role.CLINIC_ADMIN),
            (front_desk, UserClinicRole.Role.RECEPTIONIST),
        ):
            UserClinicRole.objects.create(
                user_id=user.pk,
                organization_id=organization_id,
                clinic_id=clinic.pk,
                role=role,
            )
        now = timezone.now()
        for number, physician in enumerate(physicians):
            UserClinicRole.objects.create(
                user_id=physician.pk,
                organization_id=organization_id,
                clinic_id=clinic.pk,
                role=UserClinicRole.Role.PHYSICIAN,
            )
            ProfessionalRegistration.objects.create(
                organization_id=organization_id,
                clinic_id=clinic.pk,
                user_id=physician.pk,
                role="physician",
                council="CRM",
                number=f"SINTETICO-PERF-{index}-{number}",
                jurisdiction="SP",
                specialty="Sintetico",
                status="regular",
                valid_from=now - timedelta(days=1),
                valid_to=now + timedelta(days=3650),
            )
    return Tenant(
        organization_id=organization_id,
        clinic_id=clinic.pk,
        admin_id=admin.pk,
        front_desk_id=front_desk.pk,
        physician_ids=[item.pk for item in physicians],
    )


def seed_patients(tenant: Tenant, count: int, *, sample: int) -> None:
    """Bulk-create ``count`` patients with enrollments and blind indexes.

    The encrypted name goes through the model field and the blind index through
    the database HMAC boundary, so the stored shape is what ``create_patient``
    writes.
    """
    from apps.identity.models import Clinic  # noqa: PLC0415
    from apps.intake.models import Patient, PatientClinicEnrollment  # noqa: PLC0415
    from apps.intake.patient_name_index import name_indexes  # noqa: PLC0415

    start = tenant.patient_count
    for first in range(start, start + count, BATCH):
        last = min(first + BATCH, start + count)
        with _owner_tenant(tenant.organization_id):
            patients = [
                Patient(
                    organization_id=tenant.organization_id,
                    full_name=patient_name(number),
                    full_name_index=name_indexes(patient_name(number))[-1],
                )
                for number in range(first, last)
            ]
            clinic = Clinic.objects.get(pk=tenant.clinic_id)
            Patient.objects.bulk_create(patients)
            enrollments = [
                PatientClinicEnrollment(
                    organization_id=tenant.organization_id,
                    clinic=clinic,
                    patient=patient,
                    idempotency_key=uuid4(),
                    create_fingerprint=hashlib.sha256(patient.pk.bytes).digest(),
                )
                for patient in patients
            ]
            PatientClinicEnrollment.objects.bulk_create(enrollments)
        for offset, enrollment in enumerate(enrollments):
            if len(tenant.sample_enrollments) < sample:
                tenant.sample_enrollments.append(enrollment.pk)
                tenant.sample_names.append(patient_name(first + offset))
    tenant.patient_count += count


def seed_dataset(profile: Profile, *, seed: int) -> Dataset:
    """Seed every tenant of the profile; tenant 0 is the focus tenant."""
    tenants: list[Tenant] = []
    for index in range(profile.organizations):
        tenant = seed_tenant(profile, index=index)
        # Only the focus tenant carries the profile's patient base; the others
        # are neighbours and need just enough rows to exist.
        count = (
            profile.patients_per_org
            if index == 0
            else min(profile.patients_per_org, NEIGHBOR_PATIENTS)
        )
        seed_patients(tenant, count, sample=2000)
        tenants.append(tenant)
    return Dataset(profile=profile, seed=seed, tenants=tenants)


SOAP = {
    "subjective": "Relato sintetico de desempenho",
    "objective": "Exame sintetico",
    "assessment": "Avaliacao sintetica",
    "plan": "Plano sintetico",
}


@dataclass(slots=True)
class Draft:
    """One open draft an autosave editor keeps saving."""

    physician_id: UUID
    version_id: UUID
    revision: int


@dataclass(frozen=True, slots=True)
class Recording:
    """A consented synthetic recording; its token never enters a report."""

    session_id: UUID
    token: str = field(repr=False)


def seed_recording(tenant: Tenant) -> Recording:
    """Exercise patient consent and clinician disclosure before chunk timing."""
    import django.apps  # noqa: PLC0415
    from apps.consent.services import (  # noqa: PLC0415
        prepare_acceptance,
        publish_text,
        record_ai_disclosure,
        record_consent,
    )
    from apps.ehr.services import open_unscheduled_encounter  # noqa: PLC0415
    from apps.intake.patient_access import (  # noqa: PLC0415
        issue_invitation,
        patient_session_context,
        redeem_invitation,
    )
    from apps.providers.migrations._seed_v2 import (  # noqa: PLC0415
        seed_v2_capabilities,
    )
    from apps.scribe.services import (  # noqa: PLC0415
        create_recording,
        issue_encounter_token,
    )
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    from ops.perf.runtime import PerfEnvironmentError, runtime_role  # noqa: PLC0415

    seed_v2_capabilities(django.apps.apps)
    with runtime_role(), tenant_context(tenant.admin_id, tenant.organization_id):
        text = publish_text(
            clinic_id=tenant.clinic_id,
            purpose="consultation_recording",
            text="Synthetic performance recording consent.",
        )
        invitation = issue_invitation(
            clinic_id=tenant.clinic_id,
            enrollment_id=tenant.sample_enrollments[0],
        )
    with runtime_role():
        patient_session = redeem_invitation(tenant.clinic_id, invitation.secret)
    if patient_session is None:
        message = "synthetic recording consent session refused"
        raise PerfEnvironmentError(message)
    with runtime_role(), patient_session_context(patient_session):
        _text, offer = prepare_acceptance(text_id=text.pk)
        record_consent(offer=offer, purpose="consultation_recording", accepted=True)
    with (
        runtime_role(),
        tenant_context(tenant.physician_ids[0], tenant.organization_id),
    ):
        encounter = open_unscheduled_encounter(
            clinic_id=tenant.clinic_id,
            enrollment_id=tenant.sample_enrollments[0],
            reason="walk_in",
        )
        record_ai_disclosure(
            clinic_id=tenant.clinic_id,
            encounter_id=encounter.pk,
            informed=True,
            refused=False,
        )
        token = issue_encounter_token(
            clinic_id=tenant.clinic_id,
            encounter_id=encounter.pk,
            browser_binding="synthetic-perf-chunk",
        )
        recording = create_recording(
            clinic_id=tenant.clinic_id,
            encounter_token=token,
            command_id=uuid4(),
        )
    return Recording(recording.pk, token)


def seed_drafts(tenant: Tenant, count: int) -> list[Draft]:
    """Open ``count`` unscheduled encounters with SOAP drafts, one per editor.

    Runs the real services as ``clinic_app``: the admin publishes the template,
    each physician opens a walk-in encounter and creates the draft.
    """
    from apps.ehr.services import (  # noqa: PLC0415
        SOAP_FIELDS,
        create_draft,
        open_unscheduled_encounter,
        publish_template,
    )
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    from ops.perf.runtime import runtime_role  # noqa: PLC0415

    with runtime_role(), tenant_context(tenant.admin_id, tenant.organization_id):
        template = publish_template(
            clinic_id=tenant.clinic_id,
            key="perf",
            title="Perf",
            prompts=dict.fromkeys(SOAP_FIELDS, "Registro"),
        )
    tenant.template_id = template.pk
    drafts: list[Draft] = []
    for number in range(count):
        physician = tenant.physician_ids[number % len(tenant.physician_ids)]
        enrollment = tenant.sample_enrollments[number]
        with runtime_role(), tenant_context(physician, tenant.organization_id):
            encounter = open_unscheduled_encounter(
                clinic_id=tenant.clinic_id,
                enrollment_id=enrollment,
                reason="walk_in",
            )
            version = create_draft(
                clinic_id=tenant.clinic_id,
                encounter_id=encounter.pk,
                template_id=template.pk,
            )
        drafts.append(Draft(physician, version.pk, version.revision))
    return drafts


def seed_agenda(tenant: Tenant, *, visits_per_clinician: int) -> int:
    """Book a full clinic day through the scheduling services; return count."""
    from apps.scheduling.services import (  # noqa: PLC0415
        AppointmentLocalRange,
        create_appointment,
        create_availability,
    )
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    from ops.perf.runtime import runtime_role  # noqa: PLC0415

    booked = 0
    day_minutes = visits_per_clinician * SLOT_MINUTES
    end_hour, end_minute = divmod(8 * 60 + day_minutes, 60)
    for index, physician in enumerate(tenant.physician_ids):
        with (
            runtime_role(),
            tenant_context(tenant.front_desk_id, tenant.organization_id),
        ):
            create_availability(
                clinic_id=tenant.clinic_id,
                practitioner_id=physician,
                start_local=f"{AGENDA_DAY}T08:00",
                end_local=f"{AGENDA_DAY}T{end_hour:02d}:{end_minute:02d}",
                idempotency_key=uuid4(),
            )
            for visit in range(visits_per_clinician):
                begin = 8 * 60 + visit * SLOT_MINUTES
                finish = begin + SLOT_MINUTES
                enrollment = tenant.sample_enrollments[
                    (index * visits_per_clinician + visit)
                    % len(tenant.sample_enrollments)
                ]
                create_appointment(
                    clinic_id=tenant.clinic_id,
                    enrollment_id=enrollment,
                    practitioner_id=physician,
                    local_range=AppointmentLocalRange(
                        f"{AGENDA_DAY}T{begin // 60:02d}:{begin % 60:02d}",
                        f"{AGENDA_DAY}T{finish // 60:02d}:{finish % 60:02d}",
                    ),
                    idempotency_key=uuid4(),
                )
                booked += 1
    return booked
