import hashlib
import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from importlib import import_module
from importlib.util import find_spec
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg
import pytest
import rfc8785
from apps.core.secrets import secret_store
from apps.identity.models import Clinic, Organization
from apps.intake.models import (
    PATIENT_OPERATION_VALUES,
    Patient,
    PatientClinicEnrollment,
)
from apps.tenancy.envelope import reveal
from apps.tenancy.migrations._protected_migration import (
    ProtectedPlan,
    ProtectedRow,
    encrypt_backfill,
    require_backfill_complete,
)
from django.apps import apps as django_apps
from django.db import IntegrityError, connection, connections, transaction
from django.db.backends.postgresql.base import DatabaseWrapper
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from database_urls import database_url_for_name
from tenant_key_support import issue_tenant_key_for

MIGRATION_MODULE = "apps.intake.migrations.0001_patient_and_enrollment"
FOUNDATION_TARGETS = [
    ("identity", "0003_totp_device_rls"),
    ("tenancy", "0002_rls_and_resolvers"),
]
PRE_INTAKE_TARGETS = [
    ("identity", "0005_clinic_timezone"),
    ("tenancy", "0002_rls_and_resolvers"),
]
ORG_A = UUID(int=1001)
ORG_B = UUID(int=1002)
CLINIC_A = UUID(int=1101)
CLINIC_B = UUID(int=1102)
CLINIC_FOREIGN = UUID(int=1103)


def _migrate(targets: Sequence[tuple[str, str | None]]) -> None:
    MigrationExecutor(connection).migrate(targets)


def _migrate_head() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def _assert_intake_unapply_is_closed() -> None:
    """The protected-field migration is irreversible: unapply must refuse.

    Once applied, intake can never be unapplied back to plaintext storage;
    the tables stay exactly where the sealed migration left them.
    """
    with pytest.raises(IrreversibleError):
        _migrate(
            [
                (
                    "intake",
                    "0010_remove_patientaccessgrant_intake_grant_operations_check_and_more",
                )
            ]
        )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass('clinic_app.intake_patient'), "
            "to_regclass('clinic_app.intake_patientclinicenrollment')"
        )
        row = cursor.fetchone()
        assert row is not None
        assert all(value is not None for value in row)


def _set_tenant(organization_id: UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, false)",
            [str(organization_id)],
        )


