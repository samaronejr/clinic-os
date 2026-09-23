"""Authenticated payment-event history and stored-scope reconciliation.

Adds the recorded actor to each PIX operation so provider-reconciled
settlements keep their original billing authority, and creates the
append-only payment event table whose inserts are validated by trigger
against the stored operation/invoice facts.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models

if TYPE_CHECKING:
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.state import StateApps


def finish_foreign_keys(
    _apps: StateApps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    """Validate deferred FKs as owner, restoring FORCE within this transaction."""
    for statement in schema_editor.deferred_sql:
        schema_editor.execute(statement)
    schema_editor.deferred_sql.clear()
    for table in (
        "billing_invoice",
        "billing_invoicerevision",
        "billing_pixoperation",
        "billing_settlement",
        "billing_paymentevent",
    ):
        schema_editor.execute(
            f"ALTER TABLE clinic_app.{table} FORCE ROW LEVEL SECURITY"
        )


# Owner-side backfill: FORCE RLS and the immutable-operation guard are lifted
# only inside this atomic migration so existing synthetic operations inherit
# the actor recorded on their invoice's first revision snapshot.
ACTOR_SQL = """
ALTER TABLE clinic_app.billing_pixoperation NO FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.billing_invoice NO FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.billing_invoicerevision NO FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.billing_settlement NO FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.billing_pixoperation DISABLE TRIGGER billing_pix_guard;
UPDATE clinic_app.billing_pixoperation o
   SET actor_id = r.actor_id
  FROM clinic_app.billing_invoicerevision r
 WHERE r.invoice_id = o.invoice_id AND r.revision = 1
   AND o.actor_id IS NULL;
ALTER TABLE clinic_app.billing_pixoperation
    ALTER COLUMN actor_id SET NOT NULL;
ALTER TABLE clinic_app.billing_pixoperation ENABLE TRIGGER billing_pix_guard;
"""

REVERSE_ACTOR_SQL = """
ALTER TABLE clinic_app.billing_pixoperation FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.billing_invoice FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.billing_invoicerevision FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.billing_settlement FORCE ROW LEVEL SECURITY;
"""

# The operation guard gains the recorded-actor check: an operation row must
# carry the actor bound to the transaction-local user GUC at insert time.
PIX_GUARD_SQL = """
CREATE OR REPLACE FUNCTION clinic_app.billing_pix_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 invoice clinic_app.billing_invoice;
 predecessor clinic_app.billing_pixoperation;
 operation clinic_app.billing_pixoperation;
BEGIN
 IF TG_OP <> 'INSERT' THEN
   RAISE EXCEPTION 'immutable PIX history' USING ERRCODE = '23514';
 END IF;
 SELECT * INTO invoice FROM clinic_app.billing_invoice
 WHERE id = NEW.invoice_id FOR UPDATE;
 IF invoice.id IS NULL OR invoice.organization_id <> NEW.organization_id
    OR invoice.state <> 'open' THEN
   RAISE EXCEPTION 'invalid PIX invoice' USING ERRCODE = '23514';
 END IF;
 IF TG_TABLE_NAME = 'billing_pixoperation' THEN
   IF ROW(NEW.amount_minor, NEW.currency, NEW.invoice_reference)
      IS DISTINCT FROM ROW(invoice.amount_minor, invoice.currency,
                           invoice.reference)
      OR NEW.actor_id IS DISTINCT FROM
         NULLIF(current_setting('app.current_user_id', true), '')::uuid THEN
     RAISE EXCEPTION 'invalid PIX terms' USING ERRCODE = '23514';
   END IF;
   IF NEW.previous_id IS NOT NULL THEN
     SELECT * INTO predecessor FROM clinic_app.billing_pixoperation
       WHERE id = NEW.previous_id;
     IF predecessor.id IS NULL OR predecessor.invoice_id <> invoice.id
        OR predecessor.organization_id <> NEW.organization_id
        OR predecessor.expires_at > statement_timestamp() THEN
       RAISE EXCEPTION 'invalid PIX regeneration' USING ERRCODE = '23514';
     END IF;
   END IF;
 ELSE
   SELECT * INTO operation FROM clinic_app.billing_pixoperation
     WHERE id = NEW.operation_id;
   IF operation.id IS NULL OR operation.invoice_id <> invoice.id
      OR operation.organization_id <> NEW.organization_id
      OR NEW.provider_reference <> 'synthetic-pix-' || operation.id::text
      OR NEW.copy_code NOT LIKE 'SYNTHETIC-NOT-PAYABLE|%'
      OR length(NEW.qr_base64) NOT BETWEEN 1 AND 65536 THEN
     RAISE EXCEPTION 'invalid synthetic PIX result' USING ERRCODE = '23514';
   END IF;
 END IF;
 RETURN NEW;
