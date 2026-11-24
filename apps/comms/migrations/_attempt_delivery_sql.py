"""Attribute new delivery reports to one proven inbox account and attempt."""

SQL = """
CREATE FUNCTION clinic_app.comms_status_attempt_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF NEW.kind = 'status' AND NOT EXISTS (
  SELECT 1 FROM clinic_app.comms_integrationoperation o
  JOIN clinic_app.comms_message m ON m.id=o.subject_id AND m.direction='out'
  JOIN clinic_app.comms_conversation c ON c.id=m.conversation_id
  WHERE o.id=NEW.operation_id AND o.kind='communication'
   AND o.subject_type='comms.message' AND o.provider_reference IS NOT NULL
   AND o.organization_id=NEW.organization_id AND m.organization_id=o.organization_id
   AND c.organization_id=o.organization_id AND c.clinic_id=o.clinic_id
   AND c.provider_account_id=NEW.provider_account_id AND c.channel=o.channel
   AND clinic_app.comms_ingest_account(c.clinic_id,c.channel)=NEW.provider_account_id
 ) THEN
  RAISE EXCEPTION 'status requires its authenticated attempt' USING ERRCODE='42501';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.comms_status_attempt_guard() FROM PUBLIC;
CREATE TRIGGER comms_status_attempt_guard BEFORE INSERT ON clinic_app.comms_inboundevent
 FOR EACH ROW EXECUTE FUNCTION clinic_app.comms_status_attempt_guard();

SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.comms_recover_pending_v1()
RETURNS SETOF uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT id FROM clinic_app.comms_integrationoperation
 WHERE status IN ('pending', 'in_progress') AND last_error <> 'outcome_unknown'
   AND (not_before IS NULL OR not_before <= statement_timestamp())
   AND ((status='pending' AND last_error='')
        OR updated_at <= statement_timestamp()-interval '1 minute')
 ORDER BY created_at, id LIMIT 100
$f$;
CREATE OR REPLACE FUNCTION clinic_app.comms_due_reminders_v1()
RETURNS SETOF uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT id FROM clinic_app.comms_integrationoperation
 WHERE subject_type='scheduling.appointment_reminder'
   AND status IN ('pending', 'in_progress') AND last_error <> 'outcome_unknown'
   AND not_before <= statement_timestamp()
   AND ((status='pending' AND last_error='')
        OR updated_at <= statement_timestamp()-interval '1 minute')
 ORDER BY not_before, id LIMIT 100
$f$;
RESET ROLE;
"""
