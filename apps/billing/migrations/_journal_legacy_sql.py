"""Preserve one-to-one history and widen only the current Finance boundary."""

from apps.billing.migrations._invoice_sql import SQL as INVOICE_SQL


def _replace_once(source: str, old: str, new: str) -> str:
    """Fail closed if a frozen SQL fragment no longer has its expected shape.

    The census decoder executes this module without str.count: split is the
    exactly-once check (tests/scheduling/clock_runsql.py).
    """
    if len(source.split(old)) - 1 != 1:
        message = "historical billing guard shape changed"
        raise ValueError(message)
    return source.replace(old, new, 1)


_settlement_guard = INVOICE_SQL.split(
    "CREATE FUNCTION clinic_app.billing_settlement_guard()", 1
)[1].split("CREATE FUNCTION clinic_app.billing_settlement_receipt()", 1)[0]
_SETTLEMENT_GUARD = _replace_once(
    "CREATE OR REPLACE FUNCTION clinic_app.billing_settlement_guard()"
    + _settlement_guard,
    "clinic_app.questionnaire_staff(charge.clinic_id,\n"
    "      ARRAY['owner','clinic_admin','receptionist'])",
    "clinic_app.billing_collection_authorized(charge.clinic_id)",
)

_invoice_guard = INVOICE_SQL.split(
    "CREATE FUNCTION clinic_app.billing_invoice_guard()", 1
)[1].split("CREATE FUNCTION clinic_app.billing_revision_snapshot()", 1)[0]
_invoice_paid = _replace_once(
    "CREATE OR REPLACE FUNCTION clinic_app.billing_invoice_guard()" + _invoice_guard,
    "OR (OLD.state = 'open' AND NEW.state = 'paid' AND EXISTS (",
    "OR (OLD.state = 'open' AND NEW.state = 'paid' AND (EXISTS (",
)
_INVOICE_GUARD = _replace_once(
    _invoice_paid,
    "AND s.amount_minor = OLD.amount_minor AND s.currency = OLD.currency)))",
    """AND s.amount_minor = OLD.amount_minor AND s.currency = OLD.currency)
        OR EXISTS (
          SELECT 1 FROM clinic_app.billing_invoiceterms t
          WHERE t.invoice_id=OLD.id AND t.book_id IN (
            SELECT b.id FROM clinic_app.billing_journalbook b
            WHERE b.clinic_id=OLD.clinic_id
          )
          AND t.net_minor=(
            SELECT sum(a.amount_minor) FROM clinic_app.billing_settlementallocation a
            JOIN clinic_app.billing_journalsettlement s ON s.id=a.settlement_id
            WHERE a.terms_id=t.id AND s.state='posted'
          ) - clinic_app.billing_refunded_prepayments(t.id)
        )
       )
      )
     )""",
)

# Only frozen function definitions enter this composition, never caller input.
_AUTH_FUNCTIONS = (
    """
CREATE FUNCTION clinic_app.billing_collection_authorized(requested_clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.questionnaire_staff(requested_clinic,
 ARRAY['owner','clinic_admin','receptionist'])
 OR clinic_app.has_permission('charge.collect',requested_clinic,NULL)
$f$;
CREATE FUNCTION clinic_app.billing_charge_authorized(requested_clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.questionnaire_staff(requested_clinic,
 ARRAY['owner','clinic_admin','receptionist'])
 OR clinic_app.has_permission('charge.create',requested_clinic,NULL)
$f$;
CREATE OR REPLACE FUNCTION clinic_app.billing_staff_invoice(requested_invoice uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.billing_invoice i
 WHERE i.id=requested_invoice AND i.organization_id=
 NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.billing_charge_authorized(i.clinic_id))
$f$;
""",
    _SETTLEMENT_GUARD,
    _INVOICE_GUARD,
)
LEGACY_AUTH_SQL = "\n".join(_AUTH_FUNCTIONS)