@pytest.mark.django_db(transaction=True)
def test_intake_migration_preserves_duplicates_and_enrollment_integrity() -> None:
    module = import_module(MIGRATION_MODULE) if find_spec(MIGRATION_MODULE) else None

    assert module is not None
    assert module.Migration.dependencies == PRE_INTAKE_TARGETS
    try:
        _migrate_head()
        _set_tenant(ORG_A)
        organization_a = Organization.objects.create(
            id=ORG_A,
            name="Synthetic Organization A",
            cnpj="00000000001001",
        )
        issue_tenant_key_for(ORG_A)
        clinic_a = Clinic.objects.create(
            id=CLINIC_A,
            organization=organization_a,
            name="Synthetic Clinic A",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        clinic_b = Clinic.objects.create(
            id=CLINIC_B,
            organization=organization_a,
            name="Synthetic Clinic B",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        duplicate_a = Patient.objects.create(
            organization=organization_a,
            full_name="  Ana   Synthetic  ",
            birth_date=date(2000, 1, 2),
        )
        duplicate_b = Patient.objects.create(
            organization=organization_a,
            full_name="Ana Synthetic",
            birth_date=date(2000, 1, 2),
        )
        enrollment_a = PatientClinicEnrollment.objects.create(
            organization=organization_a,
            clinic=clinic_a,
            patient=duplicate_a,
            idempotency_key=UUID(int=1201),
            create_fingerprint=b"a" * 32,
        )
        enrollment_b = PatientClinicEnrollment.objects.create(
            organization=organization_a,
            clinic=clinic_b,
            patient=duplicate_a,
            idempotency_key=UUID(int=1202),
            create_fingerprint=b"b" * 32,
        )

        assert duplicate_a.full_name == duplicate_b.full_name == "Ana Synthetic"
        assert {enrollment_a.clinic_id, enrollment_b.clinic_id} == {
            CLINIC_A,
            CLINIC_B,
        }

        _set_tenant(ORG_B)
        organization_b = Organization.objects.create(
            id=ORG_B,
            name="Synthetic Organization B",
            cnpj="00000000001002",
        )
        issue_tenant_key_for(ORG_B)
        Clinic.objects.create(
            id=CLINIC_FOREIGN,
            organization=organization_b,
            name="Synthetic Clinic Foreign",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        _set_tenant(ORG_A)
        with pytest.raises(IntegrityError), transaction.atomic():
            PatientClinicEnrollment.objects.create(
                organization=organization_a,
                clinic_id=CLINIC_FOREIGN,
                patient=duplicate_b,
                idempotency_key=UUID(int=1203),
                create_fingerprint=b"c" * 32,
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            PatientClinicEnrollment.objects.create(
                organization=organization_a,
                clinic=clinic_a,
                patient=duplicate_b,
                idempotency_key=UUID(int=1204),
                create_fingerprint=b"short",
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            PatientClinicEnrollment.objects.create(
                organization=organization_a,
                clinic=clinic_a,
                patient=duplicate_a,
                idempotency_key=UUID(int=1205),
                create_fingerprint=b"d" * 32,
            )
        with pytest.raises(IntegrityError), transaction.atomic():
            PatientClinicEnrollment.objects.create(
                organization=organization_a,
                clinic=clinic_b,
                patient=duplicate_b,
                idempotency_key=enrollment_a.idempotency_key,
                create_fingerprint=b"e" * 32,
            )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT conname FROM pg_catalog.pg_constraint "
                "WHERE conrelid = 'clinic_app.intake_patientclinicenrollment'"
                "::regclass AND conname = ANY(%s) ORDER BY conname",
                [
                    [
                        "intake_enrollment_fingerprint_32_check",
                        "intake_enrollment_org_clinic_fk",
                        "intake_enrollment_org_patient_fk",
                    ]
                ],
            )
            assert [row[0] for row in cursor.fetchall()] == [
                "intake_enrollment_fingerprint_32_check",
                "intake_enrollment_org_clinic_fk",
                "intake_enrollment_org_patient_fk",
            ]

        _assert_intake_unapply_is_closed()
        _migrate_head()
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET app.current_tenant")
        _migrate_head()


PROBE_ORG_A = UUID(int=2001)
PROBE_ORG_B = UUID(int=2002)
PROBE_ROW_A = UUID(int=2101)
PROBE_ROW_B = UUID(int=2102)
PROBE_PURPOSE = "tenancy.protectedprobe.secret"
PROBE_INTERRUPTION = "synthetic migration interruption"
PROBE_SECRETS = {
    PROBE_ROW_A: "segredo sintético A",
    PROBE_ROW_B: "segredo sintético B",
}


def _encode_probe_secret(record: ProtectedRow) -> bytes | None:
    return str(record["secret_legacy"]).encode()


PROBE_PLANS: tuple[ProtectedPlan, ...] = (
    (
        "secret",
        PROBE_PURPOSE,
        ("secret_legacy",),
        (),
        _encode_probe_secret,
        None,
    ),
)
_PROBE_SCHEMA_SQL = """
CREATE TABLE clinic_app.protected_probe (
    id pg_catalog.uuid PRIMARY KEY,
    organization_id pg_catalog.uuid NOT NULL
        REFERENCES clinic_app.identity_organization(id)
        DEFERRABLE INITIALLY DEFERRED,
    secret_legacy pg_catalog.text,
    secret pg_catalog.bytea
);
CREATE TABLE clinic_app.protected_probe_receipt (
    id pg_catalog.uuid PRIMARY KEY,
    probe_id pg_catalog.uuid NOT NULL
);
ALTER TABLE clinic_app.protected_probe ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.protected_probe FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON clinic_app.protected_probe
    USING (organization_id = NULLIF(
        pg_catalog.current_setting('app.current_tenant', true), ''
    )::pg_catalog.uuid);
GRANT SELECT ON clinic_app.protected_probe TO clinic_app;
CREATE FUNCTION clinic_app.protected_probe_receipt_trigger()
RETURNS trigger LANGUAGE plpgsql AS $probe$
BEGIN
    INSERT INTO clinic_app.protected_probe_receipt (id, probe_id)
    VALUES (pg_catalog.gen_random_uuid(), NEW.id);
    RETURN NULL;
END
$probe$;
CREATE FUNCTION clinic_app.protected_probe_guard()
RETURNS trigger LANGUAGE plpgsql AS $probe$
BEGIN
    RAISE EXCEPTION 'probe guard rejected a mechanical update'
        USING ERRCODE = '23514';
END
$probe$;
CREATE TRIGGER protected_probe_guard BEFORE UPDATE
    ON clinic_app.protected_probe FOR EACH ROW
    EXECUTE FUNCTION clinic_app.protected_probe_guard();
CREATE TRIGGER protected_probe_receipt AFTER UPDATE
    ON clinic_app.protected_probe FOR EACH ROW
    EXECUTE FUNCTION clinic_app.protected_probe_receipt_trigger();
"""
_PROBE_DROP_SQL = """
DROP TABLE IF EXISTS clinic_app.protected_probe;
DROP TABLE IF EXISTS clinic_app.protected_probe_receipt;
DROP FUNCTION IF EXISTS clinic_app.protected_probe_guard();
DROP FUNCTION IF EXISTS clinic_app.protected_probe_receipt_trigger();
"""


def _seed_protected_probe() -> None:
    """Create a populated legacy-shaped table guarded exactly like a real one.

    The protected-field migrations are irreversible by contract, so the
    populated case is proven against a table with the same posture the real
    ones have at migration time: enforced RLS, a tenant policy, an
    immutability guard that rejects mechanical updates, a receipt trigger
    that would fabricate clinical rows and a deferred foreign key whose
    constraint trigger queues pending events on every updated row — the
    exact posture that makes ALTER TABLE illegal without
    ``SET CONSTRAINTS ALL IMMEDIATE``.
    """
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(_PROBE_SCHEMA_SQL)
        for organization_id, row_id in (
            (PROBE_ORG_A, PROBE_ROW_A),
            (PROBE_ORG_B, PROBE_ROW_B),
        ):
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(organization_id)],
            )
            cursor.execute(
                "INSERT INTO clinic_app.identity_organization (id, name, cnpj) "
                "VALUES (%s, %s, %s)",
                [
                    str(organization_id),
                    f"Synthetic Legacy Organization {organization_id.int}",
                    f"{organization_id.int:014d}",
                ],
            )
            cursor.execute(
                "INSERT INTO clinic_app.protected_probe "
                "(id, organization_id, secret_legacy) VALUES (%s, %s, %s)",
                [str(row_id), str(organization_id), PROBE_SECRETS[row_id]],
            )
    for organization_id in (PROBE_ORG_A, PROBE_ORG_B):
        issue_tenant_key_for(organization_id)


def _probe_observation(app_database_url: str) -> tuple[bool, bool, str]:
    """Observe the probe table from a separate clinic_app connection.

    Returns ``(rls_enabled, force_rls, outcome)`` where outcome is the row
    count visible without a tenant context, or 'locked' when the migration's
    exclusive lock proves the table is closed to concurrent readers.
    """
    with psycopg.connect(app_database_url, autocommit=True) as app:
        posture = app.execute(
            "SELECT relrowsecurity, relforcerowsecurity "
            "FROM pg_catalog.pg_class "
            "WHERE oid = 'clinic_app.protected_probe'::pg_catalog.regclass"
        ).fetchone()
        app.execute("SET lock_timeout = '500ms'")
        try:
            row = app.execute(
                "SELECT count(*) FROM clinic_app.protected_probe"
            ).fetchone()
            outcome = str(row[0]) if row else "missing"
        except psycopg.errors.LockNotAvailable:
            outcome = "locked"
    assert posture is not None
    return (bool(posture[0]), bool(posture[1]), outcome)


def _probe_posture() -> tuple[bool, bool, int]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity "
            "FROM pg_catalog.pg_class "
            "WHERE oid = 'clinic_app.protected_probe'::pg_catalog.regclass"
        )
        security = cursor.fetchone()
        cursor.execute(
            "SELECT count(*) FROM pg_catalog.pg_trigger "
            "WHERE tgrelid = 'clinic_app.protected_probe'::pg_catalog.regclass "
            "AND NOT tgisinternal AND tgenabled <> 'O'"
        )
        disabled = cursor.fetchone()
    assert security is not None
    assert disabled is not None
    return (bool(security[0]), bool(security[1]), int(disabled[0]))


def _probe_rows() -> list[tuple[str | None, bytes | None]]:
    rows: list[tuple[str | None, bytes | None]] = []
    with transaction.atomic(), connection.cursor() as cursor:
        for organization_id, row_id in (
            (PROBE_ORG_A, PROBE_ROW_A),
            (PROBE_ORG_B, PROBE_ROW_B),
        ):
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(organization_id)],
            )
            cursor.execute(
                "SELECT secret_legacy, secret FROM clinic_app.protected_probe "
                "WHERE id = %s",
                [str(row_id)],
            )
            legacy, envelope = cursor.fetchone()
            rows.append((legacy, None if envelope is None else bytes(envelope)))
    return rows