END
$f$;
"""

REVERSE_PIX_GUARD_SQL = """
CREATE OR REPLACE FUNCTION clinic_app.billing_pix_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 invoice clinic_app.billing_invoice;
 predecessor clinic_app.billing_pixoperation;
 operation clinic_app.billing_pixoperation;
BEGIN
 IF TG_OP <> 'INSERT' THEN
   RAISE EXCEPTION 'immutable PIX history' USING ERRCODE = '23514';
 END IF;
 SELECT * INTO invoice FROM clinic_app.billing_invoice
 WHERE id = NEW.invoice_id FOR UPDATE;
 IF invoice.id IS NULL OR invoice.organization_id <> NEW.organization_id
    OR invoice.state <> 'open' THEN
   RAISE EXCEPTION 'invalid PIX invoice' USING ERRCODE = '23514';
 END IF;
 IF TG_TABLE_NAME = 'billing_pixoperation' THEN
   IF ROW(NEW.amount_minor, NEW.currency, NEW.invoice_reference)
      IS DISTINCT FROM ROW(invoice.amount_minor, invoice.currency,
                           invoice.reference) THEN
     RAISE EXCEPTION 'invalid PIX terms' USING ERRCODE = '23514';
   END IF;
   IF NEW.previous_id IS NOT NULL THEN
     SELECT * INTO predecessor FROM clinic_app.billing_pixoperation
       WHERE id = NEW.previous_id;
     IF predecessor.id IS NULL OR predecessor.invoice_id <> invoice.id
        OR predecessor.organization_id <> NEW.organization_id
        OR predecessor.expires_at > statement_timestamp() THEN
       RAISE EXCEPTION 'invalid PIX regeneration' USING ERRCODE = '23514';
     END IF;
   END IF;
 ELSE
   SELECT * INTO operation FROM clinic_app.billing_pixoperation
     WHERE id = NEW.operation_id;
   IF operation.id IS NULL OR operation.invoice_id <> invoice.id
      OR operation.organization_id <> NEW.organization_id
      OR NEW.provider_reference <> 'synthetic-pix-' || operation.id::text
      OR NEW.copy_code NOT LIKE 'SYNTHETIC-NOT-PAYABLE|%'
      OR length(NEW.qr_base64) NOT BETWEEN 1 AND 65536 THEN
     RAISE EXCEPTION 'invalid synthetic PIX result' USING ERRCODE = '23514';
   END IF;
 END IF;
 RETURN NEW;
END
$f$;
"""

# Resolver-owned functions: scope/facts resolution and deduplication never
# read tenant claims from the event; they read only stored charge rows.
RESOLVER_SQL = """
GRANT SELECT ON clinic_app.billing_pixoperation, clinic_app.billing_pixcharge
 TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.billing_payment_event_scope(
 requested_provider pg_catalog.text, requested_reference pg_catalog.text)
RETURNS TABLE(operation_id uuid, invoice_id uuid, organization_id uuid,
 clinic_id uuid, actor_id uuid, invoice_state varchar,
 invoice_amount_minor bigint, invoice_currency varchar,
 operation_amount_minor bigint, operation_currency varchar,
 invoice_reference uuid, expires_at timestamptz,
 settled_amount_minor bigint, settled_currency varchar)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT o.id, i.id, o.organization_id, i.clinic_id, o.actor_id, i.state,
   i.amount_minor, i.currency, o.amount_minor, o.currency,
   o.invoice_reference, o.expires_at, s.amount_minor, s.currency
 FROM clinic_app.billing_pixcharge c
 JOIN clinic_app.billing_pixoperation o ON o.id = c.operation_id
 JOIN clinic_app.billing_invoice i ON i.id = o.invoice_id
 LEFT JOIN clinic_app.billing_settlement s ON s.invoice_id = i.id
 WHERE o.provider = requested_provider
   AND c.provider_reference = requested_reference
