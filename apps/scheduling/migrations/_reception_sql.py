"""Reception row guards and capability-only public projection."""

TABLES = ("queueentry", "roomstatus", "cliniciandelay", "displaydevice")


def _scope(table: str) -> str:
    condition = "clinic_app.has_permission('reception.queue',clinic_id,NULL)"
    if table == "displaydevice":
        condition += " AND clinic_app.has_permission('appointment.move',clinic_id,NULL)"
    if table == "queueentry":
        condition += """
 OR clinic_app.has_permission('appointment.move',clinic_id,NULL)
 OR (clinic_app.has_permission('appointment.move_own',clinic_id,NULL) AND EXISTS (
  SELECT 1 FROM clinic_app.scheduling_appointment a
  WHERE a.id=scheduling_queueentry.appointment_id
   AND a.clinic_id=scheduling_queueentry.clinic_id
   AND a.organization_id=scheduling_queueentry.organization_id
   AND a.practitioner_id=
    NULLIF(current_setting('app.current_user_id',true),'')::uuid))
"""
    return condition


ACL_SQL = (
    "\n".join(
        f"""
REVOKE ALL ON clinic_app.scheduling_{table} FROM clinic_app;
GRANT SELECT, INSERT ON clinic_app.scheduling_{table} TO clinic_app;
GRANT SELECT ON clinic_app.scheduling_{table} TO clinic_resolver;
ALTER TABLE clinic_app.scheduling_{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.scheduling_{table} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON clinic_app.scheduling_{table}
 AS PERMISSIVE FOR ALL TO PUBLIC
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=
  NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY reception_scope ON clinic_app.scheduling_{table}
 AS RESTRICTIVE FOR ALL TO clinic_app
 USING ({_scope(table)})
 WITH CHECK ({_scope(table)});
"""
        for table in TABLES
    )
    + """
GRANT UPDATE(state,room_id,room_unit) ON clinic_app.scheduling_queueentry
 TO clinic_app;
GRANT UPDATE(state) ON clinic_app.scheduling_roomstatus TO clinic_app;
GRANT UPDATE(minutes) ON clinic_app.scheduling_cliniciandelay TO clinic_app;
GRANT UPDATE(active) ON clinic_app.scheduling_displaydevice TO clinic_app;
GRANT SELECT ON clinic_app.intake_patientidentifier TO clinic_resolver;
"""
)