@pytest.mark.django_db(transaction=True)
def test_populated_protected_migration_isolates_and_resumes(
    app_database_url: str,
) -> None:
    """A populated backfill must not open a window, fire guards or half-commit.

    A concurrent clinic_app session still sees enforced RLS and cannot read
    the table while the backfill holds it; an interruption leaves the legacy
    values and an unsealed table; the resumed run encrypts every tenant's
    rows with no guard rejection and no fabricated receipt, and restores the
    table's RLS and trigger posture exactly.
    """
    observations: list[tuple[bool, bool, str]] = []
    real_store = secret_store()

    class ObservedStore:
        """Observe the mid-backfill window, then interrupt the run."""

        def __init__(self, *, interrupt: bool) -> None:
            self._interrupt = interrupt

        def get_secret(self, name: str) -> str:
            observations.append(_probe_observation(app_database_url))
            if self._interrupt:
                raise RuntimeError(PROBE_INTERRUPTION)
            return real_store.get_secret(name)

    backfill = encrypt_backfill(
        table="protected_probe", pk_column="id", plans=PROBE_PLANS
    )
    seal = require_backfill_complete(table="protected_probe", plans=PROBE_PLANS)
    editor = connection.schema_editor(atomic=False)
    try:
        _seed_protected_probe()
        assert _probe_posture() == (True, True, 0)

        with (
            patch(
                "apps.core.secrets.secret_store",
                return_value=ObservedStore(interrupt=True),
            ),
            pytest.raises(RuntimeError, match="interruption"),
        ):
            backfill(django_apps, editor)
        # Nothing committed: plaintext intact, no envelope, table unsealed.
        assert _probe_rows() == [
            (PROBE_SECRETS[PROBE_ROW_A], None),
            (PROBE_SECRETS[PROBE_ROW_B], None),
        ]
        assert _probe_posture() == (True, True, 0)
        with pytest.raises(RuntimeError, match="still holds plaintext"):
            seal(django_apps, editor)

        with patch(
            "apps.core.secrets.secret_store",
            return_value=ObservedStore(interrupt=False),
        ):
            backfill(django_apps, editor)
        seal(django_apps, editor)

        # The concurrent observer never saw RLS off, and never read a row.
        assert observations == [(True, True, "locked"), (True, True, "locked")]
        assert _probe_posture() == (True, True, 0)
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM clinic_app.protected_probe_receipt")
            assert cursor.fetchone() == (0,)
        for (legacy, envelope), row_id in zip(
            _probe_rows(), (PROBE_ROW_A, PROBE_ROW_B), strict=True
        ):
            assert legacy == PROBE_SECRETS[row_id]
            assert envelope is not None
        for (_, envelope), organization_id, row_id in zip(
            _probe_rows(),
            (PROBE_ORG_A, PROBE_ORG_B),
            (PROBE_ROW_A, PROBE_ROW_B),
            strict=True,
        ):
            assert envelope is not None
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                    [str(organization_id)],
                )
                assert (
                    reveal(purpose=PROBE_PURPOSE, envelope=envelope)
                    == PROBE_SECRETS[row_id].encode()
                )
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET app.current_tenant")
            cursor.execute(_PROBE_DROP_SQL)


