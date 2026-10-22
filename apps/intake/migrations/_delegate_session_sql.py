"""One effective patient authority feeds every existing patient surface."""

SQL = """
ALTER TABLE clinic_app.intake_patientsession
 ADD CONSTRAINT session_delegate_pair CHECK (
 (acting_for_enrollment_id IS NULL)=(delegate_grant_id IS NULL));
GRANT UPDATE (acting_for_enrollment_id,delegate_grant_id)
 ON clinic_app.intake_patientsession TO clinic_resolver;

SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.patient_authority(requested_session uuid,
 requested_operation text)
RETURNS TABLE(session_id uuid,organization_id uuid,clinic_id uuid,patient_id uuid,
 enrollment_id uuid,operations text[],expires_at timestamptz,
 idle_expires_at timestamptz,acting_for_enrollment_id uuid,delegate_grant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT s.id,s.organization_id,s.clinic_id,e.patient_id,e.id,scope.operations,
   s.expires_at,s.idle_expires_at,s.acting_for_enrollment_id,s.delegate_grant_id
 FROM clinic_app.patient_identity(requested_session) s
 LEFT JOIN clinic_app.identity_patientdelegategrant d ON d.id=s.delegate_grant_id
   AND d.organization_id=s.organization_id AND d.clinic_id=s.clinic_id
   AND d.delegate_enrollment_id=s.enrollment_id
   AND d.represented_enrollment_id=s.acting_for_enrollment_id
   AND d.state='active' AND d.expires_at>clinic_app.delegate_now()
   AND d.verified_by_id IS NOT NULL AND d.verified_at IS NOT NULL
   AND d.activated_at IS NOT NULL AND d.revoked_at IS NULL
   AND EXISTS (SELECT 1 FROM clinic_app.ehr_clinicalattachment evidence
     WHERE evidence.id=d.evidence_attachment_id AND evidence.state='available'
       AND evidence.organization_id=d.organization_id
       AND evidence.clinic_id=d.clinic_id)
 JOIN clinic_app.intake_patientclinicenrollment e
   ON e.id=coalesce(s.acting_for_enrollment_id,s.enrollment_id)
   AND e.organization_id=s.organization_id AND e.clinic_id=s.clinic_id
 CROSS JOIN LATERAL (
   SELECT ARRAY(SELECT op FROM unnest(s.operations::text[]) op
     WHERE s.delegate_grant_id IS NULL OR (d.id IS NOT NULL
       AND op=ANY(d.operations) AND
       (op='consent' OR 'care_processing'=ANY(d.purposes)))) operations
 ) scope
 WHERE ((s.delegate_grant_id IS NULL AND s.acting_for_enrollment_id IS NULL)
    OR (s.delegate_grant_id IS NOT NULL AND d.id IS NOT NULL))
 AND (requested_operation IS NULL OR requested_operation=ANY(scope.operations))
$f$;
CREATE FUNCTION clinic_app.current_patient_authority(requested_operation text)
RETURNS TABLE(session_id uuid,organization_id uuid,clinic_id uuid,patient_id uuid,
 enrollment_id uuid,operations text[],expires_at timestamptz,
 idle_expires_at timestamptz,acting_for_enrollment_id uuid,delegate_grant_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT a.* FROM clinic_app.patient_authority(
   NULLIF(current_setting('app.current_patient_session',true),'')::uuid,
   requested_operation) a
 WHERE NULLIF(current_setting('app.current_user_id',true),'') IS NULL
$f$;
CREATE OR REPLACE FUNCTION clinic_app.touch_patient_session(requested_session uuid)
RETURNS TABLE(session_id uuid,organization_id uuid,clinic_id uuid,patient_id uuid,
 enrollment_id uuid,operations text[],expires_at timestamptz,
 idle_expires_at timestamptz)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 UPDATE clinic_app.intake_patientsession s
 SET idle_expires_at=least(s.expires_at,statement_timestamp()+interval '30 minutes')
 WHERE s.id=requested_session AND EXISTS (
   SELECT 1 FROM clinic_app.patient_authority(requested_session,NULL));
 RETURN QUERY SELECT a.session_id,a.organization_id,a.clinic_id,a.patient_id,
   a.enrollment_id,a.operations,a.expires_at,a.idle_expires_at
 FROM clinic_app.patient_authority(requested_session,NULL) a;
END
$f$;
CREATE OR REPLACE FUNCTION clinic_app.patient_session_overview(kek text)
RETURNS TABLE(session_id uuid,patient_name varchar,clinic_name varchar,
 enrolled_at timestamptz,operations text[],expires_at timestamptz,
 idle_expires_at timestamptz)
LANGUAGE sql STABLE PARALLEL UNSAFE SECURITY DEFINER ROWS 1
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT s.session_id,convert_from(clinic_app.protected_decrypt(kek,
   CASE WHEN demographics.social_name IS NOT NULL
   THEN 'intake.patientdemographics.social_name' ELSE 'intake.patient.full_name' END,
   coalesce(demographics.social_name,p.full_name)),'UTF8')::varchar,
   c.name,e.created_at,s.operations,s.expires_at,s.idle_expires_at
 FROM clinic_app.current_patient_authority('enrollment_view') s
 JOIN clinic_app.intake_patient p ON p.id=s.patient_id
 JOIN clinic_app.identity_clinic c ON c.id=s.clinic_id
 JOIN clinic_app.intake_patientclinicenrollment e ON e.id=s.enrollment_id
 LEFT JOIN LATERAL (SELECT d.social_name FROM clinic_app.intake_patientdemographics d
   WHERE d.patient_id=s.patient_id AND d.organization_id=s.organization_id
   ORDER BY d.version DESC LIMIT 1) demographics ON true
$f$;
CREATE FUNCTION clinic_app.switch_patient_delegate(requested_clinic uuid,
 requested_grant uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s clinic_app.intake_patientsession;
 d clinic_app.identity_patientdelegategrant;
BEGIN
 SELECT * INTO s FROM clinic_app.patient_identity(
   NULLIF(current_setting('app.current_patient_session',true),'')::uuid);
 IF s.id IS NULL OR s.clinic_id IS DISTINCT FROM requested_clinic
    OR NULLIF(current_setting('app.current_user_id',true),'') IS NOT NULL THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501'; END IF;
 PERFORM 1 FROM clinic_app.intake_patientsession WHERE id=s.id FOR UPDATE;
 IF requested_grant IS NOT NULL THEN
   SELECT * INTO d FROM clinic_app.identity_patientdelegategrant g
   WHERE g.id=requested_grant AND g.clinic_id=s.clinic_id
     AND g.organization_id=s.organization_id
     AND g.delegate_enrollment_id=s.enrollment_id
     AND g.state='active' AND g.revoked_at IS NULL
     AND g.expires_at>clinic_app.delegate_now() FOR SHARE;
   IF d.id IS NULL THEN
     RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501'; END IF;
 END IF;
 IF (s.delegate_grant_id,s.acting_for_enrollment_id) IS NOT DISTINCT FROM
    (d.id,d.represented_enrollment_id) THEN RETURN false; END IF;
 UPDATE clinic_app.intake_patientsession SET delegate_grant_id=d.id,
   acting_for_enrollment_id=d.represented_enrollment_id WHERE id=s.id;
 RETURN true;
END
$f$;
CREATE FUNCTION clinic_app.delegate_session_status(requested_session uuid)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT CASE WHEN d.state='revoked' THEN 'revoked'
   WHEN d.state='active' AND d.expires_at<=clinic_app.delegate_now() THEN 'expired' END
 FROM clinic_app.patient_identity(requested_session) s
 JOIN clinic_app.identity_patientdelegategrant d ON d.id=s.delegate_grant_id
   AND d.delegate_enrollment_id=s.enrollment_id AND d.clinic_id=s.clinic_id
   AND d.organization_id=s.organization_id
   AND d.represented_enrollment_id=s.acting_for_enrollment_id
$f$;
CREATE FUNCTION clinic_app.delegate_recovery_identity()
RETURNS TABLE(session_id uuid,clinic_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT s.id,s.clinic_id FROM clinic_app.patient_identity(
 NULLIF(current_setting('app.current_patient_session',true),'')::uuid) s
 WHERE NULLIF(current_setting('app.current_user_id',true),'') IS NULL
   AND clinic_app.delegate_session_status(s.id)='expired'
$f$;
CREATE FUNCTION clinic_app.renew_delegate_grant(requested_clinic uuid,
 request_key uuid,deadline timestamptz)
RETURNS TABLE(grant_id uuid,revision integer,state text,changed boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s clinic_app.intake_patientsession;
 old_grant clinic_app.identity_patientdelegategrant;
 prior clinic_app.identity_patientdelegategrant; created uuid;
BEGIN
 SELECT * INTO s FROM clinic_app.patient_identity(
   NULLIF(current_setting('app.current_patient_session',true),'')::uuid);
 IF s.id IS NULL OR s.clinic_id IS DISTINCT FROM requested_clinic
 OR NULLIF(current_setting('app.current_user_id',true),'') IS NOT NULL
 THEN RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501'; END IF;
 SELECT * INTO old_grant FROM clinic_app.identity_patientdelegategrant d
 WHERE d.id=s.delegate_grant_id AND d.clinic_id=s.clinic_id
   AND d.organization_id=s.organization_id AND d.delegate_enrollment_id=s.enrollment_id
   AND d.represented_enrollment_id=s.acting_for_enrollment_id
   AND d.state='active' AND d.expires_at<=clinic_app.delegate_now() FOR UPDATE;
 IF old_grant.id IS NULL THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501'; END IF;
 SELECT * INTO prior FROM clinic_app.identity_patientdelegategrant d
 WHERE d.clinic_id=s.clinic_id AND d.idempotency_key=request_key;
 IF prior.id IS NOT NULL THEN
   IF prior.renewal_of_id IS DISTINCT FROM old_grant.id
      OR prior.expires_at IS DISTINCT FROM deadline THEN
     RAISE EXCEPTION 'delegate conflict' USING ERRCODE='40001'; END IF;
   RETURN QUERY SELECT prior.id,prior.revision,prior.state::text,false; RETURN;
 END IF;
 created:=gen_random_uuid();
 INSERT INTO clinic_app.identity_patientdelegategrant
 (id,organization_id,clinic_id,represented_enrollment_id,delegate_enrollment_id,
  authority_kind,evidence_attachment_id,purposes,operations,expires_at,
  state,revision,idempotency_key,grantor_session_id,renewal_of_id,created_at)
 VALUES (created,old_grant.organization_id,old_grant.clinic_id,
   old_grant.represented_enrollment_id,old_grant.delegate_enrollment_id,
   old_grant.authority_kind,old_grant.evidence_attachment_id,old_grant.purposes,
   old_grant.operations,deadline,'pending_verification',1,request_key,s.id,
   old_grant.id,statement_timestamp());
 RETURN QUERY SELECT created,1,'pending_verification'::text,true;
END
$f$;
CREATE FUNCTION clinic_app.delegate_session_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF TG_OP='DELETE' THEN
   RAISE EXCEPTION 'patient sessions are retained' USING ERRCODE='23514'; END IF;
 IF (NEW.acting_for_enrollment_id IS NOT NULL OR NEW.delegate_grant_id IS NOT NULL)
 AND NOT EXISTS (SELECT 1 FROM clinic_app.identity_patientdelegategrant d
 WHERE d.id=NEW.delegate_grant_id
   AND d.represented_enrollment_id=NEW.acting_for_enrollment_id
   AND d.delegate_enrollment_id=NEW.enrollment_id
   AND d.clinic_id=NEW.clinic_id
   AND d.organization_id=NEW.organization_id) THEN
   RAISE EXCEPTION 'invalid delegate session binding' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' AND (NEW.id,NEW.organization_id,NEW.clinic_id,NEW.patient_id,
    NEW.enrollment_id,NEW.grant_id)
 IS DISTINCT FROM (OLD.id,OLD.organization_id,OLD.clinic_id,OLD.patient_id,
    OLD.enrollment_id,OLD.grant_id) THEN
   RAISE EXCEPTION 'immutable patient identity' USING ERRCODE='23514'; END IF;
  IF TG_OP='UPDATE' AND (
    NEW.created_at>OLD.created_at OR NEW.expires_at>OLD.expires_at) THEN
    RAISE EXCEPTION 'patient session deadline cannot widen' USING ERRCODE='23514';
  END IF;
 IF TG_OP='UPDATE' AND (NEW.acting_for_enrollment_id,NEW.delegate_grant_id)
 IS DISTINCT FROM (OLD.acting_for_enrollment_id,OLD.delegate_grant_id)
 AND NEW.delegate_grant_id IS NOT NULL AND NOT EXISTS (
   SELECT 1 FROM clinic_app.identity_patientdelegategrant d
   WHERE d.id=NEW.delegate_grant_id AND d.state='active'
     AND d.expires_at>clinic_app.delegate_now() AND d.revoked_at IS NULL) THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501'; END IF;
 RETURN NEW;
END
$f$;
CREATE OR REPLACE FUNCTION clinic_app.patient_booking_scope()
RETURNS TABLE(session_id uuid,organization_id uuid,clinic_id uuid,
 patient_id uuid,enrollment_id uuid,clinic_name varchar,timezone varchar)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT s.session_id,s.organization_id,s.clinic_id,s.patient_id,s.enrollment_id,
   c.name,c.timezone FROM clinic_app.current_patient_authority('booking') s
 JOIN clinic_app.identity_clinic c ON c.id=s.clinic_id
$f$;
CREATE OR REPLACE FUNCTION clinic_app.questionnaire_patient_enrollment()
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT enrollment_id FROM clinic_app.current_patient_authority('questionnaires')
$f$;
CREATE OR REPLACE FUNCTION clinic_app.retention_records_session()
RETURNS TABLE(session_id uuid,organization_id uuid,clinic_id uuid,patient_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT s.session_id,s.organization_id,s.clinic_id,s.patient_id
 FROM clinic_app.current_patient_authority('records') s
$f$;
CREATE OR REPLACE FUNCTION clinic_app.consent_session()
RETURNS TABLE(session_id uuid,organization_id uuid,clinic_id uuid,
 patient_id uuid,enrollment_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT s.session_id,s.organization_id,s.clinic_id,s.patient_id,s.enrollment_id
 FROM clinic_app.current_patient_authority('consent') s
$f$;
CREATE OR REPLACE FUNCTION clinic_app.teleconsult_patient_match(requested_session uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.teleconsult_teleconsultsession t
 JOIN clinic_app.current_patient_authority('teleconsult') s
   ON s.patient_id=t.patient_id AND s.clinic_id=t.clinic_id
   AND s.organization_id=t.organization_id WHERE t.id=requested_session)
$f$;
CREATE FUNCTION clinic_app.delegate_purpose_allowed(requested_purpose text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.current_patient_authority('consent') s
 LEFT JOIN clinic_app.identity_patientdelegategrant d ON d.id=s.delegate_grant_id
 WHERE s.delegate_grant_id IS NULL OR requested_purpose=ANY(d.purposes))
$f$;
CREATE FUNCTION clinic_app.patient_is_delegate()
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT coalesce((SELECT delegate_grant_id IS NOT NULL
 FROM clinic_app.patient_identity(
   NULLIF(current_setting('app.current_patient_session',true),'')::uuid)),false)
$f$;
REVOKE ALL ON FUNCTION clinic_app.patient_authority(uuid,text),
 clinic_app.current_patient_authority(text),
 clinic_app.switch_patient_delegate(uuid,uuid),
 clinic_app.delegate_session_status(uuid),clinic_app.delegate_recovery_identity(),
 clinic_app.renew_delegate_grant(uuid,uuid,timestamptz),
 clinic_app.delegate_session_guard(),clinic_app.delegate_purpose_allowed(text),
 clinic_app.patient_is_delegate() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.current_patient_authority(text),
 clinic_app.delegate_purpose_allowed(text),clinic_app.patient_is_delegate()
 TO clinic_app,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.switch_patient_delegate(uuid,uuid),
 clinic_app.delegate_session_status(uuid),clinic_app.delegate_recovery_identity(),
 clinic_app.renew_delegate_grant(uuid,uuid,timestamptz)
 TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.delegate_session_guard() TO clinic_owner;
RESET ROLE;
CREATE TRIGGER delegate_session_binding BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.intake_patientsession FOR EACH ROW
 EXECUTE FUNCTION clinic_app.delegate_session_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.delegate_session_guard() FROM clinic_owner;
RESET ROLE;
CREATE POLICY delegate_confidentiality ON clinic_app.intake_questionnaireresponse
 AS RESTRICTIVE TO clinic_app
 USING (NOT clinic_app.patient_is_delegate() OR NOT restricted_from_delegates)
 WITH CHECK (NOT clinic_app.patient_is_delegate() OR NOT restricted_from_delegates);
"""