GUARD_SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.reception_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE
 a clinic_app.scheduling_appointment;
 r clinic_app.scheduling_resource;
 allowed boolean;
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION 'reception history cannot be deleted' USING ERRCODE='23514';
 END IF;
 IF NEW.organization_id IS DISTINCT FROM
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
  OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
   WHERE c.id=NEW.clinic_id AND c.organization_id=NEW.organization_id) THEN
  RAISE EXCEPTION 'reception access denied' USING ERRCODE='42501';
 END IF;
 allowed := clinic_app.has_permission('reception.queue',NEW.clinic_id,NULL);
 PERFORM pg_advisory_xact_lock(hashtextextended(
  'clinic-lock-v1:clinic:' || NEW.clinic_id::text,0));
 IF TG_TABLE_NAME='scheduling_queueentry' THEN
  SELECT * INTO a FROM clinic_app.scheduling_appointment
   WHERE id=NEW.appointment_id AND clinic_id=NEW.clinic_id
    AND organization_id=NEW.organization_id;
  IF a.id IS NULL THEN
   RAISE EXCEPTION 'reception access denied' USING ERRCODE='42501';
  END IF;
  IF TG_OP='INSERT' THEN
   IF NOT (allowed
    OR clinic_app.has_permission('appointment.move',NEW.clinic_id,NULL)
    OR (a.practitioner_id=
     NULLIF(current_setting('app.current_user_id',true),'')::uuid
     AND clinic_app.has_permission('appointment.move_own',NEW.clinic_id,NULL)))
    OR a.status<>'arrived'
    OR NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_resource l
     WHERE l.id=NEW.location_id AND l.kind='location' AND l.active
      AND l.clinic_id=NEW.clinic_id AND l.organization_id=NEW.organization_id) THEN
    RAISE EXCEPTION 'reception access denied' USING ERRCODE='42501';
   END IF;
   IF NEW.state<>'waiting' OR NEW.room_id IS NOT NULL
    OR NEW.room_unit IS NOT NULL OR NEW.called_at IS NOT NULL THEN
    RAISE EXCEPTION 'invalid initial queue state' USING ERRCODE='23514';
   END IF;
   SELECT COALESCE(max(position),0)+1 INTO NEW.position
    FROM clinic_app.scheduling_queueentry WHERE clinic_id=NEW.clinic_id;
   NEW.ticket_code := 'A' || NEW.position::text;
   NEW.created_at := statement_timestamp();
   NEW.revision := 1;
   RETURN NEW;
  END IF;
  IF (to_jsonb(NEW)-ARRAY['state','room_id','room_unit','called_at'])
   IS DISTINCT FROM
   (to_jsonb(OLD)-ARRAY['state','room_id','room_unit','called_at']) THEN
   RAISE EXCEPTION 'immutable queue identity' USING ERRCODE='23514';
  END IF;
  IF OLD.state='waiting' AND NEW.state='called' THEN
   IF NOT allowed THEN
    RAISE EXCEPTION 'reception access denied' USING ERRCODE='42501';
   END IF;
   SELECT * INTO r FROM clinic_app.scheduling_resource
    WHERE id=NEW.room_id AND clinic_id=NEW.clinic_id
     AND organization_id=NEW.organization_id AND kind='room' AND active;
   IF r.id IS NULL THEN
    RAISE EXCEPTION 'reception access denied' USING ERRCODE='42501';
   END IF;
   IF a.status<>'arrived' OR NEW.room_unit IS NULL
    OR NEW.room_unit NOT BETWEEN 1 AND r.capacity
    OR NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_roomstatus s
     WHERE s.room_id=r.id AND s.unit=NEW.room_unit)
    OR EXISTS (SELECT 1 FROM clinic_app.scheduling_roomstatus s
     WHERE s.room_id=r.id AND s.unit=NEW.room_unit AND s.state<>'available')
    OR (EXISTS (SELECT 1 FROM clinic_app.scheduling_appointmentresource ar
     JOIN clinic_app.scheduling_resource resource ON resource.id=ar.resource_id
     WHERE ar.appointment_id=a.id AND resource.kind='room')
     AND NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_appointmentresource ar
      WHERE ar.appointment_id=a.id AND ar.resource_id=r.id AND ar.unit=NEW.room_unit))
   THEN
    RAISE EXCEPTION 'room unavailable' USING ERRCODE='23514',
     CONSTRAINT='scheduling_queue_room_unavailable';
   END IF;
   NEW.called_at := statement_timestamp();
  ELSIF (OLD.state IN ('waiting','called') AND NEW.state='in_room'
   AND a.status='in_progress')
   OR (OLD.state IN ('waiting','called','in_room') AND NEW.state='done'
    AND a.status IN ('completed','cancelled','no_show')) THEN
   IF NOT (allowed OR
    (a.practitioner_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
    AND clinic_app.has_permission('appointment.move_own',NEW.clinic_id,NULL))
    OR clinic_app.has_permission('appointment.move',NEW.clinic_id,NULL)) THEN
    RAISE EXCEPTION 'reception access denied' USING ERRCODE='42501';
   END IF;
   IF ROW(NEW.room_id,NEW.room_unit,NEW.called_at) IS DISTINCT FROM
    ROW(OLD.room_id,OLD.room_unit,OLD.called_at) THEN
    RAISE EXCEPTION 'immutable room call' USING ERRCODE='23514';
   END IF;
  ELSE
   RAISE EXCEPTION 'illegal queue transition' USING ERRCODE='23514',
    CONSTRAINT='scheduling_queue_transition';
  END IF;
  NEW.revision := OLD.revision+1;
  RETURN NEW;
 END IF;
 IF NOT allowed THEN
  RAISE EXCEPTION 'reception access denied' USING ERRCODE='42501';
 END IF;
 IF TG_TABLE_NAME='scheduling_roomstatus' THEN
  SELECT * INTO r FROM clinic_app.scheduling_resource
   WHERE id=NEW.room_id AND organization_id=NEW.organization_id
    AND clinic_id=NEW.clinic_id AND kind='room' AND active;
  IF r.id IS NULL THEN
   RAISE EXCEPTION 'reception access denied' USING ERRCODE='42501';
  END IF;
  IF NEW.unit NOT BETWEEN 1 AND r.capacity OR EXISTS (
   SELECT 1 FROM clinic_app.scheduling_queueentry q WHERE q.room_id=r.id
    AND q.room_unit=NEW.unit AND q.state IN ('called','in_room')) THEN
   RAISE EXCEPTION 'room unavailable' USING ERRCODE='23514',
    CONSTRAINT='scheduling_queue_room_unavailable';
  END IF;
  IF TG_OP='INSERT' THEN
   SELECT COALESCE(max(display_number),0)+1 INTO NEW.display_number
    FROM clinic_app.scheduling_roomstatus WHERE clinic_id=NEW.clinic_id;
  END IF;
 ELSIF TG_TABLE_NAME='scheduling_cliniciandelay' THEN
  IF NOT clinic_app.has_permission('appointment.move',NEW.clinic_id,NULL) THEN
   RAISE EXCEPTION 'reception access denied' USING ERRCODE='42501';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole p
   JOIN clinic_app.identity_user u ON u.id=p.user_id AND u.is_active
   WHERE p.organization_id=NEW.organization_id AND p.clinic_id=NEW.clinic_id
    AND p.user_id=NEW.practitioner_id AND p.revoked_at IS NULL
    AND p.role IN ('physician','nurse','allied_professional')) THEN
   RAISE EXCEPTION 'reception access denied' USING ERRCODE='42501';
  END IF;
 ELSIF TG_TABLE_NAME='scheduling_displaydevice' THEN
  IF NOT clinic_app.has_permission('appointment.move',NEW.clinic_id,NULL) THEN
   RAISE EXCEPTION 'reception access denied' USING ERRCODE='42501';
  END IF;
  IF TG_OP='INSERT' THEN
   IF NEW.token_hash !~ '^[0-9a-f]{64}$' OR NOT NEW.active THEN
    RAISE EXCEPTION 'invalid display device' USING ERRCODE='23514';
   END IF;
   NEW.created_at := statement_timestamp();
  ELSIF NOT OLD.active OR NEW.active
   OR (to_jsonb(NEW)-'active') IS DISTINCT FROM (to_jsonb(OLD)-'active') THEN
   RAISE EXCEPTION 'display device permits only revocation' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
 END IF;
 IF TG_OP='UPDATE' THEN
  IF (to_jsonb(NEW)-ARRAY['state','minutes']) IS DISTINCT FROM
   (to_jsonb(OLD)-ARRAY['state','minutes']) THEN
   RAISE EXCEPTION 'immutable reception identity' USING ERRCODE='23514';
  END IF;
  NEW.revision := OLD.revision+1;
 ELSE
  NEW.revision := 1;
 END IF;
 NEW.updated_at := statement_timestamp();
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.reception_guard() FROM PUBLIC,clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.reception_guard() TO clinic_owner;