# --- Real-schema populated migration proof -----------------------------------
#
# The surrogate probe above cannot reproduce the real schema's deferred
# foreign-key triggers, JSONB cursor representation or empty-to-NULL legacy
# values. This test migrates a scratch database to the exact pre-migration
# state, seeds historical rows through the real guards and receipt triggers,
# interrupts the actual migration mid-backfill, resumes it, and verifies
# decrypted bytes, digests, receipts and the sealed schema.

LEGACY_ORG = UUID(int=3001)
LEGACY_CLINIC = UUID(int=3002)
LEGACY_PHYSICIAN = UUID(int=3003)
LEGACY_PATIENT = UUID(int=3004)
LEGACY_ENROLLMENT = UUID(int=3005)
LEGACY_TEMPLATE = UUID(int=3006)
LEGACY_DRAFT_RESPONSE = UUID(int=3007)
LEGACY_REOPENED_RESPONSE = UUID(int=3008)
LEGACY_GRANT = UUID(int=3009)
LEGACY_SESSION = UUID(int=3010)
LEGACY_BLOCK = UUID(int=3011)
LEGACY_APPOINTMENT = UUID(int=3012)
LEGACY_ENCOUNTER = UUID(int=3013)
LEGACY_RX_DRAFT = UUID(int=3014)
LEGACY_DOCUMENT = UUID(int=3015)
LEGACY_OPERATION = UUID(int=3016)
LEGACY_CONTACT = UUID(int=3017)
LEGACY_NAME = "Legacy Synthetic Patient"
LEGACY_BIRTH = "2000-01-02"
LEGACY_DESTINATION = "legacy-patient@example.invalid"
LEGACY_ANSWERS = {"q_notes": "legacy answer", "q_flag": True}
LEGACY_REASON = "legacy reopen reason"
LEGACY_PDF = b"%PDF-1.4\nlegacy synthetic document\n%%EOF\n"
LEGACY_SIGNED = b"legacy synthetic signed bytes"
LEGACY_FROZEN: dict[str, Any] = {
    "v": 1,
    "clinic_label": "Legacy Clinic",
    "issuer_label": "legacy.issuer",
    "items": [{"medication_description": "legacy item"}],
    "verification_url": "https://verify.invalid/legacy",
}
LEGACY_PATIENT_TABLES = (
    "intake_patient",
    "intake_patientcontact",
    "intake_questionnaireresponse",
    "intake_questionnaireevent",
    "prescription_prescriptiondocument",
    "prescription_signatureoperation",
)


