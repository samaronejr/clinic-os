"""LGPD data-subject requests: session-bound writes, staff answers and SLA tasks."""

from apps.retention.migrations._purge_task_sql import SQL as RETENTION_TASK_SQL

TABLE = "intake_datasubjectrequest"

POSTURE_SQL = """
ALTER TABLE clinic_app.intake_datasubjectrequest
 ADD CONSTRAINT data_request_clinic_fk FOREIGN KEY (organization_id,clinic_id)
 REFERENCES clinic_app.identity_clinic(organization_id,id),
 ADD CONSTRAINT data_request_subject_fk
 FOREIGN KEY (organization_id,clinic_id,enrollment_id)
 REFERENCES clinic_app.intake_patientclinicenrollment(organization_id,clinic_id,id),
 ADD CONSTRAINT data_request_requester_fk
 FOREIGN KEY (organization_id,clinic_id,requester_enrollment_id)
 REFERENCES clinic_app.intake_patientclinicenrollment(organization_id,clinic_id,id),
 ADD CONSTRAINT data_request_session_fk
 FOREIGN KEY (organization_id,clinic_id,patient_session_id)
 REFERENCES clinic_app.intake_patientsession(organization_id,clinic_id,id),
 ADD CONSTRAINT data_request_task_scope_fk
 FOREIGN KEY (organization_id,clinic_id,task_id)
 REFERENCES clinic_app.workflows_task(organization_id,clinic_id,id)
 DEFERRABLE INITIALLY DEFERRED;
REVOKE ALL ON clinic_app.intake_datasubjectrequest
 FROM PUBLIC,clinic_app,clinic_resolver;
GRANT SELECT ON clinic_app.intake_datasubjectrequest TO clinic_app;
GRANT SELECT,INSERT ON clinic_app.intake_datasubjectrequest TO clinic_resolver;
GRANT UPDATE (state,rejection_reason,closed_at,revision,updated_by_id,updated_at)
 ON clinic_app.intake_datasubjectrequest TO clinic_resolver;
GRANT UPDATE (state,completion_evidence,revision,updated_at)
 ON clinic_app.workflows_task TO clinic_resolver;
ALTER TABLE clinic_app.intake_datasubjectrequest ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.intake_datasubjectrequest FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.intake_datasubjectrequest TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (
 organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY privacy_staff_read ON clinic_app.intake_datasubjectrequest
 FOR SELECT TO clinic_app USING (
 organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.has_permission('privacy.requests',clinic_id,NULL));
"""

FUNCTIONS_SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.data_request_now()
RETURNS timestamptz LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT statement_timestamp()
$f$;
CREATE FUNCTION clinic_app.data_request_withdrawn(requested_session uuid,
 requested_enrollment uuid,requested_purpose text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.consent_consentrevocation r
   JOIN clinic_app.consent_consentacceptance a ON a.id=r.acceptance_id
   JOIN clinic_app.consent_consenttext t ON t.id=a.text_id
   WHERE r.patient_session_id=requested_session
     AND a.enrollment_id=requested_enrollment AND t.purpose=requested_purpose)
 AND NOT EXISTS (SELECT 1 FROM clinic_app.consent_consentacceptance a
   JOIN clinic_app.consent_consenttext t ON t.id=a.text_id
   WHERE a.enrollment_id=requested_enrollment AND t.purpose=requested_purpose
     AND NOT EXISTS (SELECT 1 FROM clinic_app.consent_consentrevocation r
       WHERE r.acceptance_id=a.id))
