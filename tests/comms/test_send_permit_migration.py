"""Comms 0015 on a real scratch database at the exact pre-permit schema.

The permit backfill is irreversible, so it cannot run on the shared test
database: a scratch database is migrated to ``comms.0014``, populated through
that schema's own attempt guard, and then migrated forward for real.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from apps.comms.migrations._send_permit_sql import BACKFILL_SQL
from django.db import DatabaseError, transaction
from django.db.backends.postgresql.base import DatabaseWrapper
from django.db.migrations.executor import MigrationExecutor

from patients.test_intake_migrations import (
    _default_connection,
    _scratch_database,
    _scratch_tenant_key,
)

ORG = UUID("00000000-0000-4000-8000-00000000f701")
STAFF = UUID("00000000-0000-4000-8000-00000000f703")
CLINIC = UUID("00000000-0000-4000-8000-00000000f702")
BEFORE = [("comms", "0014_attempt_guard")]
NOW = datetime(2026, 11, 24, tzinfo=UTC)


def _tenant(wrapper: DatabaseWrapper) -> None:
    with wrapper.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true),"
            " pg_catalog.set_config('app.current_user_id', %s, true)",
            [str(ORG), str(STAFF)],
        )


def _seed(wrapper: DatabaseWrapper) -> dict[str, UUID]:
    """Write a retried email message and a single open attempt at 0014."""
    alias = wrapper.alias
    historical = MigrationExecutor(wrapper).loader.project_state(BEFORE).apps
    organization = historical.get_model("identity", "Organization")
    clinic = historical.get_model("identity", "Clinic")
    conversation = historical.get_model("comms", "Conversation")
    message = historical.get_model("comms", "Message")
    operation = historical.get_model("comms", "IntegrationOperation")
    principal = historical.get_model("identity", "ServicePrincipal")
    account = historical.get_model("comms", "ProviderAccount")
    ids: dict[str, UUID] = {}
    with transaction.atomic(using=alias):
        _tenant(wrapper)
        organization.objects.using(alias).create(
            id=ORG, name="Permit Synthetic Organization", cnpj="00000000007001"
        )
        clinic.objects.using(alias).create(
            id=CLINIC,
            organization_id=ORG,
            name="Permit Clinic",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        historical.get_model("identity", "User").objects.using(alias).create(
            id=STAFF, username="permit.staff"
        )
        historical.get_model("identity", "UserClinicRole").objects.using(alias).create(
            organization_id=ORG, clinic_id=CLINIC, user_id=STAFF, role="clinic_admin"
        )
    _scratch_tenant_key(wrapper, ORG)
    with transaction.atomic(using=alias):
        _tenant(wrapper)
        inbox = account.objects.using(alias).create(
            organization_id=ORG,
            clinic_id=CLINIC,
            channel="email",
            provider="inbox-email-v1",
            external_account_id="permit-caixa-1",
            principal=principal.objects.using(alias).create(
                organization_id=ORG,
                clinic_id=CLINIC,
                name=f"inbox-{uuid4().hex}",
                db_identity=f"clinic_agent_msg_{uuid4().hex}",
                purpose="messaging_ingest",
            ),
            signing_key_digest="0" * 64,
        )
        thread = conversation.objects.using(alias).create(
            organization_id=ORG,
            clinic_id=CLINIC,
            provider_account=inbox,
            channel="email",
            purpose="transactional",
            contact_fingerprint="0" * 64,
            contact_address="paciente.permit@example.invalid",
            contact_label="",
            last_activity_at=NOW,
        )
        for name in ("retried", "single"):
            command = uuid4()
            ids[name] = (
                message.objects.using(alias)
                .create(
                    organization_id=ORG,
                    conversation=thread,
                    direction="out",
                    body="Mensagem sintetica",
                    template_parameters="",
                    command_id=command,
                    sent_by_id=STAFF,
                    created_at=NOW,
                )
                .pk
            )
            first = operation.objects.using(alias).create(
                organization_id=ORG,
                clinic_id=CLINIC,
                kind="communication",
                channel="email",
                provider="inbox-email-v1",
                actor_id=STAFF,
                subject_type="comms.message",
                subject_id=ids[name],
                idempotency_key=command,
            )
            operation.objects.using(alias).filter(pk=first.pk).update(
                status="succeeded", provider_reference=f"ref-{name}-a"
            )
            ids[f"{name}_a"] = first.pk
        # The base projected the original's failure onto the message, then
        # admitted the retry, which the provider accepted.
        message.objects.using(alias).filter(pk=ids["retried"]).update(
            provider_state="failed"
        )
        retry = operation.objects.using(alias).create(
            organization_id=ORG,
            clinic_id=CLINIC,
            kind="communication",
            channel="email",
            provider="inbox-email-v1",
            actor_id=STAFF,
            subject_type="comms.message",
            subject_id=ids["retried"],
            idempotency_key=uuid4(),
        )
        operation.objects.using(alias).filter(pk=retry.pk).update(
            status="succeeded", provider_reference="ref-retried-b"
        )
        message.objects.using(alias).filter(pk=ids["retried"]).update(provider_state="")
        ids["retried_b"] = retry.pk
    return ids


CLAIM_SQL = (
    "UPDATE clinic_app.comms_integrationoperation SET send_permit = 'held'"
    " WHERE id = %s"
)
SUCCESSOR_SQL = """
INSERT INTO clinic_app.comms_integrationoperation (id, organization_id,
 clinic_id, actor_id, kind, channel, provider, subject_type, subject_id,
 idempotency_key, status, attempt_count, max_attempts, payload_digest,
 last_error, last_callback_event_id, created_at, updated_at)