@contextmanager
def _scratch_database(
    superuser_database_url: str,
) -> Iterator[DatabaseWrapper]:
    """Yield one owner-owned scratch database for real migration runs.

    The protected-field migrations are irreversible, so the populated case
    cannot run on the shared test database; a scratch database reproduces
    the real pre-migration schema exactly and is dropped afterwards.
    """
    name = f"mig_scratch_{uuid4().hex[:12]}"
    with psycopg.connect(superuser_database_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}" OWNER clinic_owner')
    try:
        with psycopg.connect(
            database_url_for_name(superuser_database_url, name),
            autocommit=True,
        ) as admin:
            admin.execute(
                "CREATE SCHEMA clinic_app AUTHORIZATION clinic_owner; "
                "REVOKE ALL ON SCHEMA clinic_app FROM PUBLIC, clinic_app, "
                "clinic_resolver; "
                "GRANT USAGE ON SCHEMA clinic_app TO clinic_owner, clinic_app; "
                "GRANT USAGE, CREATE ON SCHEMA clinic_app TO clinic_resolver; "
                "ALTER DEFAULT PRIVILEGES FOR ROLE clinic_owner "
                "IN SCHEMA clinic_app "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO clinic_app; "
                "ALTER DEFAULT PRIVILEGES FOR ROLE clinic_owner "
                "IN SCHEMA clinic_app GRANT USAGE ON SEQUENCES TO clinic_app"
            )
        settings_dict = connections["default"].settings_dict.copy()
        settings_dict["NAME"] = name
        wrapper = DatabaseWrapper(settings_dict, alias="migration_scratch")
        connections["migration_scratch"] = wrapper
        try:
            yield wrapper
        finally:
            wrapper.close()
            del connections["migration_scratch"]
    finally:
        with psycopg.connect(superuser_database_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@contextmanager
def _default_connection(
    wrapper: DatabaseWrapper,
) -> Iterator[DatabaseWrapper]:
    """Point the ``default`` alias at a scratch database.

    ``django.db.connection`` and ``transaction.atomic()`` resolve the
    ``default`` alias dynamically, so swapping the handler entry makes the
    ORM, ``MigrationExecutor(connection)`` and raw ``connection.cursor()``
    calls all run against the migrated scratch schema.
    """
    original = connections["default"]
    connections["default"] = wrapper
    try:
        yield wrapper
    finally:
        connections["default"] = original


def _scratch_tenant_key(wrapper: DatabaseWrapper, organization_id: UUID) -> None:
    """Issue the tenant DEK through the real owner-only key boundary."""
    kek = secret_store().get_secret("tenant-kek")
    with transaction.atomic(using=wrapper.alias), wrapper.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        cursor.execute("SELECT clinic_app.tenant_dek_issue(%s)", [kek])


def _seed_legacy_clinic(wrapper: DatabaseWrapper) -> None:  # noqa: PLR0915
    """Populate the real pre-migration schema through its own guards.

    Every insert and guarded transition runs against the historical models
    at ``prescription.0009``/``intake.0010`` state, so receipt events,
    revisions and bindings are produced by the real triggers — only the
    signature operation's terminal state is staged with its guard disabled,
    mirroring a completed historical signing lifecycle.
    """
    alias = wrapper.alias
    executor = MigrationExecutor(wrapper)
    state = executor.loader.project_state([("prescription", "0009_protected_fields")])
    historical = state.apps
    organization_model = historical.get_model("identity", "Organization")
    clinic_model = historical.get_model("identity", "Clinic")
    user_model = historical.get_model("identity", "User")
    role_model = historical.get_model("identity", "UserClinicRole")
    patient_model = historical.get_model("intake", "Patient")
    enrollment_model = historical.get_model("intake", "PatientClinicEnrollment")
    contact_model = historical.get_model("intake", "PatientContact")
    template_model = historical.get_model("intake", "QuestionnaireTemplate")
    response_model = historical.get_model("intake", "QuestionnaireResponse")
    grant_model = historical.get_model("intake", "PatientAccessGrant")
    session_model = historical.get_model("intake", "PatientSession")
    block_model = historical.get_model("scheduling", "AvailabilityBlock")
    appointment_model = historical.get_model("scheduling", "Appointment")
    encounter_model = historical.get_model("ehr", "Encounter")
    draft_model = historical.get_model("prescription", "PrescriptionDraft")
    document_model = historical.get_model("prescription", "PrescriptionDocument")
    operation_model = historical.get_model("prescription", "SignatureOperation")

    with transaction.atomic(using=alias), wrapper.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(LEGACY_ORG)],
        )
        organization = organization_model.objects.using(alias).create(
            id=LEGACY_ORG,
            name="Legacy Synthetic Organization",
            cnpj="00000000003001",
        )
        clinic = clinic_model.objects.using(alias).create(
            id=LEGACY_CLINIC,
            organization=organization,
            name="Legacy Clinic",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        physician = user_model.objects.using(alias).create(
            id=LEGACY_PHYSICIAN, username="legacy.physician"
        )
        role_model.objects.using(alias).create(
            organization=organization,
            clinic=clinic,
            user=physician,
            role="physician",
        )
        patient = patient_model.objects.using(alias).create(
            id=LEGACY_PATIENT,
            organization=organization,
            full_name=LEGACY_NAME,
            birth_date=LEGACY_BIRTH,
        )
        enrollment = enrollment_model.objects.using(alias).create(
            id=LEGACY_ENROLLMENT,
            organization=organization,
            clinic=clinic,
            patient=patient,
            idempotency_key=UUID(int=3101),
            create_fingerprint=b"l" * 32,
        )
        contact_model.objects.using(alias).create(
            id=LEGACY_CONTACT,
            organization=organization,
            patient=patient,
            channel="email",
            destination=LEGACY_DESTINATION,
        )
        template = template_model.objects.using(alias).create(
            id=LEGACY_TEMPLATE,
            organization=organization,
            clinic=clinic,
            key="legacy",
            version=1,
            title="Legacy intake",
            questions=[],
        )
        # A draft response with the normal empty reopen_reason: the receipt
        # trigger writes the 'assigned' event and the '' legacy value must
        # migrate to a NULL envelope without staying pending forever.
        response_model.objects.using(alias).create(
            id=LEGACY_DRAFT_RESPONSE,
            organization=organization,
            clinic=clinic,
            patient=patient,
            enrollment=enrollment,
            template=template,
        )
        grant = grant_model.objects.using(alias).create(
            id=LEGACY_GRANT,
            organization=organization,
            clinic=clinic,
            patient=patient,
            enrollment=enrollment,
            issued_by=physician,
            issued_by_label="legacy.physician",
            secret_hash=b"g" * 32,
            operations=list(PATIENT_OPERATION_VALUES),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        session_model.objects.using(alias).create(
            id=LEGACY_SESSION,
            organization=organization,
            clinic=clinic,
            patient=patient,
            enrollment=enrollment,
            grant=grant,
            operations=list(PATIENT_OPERATION_VALUES),
            expires_at=timezone.now() + timedelta(hours=1),
            idle_expires_at=timezone.now() + timedelta(minutes=30),
        )
        reopened = response_model.objects.using(alias).create(
            id=LEGACY_REOPENED_RESPONSE,
            organization=organization,
            clinic=clinic,
            patient=patient,
            enrollment=enrollment,
            template=template,
        )
        # Fixed future clinic-local times: the appointment guard requires a
        # covering availability block and one São Paulo calendar day.
        block_start = datetime(2035, 6, 2, 11, 0, tzinfo=UTC)
        block_model.objects.using(alias).create(
            id=LEGACY_BLOCK,
            organization=organization,
            clinic=clinic,
            practitioner=physician,
            start_at=block_start,
            end_at=block_start + timedelta(hours=4),
            idempotency_key=UUID(int=3102),
            create_fingerprint=b"m" * 32,
        )
        appointment = appointment_model.objects.using(alias).create(
            id=LEGACY_APPOINTMENT,
            organization=organization,
            clinic=clinic,
            patient=patient,
            practitioner=physician,
            start_at=block_start + timedelta(minutes=30),
            end_at=block_start + timedelta(minutes=60),
            idempotency_key=UUID(int=3103),
            create_fingerprint=b"n" * 32,
        )
        encounter = encounter_model.objects.using(alias).create(
            id=LEGACY_ENCOUNTER,
            organization=organization,
            clinic=clinic,
            appointment=appointment,
            patient=patient,
            physician=physician,
        )
        rx_draft = draft_model.objects.using(alias).create(
            id=LEGACY_RX_DRAFT,
            organization=organization,
            encounter=encounter,
            clinic=clinic,
            patient=patient,
            issuer=physician,
            category="synthetic_non_controlled",
            contract_version="synthetic-draft-v1",
        )
        frozen = dict(LEGACY_FROZEN)
        frozen["input_digest"] = hashlib.sha256(rfc8785.dumps(frozen)).hexdigest()
        document_model.objects.using(alias).create(
            id=LEGACY_DOCUMENT,
            organization=organization,
            draft=rx_draft,
            encounter=encounter,
            clinic=clinic,
            patient=patient,
            issuer=physician,
            document_version=1,
            state="rendered",
            render_params={"renderer": "legacy"},
            frozen_input=frozen,
            input_digest=frozen["input_digest"],
            pdf_digest=hashlib.sha256(LEGACY_PDF).hexdigest(),
            pdf_bytes=LEGACY_PDF,
            qr_handle="legacyhandle" + "0" * 24,
        )

    # The patient-side submission and the physician reopen run through the
    # real response guard, producing 'submitted' and 'reopened' receipts.
    with transaction.atomic(using=alias), wrapper.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true), "
            "pg_catalog.set_config('app.current_patient_session', %s, true)",
            [str(LEGACY_ORG), str(LEGACY_SESSION)],
        )
        reopened.answers = LEGACY_ANSWERS
        reopened.state = "submitted"
        reopened.revision = 2
        reopened.submitted_at = timezone.now()
        reopened.save(using=alias)
    with transaction.atomic(using=alias), wrapper.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true), "
            "pg_catalog.set_config('app.current_user_id', %s, true)",
            [str(LEGACY_ORG), str(LEGACY_PHYSICIAN)],
        )
        reopened.state = "draft"
        reopened.revision = 3
        reopened.submitted_at = None
        reopened.reopen_reason = LEGACY_REASON
        reopened.save(using=alias)

    # A completed historical signing operation cannot be inserted through
    # the guard (it only accepts 'draft'), so the terminal row is staged
    # with user triggers off and restored in a separate transaction.
    with transaction.atomic(using=alias), wrapper.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(LEGACY_ORG)],
        )
        cursor.execute(
            "ALTER TABLE clinic_app.prescription_signatureoperation "
            "DISABLE TRIGGER USER"
        )
        operation_model.objects.using(alias).create(
            id=LEGACY_OPERATION,
            organization=organization,
            document_id=LEGACY_DOCUMENT,
            encounter=encounter,
            clinic=clinic,
            patient=patient,
            issuer=physician,
            provider="synthetic-signature-v1",
            operation_id="legacy-operation-1",
            state="rehearsal_complete",
            content_digest=hashlib.sha256(LEGACY_PDF).hexdigest(),
            signer_subject="synthetic:legacy",
            signed_bytes=LEGACY_SIGNED,
            signed_digest=hashlib.sha256(LEGACY_SIGNED).hexdigest(),
            completed_at=timezone.now(),
        )
    with transaction.atomic(using=alias), wrapper.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE clinic_app.prescription_signatureoperation ENABLE TRIGGER USER"
        )