$f$;
CREATE FUNCTION clinic_app.data_request_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE actor uuid;
BEGIN
 IF TG_OP='DELETE' THEN
   RAISE EXCEPTION 'data subject requests are retained' USING ERRCODE='23514';
 END IF;
 IF TG_OP='INSERT' THEN
   IF NEW.state<>'received' OR NEW.revision<>1 OR NEW.closed_at IS NOT NULL
      OR NEW.rejection_reason<>'' OR NEW.updated_by_id IS NOT NULL
      OR NEW.due_at<>NEW.received_at+interval '15 days'
      OR NOT EXISTS (SELECT 1 FROM clinic_app.current_patient_authority(NULL) s
        JOIN clinic_app.patient_identity(s.session_id) p ON p.id=s.session_id
        WHERE s.session_id=NEW.patient_session_id
          AND s.organization_id=NEW.organization_id AND s.clinic_id=NEW.clinic_id
          AND s.patient_id=NEW.patient_id AND s.enrollment_id=NEW.enrollment_id
          AND p.enrollment_id=NEW.requester_enrollment_id
          AND s.delegate_grant_id IS NOT DISTINCT FROM NEW.delegate_grant_id) THEN
     RAISE EXCEPTION 'invalid data subject request' USING ERRCODE='23514';
   END IF;
   RETURN NEW;
 END IF;
 IF (NEW.id,NEW.organization_id,NEW.clinic_id,NEW.patient_id,NEW.enrollment_id,
     NEW.requester_enrollment_id,NEW.patient_session_id,NEW.delegate_grant_id,
     NEW.kind,NEW.purpose,NEW.details,NEW.received_at,NEW.due_at,NEW.task_id,
     NEW.idempotency_key,NEW.fingerprint)
    IS DISTINCT FROM
    (OLD.id,OLD.organization_id,OLD.clinic_id,OLD.patient_id,OLD.enrollment_id,
     OLD.requester_enrollment_id,OLD.patient_session_id,OLD.delegate_grant_id,
     OLD.kind,OLD.purpose,OLD.details,OLD.received_at,OLD.due_at,OLD.task_id,
     OLD.idempotency_key,OLD.fingerprint)
    OR NEW.revision<>OLD.revision+1 THEN
   RAISE EXCEPTION 'immutable data subject request' USING ERRCODE='23514';
 END IF;
 actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 IF actor IS NULL AND OLD.kind='objection' AND OLD.state='received'
    AND NEW.state='fulfilled' AND NEW.updated_by_id IS NULL
    AND NEW.rejection_reason='' THEN
   IF NOT EXISTS (SELECT 1 FROM clinic_app.current_patient_authority('consent') s
       WHERE s.session_id=OLD.patient_session_id
         AND s.enrollment_id=OLD.enrollment_id)
      OR NOT clinic_app.data_request_withdrawn(
        OLD.patient_session_id,OLD.enrollment_id,OLD.purpose) THEN
     RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501';
   END IF;
 ELSIF actor IS NOT NULL AND NEW.updated_by_id=actor
    AND clinic_app.has_permission('privacy.requests',NEW.clinic_id,NULL)
    AND ((OLD.state='received' AND NEW.state='in_progress'
          AND NEW.rejection_reason='')
      OR (OLD.state='in_progress' AND NEW.state='fulfilled'
          AND NEW.rejection_reason='')
      OR (OLD.state IN ('received','in_progress') AND NEW.state='rejected')) THEN
   NULL;
 ELSE
   RAISE EXCEPTION 'invalid data subject request transition' USING ERRCODE='23514';
 END IF;
 NEW.updated_at:=clinic_app.data_request_now();
 NEW.closed_at:=CASE WHEN NEW.state IN ('fulfilled','rejected')
   THEN NEW.updated_at END;
 RETURN NEW;
