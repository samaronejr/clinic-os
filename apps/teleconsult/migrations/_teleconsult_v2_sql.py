"""Teleconsult v2: provider-version room binding, device checks, new events.

``teleconsult_binding_guard`` is rewritten additively with ``CREATE OR
REPLACE`` (H-13): a new room must name a ``video`` capability version (by
provider and environment, the registry's natural identity), and its outbox
operation's provider must equal that version's provider instead of the
hardcoded synthetic key. Only the dedicated synthetic version (never
approved, ``environment='synthetic'``) or an ``activated`` real version can
bind a room. Room names are opaque (``tc-`` plus 32 hex) and can never
spell a stored session, encounter, patient, appointment, physician, clinic
or organization identifier. The recording/transcription refusal line is
kept byte-for-byte; only todo 40 relaxes it.

``teleconsult_clinician`` decides the assigned physician's participant
authority through todo 6's ``clinic_app.has_permission('clinical.write',
clinic, enrollment)``. Device-check rows are insert-only, FORCE RLS, and
carry closed result codes. The reverse SQL restores the exact v1 guard body
taken from ``_teleconsult_sql``.
"""

from ._teleconsult_sql import SQL as _V1_SQL

_V1_START = _V1_SQL.index("CREATE FUNCTION clinic_app.teleconsult_binding_guard()")
_V1_END = _V1_SQL.index("\n$f$;\n", _V1_START) + len("\n$f$;\n")
BINDING_GUARD_V1 = "CREATE OR REPLACE " + _V1_SQL[_V1_START + len("CREATE ") : _V1_END]
# The unchanged capture refusal (H-13, relaxed only by todo 40).
CAPTURE_REFUSAL = "      OR NEW.recording_enabled OR NEW.transcription_enabled THEN"
SYNTHETIC_PROVIDER = "teleconsult-synthetic-v1"
if CAPTURE_REFUSAL + "\n" not in _V1_SQL:
    _message = "the v1 capture refusal line moved; keep it byte-identical"
    raise RuntimeError(_message)

BINDING_GUARD_V2 = """CREATE OR REPLACE FUNCTION clinic_app.teleconsult_binding_guard()
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
   -- The room names its video capability version by natural identity; only
   -- the never-approved synthetic version or an activated real one binds.
   IF NOT EXISTS (SELECT 1 FROM clinic_app.providers_capabilityversion v
     JOIN clinic_app.providers_providercapability c ON c.id=v.capability_id
     WHERE c.key='video' AND (c.clinic_id IS NULL OR c.clinic_id=s.clinic_id)
     AND v.provider=NEW.provider AND v.environment=NEW.provider_environment
     AND ((v.provider='__SYNTHETIC__' AND v.environment='synthetic'
           AND v.state IN ('researched','selected_in_plan'))
       OR (v.provider<>'__SYNTHETIC__' AND v.environment<>'synthetic'
           AND v.state='activated'))) THEN
     RAISE EXCEPTION 'invalid teleconsult provider binding' USING ERRCODE='23514';
   END IF;
   IF NOT EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
     WHERE o.id=NEW.operation_id AND o.organization_id=NEW.organization_id
     AND o.clinic_id=s.clinic_id AND o.actor_id=s.physician_id
     AND o.channel='video' AND o.provider=NEW.provider
     AND o.subject_type='teleconsult.session' AND o.subject_id=s.id)
      OR NEW.room_name !~ '^tc-[0-9a-f]{32}$'
      OR substr(NEW.room_name,4) IN (replace(s.id::text,'-',''),
        replace(s.encounter_id::text,'-',''),
        replace(s.appointment_id::text,'-',''),
        replace(s.patient_id::text,'-',''),
        replace(s.physician_id::text,'-',''),
        replace(s.clinic_id::text,'-',''),
        replace(s.organization_id::text,'-',''))
__CAPTURE__
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
 ELSIF TG_TABLE_NAME='teleconsult_teleconsultdevicecheck' THEN
   IF NOT ((NEW.role='physician' AND clinic_app.teleconsult_clinician(s.id))
        OR (NEW.role='patient' AND clinic_app.teleconsult_patient_match(s.id))) THEN
     RAISE EXCEPTION 'teleconsult participant authority required'
       USING ERRCODE='42501';
   END IF;
   IF s.state NOT IN ('waiting','active')
      OR (NEW.role='physician'
          AND NEW.participant_id IS DISTINCT FROM s.physician_id)
      OR (NEW.role='patient'
          AND NEW.participant_id IS DISTINCT FROM s.patient_id) THEN
     RAISE EXCEPTION 'invalid teleconsult device check' USING ERRCODE='23514';
   END IF;
   NEW.created_at := statement_timestamp();
 ELSIF TG_TABLE_NAME='teleconsult_teleconsultevent' THEN
   IF NEW.kind NOT IN ('created','joined','join_denied','started','ended','failed',
                       'reconnected','audio_only','video_restored','removed')
      OR NEW.actor_role NOT IN ('','physician','patient')
      OR (NEW.kind IN ('reconnected','audio_only','video_restored')
          AND NEW.actor_role='')
      OR (NEW.kind='removed' AND NEW.actor_role<>'physician') THEN
     RAISE EXCEPTION 'invalid teleconsult event' USING ERRCODE='23514';
   END IF;
 END IF;
 RETURN NEW;
END
$f$;
""".replace("__SYNTHETIC__", SYNTHETIC_PROVIDER).replace("__CAPTURE__", CAPTURE_REFUSAL)

