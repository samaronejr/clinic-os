"""Representation authority: fixed resolvers, immutable scope and exact grants."""

SQL = """
ALTER TABLE clinic_app.identity_patientdelegategrant
 ADD CONSTRAINT delegate_clinic_fk FOREIGN KEY (organization_id,clinic_id)
 REFERENCES clinic_app.identity_clinic(organization_id,id),
 ADD CONSTRAINT delegate_represented_fk
 FOREIGN KEY (organization_id,clinic_id,represented_enrollment_id)
 REFERENCES clinic_app.intake_patientclinicenrollment(organization_id,clinic_id,id),
 ADD CONSTRAINT delegate_recipient_fk
 FOREIGN KEY (organization_id,clinic_id,delegate_enrollment_id)
 REFERENCES clinic_app.intake_patientclinicenrollment(organization_id,clinic_id,id),
 ADD CONSTRAINT delegate_operations_check CHECK (
   coalesce(cardinality(operations) BETWEEN 1 AND 7
   AND array_position(operations,NULL) IS NULL
   AND operations::text[] <@ ARRAY['enrollment_view','questionnaires','booking',
       'records','consent','teleconsult','billing'],false)),
 ADD CONSTRAINT delegate_purposes_check CHECK (
   coalesce(cardinality(purposes) BETWEEN 1 AND 7
   AND array_position(purposes,NULL) IS NULL
   AND purposes::text[] <@ ARRAY['care_processing','teleconsultation',
       'consultation_recording','ai_assistance','transactional_messaging',
       'marketing','research_model_improvement'],false));
REVOKE ALL ON clinic_app.identity_patientdelegategrant
 FROM PUBLIC,clinic_app,clinic_resolver;
GRANT SELECT ON clinic_app.identity_patientdelegategrant TO clinic_app;
GRANT SELECT,INSERT ON clinic_app.identity_patientdelegategrant TO clinic_resolver;
GRANT UPDATE (state,revision,verified_by_id,verified_at,activated_at,revoked_at)
 ON clinic_app.identity_patientdelegategrant TO clinic_resolver;
ALTER TABLE clinic_app.identity_patientdelegategrant ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_patientdelegategrant FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.identity_patientdelegategrant TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (
 organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid);

SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.delegate_now()
RETURNS timestamptz LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT statement_timestamp()
$f$;
CREATE FUNCTION clinic_app.patient_identity(requested_session uuid)
RETURNS SETOF clinic_app.intake_patientsession
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT s.* FROM clinic_app.intake_patientsession s
 JOIN clinic_app.intake_patientaccessgrant g ON g.id=s.grant_id
   AND g.organization_id=s.organization_id AND g.clinic_id=s.clinic_id
   AND g.patient_id=s.patient_id AND g.enrollment_id=s.enrollment_id
 JOIN clinic_app.intake_patientclinicenrollment e ON e.id=s.enrollment_id
   AND e.organization_id=s.organization_id AND e.clinic_id=s.clinic_id
   AND e.patient_id=s.patient_id
 WHERE s.id=requested_session AND s.revoked_at IS NULL AND g.revoked_at IS NULL
   AND s.expires_at>statement_timestamp()
   AND s.idle_expires_at>statement_timestamp()
   AND s.operations::text[] <@ g.operations::text[]
$f$;
CREATE FUNCTION clinic_app.delegate_staff(requested_clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.has_permission('configuration.clinic',requested_clinic,NULL)
 OR clinic_app.has_permission('configuration.organization',requested_clinic,NULL)
$f$;
CREATE FUNCTION clinic_app.delegate_grantor(requested_clinic uuid,
 requested_enrollment uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT NULLIF(current_setting('app.current_user_id',true),'') IS NULL
 AND EXISTS (SELECT 1 FROM clinic_app.patient_identity(
   NULLIF(current_setting('app.current_patient_session',true),'')::uuid) s
 WHERE s.clinic_id=requested_clinic AND s.enrollment_id=requested_enrollment)
$f$;
CREATE FUNCTION clinic_app.delegate_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE staff boolean; actor uuid; evidence RECORD; renewal boolean;
BEGIN
 IF TG_OP='DELETE' THEN
   RAISE EXCEPTION 'delegate history is immutable' USING ERRCODE='23514';
 END IF;
 staff:=clinic_app.delegate_staff(NEW.clinic_id);
 actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 renewal:=TG_OP='INSERT' AND NEW.renewal_of_id IS NOT NULL AND actor IS NULL
 AND EXISTS (SELECT 1 FROM clinic_app.identity_patientdelegategrant d
 WHERE d.id=NEW.renewal_of_id AND d.state='active'
   AND clinic_app.delegate_grantor(d.clinic_id,d.delegate_enrollment_id)
   AND d.expires_at<=clinic_app.delegate_now()
   AND (d.organization_id,d.clinic_id,d.represented_enrollment_id,
        d.delegate_enrollment_id,d.authority_kind,d.evidence_attachment_id,
        d.operations,d.purposes)
   IS NOT DISTINCT FROM
       (NEW.organization_id,NEW.clinic_id,NEW.represented_enrollment_id,
        NEW.delegate_enrollment_id,NEW.authority_kind,NEW.evidence_attachment_id,
        NEW.operations,NEW.purposes));
 IF NOT (staff OR clinic_app.delegate_grantor(
       NEW.clinic_id,NEW.represented_enrollment_id) OR renewal) THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501';
 END IF;
 SELECT a.* INTO evidence FROM clinic_app.ehr_clinicalattachment a
 JOIN clinic_app.intake_patientclinicenrollment e
   ON e.id=NEW.represented_enrollment_id AND e.patient_id=a.patient_id
   AND e.organization_id=a.organization_id AND e.clinic_id=a.clinic_id
 WHERE a.id=NEW.evidence_attachment_id AND a.clinic_id=NEW.clinic_id
   AND a.organization_id=NEW.organization_id AND
     (a.state<>'rejected' OR (TG_OP='UPDATE' AND NEW.state='revoked'));
 IF evidence.id IS NULL THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501';
 END IF;
 IF TG_OP='INSERT' THEN
   IF NEW.state<>'pending_verification' OR NEW.revision<>1
      OR NEW.verified_by_id IS NOT NULL OR NEW.verified_at IS NOT NULL
      OR NEW.activated_at IS NOT NULL OR NEW.revoked_at IS NOT NULL
      OR (staff AND (NEW.requested_by_id IS DISTINCT FROM actor
          OR NEW.grantor_session_id IS NOT NULL))
      OR (NOT staff AND (NEW.requested_by_id IS NOT NULL
          OR NEW.grantor_session_id IS DISTINCT FROM
          NULLIF(current_setting('app.current_patient_session',true),'')::uuid))
   THEN RAISE EXCEPTION 'invalid delegate request' USING ERRCODE='23514'; END IF;
   NEW.created_at:=statement_timestamp();
 ELSE
   IF (NEW.id,NEW.organization_id,NEW.clinic_id,NEW.represented_enrollment_id,
       NEW.delegate_enrollment_id,NEW.authority_kind,NEW.evidence_attachment_id,
       NEW.purposes,NEW.operations,NEW.expires_at,NEW.created_at,
       NEW.idempotency_key,NEW.requested_by_id,NEW.grantor_session_id,NEW.renewal_of_id)
      IS DISTINCT FROM
      (OLD.id,OLD.organization_id,OLD.clinic_id,OLD.represented_enrollment_id,
       OLD.delegate_enrollment_id,OLD.authority_kind,OLD.evidence_attachment_id,
       OLD.purposes,OLD.operations,OLD.expires_at,OLD.created_at,
       OLD.idempotency_key,OLD.requested_by_id,OLD.grantor_session_id,OLD.renewal_of_id)
      OR NEW.revision<>OLD.revision+1 THEN
     RAISE EXCEPTION 'immutable delegate scope' USING ERRCODE='23514';
   END IF;
   IF OLD.state='pending_verification' AND NEW.state IN ('verified','rejected') THEN
     IF NOT staff OR NEW.verified_by_id IS DISTINCT FROM actor
        OR NEW.expires_at<=clinic_app.delegate_now()
        OR (NEW.state='verified' AND evidence.state<>'available')
        OR NEW.activated_at IS NOT NULL OR NEW.revoked_at IS NOT NULL THEN
       RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501'; END IF;
     NEW.verified_at:=statement_timestamp();
   ELSIF OLD.state='verified' AND NEW.state='active' THEN
     IF NEW.expires_at<=clinic_app.delegate_now()
        OR NEW.verified_by_id IS DISTINCT FROM OLD.verified_by_id
        OR NEW.verified_at IS DISTINCT FROM OLD.verified_at
        OR NEW.revoked_at IS NOT NULL THEN
       RAISE EXCEPTION 'invalid delegate activation' USING ERRCODE='23514'; END IF;
     NEW.activated_at:=statement_timestamp();
   ELSIF OLD.state IN ('pending_verification','verified','active')
         AND NEW.state='revoked' THEN
     IF (NEW.verified_by_id,NEW.verified_at,NEW.activated_at)
       IS DISTINCT FROM (OLD.verified_by_id,OLD.verified_at,OLD.activated_at) THEN
       RAISE EXCEPTION 'immutable delegate verification' USING ERRCODE='23514';
     END IF;
     NEW.revoked_at:=statement_timestamp();
   ELSE
     RAISE EXCEPTION 'invalid delegate transition' USING ERRCODE='23514';
   END IF;
 END IF;
 RETURN NEW;
END
$f$;
CREATE FUNCTION clinic_app.request_delegate_grant(requested_clinic uuid,
 represented uuid, recipient uuid, kind text, evidence uuid,
 requested_purposes text[], requested_operations text[], deadline timestamptz,
 request_key uuid)
RETURNS TABLE(grant_id uuid,revision integer,state text,changed boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE org uuid; prior clinic_app.identity_patientdelegategrant; created uuid;
BEGIN
 IF NOT (clinic_app.delegate_staff(requested_clinic)
    OR clinic_app.delegate_grantor(requested_clinic,represented)) THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501'; END IF;
 SELECT e.organization_id INTO org FROM clinic_app.intake_patientclinicenrollment e
 JOIN clinic_app.intake_patientclinicenrollment d
   ON d.organization_id=e.organization_id AND d.clinic_id=e.clinic_id
 WHERE e.id=represented AND d.id=recipient AND e.clinic_id=requested_clinic
   AND e.patient_id<>d.patient_id;
 IF org IS NULL THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
   'delegate-request:'||requested_clinic||':'||request_key,0));
 SELECT * INTO prior FROM clinic_app.identity_patientdelegategrant d
 WHERE d.clinic_id=requested_clinic AND d.idempotency_key=request_key;
 IF prior.id IS NOT NULL THEN
   IF (prior.represented_enrollment_id,prior.delegate_enrollment_id,
       prior.authority_kind,prior.evidence_attachment_id,prior.purposes::text[],
       prior.operations::text[],prior.expires_at)
      IS DISTINCT FROM (represented,recipient,kind,evidence,requested_purposes,
                        requested_operations,deadline) THEN
     RAISE EXCEPTION 'delegate conflict' USING ERRCODE='40001'; END IF;
   RETURN QUERY SELECT prior.id,prior.revision,prior.state::text,false;
   RETURN;
 END IF;
 created:=gen_random_uuid();
 INSERT INTO clinic_app.identity_patientdelegategrant
 (id,organization_id,clinic_id,represented_enrollment_id,delegate_enrollment_id,
  authority_kind,evidence_attachment_id,purposes,operations,expires_at,
  state,revision,idempotency_key,requested_by_id,grantor_session_id,created_at)
 VALUES (created,org,requested_clinic,represented,recipient,kind,evidence,
   requested_purposes,requested_operations,deadline,'pending_verification',1,
   request_key,NULLIF(current_setting('app.current_user_id',true),'')::uuid,
   CASE WHEN NULLIF(current_setting('app.current_user_id',true),'') IS NULL
     THEN NULLIF(current_setting('app.current_patient_session',true),'')::uuid END,
   statement_timestamp());
 RETURN QUERY SELECT created,1,'pending_verification'::text,true;
END
$f$;
CREATE FUNCTION clinic_app.transition_delegate_grant(requested_clinic uuid,
 requested_grant uuid, expected_revision integer, next_state text)
RETURNS TABLE(grant_id uuid,revision integer,state text,changed boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE d clinic_app.identity_patientdelegategrant;
BEGIN
 SELECT * INTO d FROM clinic_app.identity_patientdelegategrant g
 WHERE g.id=requested_grant AND g.clinic_id=requested_clinic FOR UPDATE;
 IF d.id IS NULL OR NOT (clinic_app.delegate_staff(requested_clinic)
    OR (next_state IN ('active','revoked') AND clinic_app.delegate_grantor(
        requested_clinic,d.represented_enrollment_id))) THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501'; END IF;
 IF d.state=next_state THEN
   RETURN QUERY SELECT d.id,d.revision,d.state::text,false; RETURN; END IF;
 IF d.revision IS DISTINCT FROM expected_revision THEN
   RAISE EXCEPTION 'delegate conflict' USING ERRCODE='40001'; END IF;
 UPDATE clinic_app.identity_patientdelegategrant g SET
   state=next_state,revision=g.revision+1,
   verified_by_id=CASE WHEN next_state IN ('verified','rejected')
     THEN NULLIF(current_setting('app.current_user_id',true),'')::uuid
     ELSE g.verified_by_id END
 WHERE g.id=d.id;
 SELECT * INTO d FROM clinic_app.identity_patientdelegategrant g WHERE g.id=d.id;
 RETURN QUERY SELECT d.id,d.revision,d.state::text,true;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.patient_identity(uuid),
 clinic_app.delegate_now(),
 clinic_app.delegate_staff(uuid),clinic_app.delegate_grantor(uuid,uuid),
 clinic_app.delegate_guard(),
 clinic_app.request_delegate_grant(uuid,uuid,uuid,text,uuid,text[],text[],
   timestamptz,uuid),
 clinic_app.transition_delegate_grant(uuid,uuid,integer,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.delegate_staff(uuid),
 clinic_app.delegate_grantor(uuid,uuid) TO clinic_app,clinic_owner;
GRANT EXECUTE ON FUNCTION
 clinic_app.request_delegate_grant(uuid,uuid,uuid,text,uuid,text[],text[],
   timestamptz,uuid),
 clinic_app.transition_delegate_grant(uuid,uuid,integer,text) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.delegate_guard() TO clinic_owner;
RESET ROLE;
CREATE TRIGGER delegate_binding BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.identity_patientdelegategrant FOR EACH ROW
 EXECUTE FUNCTION clinic_app.delegate_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.delegate_guard() FROM clinic_owner;
RESET ROLE;
CREATE POLICY delegate_read ON clinic_app.identity_patientdelegategrant
 FOR SELECT TO clinic_app USING (
 clinic_app.delegate_staff(clinic_id)
 OR clinic_app.delegate_grantor(clinic_id,represented_enrollment_id)
 OR clinic_app.delegate_grantor(clinic_id,delegate_enrollment_id));
"""
