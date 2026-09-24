"""Supersession and recovery predicates for authoritative payment lookups.

Adds resolver-owned functions reporting whether a newer recorded event
contradicts an authoritative lookup answer captured before the per-operation
lock was held (status, amount and currency all count as terms), so a stale
settled observation cannot issue a receipt after a newer committed reversal
or overpayment, and whether an authoritative observation was already
recorded or settled for an operation, so a superseded recovery attempt never
permanently consumes the recovered facts.
"""

from __future__ import annotations

from typing import ClassVar

from django.db import migrations

# Resolver-owned like the other event functions: SECURITY DEFINER plus the
# resolver's BYPASSRLS keeps the check correct even when the recorded actor
# no longer passes the staff SELECT policy.
SUPERSEDED_SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.billing_payment_event_superseded(
 requested_operation uuid, observed_at timestamptz,
 observed_status pg_catalog.text, observed_amount bigint,
 observed_currency pg_catalog.varchar)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.billing_paymentevent e
   WHERE e.operation_id = requested_operation
   AND e.received_at >= observed_at
   AND e.authoritative_status IS NOT NULL
   AND (e.authoritative_status <> observed_status
     OR e.authoritative_amount_minor <> observed_amount
     OR e.authoritative_currency <> observed_currency))
$f$;
CREATE FUNCTION clinic_app.billing_payment_event_recovered(
 requested_provider pg_catalog.text, requested_operation uuid,
 requested_event_id pg_catalog.text, observed_status pg_catalog.text,
 observed_amount bigint, observed_currency pg_catalog.varchar)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.billing_paymentevent e
   WHERE e.provider = requested_provider
   AND (e.event_id = requested_event_id
     OR (e.operation_id = requested_operation
       AND e.resolution <> 'operator_required'
       AND e.authoritative_status = observed_status
       AND e.authoritative_amount_minor = observed_amount
       AND e.authoritative_currency = observed_currency)))
$f$;
REVOKE ALL ON FUNCTION clinic_app.billing_payment_event_superseded(
 uuid, timestamptz, text, bigint, varchar),
 clinic_app.billing_payment_event_recovered(
 text, uuid, text, text, bigint, varchar) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.billing_payment_event_superseded(
 uuid, timestamptz, text, bigint, varchar),
 clinic_app.billing_payment_event_recovered(
 text, uuid, text, text, bigint, varchar) TO clinic_app;
RESET ROLE;
"""

REVERSE_SUPERSEDED_SQL = """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.billing_payment_event_superseded(
 uuid, timestamptz, text, bigint, varchar);
DROP FUNCTION clinic_app.billing_payment_event_recovered(
 text, uuid, text, text, bigint, varchar);
RESET ROLE;
"""


class Migration(migrations.Migration):
    """Add the recorded-event supersession predicate for reconciliation."""

    dependencies: ClassVar = [("billing", "0005_payment_events")]

    operations: ClassVar = [
        migrations.RunSQL(SUPERSEDED_SQL, REVERSE_SUPERSEDED_SQL),
    ]
