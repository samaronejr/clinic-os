"""Expose dispatch state, not an operation UUID, only to its clinic's observer."""

SQL = """
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.billing_psp_dispatched(uuid) FROM PUBLIC,clinic_app;

CREATE FUNCTION clinic_app.billing_psp_dispatch_state(
 requested uuid, caller_clinic uuid, proof bytea)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE verifier bytea;
BEGIN
 IF octet_length(proof) IS DISTINCT FROM 32
 OR current_setting('transaction_isolation')<>'read committed'
 OR NULLIF(current_setting('app.current_user_id',true),'') IS NOT NULL
 OR NULLIF(current_setting('app.current_patient_session',true),'') IS NOT NULL
 OR NULLIF(current_setting('app.current_principal',true),'') IS NOT NULL
 OR NULLIF(current_setting('app.current_scim_token',true),'') IS NOT NULL
 OR NULLIF(current_setting('app.current_sso_connection',true),'') IS NOT NULL
 OR NULLIF(current_setting('app.current_support_grant',true),'') IS NOT NULL THEN
  RETURN NULL;
 END IF;
 SELECT p.observer_verifier INTO verifier FROM clinic_app.billing_pspintent i
 JOIN clinic_app.billing_journalbook b ON b.id=i.book_id
  AND b.organization_id=i.organization_id
 JOIN clinic_app.billing_psppolicy p ON p.book_id=b.id
  AND p.organization_id=b.organization_id
 WHERE i.id=requested AND b.clinic_id=caller_clinic
 AND coalesce(NULLIF(current_setting('app.current_tenant',true),''),
  i.organization_id::text)=i.organization_id::text
 ORDER BY p.created_at DESC,p.id DESC LIMIT 1;
 IF verifier IS NULL OR verifier<>sha256(proof) THEN RETURN NULL; END IF;
 IF clinic_app.billing_psp_dispatched(requested) IS NULL THEN RETURN false; END IF;
 IF clinic_app.billing_psp_observer_target(requested) IS NOT NULL THEN
  RETURN true;
 END IF;
 RETURN NULL;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.billing_psp_dispatch_state(uuid,uuid,bytea)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.billing_psp_dispatch_state(uuid,uuid,bytea)
 TO clinic_app;

-- Serialize even direct, authorized invocations with every evidence insert.
-- The order matches billing_psp_guard. A waiter reads a new READ COMMITTED
-- snapshot after the locks, then checks the fact and changes state together.
CREATE OR REPLACE FUNCTION clinic_app.billing_psp_observer_finish(
 requested uuid, operation uuid, proof bytea, reference text, observation text,
 occurred timestamptz, content_hash bytea)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE intent clinic_app.billing_pspintent; clinic uuid;
BEGIN
 SELECT * INTO intent FROM clinic_app.billing_psp_observer_request(requested,proof);
 IF intent.id IS NULL THEN RETURN false; END IF;
 SELECT clinic_id INTO STRICT clinic FROM clinic_app.billing_journalbook
 WHERE id=intent.book_id;
 PERFORM pg_advisory_xact_lock(hashtextextended('clinic-lock-v1:clinic:'||clinic,0));
 PERFORM pg_advisory_xact_lock(hashtextextended('clinic-audit:'||intent.organization_id,
  0));
 PERFORM pg_advisory_xact_lock(hashtextextended('clinic-lock-v1:psp:'||intent.id,0));
 UPDATE clinic_app.comms_integrationoperation SET status='succeeded',
  provider_reference=reference,last_error='',updated_at=clock_timestamp()
 WHERE id=operation AND status='in_progress'
 AND operation=clinic_app.billing_psp_observer_target(intent.id)
 AND EXISTS (SELECT 1 FROM clinic_app.billing_psp_observer_request(requested,proof))
 AND EXISTS (SELECT 1 FROM clinic_app.billing_pspbinding
  WHERE intent_id=intent.id AND provider_reference=reference)
 AND EXISTS (SELECT 1 FROM clinic_app.billing_pspevidence e
  WHERE e.intent_id=intent.id AND e.source='status' AND e.event_id=observation
  AND e.decision IN ('verified','recorded')
  AND NOT EXISTS (SELECT 1 FROM clinic_app.billing_pspevidence n
   WHERE n.intent_id=e.intent_id
   AND NOT (n.source='statement' AND n.decision='quarantined')
   AND (n.version>e.version OR (n.version=e.version AND n.status<>'unknown' AND
    (n.status,n.amount_minor,n.currency,n.fee_minor) IS DISTINCT FROM
    (e.status,e.amount_minor,e.currency,e.fee_minor)))));
 IF NOT FOUND THEN RETURN false; END IF;
 PERFORM clinic_app.billing_psp_observer_audit(
  intent.organization_id,clinic,operation,true,occurred,content_hash);
 RETURN true;
END $f$;
RESET ROLE;
"""