LEGACY_BRIDGE_SQL = """
CREATE FUNCTION clinic_app.billing_allocation_paid()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 UPDATE clinic_app.billing_invoice i SET state='paid'
 FROM clinic_app.billing_invoiceterms t WHERE t.id=NEW.terms_id AND i.id=t.invoice_id
 AND i.state='open' AND t.net_minor=(SELECT sum(a.amount_minor)
 FROM clinic_app.billing_settlementallocation a WHERE a.terms_id=t.id)
 -clinic_app.billing_refunded_prepayments(t.id);
 RETURN NULL;
END $f$;

CREATE FUNCTION clinic_app.billing_import_legacy(
 requested_clinic uuid, requested_settlement uuid)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE book clinic_app.billing_journalbook; legacy clinic_app.billing_settlement;
 invoice clinic_app.billing_invoice; terms clinic_app.billing_invoiceterms;
 receipt_id uuid; result uuid; due numeric;
BEGIN
 SELECT * INTO book FROM clinic_app.billing_journalbook WHERE clinic_id=requested_clinic
 AND organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 IF book.id IS NULL THEN RETURN NULL; END IF;
 IF NOT clinic_app.billing_collection_authorized(requested_clinic) THEN
  RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
 END IF;
 PERFORM clinic_app.billing_lock_book(book.id);
 SELECT * INTO legacy FROM clinic_app.billing_settlement WHERE id=requested_settlement;
 SELECT * INTO invoice FROM clinic_app.billing_invoice WHERE id=legacy.invoice_id;
 IF legacy.id IS NULL OR invoice.clinic_id<>requested_clinic
 OR legacy.confirmed_by_id IS DISTINCT FROM
 NULLIF(current_setting('app.current_user_id',true),'')::uuid THEN
  RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
 END IF;
 SELECT id INTO receipt_id FROM clinic_app.billing_journalsettlement
 WHERE legacy_settlement_id=legacy.id;
 IF receipt_id IS NOT NULL THEN RETURN receipt_id; END IF;
 SELECT * INTO terms FROM clinic_app.billing_invoiceterms WHERE invoice_id=invoice.id;
 IF terms.id IS NULL THEN
  INSERT INTO clinic_app.billing_invoiceterms
  (id,organization_id,book_id,invoice_id,payer,discount_minor,payer_adjustment_minor,
   net_minor,created_at)
  VALUES(gen_random_uuid(),book.organization_id,book.id,invoice.id,'patient',0,0,
   invoice.amount_minor,statement_timestamp()) RETURNING * INTO terms;
 END IF;
 IF EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment a
 WHERE a.id=invoice.appointment_id AND a.status='completed') THEN
  PERFORM * FROM clinic_app.billing_recognize_completion(
   requested_clinic,invoice.id,NULL);
 END IF;
 SELECT terms.net_minor-coalesce(sum(a.amount_minor),0)
 +clinic_app.billing_refunded_prepayments(terms.id) INTO due
 FROM clinic_app.billing_settlementallocation a WHERE a.terms_id=terms.id;
 IF due<=0 THEN
  RAISE EXCEPTION 'legacy allocation has no remaining debt' USING ERRCODE='23514';
 END IF;
 receipt_id:=gen_random_uuid();
 INSERT INTO clinic_app.billing_journalsettlement
 (id,organization_id,book_id,legacy_settlement_id,amount_minor,kind,account,
 evidence_reference,command_id,state,created_at)
 VALUES(receipt_id,book.organization_id,book.id,legacy.id,legacy.amount_minor,
 terms.payer,'bank',legacy.confirmation_reference,legacy.id,
 'observed',statement_timestamp());
 UPDATE clinic_app.billing_journalsettlement SET state='verified' WHERE id=receipt_id;
 UPDATE clinic_app.billing_journalsettlement SET state='posted' WHERE id=receipt_id;
 INSERT INTO clinic_app.billing_settlementallocation
 (id,organization_id,book_id,settlement_id,terms_id,amount_minor,
 repasse_minor,repasse_side,
 command_id,created_at) VALUES
 (gen_random_uuid(),book.organization_id,book.id,receipt_id,terms.id,
 due::bigint,0,'credit',legacy.id,statement_timestamp()) RETURNING id INTO result;
 RETURN receipt_id;
END $f$;
"""
