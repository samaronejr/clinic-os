"""Relax only capture flags; keep every legacy room/session/credential binding."""

from apps.teleconsult.migrations._event_authority_sql import BINDING_GUARD

_binding = BINDING_GUARD
_consented_binding = _binding.replace(
    "OR NEW.recording_enabled OR NEW.transcription_enabled",
    "OR NEW.transcription_enabled OR (NEW.recording_enabled"
    " AND NOT clinic_app.scribe_room_capture_allowed(s.id))",
)

_forward_parts = (
    """
GRANT UPDATE(recording_enabled,transcription_enabled)
 ON clinic_app.teleconsult_teleconsultroom TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.scribe_room_capture_allowed(requested_session uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.teleconsult_teleconsultsession t
 JOIN clinic_app.ehr_encounter e ON e.id=t.encounter_id
 JOIN clinic_app.scribe_recordingsession s ON s.encounter_id=e.id
  AND s.organization_id=t.organization_id AND s.clinic_id=t.clinic_id
  AND s.physician_id=t.physician_id
 WHERE t.id=requested_session AND t.state IN ('waiting','active')
 AND e.state='open' AND NOT s.discarded
 AND s.state='recording'
 AND EXISTS(SELECT 1 FROM clinic_app.scribe_captureepoch ep
  WHERE ep.session_id=s.id AND ep.number=s.epoch AND ep.ended_at IS NULL)
 AND clinic_app.scribe_consent(s.consent_id,s.clinic_id,s.enrollment_id))
$f$;
CREATE FUNCTION clinic_app.scribe_room_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF TG_OP='DELETE' OR
  (to_jsonb(NEW)-ARRAY['recording_enabled','transcription_enabled'])
   IS DISTINCT FROM
  (to_jsonb(OLD)-ARRAY['recording_enabled','transcription_enabled'])
 THEN RAISE EXCEPTION 'immutable teleconsult room' USING ERRCODE='23514'; END IF;
 IF NEW.transcription_enabled THEN
  RAISE EXCEPTION 'transcription requires its separate gate' USING ERRCODE='42501';
 END IF;
 IF NEW.recording_enabled THEN
  IF NOT clinic_app.teleconsult_assigned(NEW.session_id)
   OR NOT clinic_app.scribe_room_capture_allowed(NEW.session_id) THEN
   RAISE EXCEPTION 'room capture denied' USING ERRCODE='42501'; END IF;
 ELSIF NOT clinic_app.teleconsult_assigned(NEW.session_id)
  AND clinic_app.scribe_room_capture_allowed(NEW.session_id) THEN
  RAISE EXCEPTION 'room capture denied' USING ERRCODE='42501';
 END IF;
 RETURN NEW;
END
$f$;
CREATE FUNCTION clinic_app.scribe_room_cleanup()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 UPDATE clinic_app.teleconsult_teleconsultroom r
 SET recording_enabled=false,transcription_enabled=false
 FROM clinic_app.teleconsult_teleconsultsession t
 WHERE r.session_id=t.id AND t.clinic_id=NEW.clinic_id
 AND (r.recording_enabled OR r.transcription_enabled)
 AND NOT clinic_app.scribe_room_capture_allowed(t.id);
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.scribe_room_capture_allowed(uuid),
 clinic_app.scribe_room_guard(),clinic_app.scribe_room_cleanup() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.scribe_room_guard(),
 clinic_app.scribe_room_cleanup() TO clinic_owner;
""",
    _consented_binding,
    """
RESET ROLE;
DROP TRIGGER teleconsult_immutable ON clinic_app.teleconsult_teleconsultroom;
CREATE TRIGGER teleconsult_capture_guard BEFORE UPDATE OR DELETE
 ON clinic_app.teleconsult_teleconsultroom
 FOR EACH ROW EXECUTE FUNCTION clinic_app.scribe_room_guard();
CREATE TRIGGER scribe_capture_stopped AFTER UPDATE ON clinic_app.scribe_recordingsession
 FOR EACH ROW EXECUTE FUNCTION clinic_app.scribe_room_cleanup();
CREATE TRIGGER scribe_capture_consent_revoked
 AFTER INSERT ON clinic_app.consent_consentrevocation
 FOR EACH ROW EXECUTE FUNCTION clinic_app.scribe_room_cleanup();
CREATE TRIGGER scribe_capture_consent_published
 AFTER INSERT ON clinic_app.consent_consenttext
 FOR EACH ROW EXECUTE FUNCTION clinic_app.scribe_room_cleanup();
CREATE TRIGGER scribe_capture_room_ended
 AFTER UPDATE ON clinic_app.teleconsult_teleconsultsession
 FOR EACH ROW EXECUTE FUNCTION clinic_app.scribe_room_cleanup();
CREATE TRIGGER scribe_capture_encounter_ended
 AFTER UPDATE OF state ON clinic_app.ehr_encounter
 FOR EACH ROW EXECUTE FUNCTION clinic_app.scribe_room_cleanup();
GRANT UPDATE(recording_enabled)
 ON clinic_app.teleconsult_teleconsultroom TO clinic_app;
CREATE POLICY teleconsult_room_capture_update
 ON clinic_app.teleconsult_teleconsultroom FOR UPDATE TO clinic_app
 USING(clinic_app.teleconsult_assigned(session_id))
 WITH CHECK(clinic_app.teleconsult_assigned(session_id));
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.scribe_room_guard(),
 clinic_app.scribe_room_cleanup() FROM clinic_owner;
RESET ROLE;
""",
)
SQL = "\n".join(_forward_parts)

_reverse_parts = (
    """
DO $f$ BEGIN
 IF EXISTS(SELECT 1 FROM clinic_app.teleconsult_teleconsultroom
  WHERE recording_enabled OR transcription_enabled) THEN
  RAISE EXCEPTION 'capture rollback requires restore' USING ERRCODE='23514';
 END IF;
END $f$;
DROP TRIGGER scribe_capture_room_ended ON clinic_app.teleconsult_teleconsultsession;
DROP TRIGGER scribe_capture_encounter_ended ON clinic_app.ehr_encounter;
DROP TRIGGER scribe_capture_stopped ON clinic_app.scribe_recordingsession;
DROP TRIGGER scribe_capture_consent_revoked ON clinic_app.consent_consentrevocation;
DROP TRIGGER scribe_capture_consent_published ON clinic_app.consent_consenttext;
DROP TRIGGER teleconsult_capture_guard ON clinic_app.teleconsult_teleconsultroom;
DROP POLICY teleconsult_room_capture_update ON clinic_app.teleconsult_teleconsultroom;
REVOKE UPDATE(recording_enabled,transcription_enabled)
 ON clinic_app.teleconsult_teleconsultroom FROM clinic_app,clinic_resolver;
SET LOCAL ROLE clinic_resolver;
""",
    _binding,
    """
GRANT EXECUTE ON FUNCTION clinic_app.teleconsult_immutable() TO clinic_owner;
DROP FUNCTION clinic_app.scribe_room_cleanup();
DROP FUNCTION clinic_app.scribe_room_guard();
DROP FUNCTION clinic_app.scribe_room_capture_allowed(uuid);
RESET ROLE;
CREATE TRIGGER teleconsult_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.teleconsult_teleconsultroom FOR EACH ROW
 EXECUTE FUNCTION clinic_app.teleconsult_immutable();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.teleconsult_immutable() FROM clinic_owner;
RESET ROLE;
""",
)
REVERSE_SQL = "\n".join(_reverse_parts)