END
$f$;
CREATE FUNCTION clinic_app.data_request_task_valid(row_data jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT pg_trigger_depth()>1 AND EXISTS(
 SELECT 1 FROM clinic_app.intake_datasubjectrequest r
 WHERE r.task_id=(row_data->>'id')::uuid AND r.revision=1 AND r.state='received'
 AND r.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND (row_data-'id'-'created_at'-'updated_at')=jsonb_build_object(
  'organization_id',r.organization_id,'clinic_id',r.clinic_id,'kind','review',
  'subject_ref',jsonb_build_object('kind','enrollment','id',r.enrollment_id::text),
  'created_by_id',NULL,'owner_user_id',NULL,'owner_role','clinic_manager',
  'due_at',r.due_at,'priority','normal','state','open',
  'escalation_policy_version',1,'escalated_at',NULL,'depends_on_id',NULL,
  'completion_evidence','{}'::jsonb,'revision',1,'idempotency_key',r.task_id,
  'fingerprint',encode(clinic_app.digest(
   'intake-data-request-task:'||r.id::text,'sha256'),'hex'),
  'last_command_key',NULL,'last_command_digest',''))
$f$;
CREATE FUNCTION clinic_app.data_request_task_close_valid(old_row jsonb,
 new_row jsonb)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT pg_trigger_depth()>1 AND EXISTS(
 SELECT 1 FROM clinic_app.intake_datasubjectrequest r
 WHERE r.task_id=(new_row->>'id')::uuid AND r.state IN ('fulfilled','rejected')
 AND r.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND old_row->>'state' NOT IN ('done','cancelled') AND new_row->>'state'='done'
 AND (new_row->>'revision')::bigint=(old_row->>'revision')::bigint+1
 AND new_row->'completion_evidence'=jsonb_build_object('record',
  jsonb_build_object('kind','enrollment','id',r.enrollment_id::text),
  'outcome','resolved')
 AND (new_row-'state'-'completion_evidence'-'revision'-'updated_at')
  =(old_row-'state'-'completion_evidence'-'revision'-'updated_at'))
$f$;
CREATE FUNCTION clinic_app.data_request_close_task()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE saved_tenant text:=current_setting('app.current_tenant',true);
BEGIN
 IF NEW.state IN ('fulfilled','rejected')
    AND OLD.state IN ('received','in_progress') THEN
   PERFORM set_config('app.current_tenant',NEW.organization_id::text,true);
   UPDATE clinic_app.workflows_task t SET state='done',
     completion_evidence=jsonb_build_object('record',
       jsonb_build_object('kind','enrollment','id',NEW.enrollment_id::text),
       'outcome','resolved'),
     revision=t.revision+1,updated_at=statement_timestamp()
   WHERE t.id=NEW.task_id AND t.state NOT IN ('done','cancelled');
   PERFORM set_config('app.current_tenant',coalesce(saved_tenant,''),true);
 END IF;
 RETURN NEW;
END
$f$;
CREATE FUNCTION clinic_app.data_request_emit_task()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE saved_tenant text:=current_setting('app.current_tenant',true);
BEGIN
 PERFORM set_config('app.current_tenant',NEW.organization_id::text,true);
 INSERT INTO clinic_app.workflows_task
 (id,organization_id,clinic_id,kind,subject_ref,created_by_id,owner_user_id,
  owner_role,due_at,priority,state,escalation_policy_version,escalated_at,
  depends_on_id,completion_evidence,revision,idempotency_key,fingerprint,
  last_command_key,last_command_digest,created_at,updated_at)
 VALUES(NEW.task_id,NEW.organization_id,NEW.clinic_id,'review',
  jsonb_build_object('kind','enrollment','id',NEW.enrollment_id::text),NULL,NULL,
  'clinic_manager',NEW.due_at,'normal','open',1,NULL,NULL,'{}',1,NEW.task_id,
  encode(clinic_app.digest('intake-data-request-task:'||NEW.id::text,'sha256'),
   'hex'),NULL,'',statement_timestamp(),statement_timestamp());
 PERFORM set_config('app.current_tenant',coalesce(saved_tenant,''),true);
 RETURN NEW;
END
$f$;
CREATE FUNCTION clinic_app.submit_data_request(requested_clinic uuid,
 requested_kind text,requested_purpose text,details_envelope bytea,
 request_key uuid,request_fingerprints text[],withdrawn uuid[])
RETURNS TABLE(request_id uuid,kind text,state text,revision integer,
 received_at timestamptz,due_at timestamptz,task_id uuid,delegate_acted boolean,
 changed boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s RECORD; requester uuid; required_operation text; received timestamptz;
 prior clinic_app.intake_datasubjectrequest; created uuid;
 result clinic_app.intake_datasubjectrequest;
BEGIN
 required_operation:=CASE requested_kind
   WHEN 'access' THEN 'records' WHEN 'portability' THEN 'records'
   WHEN 'objection' THEN 'consent' WHEN 'correction' THEN 'enrollment_view'
   WHEN 'sharing_information' THEN 'enrollment_view' END;
 IF required_operation IS NULL OR request_key IS NULL
    OR coalesce(cardinality(request_fingerprints),0)=0
    OR array_position(request_fingerprints,NULL) IS NOT NULL
    OR EXISTS (SELECT 1 FROM unnest(request_fingerprints) f(v)
      WHERE f.v !~ '^[0-9a-f]{64}$')
    OR (requested_kind='objection')<>(coalesce(requested_purpose,'')<>'')
    OR (requested_kind='correction')<>(details_envelope IS NOT NULL)
    OR (requested_kind<>'objection' AND coalesce(cardinality(withdrawn),0)>0)
    OR array_position(withdrawn,NULL) IS NOT NULL THEN
   RAISE EXCEPTION 'invalid data subject request' USING ERRCODE='22023';
 END IF;
 SELECT * INTO s FROM clinic_app.current_patient_authority(required_operation);
 IF s.session_id IS NULL OR s.clinic_id IS DISTINCT FROM requested_clinic
    OR (requested_kind='objection'
        AND NOT clinic_app.delegate_purpose_allowed(requested_purpose)) THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501';
 END IF;
 SELECT p.enrollment_id INTO requester
 FROM clinic_app.patient_identity(s.session_id) p;
 PERFORM pg_advisory_xact_lock(hashtextextended(
   'intake-data-request:'||requested_clinic::text||':'||request_key::text,0));
 SELECT * INTO prior FROM clinic_app.intake_datasubjectrequest r
 WHERE r.clinic_id=requested_clinic AND r.idempotency_key=request_key;
 IF prior.id IS NOT NULL THEN
   IF prior.kind<>requested_kind OR prior.enrollment_id<>s.enrollment_id
      OR prior.requester_enrollment_id<>requester
      OR prior.delegate_grant_id IS DISTINCT FROM s.delegate_grant_id
      OR NOT prior.fingerprint=ANY(request_fingerprints) THEN
     RAISE EXCEPTION 'data subject request conflict' USING ERRCODE='40001';
   END IF;
   RETURN QUERY SELECT prior.id,prior.kind::text,prior.state::text,prior.revision,
     prior.received_at,prior.due_at,prior.task_id,
     prior.delegate_grant_id IS NOT NULL,false;
   RETURN;
 END IF;
 IF requested_kind='objection' AND EXISTS (SELECT 1 FROM unnest(withdrawn) w(id)
   WHERE NOT EXISTS (SELECT 1 FROM clinic_app.consent_consentrevocation r
     JOIN clinic_app.consent_consentacceptance a ON a.id=r.acceptance_id
     JOIN clinic_app.consent_consenttext t ON t.id=a.text_id
     WHERE r.id=w.id AND r.patient_session_id=s.session_id
       AND a.enrollment_id=s.enrollment_id AND t.purpose=requested_purpose)) THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501';
 END IF;
 received:=clinic_app.data_request_now();
 created:=gen_random_uuid();
 INSERT INTO clinic_app.intake_datasubjectrequest
 (id,organization_id,clinic_id,patient_id,enrollment_id,requester_enrollment_id,
  patient_session_id,delegate_grant_id,kind,purpose,details,state,
  rejection_reason,received_at,due_at,closed_at,task_id,revision,
  idempotency_key,fingerprint,updated_by_id,updated_at)
 VALUES(created,s.organization_id,s.clinic_id,s.patient_id,s.enrollment_id,
  requester,s.session_id,s.delegate_grant_id,requested_kind,
  coalesce(requested_purpose,''),details_envelope,'received','',received,
  received+interval '15 days',NULL,gen_random_uuid(),1,request_key,
  request_fingerprints[cardinality(request_fingerprints)],NULL,received);
 IF requested_kind='objection' AND coalesce(cardinality(withdrawn),0)>0 THEN
   UPDATE clinic_app.intake_datasubjectrequest r SET state='fulfilled',
     revision=r.revision+1 WHERE r.id=created;
 END IF;
 SELECT * INTO result FROM clinic_app.intake_datasubjectrequest r
 WHERE r.id=created;
 RETURN QUERY SELECT result.id,result.kind::text,result.state::text,
   result.revision,result.received_at,result.due_at,result.task_id,
   result.delegate_grant_id IS NOT NULL,true;
END
$f$;
CREATE FUNCTION clinic_app.patient_data_requests()
RETURNS TABLE(request_id uuid,kind text,purpose text,state text,
 rejection_reason text,received_at timestamptz,due_at timestamptz,
 closed_at timestamptz,delegate_acted boolean)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT r.id,r.kind::text,r.purpose::text,r.state::text,r.rejection_reason::text,
   r.received_at,r.due_at,r.closed_at,r.delegate_grant_id IS NOT NULL
 FROM clinic_app.current_patient_authority(NULL) s
 JOIN clinic_app.intake_datasubjectrequest r
   ON r.organization_id=s.organization_id AND r.clinic_id=s.clinic_id
   AND r.enrollment_id=s.enrollment_id
 WHERE s.delegate_grant_id IS NULL OR r.delegate_grant_id=s.delegate_grant_id
 ORDER BY r.received_at DESC,r.id
$f$;
CREATE FUNCTION clinic_app.transition_data_request(requested_clinic uuid,
 requested_request uuid,expected_revision integer,next_state text,reason text)
RETURNS TABLE(request_id uuid,state text,revision integer)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE actor uuid:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 current clinic_app.intake_datasubjectrequest;
BEGIN
 IF actor IS NULL
    OR NOT clinic_app.has_permission('privacy.requests',requested_clinic,NULL) THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501';
 END IF;
 SELECT * INTO current FROM clinic_app.intake_datasubjectrequest r
 WHERE r.id=requested_request AND r.clinic_id=requested_clinic
   AND r.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 FOR UPDATE;
 IF current.id IS NULL THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501';
 END IF;
 IF current.revision IS DISTINCT FROM expected_revision OR NOT (
    (current.state='received' AND next_state IN ('in_progress','rejected'))
    OR (current.state='in_progress' AND next_state IN ('fulfilled','rejected'))) THEN
   RAISE EXCEPTION 'data subject request conflict' USING ERRCODE='40001';
 END IF;
 UPDATE clinic_app.intake_datasubjectrequest r SET state=next_state,
   revision=r.revision+1,updated_by_id=actor,
   rejection_reason=CASE WHEN next_state='rejected' THEN coalesce(reason,'')
     ELSE '' END
 WHERE r.id=current.id;
 SELECT * INTO current FROM clinic_app.intake_datasubjectrequest r
 WHERE r.id=current.id;
 RETURN QUERY SELECT current.id,current.state::text,current.revision;
END
$f$;
CREATE FUNCTION clinic_app.data_request_audit_scope(record_id uuid,event_name text)
RETURNS TABLE(session_id uuid,organization_id uuid,clinic_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT s.session_id,s.organization_id,s.clinic_id
 FROM clinic_app.current_patient_authority(NULL) s
 JOIN clinic_app.intake_datasubjectrequest r ON r.id=record_id
   AND r.organization_id=s.organization_id AND r.clinic_id=s.clinic_id
   AND r.enrollment_id=s.enrollment_id
   AND (s.delegate_grant_id IS NULL OR r.delegate_grant_id=s.delegate_grant_id)
 WHERE (event_name='intake.data_request.received'
     AND r.patient_session_id=s.session_id)
  OR (event_name='intake.data_request.fulfilled'
     AND r.patient_session_id=s.session_id AND r.kind='objection'
     AND r.state='fulfilled' AND r.updated_by_id IS NULL)
  OR (event_name='intake.data_request.exported'
     AND r.kind IN ('access','portability') AND r.state<>'rejected'
     AND EXISTS (SELECT 1 FROM clinic_app.current_patient_authority('records') a
       WHERE a.session_id=s.session_id))
$f$;
REVOKE ALL ON FUNCTION clinic_app.data_request_now(),
 clinic_app.data_request_withdrawn(uuid,uuid,text),
 clinic_app.data_request_guard(),clinic_app.data_request_task_valid(jsonb),
 clinic_app.data_request_task_close_valid(jsonb,jsonb),
 clinic_app.data_request_close_task(),clinic_app.data_request_emit_task(),
 clinic_app.submit_data_request(uuid,text,text,bytea,uuid,text[],uuid[]),
 clinic_app.patient_data_requests(),
 clinic_app.transition_data_request(uuid,uuid,integer,text,text),
 clinic_app.data_request_audit_scope(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.data_request_now(),
 clinic_app.submit_data_request(uuid,text,text,bytea,uuid,text[],uuid[]),
 clinic_app.patient_data_requests(),
 clinic_app.transition_data_request(uuid,uuid,integer,text,text) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.data_request_audit_scope(uuid,text)
 TO clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.data_request_guard(),
 clinic_app.data_request_emit_task(),clinic_app.data_request_close_task()
 TO clinic_owner;
RESET ROLE;
CREATE TRIGGER data_request_binding BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.intake_datasubjectrequest FOR EACH ROW
 EXECUTE FUNCTION clinic_app.data_request_guard();
CREATE TRIGGER data_request_task AFTER INSERT
 ON clinic_app.intake_datasubjectrequest FOR EACH ROW
 EXECUTE FUNCTION clinic_app.data_request_emit_task();
CREATE TRIGGER data_request_task_close AFTER UPDATE
 ON clinic_app.intake_datasubjectrequest FOR EACH ROW
 EXECUTE FUNCTION clinic_app.data_request_close_task();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.data_request_guard(),
 clinic_app.data_request_emit_task(),clinic_app.data_request_close_task()
 FROM clinic_owner;
RESET ROLE;
CREATE FUNCTION clinic_app.data_request_audit(record_id uuid,event_name text,
 occurred_at_utc timestamptz,content_hash bytea)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s RECORD; previous_hash bytea; inserted_seq bigint;
BEGIN
 SELECT * INTO s FROM clinic_app.data_request_audit_scope(record_id,event_name);
 IF s.session_id IS NULL THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501';
 END IF;
 IF current_setting('transaction_isolation')<>'read committed'
    OR content_hash IS NULL OR octet_length(content_hash)<>32
    OR occurred_at_utc IS NULL
    OR occurred_at_utc NOT BETWEEN clock_timestamp()-interval '5 minutes'
      AND clock_timestamp()+interval '5 minutes' THEN
   RAISE EXCEPTION 'invalid audit input' USING ERRCODE='22023';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
   ('clinic-audit:'||s.organization_id::text) COLLATE pg_catalog."C",0));
 SELECT a.curr_hash INTO previous_hash FROM clinic_app.audit_event a
 WHERE a.organization_id=s.organization_id ORDER BY a.seq DESC LIMIT 1;
 previous_hash:=coalesce(previous_hash,decode(repeat('00',32),'hex'));
 INSERT INTO clinic_app.audit_event
 (organization_id,actor_user_id,event_type,component_id,component_ip,
  affected_record_type,affected_record_id,occurred_at_utc,payload,prev_hash,
  curr_hash)
 VALUES (s.organization_id,s.session_id,event_name,'clinic-os-web',NULL,
  'intake.data_subject_request',record_id::text,occurred_at_utc,
  jsonb_build_object('clinic_id',s.clinic_id::text,'object_verb',
   substring(event_name from length('intake.data_request.')+1)),
  previous_hash,clinic_app.digest(content_hash||previous_hash,'sha256'))
 RETURNING seq INTO inserted_seq;
 RETURN inserted_seq;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.data_request_audit(uuid,text,timestamptz,bytea)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
 clinic_app.data_request_audit(uuid,text,timestamptz,bytea) TO clinic_app;
"""

# The workflow guard is replaced as one unit; the newest definition (the
# retention purge bridge) is reused byte for byte plus one source-bound branch.
_GUARD_START = RETENTION_TASK_SQL.index(
    "CREATE OR REPLACE FUNCTION clinic_app.workflows_guard()"
)
_GUARD_END = RETENTION_TASK_SQL.index("END $f$;", _GUARD_START) + len("END $f$;")
_PRIOR_GUARD = RETENTION_TASK_SQL[_GUARD_START:_GUARD_END]
_ANCHOR = (
    " IF TG_TABLE_NAME='workflows_task' AND TG_OP='INSERT'\n"
    " AND clinic_app.retention_system_task_valid(to_jsonb(NEW))"
)
if _PRIOR_GUARD.count(_ANCHOR) != 1:  # pragma: no cover - import-time contract
    message = "prior workflow guard bridge moved"
    raise RuntimeError(message)
_BRIDGE = (
    " IF TG_TABLE_NAME='workflows_task' AND TG_OP='INSERT'\n"
    " AND clinic_app.data_request_task_valid(to_jsonb(NEW)) THEN RETURN NEW; END IF;\n"
    " IF TG_TABLE_NAME='workflows_task' AND TG_OP='UPDATE'\n"
    " AND clinic_app.data_request_task_close_valid(to_jsonb(OLD),to_jsonb(NEW))\n"
    " THEN RETURN NEW; END IF;\n"
)
GUARD_SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _PRIOR_GUARD.replace(_ANCHOR, _BRIDGE + _ANCHOR, 1)
    + "\nRESET ROLE;\n"
)
PRIOR_GUARD_SQL = "SET LOCAL ROLE clinic_resolver;\n" + _PRIOR_GUARD + "\nRESET ROLE;\n"

SQL = POSTURE_SQL + FUNCTIONS_SQL + GUARD_SQL

REVERSE_SQL = (
    PRIOR_GUARD_SQL
    + """
DROP TRIGGER data_request_task_close ON clinic_app.intake_datasubjectrequest;
DROP TRIGGER data_request_task ON clinic_app.intake_datasubjectrequest;
DROP TRIGGER data_request_binding ON clinic_app.intake_datasubjectrequest;
REVOKE UPDATE (state,completion_evidence,revision,updated_at)
 ON clinic_app.workflows_task FROM clinic_resolver;
DROP FUNCTION clinic_app.data_request_audit(uuid,text,timestamptz,bytea);
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.data_request_audit_scope(uuid,text);
DROP FUNCTION clinic_app.transition_data_request(uuid,uuid,integer,text,text);
DROP FUNCTION clinic_app.patient_data_requests();
DROP FUNCTION clinic_app.submit_data_request(uuid,text,text,bytea,uuid,text[],uuid[]);
DROP FUNCTION clinic_app.data_request_emit_task();
DROP FUNCTION clinic_app.data_request_close_task();
DROP FUNCTION clinic_app.data_request_task_close_valid(jsonb,jsonb);
DROP FUNCTION clinic_app.data_request_task_valid(jsonb);
DROP FUNCTION clinic_app.data_request_guard();
DROP FUNCTION clinic_app.data_request_withdrawn(uuid,uuid,text);
DROP FUNCTION clinic_app.data_request_now();
RESET ROLE;
"""
)