SQL += """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.delegate_audit_scope(record_id uuid,event_name text)
RETURNS TABLE(session_id uuid,organization_id uuid,clinic_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT s.id,s.organization_id,s.clinic_id FROM clinic_app.patient_identity(
 NULLIF(current_setting('app.current_patient_session',true),'')::uuid) s
 WHERE NULLIF(current_setting('app.current_user_id',true),'') IS NULL AND
 ((event_name='identity.delegate.session_switched' AND record_id=s.id)
 OR EXISTS (SELECT 1 FROM clinic_app.identity_patientdelegategrant d
 WHERE d.id=record_id AND d.clinic_id=s.clinic_id
   AND d.organization_id=s.organization_id AND (
   (event_name='identity.delegate.requested' AND d.state='pending_verification'
     AND d.grantor_session_id=s.id)
   OR (d.represented_enrollment_id=s.enrollment_id AND
     ((event_name='identity.delegate.activated' AND d.state='active')
      OR (event_name='identity.delegate.revoked' AND d.state='revoked'))))))
$f$;
REVOKE ALL ON FUNCTION clinic_app.delegate_audit_scope(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.delegate_audit_scope(uuid,text)
 TO clinic_app,clinic_owner;
RESET ROLE;
CREATE FUNCTION clinic_app.delegate_audit(record_id uuid,event_name text,
 occurred_at_utc timestamptz,content_hash bytea)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s RECORD; previous_hash bytea; inserted_seq bigint; verb text;
BEGIN
 SELECT * INTO s FROM clinic_app.delegate_audit_scope(record_id,event_name);
 IF s.session_id IS NULL THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501'; END IF;
 IF current_setting('transaction_isolation')<>'read committed'
 OR content_hash IS NULL OR octet_length(content_hash)<>32
 OR occurred_at_utc IS NULL
 OR occurred_at_utc NOT BETWEEN clock_timestamp()-interval '5 minutes'
   AND clock_timestamp()+interval '5 minutes' THEN
   RAISE EXCEPTION 'invalid audit input' USING ERRCODE='22023'; END IF;
 verb:=substring(event_name from length('identity.delegate.')+1);
 PERFORM pg_advisory_xact_lock(hashtextextended(
   ('clinic-audit:'||s.organization_id::text) COLLATE pg_catalog."C",0));
 SELECT a.curr_hash INTO previous_hash FROM clinic_app.audit_event a
 WHERE a.organization_id=s.organization_id ORDER BY a.seq DESC LIMIT 1;
 previous_hash:=coalesce(previous_hash,decode(repeat('00',32),'hex'));
 INSERT INTO clinic_app.audit_event
 (organization_id,actor_user_id,event_type,component_id,component_ip,
  affected_record_type,affected_record_id,occurred_at_utc,payload,prev_hash,curr_hash)
 VALUES (s.organization_id,s.session_id,event_name,'clinic-os-web',NULL,
 CASE WHEN verb='session_switched' THEN 'intake.patient_session'
 ELSE 'identity.patient_delegate_grant' END,
 record_id::text,occurred_at_utc,
 jsonb_build_object('clinic_id',s.clinic_id::text,'object_verb',verb),
 previous_hash,clinic_app.digest(content_hash||previous_hash,'sha256'))
 RETURNING seq INTO inserted_seq;
 RETURN inserted_seq;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.delegate_audit(uuid,text,timestamptz,bytea)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.delegate_audit(uuid,text,timestamptz,bytea)
 TO clinic_app;
"""