_sql = """
GRANT SELECT ON clinic_app.providers_capabilityversion,
 clinic_app.providers_providercapability TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.teleconsult_clinician(requested_session uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.teleconsult_teleconsultsession s
 WHERE s.id=requested_session
 AND s.organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND s.physician_id =
   NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND clinic_app.has_permission('clinical.write', s.clinic_id,
   (SELECT en.id FROM clinic_app.intake_patientclinicenrollment en
    WHERE en.organization_id=s.organization_id AND en.clinic_id=s.clinic_id
    AND en.patient_id=s.patient_id)))
$f$;
REVOKE ALL ON FUNCTION clinic_app.teleconsult_clinician(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.teleconsult_clinician(uuid)
 TO clinic_app, clinic_owner;
"""
_sql += BINDING_GUARD_V2
_sql += """
GRANT EXECUTE ON FUNCTION clinic_app.teleconsult_immutable(),
 clinic_app.teleconsult_binding_guard() TO clinic_owner;
RESET ROLE;
ALTER TABLE clinic_app.teleconsult_teleconsultdevicecheck ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.teleconsult_teleconsultdevicecheck FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.teleconsult_teleconsultdevicecheck FROM PUBLIC,clinic_app;
GRANT SELECT,INSERT ON clinic_app.teleconsult_teleconsultdevicecheck TO clinic_app;
CREATE POLICY setup_tenant ON clinic_app.teleconsult_teleconsultdevicecheck
 TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE TRIGGER teleconsult_binding BEFORE INSERT
 ON clinic_app.teleconsult_teleconsultdevicecheck
 FOR EACH ROW EXECUTE FUNCTION clinic_app.teleconsult_binding_guard();
CREATE TRIGGER teleconsult_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.teleconsult_teleconsultdevicecheck FOR EACH ROW
 EXECUTE FUNCTION clinic_app.teleconsult_immutable();
CREATE POLICY teleconsult_device_read
 ON clinic_app.teleconsult_teleconsultdevicecheck FOR SELECT TO clinic_app
 USING (clinic_app.teleconsult_clinician(session_id)
 OR (role='patient' AND clinic_app.teleconsult_patient_match(session_id)));
CREATE POLICY teleconsult_device_insert
 ON clinic_app.teleconsult_teleconsultdevicecheck FOR INSERT TO clinic_app
 WITH CHECK ((role='physician' AND clinic_app.teleconsult_clinician(session_id))
 OR (role='patient' AND clinic_app.teleconsult_patient_match(session_id)));
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.teleconsult_immutable(),
 clinic_app.teleconsult_binding_guard() FROM clinic_owner;
RESET ROLE;
"""
SQL = _sql

_reverse = """
DROP POLICY teleconsult_device_insert
 ON clinic_app.teleconsult_teleconsultdevicecheck;
DROP POLICY teleconsult_device_read
 ON clinic_app.teleconsult_teleconsultdevicecheck;
DROP TRIGGER teleconsult_immutable ON clinic_app.teleconsult_teleconsultdevicecheck;
DROP TRIGGER teleconsult_binding ON clinic_app.teleconsult_teleconsultdevicecheck;
DROP POLICY setup_tenant ON clinic_app.teleconsult_teleconsultdevicecheck;
REVOKE ALL ON clinic_app.teleconsult_teleconsultdevicecheck FROM clinic_app;
SET LOCAL ROLE clinic_resolver;
"""
_reverse += BINDING_GUARD_V1
_reverse += """
DROP FUNCTION clinic_app.teleconsult_clinician(uuid);
RESET ROLE;
REVOKE SELECT ON clinic_app.providers_capabilityversion,
 clinic_app.providers_providercapability FROM clinic_resolver;
"""
REVERSE_SQL = _reverse