def _scratch_posture(wrapper: DatabaseWrapper, table: str) -> tuple[bool, bool, int]:
    with wrapper.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity "
            "FROM pg_catalog.pg_class "
            "WHERE oid = %s::pg_catalog.regclass",
            [f"clinic_app.{table}"],
        )
        security = cursor.fetchone()
        cursor.execute(
            "SELECT count(*) FROM pg_catalog.pg_trigger "
            "WHERE tgrelid = %s::pg_catalog.regclass "
            "AND NOT tgisinternal AND tgenabled <> 'O'",
            [f"clinic_app.{table}"],
        )
        disabled = cursor.fetchone()
    assert security is not None
    assert disabled is not None
    return (bool(security[0]), bool(security[1]), int(disabled[0]))


def _scratch_decrypt(
    wrapper: DatabaseWrapper,
    *,
    table: str,
    column: str,
    row_id: UUID,
    purpose: str,
) -> bytes | None:
    """Decrypt one envelope as the runtime role inside the tenant context."""
    kek = secret_store().get_secret("tenant-kek")
    with transaction.atomic(using=wrapper.alias), wrapper.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_app")
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true), "
            "pg_catalog.set_config('app.current_user_id', %s, true)",
            [str(LEGACY_ORG), str(LEGACY_PHYSICIAN)],
        )
        cursor.execute(
            f"SELECT {column} FROM clinic_app.{table} WHERE id = %s",  # noqa: S608
            [str(row_id)],
        )
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return None
        cursor.execute(
            "SELECT clinic_app.protected_decrypt(%s, %s, %s)",
            [kek, purpose, bytes(row[0])],
        )
        decrypted = cursor.fetchone()
    if decrypted is None or decrypted[0] is None:
        return None
    return bytes(decrypted[0])


