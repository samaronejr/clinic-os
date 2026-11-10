"""RNDS submission log posture: FORCE RLS, retained transitions and resolvers.

Only resolvers write ``interop_rndssubmission``. ``interop_rnds_request``
re-checks, in the database, everything the service checks: the actor's
``result.release`` authority for the result's patient, the current and
acknowledged result version, the patient's unrevoked ``rnds_exchange`` consent
and the clinic's sandbox RNDS capability version on the fixture provider.
``interop_rnds_settle`` records the RNDS answer only while the submission's
outbox operation is in flight; ``interop_rnds_undelivered`` marks a pending
submission uncertain when its operation failed or was cancelled without an
answer. An uncertain submission keeps its bundle identifier: only its exact
terms resend it, and only a definitive RNDS rejection frees the result version.
"""

from typing import Final

_CONTRACT: Final = r"""
ALTER TABLE clinic_app.interop_rndssubmission ADD CONSTRAINT interop_rnds_contract
 CHECK (state IN ('pending','accepted','rejected','uncertain') AND revision>=1
  AND environment='homologation' AND profile='BRResultadoExameLaboratorial-1.1'
  AND request_digest ~ '^[0-9a-f]{64}$' AND bundle_sha256 ~ '^[0-9a-f]{64}$'
  AND rnds_identifier ~ '^[A-Za-z0-9.-]{0,128}$'
  AND answer_code ~ '^(|EHR-ERR[0-9]{3}|http_[0-9]{3}|undelivered|withdrawn)$'
  AND ((state='pending' AND decided_at IS NULL AND rnds_identifier=''
    AND answer_code='')
   OR (state='accepted' AND decided_at IS NOT NULL
    AND ((rnds_identifier<>'' AND answer_code='')
     OR (rnds_identifier='' AND answer_code='EHR-ERR866')))
   OR (state='rejected' AND decided_at IS NOT NULL AND rnds_identifier=''
    AND answer_code ~ '^EHR-ERR[0-9]{3}$' AND answer_code<>'EHR-ERR866')
   OR (state='uncertain' AND decided_at IS NOT NULL AND rnds_identifier=''
    AND answer_code<>'' AND answer_code<>'EHR-ERR866')));
REVOKE ALL ON clinic_app.interop_rndssubmission FROM PUBLIC, clinic_app;
GRANT SELECT ON clinic_app.interop_rndssubmission TO clinic_app;
GRANT SELECT, INSERT ON clinic_app.interop_rndssubmission TO clinic_resolver;
GRANT UPDATE (state,revision,rnds_identifier,answer_code,decided_at,command_id,
 requested_by_id,capability_version_id)
 ON clinic_app.interop_rndssubmission TO clinic_resolver;
ALTER TABLE clinic_app.interop_rndssubmission ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.interop_rndssubmission FORCE ROW LEVEL SECURITY;
CREATE POLICY interop_rnds_read ON clinic_app.interop_rndssubmission
 AS PERMISSIVE FOR SELECT TO PUBLIC
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND clinic_app.has_permission('result.read',clinic_id,enrollment_id));
"""