$f$;
CREATE FUNCTION clinic_app.billing_payment_event_seen(
 requested_provider pg_catalog.text, requested_event_id pg_catalog.text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.billing_paymentevent e
   WHERE e.provider = requested_provider AND e.event_id = requested_event_id)
$f$;
CREATE FUNCTION clinic_app.billing_payment_event_recorder(
 requested_operation uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.billing_pixoperation o
   WHERE o.id = requested_operation
   AND o.organization_id =
     NULLIF(current_setting('app.current_tenant', true), '')::uuid
   AND (o.actor_id =
     NULLIF(current_setting('app.current_user_id', true), '')::uuid
     OR clinic_app.billing_staff_invoice(o.invoice_id)))
$f$;
CREATE FUNCTION clinic_app.billing_payment_event_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 operation clinic_app.billing_pixoperation;
BEGIN
 IF TG_OP <> 'INSERT' THEN
   RAISE EXCEPTION 'immutable payment event history' USING ERRCODE = '23514';
 END IF;
 SELECT * INTO operation FROM clinic_app.billing_pixoperation
   WHERE id = NEW.operation_id;
 IF operation.id IS NULL
    OR operation.organization_id <> NEW.organization_id
    OR operation.invoice_id <> NEW.invoice_id
    OR operation.provider <> NEW.provider
    OR operation.actor_id IS DISTINCT FROM NEW.actor_id
    OR NEW.reported_status NOT IN
       ('pending', 'settled', 'expired', 'cancelled', 'reversed')
    OR NEW.reported_amount_minor <= 0
    OR (NEW.authoritative_status IS NOT NULL AND NEW.authoritative_status
        NOT IN ('pending', 'settled', 'expired', 'cancelled', 'reversed'))
    OR (NEW.authoritative_status IS NULL AND (
        NEW.authoritative_amount_minor IS NOT NULL
        OR NEW.authoritative_currency IS NOT NULL))
    OR (NEW.authoritative_status IS NOT NULL AND (
        NEW.authoritative_amount_minor IS NULL
        OR NEW.authoritative_amount_minor <= 0
        OR NEW.authoritative_currency IS NULL))
    OR (NEW.resolution = 'settled' AND (
        NEW.reason_code IS NOT NULL OR NEW.settlement_id IS NULL
        OR NEW.reported_status <> 'settled'
        OR NEW.authoritative_status IS DISTINCT FROM 'settled'
        OR NOT EXISTS (SELECT 1 FROM clinic_app.billing_settlement s
          WHERE s.id = NEW.settlement_id AND s.invoice_id = NEW.invoice_id
            AND s.organization_id = NEW.organization_id
            AND s.confirmation_reference = NEW.id)))
    OR (NEW.resolution IN ('recorded', 'operator_required') AND (
        NEW.reason_code IS NULL OR NEW.settlement_id IS NOT NULL))
    OR NEW.resolution NOT IN ('settled', 'recorded', 'operator_required')
 THEN
   RAISE EXCEPTION 'invalid payment event' USING ERRCODE = '23514';
 END IF;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.billing_payment_event_scope(text, text),
 clinic_app.billing_payment_event_seen(text, text),
 clinic_app.billing_payment_event_recorder(uuid),
 clinic_app.billing_payment_event_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.billing_payment_event_scope(text, text),
 clinic_app.billing_payment_event_seen(text, text),
 clinic_app.billing_payment_event_recorder(uuid) TO clinic_app;
RESET ROLE;
"""

REVERSE_RESOLVER_SQL = """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.billing_payment_event_scope(text, text);
DROP FUNCTION clinic_app.billing_payment_event_seen(text, text);
DROP FUNCTION clinic_app.billing_payment_event_recorder(uuid);
DROP FUNCTION clinic_app.billing_payment_event_guard();
RESET ROLE;
REVOKE SELECT ON clinic_app.billing_pixoperation, clinic_app.billing_pixcharge
 FROM clinic_resolver;
"""

# The resolver-owned event guard validates every insert against stored facts
# and rejects update/delete outright, keeping history append-only even when
# the recorded actor no longer holds a billing role.
EVENT_SQL = """
GRANT SELECT ON clinic_app.billing_paymentevent TO clinic_resolver;
REVOKE ALL ON clinic_app.billing_paymentevent FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.billing_paymentevent TO clinic_app;
-- FORCE is restored by finish_foreign_keys after deferred FK validation.
ALTER TABLE clinic_app.billing_paymentevent ENABLE ROW LEVEL SECURITY;
CREATE POLICY billing_staff ON clinic_app.billing_paymentevent
 FOR SELECT
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
   AND clinic_app.billing_staff_invoice(invoice_id));
