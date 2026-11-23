"""Scope inspection and evidence writes to one claimed recorded PSP action."""

from apps.billing.migrations._psp_guard_sql import SQL as ORIGINAL_GUARD

PRINCIPAL = "681a59f4-1475-4b9d-8bf5-73a61fd285e1"

_HUMAN = """OR intent.requested_by_id IS DISTINCT FROM actor
  OR NOT clinic_app.billing_psp_access(intent.id)"""
_OBSERVER = f"""OR NOT ((
   intent.requested_by_id IS NOT DISTINCT FROM actor
   AND clinic_app.billing_psp_access(intent.id)
  ) OR (
   actor IS NULL
   AND coalesce(current_setting('app.current_principal',true),'')='{PRINCIPAL}'
   AND clinic_app.billing_psp_observer_target(intent.id) IS NOT NULL
  ))"""
if ORIGINAL_GUARD.count(_HUMAN) != 1:
    msg = "unexpected frozen PSP guard"
    raise ValueError(msg)
GUARD = ORIGINAL_GUARD.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION").replace(
    _HUMAN, _OBSERVER
)

_SQL_TEMPLATE = """
CREATE FUNCTION clinic_app.billing_psp_observer_audit(
 tenant uuid, clinic uuid, subject uuid, completed boolean,
 occurred timestamptz, content_hash bytea)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE payload jsonb; prior_tenant text; inserted_seq bigint;
BEGIN
 -- This function is private to the two observer writers. Only the canonical
 -- audit append sees the system UUID in its actor slot; no domain query does.
 prior_tenant:=coalesce(current_setting('app.current_tenant',true),'');
 PERFORM set_config('app.current_tenant',tenant::text,true);
 PERFORM set_config('app.current_user_id','__PRINCIPAL__',true);
 payload:=jsonb_build_object('clinic_id',clinic::text,'object_verb',
  CASE WHEN completed THEN 'succeeded' ELSE 'evidence' END);
 IF completed THEN
  payload:=payload || jsonb_build_object('request_id',subject::text);
 END IF;
 inserted_seq:=clinic_app.audit_append(
  CASE WHEN completed THEN 'comms.operation.succeeded' ELSE 'billing.psp.evidence' END,
  CASE WHEN completed THEN 'clinic-os-worker' ELSE 'clinic-os-web' END,NULL,
  CASE WHEN completed THEN 'comms.integration_operation' ELSE 'billing.psp' END,
  subject::text,occurred,payload,content_hash);
 PERFORM set_config('app.current_user_id','',true);
 PERFORM set_config('app.current_tenant',prior_tenant,true);
 RETURN inserted_seq;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.billing_psp_observer_audit(
 uuid,uuid,uuid,boolean,timestamptz,bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.billing_psp_observer_audit(
 uuid,uuid,uuid,boolean,timestamptz,bytea) TO clinic_resolver;

GRANT INSERT ON clinic_app.billing_pspbinding,clinic_app.billing_pspevidence
 TO clinic_resolver;
GRANT UPDATE(provider_reference) ON clinic_app.comms_integrationoperation
 TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.billing_psp_dispatched(requested uuid)
RETURNS uuid LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT o.id FROM clinic_app.billing_pspintent i
 JOIN clinic_app.billing_journalbook b ON b.id=i.book_id
  AND b.organization_id=i.organization_id
 JOIN clinic_app.comms_integrationoperation o ON o.subject_id=i.id
  AND o.idempotency_key=i.id AND o.provider=i.provider
  AND o.organization_id=i.organization_id AND o.clinic_id=b.clinic_id
  AND o.actor_id=i.requested_by_id
 WHERE i.id=requested AND i.provider='recorded-psp-v1'
 AND o.kind='action' AND o.subject_type='billing.psp_intent' AND o.attempt_count>0
$f$;

CREATE FUNCTION clinic_app.billing_psp_observer_target(requested uuid)
RETURNS uuid LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.billing_psp_dispatched(i.id)
 FROM clinic_app.billing_pspintent i
 JOIN clinic_app.billing_journalbook b ON b.id=i.book_id
 JOIN clinic_app.providers_capabilityversion v ON v.id=i.capability_version_id
 JOIN clinic_app.providers_providercapability c ON c.id=v.capability_id
 WHERE i.id=requested AND v.state='sandbox' AND v.approval_id IS NOT NULL
 AND v.provider=i.provider AND v.environment='sandbox' AND v.account='fixture'
 AND c.key=CASE i.method WHEN 'pix' THEN 'pix' WHEN 'card' THEN 'psp_card' END
 AND (c.clinic_id IS NULL OR c.clinic_id=b.clinic_id)
$f$;

CREATE FUNCTION clinic_app.billing_psp_observer_request(requested uuid, proof bytea)
RETURNS SETOF clinic_app.billing_pspintent LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE intent clinic_app.billing_pspintent; verifier bytea;
BEGIN
 IF octet_length(proof) IS DISTINCT FROM 32
 OR current_setting('transaction_isolation')<>'read committed'
 OR NULLIF(current_setting('app.current_user_id',true),'') IS NOT NULL
 OR NULLIF(current_setting('app.current_patient_session',true),'') IS NOT NULL
 OR NULLIF(current_setting('app.current_principal',true),'') IS NOT NULL
 OR NULLIF(current_setting('app.current_scim_token',true),'') IS NOT NULL
 OR NULLIF(current_setting('app.current_sso_connection',true),'') IS NOT NULL
 OR NULLIF(current_setting('app.current_support_grant',true),'') IS NOT NULL
 OR clinic_app.billing_psp_observer_target(requested) IS NULL THEN RETURN; END IF;
 SELECT * INTO STRICT intent FROM clinic_app.billing_pspintent WHERE id=requested;
 IF coalesce(NULLIF(current_setting('app.current_tenant',true),''),
  intent.organization_id::text)<>intent.organization_id::text THEN RETURN; END IF;
 -- An authorized policy successor rotates the observer without rewriting or
 -- changing the provider/financial terms of any existing intent.
 SELECT p.observer_verifier INTO verifier FROM clinic_app.billing_psppolicy p
 WHERE p.book_id=intent.book_id ORDER BY p.created_at DESC,p.id DESC LIMIT 1;
 IF verifier=sha256(proof) THEN
  RETURN NEXT intent;
 END IF;
END $f$;

-- The regular FORCE RLS policies still deny these writes to a connection
-- with a principal GUC. Only the two bounded definer writers can bypass RLS;
-- the shared trigger still validates exact scope, vocabulary and ordering.
__GUARD__

CREATE FUNCTION clinic_app.billing_psp_observer_record(
 requested uuid, proof bytea, reference text, fact jsonb,
 code bytea, url bytea, occurred timestamptz, content_hash bytea)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp
AS $f$
DECLARE intent clinic_app.billing_pspintent; clinic uuid;
 evidence clinic_app.billing_pspevidence; outcome text;
BEGIN
 SELECT * INTO intent FROM clinic_app.billing_psp_observer_request(requested,proof);
 IF intent.id IS NULL THEN
  RAISE EXCEPTION 'PSP observation denied' USING ERRCODE='42501';
 END IF;
 SELECT clinic_id INTO STRICT clinic FROM clinic_app.billing_journalbook
 WHERE id=intent.book_id;
 PERFORM set_config('app.current_principal','__PRINCIPAL__',true);
 IF reference IS NOT NULL THEN
  INSERT INTO clinic_app.billing_pspbinding
    (id,organization_id,intent_id,provider,provider_reference,created_at)
   VALUES(gen_random_uuid(),intent.organization_id,intent.id,intent.provider,
    reference,clock_timestamp())
   ON CONFLICT (intent_id) DO NOTHING;
  IF NOT EXISTS (SELECT 1 FROM clinic_app.billing_pspbinding
   WHERE intent_id=intent.id AND provider_reference=reference) THEN
   RAISE EXCEPTION 'PSP binding conflict' USING ERRCODE='23514';
  END IF;
 END IF;
 evidence:=jsonb_populate_record(NULL::clinic_app.billing_pspevidence,fact);
 INSERT INTO clinic_app.billing_pspevidence(
  id,organization_id,intent_id,provider,event_id,source,status,decision,amount_minor,
  currency,fee_minor,version,payment_code,payment_url,created_at)
 VALUES(evidence.id,intent.organization_id,intent.id,intent.provider,evidence.event_id,
  evidence.source,evidence.status,evidence.decision,evidence.amount_minor,
  evidence.currency,evidence.fee_minor,evidence.version,code,url,clock_timestamp())
 RETURNING decision INTO STRICT outcome;
 PERFORM set_config('app.current_principal','',true);
 PERFORM clinic_app.billing_psp_observer_audit(
  intent.organization_id,clinic,evidence.id,false,occurred,content_hash);
 RETURN outcome;
END $f$;

CREATE FUNCTION clinic_app.billing_psp_observer_finish(
 requested uuid, operation uuid, proof bytea, reference text, observation text,
 occurred timestamptz, content_hash bytea)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE intent clinic_app.billing_pspintent; clinic uuid;
BEGIN
 SELECT * INTO intent FROM clinic_app.billing_psp_observer_request(requested,proof);
 IF intent.id IS NULL OR operation IS DISTINCT FROM
 clinic_app.billing_psp_observer_target(intent.id) THEN RETURN false; END IF;
 IF NOT EXISTS (SELECT 1 FROM clinic_app.billing_pspbinding
  WHERE intent_id=intent.id AND provider_reference=reference)
 OR NOT EXISTS (SELECT 1 FROM clinic_app.billing_pspevidence e
  WHERE e.intent_id=intent.id AND e.source='status' AND e.event_id=observation
  AND e.decision IN ('verified','recorded')
  AND NOT EXISTS (SELECT 1 FROM clinic_app.billing_pspevidence n
   WHERE n.intent_id=e.intent_id
   AND NOT (n.source='statement' AND n.decision='quarantined')
   AND (n.version>e.version OR (n.version=e.version AND
    (n.status,n.amount_minor,n.currency,n.fee_minor) IS DISTINCT FROM
    (e.status,e.amount_minor,e.currency,e.fee_minor))))) THEN
  RETURN false;
 END IF;
 UPDATE clinic_app.comms_integrationoperation SET status='succeeded',
  provider_reference=reference,last_error='',updated_at=clock_timestamp()
  WHERE id=operation AND status='in_progress' RETURNING clinic_id INTO clinic;
 IF NOT FOUND THEN RETURN false; END IF;
 PERFORM clinic_app.billing_psp_observer_audit(
  intent.organization_id,clinic,operation,true,occurred,content_hash);
 RETURN true;
END $f$;

CREATE FUNCTION clinic_app.billing_psp_observed(
 requested uuid, observed_status text, observed_version bigint,
 observed_amount bigint, observed_currency text, observed_fee bigint, proof bytea)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.billing_pspevidence e
  WHERE e.intent_id=requested AND e.organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND EXISTS (SELECT 1 FROM clinic_app.billing_psp_observer_request(requested,proof))
  AND (e.source IN ('status','callback') OR e.decision='matched')
  AND (e.status,e.version,e.amount_minor,e.currency,e.fee_minor)=
   (observed_status,observed_version,observed_amount,observed_currency,observed_fee))
$f$;

REVOKE ALL ON FUNCTION clinic_app.billing_psp_observer_target(uuid),
 clinic_app.billing_psp_dispatched(uuid),clinic_app.billing_psp_observer_request(
 uuid,bytea),clinic_app.billing_psp_observer_record(
 uuid,bytea,text,jsonb,bytea,bytea,timestamptz,bytea),
 clinic_app.billing_psp_observer_finish(uuid,uuid,bytea,text,text,timestamptz,bytea),
 clinic_app.billing_psp_observed(uuid,text,bigint,bigint,text,bigint,bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.billing_psp_dispatched(uuid),
 clinic_app.billing_psp_observer_request(uuid,bytea),
 clinic_app.billing_psp_observer_record(
 uuid,bytea,text,jsonb,bytea,bytea,timestamptz,bytea),
 clinic_app.billing_psp_observer_finish(uuid,uuid,bytea,text,text,timestamptz,bytea),
 clinic_app.billing_psp_observed(uuid,text,bigint,bigint,text,bigint,bytea)
 TO clinic_app;
RESET ROLE;
"""

SQL = _SQL_TEMPLATE.replace("__PRINCIPAL__", PRINCIPAL).replace("__GUARD__", GUARD)