_FUNCTIONS: Final = r"""
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.interop_rnds_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION 'interop history is retained' USING ERRCODE='23514';
 END IF;
 IF TG_OP='INSERT' THEN
  IF NEW.state<>'pending' OR NEW.revision<>1 OR NEW.rnds_identifier<>''
   OR NEW.answer_code<>'' OR NEW.decided_at IS NOT NULL OR NEW.bundle IS NULL THEN
   RAISE EXCEPTION 'invalid rnds submission' USING ERRCODE='23514';
  END IF;
  NEW.created_at:=statement_timestamp();
  RETURN NEW;
 END IF;
 IF OLD.state='pending' AND NEW.state IN ('accepted','rejected','uncertain')
  AND NEW.revision=OLD.revision+1
  AND (to_jsonb(NEW)-ARRAY['state','revision','rnds_identifier','answer_code',
   'decided_at'])=(to_jsonb(OLD)-ARRAY['state','revision','rnds_identifier',
   'answer_code','decided_at']) THEN
  NEW.decided_at:=statement_timestamp();
  RETURN NEW;
 END IF;
 -- A resend keeps the id, bundle and terms; only its arming command, actor and
 -- capability version change.
 IF OLD.state='uncertain' AND NEW.state='pending'
  AND NEW.revision=OLD.revision+1 AND NEW.answer_code=''
  AND (to_jsonb(NEW)-ARRAY['state','revision','answer_code','decided_at',
   'command_id','requested_by_id','capability_version_id'])=(to_jsonb(OLD)
   -ARRAY['state','revision','answer_code','decided_at','command_id',
   'requested_by_id','capability_version_id']) THEN
  NEW.decided_at:=NULL;
  RETURN NEW;
 END IF;
 RAISE EXCEPTION 'interop transition denied' USING ERRCODE='23514';
END $f$;
REVOKE ALL ON FUNCTION clinic_app.interop_rnds_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_rnds_guard() TO clinic_owner;

CREATE FUNCTION clinic_app.interop_rnds_request(requested_clinic uuid,
 requested_submission uuid, requested_version uuid, requested_command uuid,
 requested_digest text, requested_capability uuid, sealed_bundle bytea,
 bundle_digest text)
RETURNS TABLE(record_id uuid, outcome text, payload_digest text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE org uuid; actor uuid; patient_enrollment uuid; consent_text uuid;
 existing clinic_app.interop_rndssubmission;
BEGIN
 SELECT c.organization_id INTO org FROM clinic_app.identity_clinic c
 WHERE c.id=requested_clinic
  AND c.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 SELECT u.id INTO actor FROM clinic_app.load_current_user() u;
 SELECT o.enrollment_id INTO patient_enrollment FROM clinic_app.orders_resultmatch m
 JOIN clinic_app.orders_order o ON o.id=m.order_id
  AND o.organization_id=m.organization_id AND o.clinic_id=m.clinic_id
 WHERE m.version_id=requested_version AND m.clinic_id=requested_clinic
  AND m.organization_id=org;
 IF org IS NULL OR actor IS NULL OR patient_enrollment IS NULL
  OR NOT clinic_app.has_permission('result.release',requested_clinic,
   patient_enrollment) THEN
  RAISE EXCEPTION 'rnds submission denied' USING ERRCODE='42501';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
  'clinic-lock-v1:interop-rnds-version:' || requested_version::text,0));
 SELECT * INTO existing FROM clinic_app.interop_rndssubmission s
 WHERE s.clinic_id=requested_clinic AND s.command_id=requested_command;
 IF existing.id IS NOT NULL THEN
  IF existing.request_digest=requested_digest AND existing.requested_by_id=actor
   AND existing.result_version_id=requested_version THEN
   RETURN QUERY SELECT existing.id,'replayed'::text,NULL::text;
  ELSE
   RETURN QUERY SELECT NULL::uuid,'conflict'::text,NULL::text;
  END IF;
  RETURN;
 END IF;
 IF NOT clinic_app.orders_is_current(requested_version) THEN
  RETURN QUERY SELECT NULL::uuid,'source_superseded'::text,NULL::text;
  RETURN;
 END IF;
 IF NOT EXISTS (SELECT 1 FROM clinic_app.orders_resultacknowledgment a
  WHERE a.version_id=requested_version AND a.organization_id=org) THEN
  RETURN QUERY SELECT NULL::uuid,'not_acknowledged'::text,NULL::text;
  RETURN;
 END IF;
 SELECT t.id INTO consent_text FROM clinic_app.consent_consenttext t
 WHERE t.clinic_id=requested_clinic AND t.organization_id=org
  AND t.purpose='rnds_exchange' ORDER BY t.version DESC LIMIT 1;
 IF consent_text IS NULL OR NOT EXISTS (
  SELECT 1 FROM clinic_app.consent_consentacceptance a
  WHERE a.text_id=consent_text AND a.enrollment_id=patient_enrollment
   AND a.clinic_id=requested_clinic AND a.organization_id=org
   AND NOT EXISTS (SELECT 1 FROM clinic_app.consent_consentrevocation r
    WHERE r.acceptance_id=a.id)) THEN
  RETURN QUERY SELECT NULL::uuid,'consent_required'::text,NULL::text;
  RETURN;
 END IF;
 IF NOT EXISTS (SELECT 1 FROM clinic_app.providers_capabilityversion v
  JOIN clinic_app.providers_providercapability p ON p.id=v.capability_id
   AND p.current_version_id=v.id
  WHERE v.id=requested_capability AND p.key='rnds'
   AND (p.clinic_id=requested_clinic OR (p.clinic_id IS NULL AND NOT EXISTS (
    SELECT 1 FROM clinic_app.providers_providercapability scoped
    WHERE scoped.key='rnds' AND scoped.clinic_id=requested_clinic)))
   AND v.state='sandbox' AND v.approval_id IS NOT NULL
   AND v.provider='rnds-fixture' AND v.environment='homologation'
   AND v.account='fixture') THEN
  RETURN QUERY SELECT NULL::uuid,'unavailable'::text,NULL::text;
  RETURN;
 END IF;
 -- RNDS may hold an uncertain document: resend that exact bundle under its own
 -- identifier, never a new one, once its last operation is over.
 SELECT * INTO existing FROM clinic_app.interop_rndssubmission s
 WHERE s.result_version_id=requested_version AND s.state='uncertain';
 IF existing.id IS NOT NULL THEN
  IF existing.request_digest<>requested_digest THEN
   RETURN QUERY SELECT NULL::uuid,'delivery_unresolved'::text,NULL::text;
   RETURN;
  END IF;
  IF EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation x
   WHERE x.subject_type='interop.rnds_submission' AND x.subject_id=existing.id
    AND x.organization_id=org AND x.status IN ('pending','in_progress')) THEN
   RETURN QUERY SELECT NULL::uuid,'already_submitted'::text,NULL::text;
   RETURN;
  END IF;
  UPDATE clinic_app.interop_rndssubmission s SET state='pending',
   revision=s.revision+1,answer_code='',command_id=requested_command,
   requested_by_id=actor,capability_version_id=requested_capability
  WHERE s.id=existing.id;
  RETURN QUERY SELECT existing.id,'resent'::text,existing.bundle_sha256::text;
  RETURN;
 END IF;
 IF EXISTS (SELECT 1 FROM clinic_app.interop_rndssubmission s
  WHERE s.result_version_id=requested_version
   AND s.state IN ('pending','accepted')) THEN
  RETURN QUERY SELECT NULL::uuid,'already_submitted'::text,NULL::text;
  RETURN;
 END IF;
 IF requested_submission IS NULL OR requested_command IS NULL
  OR requested_digest IS NULL OR requested_digest !~ '^[0-9a-f]{64}$'
  OR bundle_digest IS NULL OR bundle_digest !~ '^[0-9a-f]{64}$'
  OR sealed_bundle IS NULL THEN
  RAISE EXCEPTION 'invalid rnds submission' USING ERRCODE='22023';
 END IF;
 INSERT INTO clinic_app.interop_rndssubmission (id,organization_id,clinic_id,
  result_version_id,enrollment_id,requested_by_id,capability_version_id,
  command_id,request_digest,environment,profile,state,revision,bundle,
  bundle_sha256,rnds_identifier,answer_code,created_at,decided_at)
 VALUES (requested_submission,org,requested_clinic,requested_version,
  patient_enrollment,actor,requested_capability,requested_command,
  requested_digest,'homologation','BRResultadoExameLaboratorial-1.1','pending',
  1,sealed_bundle,bundle_digest,'','',statement_timestamp(),NULL);
 RETURN QUERY SELECT requested_submission,'created'::text,bundle_digest;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.interop_rnds_request(uuid,uuid,uuid,uuid,text,uuid,
 bytea,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_rnds_request(uuid,uuid,uuid,uuid,text,
 uuid,bytea,text) TO clinic_app;

CREATE FUNCTION clinic_app.interop_rnds_settle(operation uuid,
 requested_outcome text, requested_identifier text, requested_code text)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE o clinic_app.comms_integrationoperation; settled uuid;
BEGIN
 SELECT * INTO o FROM clinic_app.comms_integrationoperation x
 WHERE x.id=operation AND x.subject_type='interop.rnds_submission'
  AND x.provider='interop-rnds-v1' AND x.status='in_progress';
 IF o.id IS NULL OR requested_outcome IS NULL
  OR requested_outcome NOT IN ('accepted','rejected','uncertain') THEN
  RAISE EXCEPTION 'rnds settlement denied' USING ERRCODE='42501';
 END IF;
 UPDATE clinic_app.interop_rndssubmission s SET state=requested_outcome,
  revision=s.revision+1,rnds_identifier=coalesce(requested_identifier,''),
  answer_code=coalesce(requested_code,''),decided_at=statement_timestamp()
 WHERE s.id=o.subject_id AND s.organization_id=o.organization_id
  AND s.state='pending'
 RETURNING s.id INTO settled;
 RETURN settled;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.interop_rnds_settle(uuid,text,text,text)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_rnds_settle(uuid,text,text,text)
 TO clinic_app;

CREATE FUNCTION clinic_app.interop_rnds_undelivered()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 UPDATE clinic_app.interop_rndssubmission s SET state='uncertain',
  revision=s.revision+1,
  answer_code=CASE WHEN NEW.status='cancelled' THEN 'withdrawn'
   ELSE 'undelivered' END,
  decided_at=statement_timestamp()
 WHERE s.id=NEW.subject_id AND s.organization_id=NEW.organization_id
  AND s.state='pending';
 RETURN NULL;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.interop_rnds_undelivered() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_rnds_undelivered() TO clinic_owner;
RESET ROLE;
"""

