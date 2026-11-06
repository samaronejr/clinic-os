"""Stored-only PSP scope resolution and patient instruction projection."""

SQL = """
CREATE FUNCTION clinic_app.billing_psp_access(requested_intent uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.billing_pspintent i
 WHERE i.id=requested_intent AND i.organization_id=
 NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND NULLIF(current_setting('app.current_patient_session',true),'') IS NULL
 AND NULLIF(current_setting('app.current_principal',true),'') IS NULL
 AND EXISTS (SELECT 1 FROM clinic_app.identity_user u
  WHERE u.id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
  AND u.is_active)
 AND (i.requested_by_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 OR clinic_app.billing_book_access(i.book_id,'journal.read')
 OR clinic_app.billing_book_access(i.book_id,'charge.collect')))
$f$;

CREATE FUNCTION clinic_app.billing_psp_scope(requested_provider text, reference text)
RETURNS TABLE(intent_id uuid,organization_id uuid,clinic_id uuid,actor_id uuid)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT i.id,i.organization_id,b.clinic_id,i.requested_by_id
 FROM clinic_app.billing_pspbinding r
 JOIN clinic_app.billing_pspintent i ON i.id=r.intent_id
 JOIN clinic_app.billing_journalbook b ON b.id=i.book_id
 WHERE r.provider=requested_provider AND r.provider_reference=reference
$f$;

CREATE FUNCTION clinic_app.billing_psp_intent_scope(requested_intent uuid)
RETURNS TABLE(intent_id uuid,organization_id uuid,clinic_id uuid,actor_id uuid)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT i.id,i.organization_id,b.clinic_id,i.requested_by_id
 FROM clinic_app.billing_pspintent i
 JOIN clinic_app.billing_journalbook b ON b.id=i.book_id
 WHERE i.id=requested_intent
$f$;

CREATE FUNCTION clinic_app.billing_psp_seen(
 requested_provider text, requested_source text, requested_event text)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.billing_pspevidence
 WHERE provider=requested_provider AND source=requested_source
 AND event_id=requested_event)
$f$;

CREATE FUNCTION clinic_app.billing_psp_dispatchable(
 requested_intent uuid, fresh_session boolean)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.billing_pspintent i
 JOIN clinic_app.billing_journalbook b ON b.id=i.book_id
 WHERE i.id=requested_intent
 AND i.requested_by_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.billing_book_access(b.id,CASE i.kind
  WHEN 'charge' THEN 'charge.collect' WHEN 'refund' THEN 'refund.approve'
  ELSE 'payout.approve' END)
 AND ((i.kind='charge' AND EXISTS (SELECT 1 FROM clinic_app.billing_invoice v
  WHERE v.id=i.target_id AND v.state='open'))
 OR (i.kind='refund' AND EXISTS (SELECT 1 FROM clinic_app.billing_refund r
  WHERE r.id=i.target_id AND r.state IN ('sending','unknown')))
 OR (i.kind='payout' AND EXISTS (SELECT 1 FROM clinic_app.billing_payout p
  WHERE p.id=i.target_id AND p.state IN ('sending','unknown'))))
 AND (i.kind='charge' OR NOT fresh_session OR EXISTS (
  SELECT 1 FROM clinic_app.identity_sessionrecord s
  WHERE s.id=i.dispatch_session_id
  AND clinic_app.identity_session_verified(s.session_key_hash))))
$f$;

CREATE FUNCTION clinic_app.billing_patient_psp_instructions(requested_invoice uuid)
RETURNS TABLE(payment_code bytea,payment_url bytea,installments smallint,
 amount_minor bigint,receipt_reference uuid,receipt_issued_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT CASE WHEN payable.allowed THEN e.payment_code END,
 CASE WHEN payable.allowed THEN e.payment_url END,
 p.installments,p.amount_minor,r.reference,r.issued_at
 FROM clinic_app.billing_invoice i
 JOIN clinic_app.billing_pspintent p ON p.target_id=i.id AND p.kind='charge'
 LEFT JOIN LATERAL (
  SELECT * FROM clinic_app.billing_pspevidence e WHERE e.intent_id=p.id
  ORDER BY e.created_at DESC,e.id DESC LIMIT 1
 ) e ON true
 JOIN clinic_app.providers_capabilityversion v ON v.id=p.capability_version_id
 JOIN clinic_app.providers_providercapability c ON c.id=v.capability_id
 LEFT JOIN clinic_app.billing_pspreceipt r ON r.intent_id=p.id
 CROSS JOIN LATERAL (SELECT i.state='open' AND p.expires_at>statement_timestamp()
  AND c.current_version_id=v.id AND v.state='sandbox'
  AND v.provider='recorded-psp-v1'
  AND e.status='pending' AND e.decision='recorded'
  AND v.id=(SELECT current_version_id FROM clinic_app.providers_providercapability
   WHERE key=c.key AND (clinic_id=i.clinic_id OR clinic_id IS NULL)
   ORDER BY clinic_id NULLS LAST LIMIT 1)
  AND NOT EXISTS (SELECT 1 FROM clinic_app.billing_receipt old
   WHERE old.invoice_id=i.id) AS allowed) payable
 WHERE i.id=requested_invoice AND i.released_at IS NOT NULL
 AND i.issued_at IS NOT NULL AND p.organization_id=i.organization_id
 AND (payable.allowed OR r.reference IS NOT NULL)
 AND EXISTS (SELECT 1 FROM clinic_app.billing_patient_charges() authorized
 WHERE authorized.invoice_id=i.id)
$f$;
"""