CREATE POLICY billing_event_recorder ON clinic_app.billing_paymentevent
 FOR INSERT
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
   AND clinic_app.billing_payment_event_recorder(operation_id));
SET LOCAL ROLE clinic_resolver;
GRANT EXECUTE ON FUNCTION clinic_app.billing_payment_event_guard()
 TO clinic_owner;
RESET ROLE;
CREATE TRIGGER billing_payment_event_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.billing_paymentevent FOR EACH ROW
 EXECUTE FUNCTION clinic_app.billing_payment_event_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.billing_payment_event_guard()
 FROM clinic_owner;
RESET ROLE;
"""

REVERSE_EVENT_SQL = """
DROP TRIGGER billing_payment_event_guard ON clinic_app.billing_paymentevent;
DROP POLICY billing_event_recorder ON clinic_app.billing_paymentevent;
DROP POLICY billing_staff ON clinic_app.billing_paymentevent;
ALTER TABLE clinic_app.billing_paymentevent NO FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.billing_paymentevent DISABLE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.billing_paymentevent FROM clinic_app, clinic_resolver;
"""


class Migration(migrations.Migration):
    """Record actors on operations and add append-only payment events."""

    dependencies: ClassVar = [("billing", "0004_pix_policy")]

    operations: ClassVar = [
        migrations.AddField(
            model_name="pixoperation",
            name="actor_id",
            field=models.UUIDField(null=True),
        ),
        migrations.RunSQL(ACTOR_SQL, REVERSE_ACTOR_SQL),
        migrations.AlterField(
            model_name="pixoperation",
            name="actor_id",
            field=models.UUIDField(),
        ),
        migrations.RunSQL(PIX_GUARD_SQL, REVERSE_PIX_GUARD_SQL),
        migrations.CreateModel(
            name="PaymentEvent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("provider", models.CharField(max_length=64)),
                ("event_id", models.CharField(max_length=255)),
                ("provider_reference", models.CharField(max_length=128)),
                ("actor_id", models.UUIDField()),
                (
                    "reported_status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("settled", "Settled"),
                            ("expired", "Expired"),
                            ("cancelled", "Cancelled"),
                            ("reversed", "Reversed"),
                        ],
                        max_length=16,
                    ),
                ),
                ("reported_amount_minor", models.PositiveBigIntegerField()),
                ("reported_currency", models.CharField(max_length=3)),
                (
                    "authoritative_status",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("pending", "Pending"),
                            ("settled", "Settled"),
                            ("expired", "Expired"),
                            ("cancelled", "Cancelled"),
                            ("reversed", "Reversed"),
                        ],
                        max_length=16,
                        null=True,
                    ),
                ),
                (
                    "authoritative_amount_minor",
                    models.PositiveBigIntegerField(blank=True, null=True),
                ),
                (
                    "authoritative_currency",
                    models.CharField(blank=True, max_length=3, null=True),
                ),
                (
                    "resolution",
                    models.CharField(
                        choices=[
                            ("settled", "Settled"),
                            ("recorded", "Recorded"),
                            ("operator_required", "Operator required"),
                        ],
                        max_length=24,
                    ),
                ),
                (
                    "reason_code",
                    models.CharField(blank=True, max_length=64, null=True),
                ),
                (
                    "received_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                (
                    "invoice",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="billing.invoice",
                    ),
                ),
                (
                    "operation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="billing.pixoperation",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "settlement",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="billing.settlement",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="paymentevent",
            constraint=models.UniqueConstraint(
                fields=("provider", "event_id"),
                name="billing_payment_event_dedupe",
            ),
        ),
        migrations.AddConstraint(
            model_name="paymentevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    resolution="settled",
                    reason_code__isnull=True,
                    settlement__isnull=False,
                )
                | models.Q(
                    resolution__in=("recorded", "operator_required"),
                    reason_code__isnull=False,
                    settlement__isnull=True,
                ),
                name="billing_payment_event_resolution",
            ),
        ),
        migrations.RunSQL(RESOLVER_SQL, REVERSE_RESOLVER_SQL),
        migrations.RunSQL(EVENT_SQL, REVERSE_EVENT_SQL),
        migrations.RunPython(finish_foreign_keys, migrations.RunPython.noop),
    ]