CREATE FUNCTION clinic_app.reception_display(token_digest text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT jsonb_build_object('tickets',COALESCE((
  SELECT jsonb_agg(jsonb_build_object(
   'ticket',q.ticket_code,
   'room','R' || s.display_number::text,
   'state',q.state) ORDER BY q.called_at,q.id)
  FROM clinic_app.scheduling_queueentry q
  JOIN clinic_app.scheduling_roomstatus s ON s.room_id=q.room_id
   AND s.unit=q.room_unit AND s.clinic_id=q.clinic_id
   AND s.organization_id=q.organization_id
  WHERE q.organization_id=d.organization_id AND q.clinic_id=d.clinic_id
   AND q.state IN ('called','in_room') AND q.room_id IS NOT NULL), '[]'::jsonb))
 FROM clinic_app.scheduling_displaydevice d
 WHERE d.token_hash=token_digest AND d.active
$f$;
REVOKE ALL ON FUNCTION clinic_app.reception_display(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.reception_display(text) TO clinic_app;

CREATE FUNCTION clinic_app.reception_readiness(
 requested_clinic uuid, requested_appointment uuid)
RETURNS TABLE(forms_pending bigint, consents_pending bigint,
 payment_pending boolean, identity_document_recorded boolean)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT
 (SELECT count(*) FROM clinic_app.intake_questionnaireresponse q
  WHERE q.organization_id=a.organization_id AND q.clinic_id=a.clinic_id
   AND q.appointment_id=a.id AND q.state='draft'),
 (SELECT count(*) FROM clinic_app.consent_consenttext t
  WHERE t.organization_id=a.organization_id AND t.clinic_id=a.clinic_id
   AND t.purpose IN ('teleconsultation','consultation_recording','ai_assistance')
   AND NOT EXISTS (SELECT 1 FROM clinic_app.consent_consenttext newer
    WHERE newer.clinic_id=t.clinic_id AND newer.purpose=t.purpose
     AND newer.version>t.version)
   AND NOT EXISTS (SELECT 1 FROM clinic_app.consent_consentacceptance acceptance
    WHERE acceptance.text_id=t.id AND acceptance.patient_id=a.patient_id
     AND acceptance.clinic_id=a.clinic_id
     AND NOT EXISTS (SELECT 1 FROM clinic_app.consent_consentrevocation revocation
      WHERE revocation.acceptance_id=acceptance.id))),
 CASE WHEN clinic_app.has_permission('charge.read',a.clinic_id,NULL)
  THEN EXISTS (SELECT 1 FROM clinic_app.billing_invoice invoice
   WHERE invoice.appointment_id=a.id AND invoice.organization_id=a.organization_id
    AND invoice.clinic_id=a.clinic_id AND invoice.state='open')
  ELSE NULL END,
 EXISTS (SELECT 1 FROM clinic_app.intake_patientidentifier identifier
  WHERE identifier.patient_id=a.patient_id AND identifier.clinic_id=a.clinic_id
   AND identifier.organization_id=a.organization_id AND identifier.retired_at IS NULL
   AND NOT EXISTS (SELECT 1 FROM clinic_app.intake_patientidentifier newer
    WHERE newer.patient_id=identifier.patient_id AND newer.kind=identifier.kind
     AND newer.version>identifier.version))
 FROM clinic_app.scheduling_appointment a
 WHERE a.id=requested_appointment AND a.clinic_id=requested_clinic
  AND a.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND (clinic_app.has_permission('appointment.move',requested_clinic,NULL)
   OR (a.practitioner_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
    AND clinic_app.has_permission('appointment.move_own',requested_clinic,NULL)))
$f$;
REVOKE ALL ON FUNCTION clinic_app.reception_readiness(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.reception_readiness(uuid,uuid) TO clinic_app;
CREATE FUNCTION clinic_app.reception_practitioners(requested_clinic uuid)
RETURNS TABLE(user_id uuid, display_label text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT DISTINCT u.id,u.username::text
 FROM clinic_app.identity_user u
 JOIN clinic_app.identity_userclinicrole r ON r.user_id=u.id
 WHERE u.is_active AND r.revoked_at IS NULL
  AND r.clinic_id=requested_clinic
  AND r.organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND r.role IN ('physician','nurse','allied_professional')
  AND clinic_app.has_permission('reception.queue',requested_clinic,NULL)
 ORDER BY u.id
$f$;
REVOKE ALL ON FUNCTION clinic_app.reception_practitioners(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.reception_practitioners(uuid) TO clinic_app;
RESET ROLE;
"""

TRIGGER_SQL = "\n".join(
    f"""
CREATE TRIGGER reception_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.scheduling_{table}
 FOR EACH ROW EXECUTE FUNCTION clinic_app.reception_guard();
CREATE TRIGGER reception_guard_truncate BEFORE TRUNCATE
 ON clinic_app.scheduling_{table}
 FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.reception_guard();
"""
    for table in TABLES
)

REVERSE_FUNCTION_SQL = """
DROP FUNCTION clinic_app.reception_practitioners(uuid);
DROP FUNCTION clinic_app.reception_readiness(uuid,uuid);
DROP FUNCTION clinic_app.reception_display(text);
"""
REVERSE_TRIGGER_SQL = "\n".join(
    f"""
DROP TRIGGER reception_guard ON clinic_app.scheduling_{table};
DROP TRIGGER reception_guard_truncate ON clinic_app.scheduling_{table};
"""
    for table in TABLES
)
SQL = (
    f"{ACL_SQL}{GUARD_SQL}{TRIGGER_SQL}"
    "SET LOCAL ROLE clinic_resolver;"
    "REVOKE EXECUTE ON FUNCTION clinic_app.reception_guard() FROM clinic_owner;"
    "RESET ROLE;"
)
ROLLBACK_GUARD_SQL = """
DO $f$ BEGIN
 IF EXISTS (SELECT 1 FROM clinic_app.scheduling_queueentry)
  OR EXISTS (SELECT 1 FROM clinic_app.scheduling_roomstatus)
  OR EXISTS (SELECT 1 FROM clinic_app.scheduling_cliniciandelay)
  OR EXISTS (SELECT 1 FROM clinic_app.scheduling_displaydevice) THEN
  RAISE EXCEPTION 'reception history exists; rollback requires restore';
 END IF;
END $f$;
"""
DROP_GUARD_SQL = "DROP FUNCTION clinic_app.reception_guard();"
REVERSE_SQL = (
    f"{ROLLBACK_GUARD_SQL}{REVERSE_FUNCTION_SQL}{REVERSE_TRIGGER_SQL}{DROP_GUARD_SQL}"
)
