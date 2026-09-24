"""Teleconsult FORCE RLS, stored-authority resolvers and lifecycle guards."""

_sql = """
GRANT SELECT ON clinic_app.teleconsult_teleconsultsession,
 clinic_app.teleconsult_teleconsultroom,
 clinic_app.teleconsult_teleconsultcredential,
 clinic_app.teleconsult_teleconsultevent,
 clinic_app.ehr_encounter,
 clinic_app.comms_integrationoperation
 TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.teleconsult_patient_match(requested_session uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.teleconsult_teleconsultsession s
 JOIN clinic_app.intake_patientsession p ON p.patient_id=s.patient_id
 AND p.clinic_id=s.clinic_id AND p.organization_id=s.organization_id
 JOIN clinic_app.intake_patientaccessgrant g ON g.id=p.grant_id
 WHERE s.id=requested_session
 AND p.id=NULLIF(current_setting('app.current_patient_session',true),'')::uuid
 AND NULLIF(current_setting('app.current_user_id',true),'') IS NULL
 AND p.revoked_at IS NULL AND g.revoked_at IS NULL
 AND p.expires_at>statement_timestamp()
 AND p.idle_expires_at>statement_timestamp()
 AND 'teleconsult'=ANY(p.operations))
$f$;
CREATE FUNCTION clinic_app.teleconsult_session_scope(requested_session uuid)
RETURNS TABLE(clinic_id uuid, patient_id uuid, physician_id uuid,
 state varchar, encounter_state varchar, consent_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT s.clinic_id,s.patient_id,s.physician_id,s.state,e.state,s.consent_id
 FROM clinic_app.teleconsult_teleconsultsession s
 JOIN clinic_app.ehr_encounter e ON e.id=s.encounter_id
 WHERE s.id=requested_session
 AND (s.organization_id =
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
 OR clinic_app.teleconsult_patient_match(s.id))
$f$;
CREATE FUNCTION clinic_app.teleconsult_room_state(requested_session uuid)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT o.status::text FROM clinic_app.teleconsult_teleconsultroom r
 JOIN clinic_app.comms_integrationoperation o ON o.id=r.operation_id
 WHERE r.session_id=requested_session
 AND (r.organization_id =
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
 OR clinic_app.teleconsult_patient_match(r.session_id))
$f$;
CREATE FUNCTION clinic_app.teleconsult_assigned(requested_session uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.teleconsult_teleconsultsession s
 JOIN clinic_app.scheduling_appointment a ON a.id=s.appointment_id
 WHERE s.id=requested_session
 AND s.organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND s.physician_id =
   NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND a.practitioner_id = s.physician_id
 AND clinic_app.questionnaire_staff(s.clinic_id, ARRAY['physician']))
$f$;
CREATE FUNCTION clinic_app.teleconsult_immutable()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 RAISE EXCEPTION 'teleconsult history is immutable' USING ERRCODE='23514';
END
$f$;
CREATE FUNCTION clinic_app.teleconsult_binding_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE e RECORD; s RECORD; a RECORD; t RECORD;
BEGIN
 IF TG_TABLE_NAME='teleconsult_teleconsultsession' THEN
   SELECT * INTO e FROM clinic_app.ehr_encounter WHERE id=NEW.encounter_id;
   IF e.id IS NULL OR e.organization_id IS DISTINCT FROM NEW.organization_id
      OR e.clinic_id IS DISTINCT FROM NEW.clinic_id
      OR e.appointment_id IS DISTINCT FROM NEW.appointment_id
      OR e.patient_id IS DISTINCT FROM NEW.patient_id
      OR e.physician_id IS DISTINCT FROM NEW.physician_id
      OR e.state <> 'open' OR NEW.state <> 'waiting' OR NEW.revision <> 1
      OR NEW.started_at IS NOT NULL OR NEW.ended_at IS NOT NULL
      OR NEW.failure_reason <> '' THEN
     RAISE EXCEPTION 'invalid teleconsult session binding' USING ERRCODE='23514';
   END IF;
   SELECT * INTO a FROM clinic_app.consent_consentacceptance
     WHERE id=NEW.consent_id;
   IF a.id IS NULL OR a.clinic_id IS DISTINCT FROM NEW.clinic_id
      OR a.patient_id IS DISTINCT FROM NEW.patient_id
      OR EXISTS (SELECT 1 FROM clinic_app.consent_consentrevocation r
        WHERE r.acceptance_id=a.id) THEN
     RAISE EXCEPTION 'invalid teleconsult consent binding' USING ERRCODE='23514';
   END IF;
   SELECT * INTO t FROM clinic_app.consent_consenttext WHERE id=a.text_id;
   IF t.id IS NULL OR t.purpose <> 'teleconsultation'
      OR EXISTS (SELECT 1 FROM clinic_app.consent_consenttext newer
        WHERE newer.clinic_id=t.clinic_id AND newer.purpose=t.purpose
        AND newer.version>t.version) THEN
     RAISE EXCEPTION 'invalid teleconsult consent binding' USING ERRCODE='23514';
   END IF;
   RETURN NEW;
 END IF;
 SELECT * INTO s FROM clinic_app.teleconsult_teleconsultsession
   WHERE id=NEW.session_id;
 IF s.id IS NULL OR s.organization_id IS DISTINCT FROM NEW.organization_id THEN
   RAISE EXCEPTION 'invalid teleconsult binding' USING ERRCODE='23514';
 END IF;
 IF TG_TABLE_NAME='teleconsult_teleconsultroom' THEN
   IF NOT EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
     WHERE o.id=NEW.operation_id AND o.organization_id=NEW.organization_id
     AND o.clinic_id=s.clinic_id AND o.actor_id=s.physician_id
     AND o.channel='video' AND o.provider='teleconsult-synthetic-v1'
     AND o.subject_type='teleconsult.session' AND o.subject_id=s.id)
      OR NEW.room_name <> 'tc-' || s.id::text
      OR NEW.recording_enabled OR NEW.transcription_enabled THEN
     RAISE EXCEPTION 'invalid teleconsult room binding' USING ERRCODE='23514';
   END IF;
 ELSIF TG_TABLE_NAME='teleconsult_teleconsultcredential' THEN
   IF (NEW.role='physician' AND NEW.participant_id IS DISTINCT FROM s.physician_id)
      OR (NEW.role='patient' AND NEW.participant_id IS DISTINCT FROM s.patient_id)
      OR NEW.expires_at <= statement_timestamp()
      OR NEW.expires_at > statement_timestamp()+interval '1 hour'
      OR NEW.first_used_at IS NOT NULL OR NEW.revoked_at IS NOT NULL THEN
     RAISE EXCEPTION 'invalid teleconsult credential' USING ERRCODE='23514';
   END IF;
 ELSIF TG_TABLE_NAME='teleconsult_teleconsultevent' THEN
   IF NEW.kind NOT IN ('created','joined','join_denied','started','ended','failed')
      OR NEW.actor_role NOT IN ('','physician','patient') THEN
     RAISE EXCEPTION 'invalid teleconsult event' USING ERRCODE='23514';
   END IF;
 END IF;
 RETURN NEW;
END
$f$;
CREATE FUNCTION clinic_app.teleconsult_session_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF (NEW.id,NEW.organization_id,NEW.clinic_id,NEW.encounter_id,NEW.appointment_id,
     NEW.patient_id,NEW.physician_id,NEW.consent_id,NEW.created_at)
    IS DISTINCT FROM
    (OLD.id,OLD.organization_id,OLD.clinic_id,OLD.encounter_id,OLD.appointment_id,
     OLD.patient_id,OLD.physician_id,OLD.consent_id,OLD.created_at)
    OR OLD.state IN ('ended','failed')
    OR NEW.revision <> OLD.revision + 1 THEN
   RAISE EXCEPTION 'immutable session or stale revision' USING ERRCODE='23514';
 END IF;
 IF (OLD.state,NEW.state) NOT IN
    (('waiting','waiting'),('waiting','active'),('waiting','ended'),
     ('waiting','failed'),('active','ended'),('active','failed')) THEN
   RAISE EXCEPTION 'invalid teleconsult transition' USING ERRCODE='23514';
 END IF;
 IF (NEW.state='active' AND (NEW.started_at IS NULL OR NEW.ended_at IS NOT NULL
     OR NEW.failure_reason <> ''))
    OR (NEW.state='ended' AND (NEW.ended_at IS NULL OR NEW.failure_reason <> ''))
    OR (NEW.state='failed' AND (NEW.ended_at IS NULL OR NEW.failure_reason=''))
    OR (NEW.state='waiting' AND (NEW.started_at IS NOT NULL
     OR NEW.ended_at IS NOT NULL OR NEW.failure_reason <> '')) THEN
   RAISE EXCEPTION 'invalid teleconsult lifecycle fields' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END
$f$;
CREATE FUNCTION clinic_app.teleconsult_credential_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF (NEW.id,NEW.organization_id,NEW.session_id,NEW.role,NEW.participant_id,
     NEW.token_digest,NEW.expires_at,NEW.created_at) IS DISTINCT FROM
    (OLD.id,OLD.organization_id,OLD.session_id,OLD.role,OLD.participant_id,
     OLD.token_digest,OLD.expires_at,OLD.created_at)
    OR (OLD.revoked_at IS NOT NULL
        AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
    OR (OLD.first_used_at IS NOT NULL
        AND NEW.first_used_at IS DISTINCT FROM OLD.first_used_at) THEN
   RAISE EXCEPTION 'immutable credential fields' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.teleconsult_session_scope(uuid),
 clinic_app.teleconsult_room_state(uuid), clinic_app.teleconsult_assigned(uuid),
 clinic_app.teleconsult_patient_match(uuid), clinic_app.teleconsult_immutable(),
 clinic_app.teleconsult_binding_guard(), clinic_app.teleconsult_session_guard(),
 clinic_app.teleconsult_credential_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.teleconsult_session_scope(uuid),
 clinic_app.teleconsult_room_state(uuid), clinic_app.teleconsult_assigned(uuid),
 clinic_app.teleconsult_patient_match(uuid) TO clinic_app, clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.teleconsult_immutable(),
 clinic_app.teleconsult_binding_guard(), clinic_app.teleconsult_session_guard(),
 clinic_app.teleconsult_credential_guard() TO clinic_owner;
RESET ROLE;
"""