_TRIGGERS: Final = r"""
CREATE TRIGGER interop_rnds_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.interop_rndssubmission FOR EACH ROW
 EXECUTE FUNCTION clinic_app.interop_rnds_guard();
CREATE TRIGGER interop_rnds_retained_truncate BEFORE TRUNCATE
 ON clinic_app.interop_rndssubmission FOR EACH STATEMENT
 EXECUTE FUNCTION clinic_app.interop_rnds_guard();
CREATE TRIGGER interop_rnds_undelivered AFTER UPDATE OF status
 ON clinic_app.comms_integrationoperation FOR EACH ROW
 WHEN (NEW.subject_type='interop.rnds_submission'
  AND NEW.status IN ('failed','cancelled') AND OLD.status IS DISTINCT FROM NEW.status)
 EXECUTE FUNCTION clinic_app.interop_rnds_undelivered();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.interop_rnds_guard(),
 clinic_app.interop_rnds_undelivered() FROM clinic_owner;
RESET ROLE;
"""

SQL: Final = _CONTRACT + _FUNCTIONS + _TRIGGERS

# Retained submission history refuses schema removal, as the 0001 tables do.
REVERSE: Final = r"""
SET LOCAL ROLE clinic_resolver;
DO $f$
BEGIN
 IF EXISTS(SELECT 1 FROM clinic_app.interop_rndssubmission) THEN
  RAISE EXCEPTION 'interop history requires restore' USING ERRCODE='55000';
 END IF;
END $f$;
RESET ROLE;
DROP TRIGGER interop_rnds_undelivered ON clinic_app.comms_integrationoperation;
DROP TRIGGER interop_rnds_retained_truncate ON clinic_app.interop_rndssubmission;
DROP TRIGGER interop_rnds_guard ON clinic_app.interop_rndssubmission;
DROP POLICY interop_rnds_read ON clinic_app.interop_rndssubmission;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.interop_rnds_undelivered();
DROP FUNCTION clinic_app.interop_rnds_settle(uuid,text,text,text);
DROP FUNCTION clinic_app.interop_rnds_request(uuid,uuid,uuid,uuid,text,uuid,bytea,
 text);
DROP FUNCTION clinic_app.interop_rnds_guard();
RESET ROLE;
"""
