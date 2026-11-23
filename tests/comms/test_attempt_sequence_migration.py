"""Attempts that predate the attempt sequence are numbered once, closed-first."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from patients.test_intake_migrations import _default_connection, _scratch_database

if TYPE_CHECKING:
    from django.db.backends.postgresql.base import DatabaseWrapper
    from django.db.backends.utils import CursorWrapper

pytestmark = pytest.mark.django_db(transaction=True)

ORG = UUID(int=7001)
CLINIC = UUID(int=7101)
ACTOR = UUID(int=7201)
# Each message's attempts are listed in creation order; the second attempt's
# worker clock ran behind the first one's.
SKEWED = UUID(int=7301)  # failed, then accepted successor stamped earlier
REVERSED = UUID(int=7302)  # accepted, then a failure stamped later
FAILED_ONLY = UUID(int=7303)
REMINDER = UUID(int=7304)
T0 = datetime(2026, 11, 1, 12, tzinfo=UTC)
LEGACY = [
    ("a1", SKEWED, "comms.message", "failed", T0),
    ("a2", SKEWED, "comms.message", "succeeded", T0 - timedelta(seconds=10)),
    ("b1", REVERSED, "comms.message", "succeeded", T0),
    ("b2", REVERSED, "comms.message", "failed", T0 + timedelta(seconds=5)),
    ("c1", FAILED_ONLY, "comms.message", "failed", T0),
    ("r1", REMINDER, "scheduling.appointment_reminder", "pending", T0),
]


def _targets(executor: MigrationExecutor) -> list[tuple[str, str]]:
    before = {"comms": "0012_attempt_delivery", "prescription": "0016_dispatch_custody"}
    return [
        (app, before.get(app, name)) for app, name in executor.loader.graph.leaf_nodes()
    ]


def _tenant(cursor: CursorWrapper) -> None:
    cursor.execute(
        "SELECT pg_catalog.set_config('app.current_tenant', %s, true)", [str(ORG)]
    )


def _seed(wrapper: DatabaseWrapper, executor: MigrationExecutor) -> dict[str, UUID]:
    """Write pre-sequence attempts through the historical models and guards."""
    historical = executor.loader.project_state(_targets(executor)).apps
    organization = historical.get_model("identity", "Organization")
    clinic = historical.get_model("identity", "Clinic")
    user = historical.get_model("identity", "User")
    operation = historical.get_model("comms", "IntegrationOperation")
    ids: dict[str, UUID] = {}
    with transaction.atomic(using=wrapper.alias), wrapper.cursor() as cursor:
        _tenant(cursor)
        org = organization.objects.using(wrapper.alias).create(
            id=ORG, name="Synthetic Sequence Org", cnpj="00000000007001"
        )
        site = clinic.objects.using(wrapper.alias).create(
            id=CLINIC,
            organization=org,
            name="Synthetic Sequence Clinic",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        actor = user.objects.using(wrapper.alias).create(
            id=ACTOR, username="synthetic.sequence"
        )
        for number, (label, subject, subject_type, status, stamped) in enumerate(
            LEGACY, start=1
        ):
            row = operation.objects.using(wrapper.alias).create(
                organization=org,
                clinic=site,
                actor=actor,
                channel="whatsapp",
                provider="synthetic-whatsapp",
                subject_type=subject_type,
                subject_id=subject,
                idempotency_key=UUID(int=7400 + number),
            )
            # Stamp the clock before the terminal guard freezes the row.
            operation.objects.using(wrapper.alias).filter(pk=row.pk).update(
                created_at=stamped
            )
            operation.objects.using(wrapper.alias).filter(pk=row.pk).update(
                status=status
            )
            ids[label] = row.pk
    return ids


def _attempt(subject: UUID, key: int) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        _tenant(cursor)
        cursor.execute(
            "INSERT INTO clinic_app.comms_integrationoperation (id, organization_id,"
            " clinic_id, actor_id, kind, channel, payload_digest, provider,"
            " subject_type, subject_id, status, attempt_count, max_attempts,"
            " last_error, idempotency_key, created_at, updated_at)"
            " VALUES (gen_random_uuid(), %s, %s, %s, 'communication', 'whatsapp', '',"
            " 'synthetic-whatsapp', 'comms.message', %s, 'pending', 0, 5, '', %s,"
            " statement_timestamp(), statement_timestamp())",
            [str(ORG), str(CLINIC), str(ACTOR), str(subject), str(UUID(int=key))],
        )


def test_pre_sequence_attempts_are_numbered_closed_first_and_gated(
    superuser_database_url: str,
) -> None:
    with (
        _scratch_database(superuser_database_url) as wrapper,
        _default_connection(wrapper),
    ):
        executor = MigrationExecutor(wrapper)
        executor.migrate(_targets(executor))
        ids = _seed(wrapper, executor)
        executor = MigrationExecutor(wrapper)
        executor.migrate(executor.loader.graph.leaf_nodes())
        with transaction.atomic(), connection.cursor() as cursor:
            _tenant(cursor)
            cursor.execute(
                "SELECT id, attempt_sequence FROM clinic_app.comms_integrationoperation"
            )
            numbered = dict(cursor.fetchall())
            cursor.execute(
                "SELECT relrowsecurity, relforcerowsecurity,"
                " (SELECT count(*) FROM pg_catalog.pg_trigger t"
                "   WHERE t.tgrelid = c.oid AND t.tgenabled = 'D')"
                " FROM pg_catalog.pg_class c"
                " WHERE c.oid = 'clinic_app.comms_integrationoperation'::regclass"
            )
            posture = cursor.fetchone()
        assert {label: numbered[pk] for label, pk in ids.items()} == {
            "a1": 1,
            "a2": 2,
            "b2": 1,
            "b1": 2,
            "c1": 1,
            "r1": None,
        }
        assert posture == (True, True, 0)
        # The possibly accepted attempt stays current: no successor is admitted.
        for subject, key in ((SKEWED, 7501), (REVERSED, 7502)):
            with pytest.raises(IntegrityError) as refused:
                _attempt(subject, key)
            diag = getattr(refused.value.__cause__, "diag", None)
            assert getattr(diag, "constraint_name", None) == (
                "comms_message_attempt_open_check"
            )
        _attempt(FAILED_ONLY, 7503)
        with transaction.atomic(), connection.cursor() as cursor:
            _tenant(cursor)
            cursor.execute(
                "SELECT attempt_sequence FROM clinic_app.comms_integrationoperation"
                " WHERE subject_id = %s ORDER BY attempt_sequence",
                [str(FAILED_ONLY)],
            )
            assert cursor.fetchall() == [(1,), (2,)]