SELECT %s, organization_id, clinic_id, actor_id, kind, channel, provider,
 subject_type, subject_id, %s, 'pending', 0, max_attempts, payload_digest, '',
 last_callback_event_id, now(), now()
  FROM clinic_app.comms_integrationoperation WHERE id = %s
"""


def _execute(wrapper: DatabaseWrapper, sql: str, params: list[UUID]) -> None:
    with transaction.atomic(using=wrapper.alias), wrapper.cursor() as cursor:
        _tenant(wrapper)
        cursor.execute(sql, params)


def _permits(wrapper: DatabaseWrapper) -> dict[UUID, str | None]:
    with transaction.atomic(using=wrapper.alias), wrapper.cursor() as cursor:
        _tenant(wrapper)
        cursor.execute(
            "SELECT id, send_permit FROM clinic_app.comms_integrationoperation"
        )
        return dict(cursor.fetchall())


@pytest.mark.django_db(transaction=True)
def test_permit_backfill_runs_on_the_real_pre_permit_schema(
    superuser_database_url: str,
) -> None:
    with (
        _scratch_database(superuser_database_url) as scratch,
        _default_connection(scratch) as wrapper,
    ):
        executor = MigrationExecutor(wrapper)
        executor.migrate(
            [leaf for leaf in executor.loader.graph.leaf_nodes() if leaf[0] != "comms"]
            + BEFORE
        )
        ids = _seed(wrapper)
        MigrationExecutor(wrapper).migrate([("comms", "0015_send_permit")])
        permits = _permits(wrapper)
        # The superseded original is released by the base's own retry; the
        # accepted retry and the single accepted attempt hold their permits.
        assert permits == {
            ids["retried_a"]: "released",
            ids["retried_b"]: "held",
            ids["single_a"]: "held",
        }
        # The pass resumes unchanged.
        with transaction.atomic(using=wrapper.alias), wrapper.cursor() as cursor:
            _tenant(wrapper)
            cursor.execute(BACKFILL_SQL)
        assert _permits(wrapper) == permits
        # One held permit per message, enforced by the database itself.
        with pytest.raises(DatabaseError, match="comms_operation_send_permit_uniq"):
            _execute(wrapper, CLAIM_SQL, [ids["retried_a"]])
        # The accepted retry is open: no further attempt is admitted.
        with pytest.raises(DatabaseError, match="no negative outcome"):
            _execute(wrapper, SUCCESSOR_SQL, [uuid4(), uuid4(), ids["retried_b"]])