for table in (
    "teleconsultsession",
    "teleconsultroom",
    "teleconsultcredential",
    "teleconsultevent",
):
    _sql += f"""
ALTER TABLE clinic_app.teleconsult_{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.teleconsult_{table} FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.teleconsult_{table} FROM PUBLIC,clinic_app;
GRANT SELECT,INSERT ON clinic_app.teleconsult_{table} TO clinic_app;
CREATE POLICY setup_tenant ON clinic_app.teleconsult_{table} TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE TRIGGER teleconsult_binding BEFORE INSERT ON clinic_app.teleconsult_{table}
 FOR EACH ROW EXECUTE FUNCTION clinic_app.teleconsult_binding_guard();
"""

_sql += """
CREATE TRIGGER teleconsult_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.teleconsult_teleconsultroom FOR EACH ROW
 EXECUTE FUNCTION clinic_app.teleconsult_immutable();
CREATE TRIGGER teleconsult_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.teleconsult_teleconsultevent FOR EACH ROW
 EXECUTE FUNCTION clinic_app.teleconsult_immutable();
CREATE TRIGGER teleconsult_immutable BEFORE DELETE
 ON clinic_app.teleconsult_teleconsultsession FOR EACH ROW
 EXECUTE FUNCTION clinic_app.teleconsult_immutable();
CREATE TRIGGER teleconsult_immutable BEFORE DELETE
 ON clinic_app.teleconsult_teleconsultcredential FOR EACH ROW
 EXECUTE FUNCTION clinic_app.teleconsult_immutable();
CREATE TRIGGER teleconsult_transition BEFORE UPDATE
 ON clinic_app.teleconsult_teleconsultsession FOR EACH ROW
 EXECUTE FUNCTION clinic_app.teleconsult_session_guard();
CREATE TRIGGER teleconsult_credential_update BEFORE UPDATE
 ON clinic_app.teleconsult_teleconsultcredential FOR EACH ROW
 EXECUTE FUNCTION clinic_app.teleconsult_credential_guard();
GRANT UPDATE (state,revision,started_at,ended_at,failure_reason)
 ON clinic_app.teleconsult_teleconsultsession TO clinic_app;
GRANT UPDATE (revoked_at,first_used_at)
 ON clinic_app.teleconsult_teleconsultcredential TO clinic_app;

CREATE POLICY teleconsult_session_read ON clinic_app.teleconsult_teleconsultsession
 FOR SELECT TO clinic_app
 USING (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.questionnaire_staff(clinic_id,
   ARRAY['physician','receptionist','owner','clinic_admin'])
 OR clinic_app.teleconsult_patient_match(id));
CREATE POLICY teleconsult_session_insert ON clinic_app.teleconsult_teleconsultsession
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND physician_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.questionnaire_staff(clinic_id,ARRAY['physician']));
CREATE POLICY teleconsult_session_update ON clinic_app.teleconsult_teleconsultsession
 FOR UPDATE TO clinic_app
 USING (clinic_app.teleconsult_assigned(id))
 WITH CHECK (clinic_app.teleconsult_assigned(id));

CREATE POLICY teleconsult_room_read ON clinic_app.teleconsult_teleconsultroom
 FOR SELECT TO clinic_app
 USING (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.questionnaire_staff(
   (SELECT s.clinic_id FROM clinic_app.teleconsult_teleconsultsession s
    WHERE s.id=session_id),
   ARRAY['physician','receptionist','owner','clinic_admin'])
 OR clinic_app.teleconsult_patient_match(session_id));
CREATE POLICY teleconsult_room_insert ON clinic_app.teleconsult_teleconsultroom
 FOR INSERT TO clinic_app
 WITH CHECK (clinic_app.teleconsult_assigned(session_id));

CREATE POLICY teleconsult_credential_read
 ON clinic_app.teleconsult_teleconsultcredential FOR SELECT TO clinic_app
 USING (clinic_app.teleconsult_assigned(session_id)
 OR (role='patient' AND clinic_app.teleconsult_patient_match(session_id)));
CREATE POLICY teleconsult_credential_insert
 ON clinic_app.teleconsult_teleconsultcredential FOR INSERT TO clinic_app
 WITH CHECK ((role='physician' AND clinic_app.teleconsult_assigned(session_id))
 OR (role='patient' AND clinic_app.teleconsult_patient_match(session_id)));
CREATE POLICY teleconsult_credential_update
 ON clinic_app.teleconsult_teleconsultcredential FOR UPDATE TO clinic_app
 USING (clinic_app.teleconsult_assigned(session_id)
 OR (role='patient' AND clinic_app.teleconsult_patient_match(session_id)))
 WITH CHECK (clinic_app.teleconsult_assigned(session_id)
 OR (role='patient' AND clinic_app.teleconsult_patient_match(session_id)));

CREATE POLICY teleconsult_event_read ON clinic_app.teleconsult_teleconsultevent
 FOR SELECT TO clinic_app
 USING (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.questionnaire_staff(
   (SELECT s.clinic_id FROM clinic_app.teleconsult_teleconsultsession s
    WHERE s.id=session_id),
   ARRAY['physician','receptionist','owner','clinic_admin'])
 OR clinic_app.teleconsult_patient_match(session_id));
CREATE POLICY teleconsult_event_insert ON clinic_app.teleconsult_teleconsultevent
 FOR INSERT TO clinic_app
 WITH CHECK (clinic_app.teleconsult_assigned(session_id)
 OR clinic_app.teleconsult_patient_match(session_id));

SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.teleconsult_immutable(),
 clinic_app.teleconsult_binding_guard(), clinic_app.teleconsult_session_guard(),
 clinic_app.teleconsult_credential_guard() FROM clinic_owner;
RESET ROLE;
"""