@pytest.mark.django_db(transaction=True)
def test_populated_protected_migration_runs_on_real_schema(  # noqa: PLR0915
    superuser_database_url: str,
) -> None:
    """The real populated schema migrates, resumes and preserves semantics.

    A scratch database is migrated to the exact pre-migration state and
    populated through the real guards; the actual ``intake.0011`` and
    ``prescription.0010`` migrations are interrupted mid-backfill and
    resumed. Decrypted values, digests, receipt events and revisions must
    survive, empty-to-NULL encodings must seal, and the staging columns are
    dropped only after the seal passes.
    """
    with _scratch_database(superuser_database_url) as wrapper:
        executor = MigrationExecutor(wrapper)
        executor.migrate([("prescription", "0009_protected_fields")])
        _seed_legacy_clinic(wrapper)
        _scratch_tenant_key(wrapper, LEGACY_ORG)

        real_store = secret_store()
        calls = {"count": 0}

        class InterruptStore:
            def get_secret(self, name: str) -> str:
                calls["count"] += 1
                if calls["count"] == 2:
                    raise RuntimeError(PROBE_INTERRUPTION)
                return real_store.get_secret(name)

        with (
            patch(
                "apps.core.secrets.secret_store",
                return_value=InterruptStore(),
            ),
            pytest.raises(RuntimeError, match="interruption"),
        ):
            MigrationExecutor(wrapper).migrate([("intake", "0011_protected_fields")])

        # The interruption committed the first table pass only: the patient
        # envelope exists, the contact row is untouched, the migration is
        # unrecorded and every table kept its RLS/trigger posture.
        with wrapper.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, false)",
                [str(LEGACY_ORG)],
            )
            cursor.execute(
                "SELECT count(*) FROM django_migrations "
                "WHERE app = 'intake' AND name = '0011_protected_fields'"
            )
            assert cursor.fetchone() == (0,)
            cursor.execute(
                "SELECT full_name_legacy IS NOT NULL, full_name IS NOT NULL "
                "FROM clinic_app.intake_patient WHERE id = %s",
                [str(LEGACY_PATIENT)],
            )
            assert cursor.fetchone() == (True, True)
            cursor.execute(
                "SELECT destination_legacy, destination "
                "FROM clinic_app.intake_patientcontact WHERE id = %s",
                [str(LEGACY_CONTACT)],
            )
            assert cursor.fetchone() == (LEGACY_DESTINATION, None)
        for table in LEGACY_PATIENT_TABLES[:4]:
            assert _scratch_posture(wrapper, table) == (True, True, 0)

        MigrationExecutor(wrapper).migrate(
            [
                ("intake", "0011_protected_fields"),
                ("prescription", "0010_protected_documents"),
            ]
        )

        with wrapper.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, false)",
                [str(LEGACY_ORG)],
            )
            cursor.execute(
                "SELECT count(*) FROM django_migrations "
                "WHERE (app, name) IN ("
                "('intake', '0011_protected_fields'), "
                "('prescription', '0010_protected_documents'))"
            )
            assert cursor.fetchone() == (2,)
            cursor.execute(
                "SELECT count(*) FROM pg_catalog.pg_attribute "
                "JOIN pg_catalog.pg_class ON pg_class.oid = attrelid "
                "JOIN pg_catalog.pg_namespace ON pg_namespace.oid = relnamespace "
                "WHERE nspname = 'clinic_app' AND attname LIKE '%\\_legacy' "
                "AND attnum > 0 AND NOT attisdropped"
            )
            assert cursor.fetchone() == (0,)
            cursor.execute(
                "SELECT attnotnull FROM pg_catalog.pg_attribute "
                "WHERE attrelid = 'clinic_app.intake_patient'::pg_catalog.regclass "
                "AND attname = 'full_name'"
            )
            assert cursor.fetchone() == (True,)
            cursor.execute(
                "SELECT action, revision FROM clinic_app.intake_questionnaireevent "
                "WHERE response_id = %s ORDER BY revision",
                [str(LEGACY_REOPENED_RESPONSE)],
            )
            assert cursor.fetchall() == [
                ("assigned", 1),
                ("submitted", 2),
                ("reopened", 3),
            ]
        for table in LEGACY_PATIENT_TABLES:
            assert _scratch_posture(wrapper, table) == (True, True, 0)

        assert (
            _scratch_decrypt(
                wrapper,
                table="intake_patient",
                column="full_name",
                row_id=LEGACY_PATIENT,
                purpose="intake.patient.full_name",
            )
            == LEGACY_NAME.encode()
        )
        assert (
            _scratch_decrypt(
                wrapper,
                table="intake_patient",
                column="birth_date",
                row_id=LEGACY_PATIENT,
                purpose="intake.patient.birth_date",
            )
            == LEGACY_BIRTH.encode()
        )
        assert (
            _scratch_decrypt(
                wrapper,
                table="intake_patientcontact",
                column="destination",
                row_id=LEGACY_CONTACT,
                purpose="intake.patientcontact.destination",
            )
            == LEGACY_DESTINATION.encode()
        )
        # The draft's empty reopen_reason migrated to a NULL envelope and
        # sealed; its '{}' answers migrated as a JSON object, not a string.
        assert (
            _scratch_decrypt(
                wrapper,
                table="intake_questionnaireresponse",
                column="reopen_reason",
                row_id=LEGACY_DRAFT_RESPONSE,
                purpose="intake.questionnaireresponse.reopen_reason",
            )
            is None
        )
        draft_answers = _scratch_decrypt(
            wrapper,
            table="intake_questionnaireresponse",
            column="answers",
            row_id=LEGACY_DRAFT_RESPONSE,
            purpose="intake.questionnaireresponse.answers",
        )
        assert draft_answers is not None
        assert json.loads(draft_answers) == {}
        reopened_answers = _scratch_decrypt(
            wrapper,
            table="intake_questionnaireresponse",
            column="answers",
            row_id=LEGACY_REOPENED_RESPONSE,
            purpose="intake.questionnaireresponse.answers",
        )
        assert reopened_answers is not None
        assert json.loads(reopened_answers) == LEGACY_ANSWERS
        reopened_reason = _scratch_decrypt(
            wrapper,
            table="intake_questionnaireresponse",
            column="reopen_reason",
            row_id=LEGACY_REOPENED_RESPONSE,
            purpose="intake.questionnaireresponse.reopen_reason",
        )
        assert reopened_reason == LEGACY_REASON.encode()
        with wrapper.cursor() as cursor:
            cursor.execute(
                "SELECT answers, reason FROM clinic_app.intake_questionnaireevent "
                "WHERE response_id = %s AND action = 'reopened'",
                [str(LEGACY_REOPENED_RESPONSE)],
            )
            event_row = cursor.fetchone()
        assert event_row is not None
        event_answers, event_reason = event_row
        assert event_answers is not None
        assert event_reason is not None
        with transaction.atomic(using=wrapper.alias), wrapper.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_app")
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(LEGACY_ORG)],
            )
            kek = secret_store().get_secret("tenant-kek")
            cursor.execute(
                "SELECT clinic_app.protected_decrypt(%s, %s, %s), "
                "clinic_app.protected_decrypt(%s, %s, %s)",
                [
                    kek,
                    "intake.questionnaireresponse.answers",
                    bytes(event_answers),
                    kek,
                    "intake.questionnaireresponse.reopen_reason",
                    bytes(event_reason),
                ],
            )
            decrypted_event = cursor.fetchone()
        assert decrypted_event is not None
        assert json.loads(decrypted_event[0]) == LEGACY_ANSWERS
        assert bytes(decrypted_event[1]) == LEGACY_REASON.encode()

        assert (
            _scratch_decrypt(
                wrapper,
                table="prescription_prescriptiondocument",
                column="pdf_bytes",
                row_id=LEGACY_DOCUMENT,
                purpose="prescription.prescriptiondocument.pdf_bytes",
            )
            == LEGACY_PDF
        )
        frozen_plain = _scratch_decrypt(
            wrapper,
            table="prescription_prescriptiondocument",
            column="frozen_input",
            row_id=LEGACY_DOCUMENT,
            purpose="prescription.prescriptiondocument.frozen_input",
        )
        assert frozen_plain is not None
        assert json.loads(frozen_plain)["clinic_label"] == "Legacy Clinic"
        assert (
            _scratch_decrypt(
                wrapper,
                table="prescription_signatureoperation",
                column="signed_bytes",
                row_id=LEGACY_OPERATION,
                purpose="prescription.signatureoperation.signed_bytes",
            )
            == LEGACY_SIGNED
        )

        # The recreated overview resolver decrypts the patient name inside
        # the session-validating boundary for the runtime role.
        with transaction.atomic(using=wrapper.alias), wrapper.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_app")
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_patient_session', %s, true)",
                [str(LEGACY_SESSION)],
            )
            cursor.execute(
                "SELECT patient_name, clinic_name "
                "FROM clinic_app.patient_session_overview(%s)",
                [secret_store().get_secret("tenant-kek")],
            )
            overview_row = cursor.fetchone()
        assert overview_row == (LEGACY_NAME, "Legacy Clinic")