SQL = _sql

# Dependents come down before the functions they call: triggers and
# policies reference the guards, so the functions drop last.
_reverse_sql = ""
for table in (
    "teleconsultevent",
    "teleconsultcredential",
    "teleconsultroom",
    "teleconsultsession",
):
    _reverse_sql += f"""
DROP TRIGGER teleconsult_binding ON clinic_app.teleconsult_{table};
DROP POLICY setup_tenant ON clinic_app.teleconsult_{table};
"""
_reverse_sql += """
DROP TRIGGER teleconsult_immutable ON clinic_app.teleconsult_teleconsultroom;
DROP TRIGGER teleconsult_immutable ON clinic_app.teleconsult_teleconsultevent;
DROP TRIGGER teleconsult_immutable ON clinic_app.teleconsult_teleconsultsession;
DROP TRIGGER teleconsult_immutable ON clinic_app.teleconsult_teleconsultcredential;
DROP TRIGGER teleconsult_transition ON clinic_app.teleconsult_teleconsultsession;
DROP TRIGGER teleconsult_credential_update
 ON clinic_app.teleconsult_teleconsultcredential;
DROP POLICY teleconsult_session_read
 ON clinic_app.teleconsult_teleconsultsession;
DROP POLICY teleconsult_session_insert
 ON clinic_app.teleconsult_teleconsultsession;
DROP POLICY teleconsult_session_update
 ON clinic_app.teleconsult_teleconsultsession;
DROP POLICY teleconsult_room_read ON clinic_app.teleconsult_teleconsultroom;
DROP POLICY teleconsult_room_insert ON clinic_app.teleconsult_teleconsultroom;
DROP POLICY teleconsult_credential_read
 ON clinic_app.teleconsult_teleconsultcredential;
DROP POLICY teleconsult_credential_insert
 ON clinic_app.teleconsult_teleconsultcredential;
DROP POLICY teleconsult_credential_update
 ON clinic_app.teleconsult_teleconsultcredential;
DROP POLICY teleconsult_event_read ON clinic_app.teleconsult_teleconsultevent;
DROP POLICY teleconsult_event_insert ON clinic_app.teleconsult_teleconsultevent;

SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.teleconsult_credential_guard();
DROP FUNCTION clinic_app.teleconsult_session_guard();
DROP FUNCTION clinic_app.teleconsult_binding_guard();
DROP FUNCTION clinic_app.teleconsult_immutable();
DROP FUNCTION clinic_app.teleconsult_patient_match(uuid);
DROP FUNCTION clinic_app.teleconsult_assigned(uuid);
DROP FUNCTION clinic_app.teleconsult_room_state(uuid);
DROP FUNCTION clinic_app.teleconsult_session_scope(uuid);
RESET ROLE;
"""

REVERSE_SQL = _reverse_sql
