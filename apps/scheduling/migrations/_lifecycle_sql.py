"""Appointment lifecycle v2 (todo 22, D-9): transitions, holds, series, receipts.

Rollback is a restore: once any row uses a v2 state the reverse refuses rather
than rewriting history (constraint semantics change).
"""

from apps.scheduling.migrations._appointment_sql import INTEGRITY_SQL
from apps.scheduling.migrations._patient_booking_sql import SQL as PATIENT_SQL
from apps.scheduling.migrations._resources_sql import GUARDS_SQL as RESOURCE_SQL

# Legacy guard: future start, one clinic-local date and covering availability now
# also bind requested and held rows (both must name a real bookable slot). The
# live body is migration 0003's patient-aware guard, rebuilt here identically.
_GUARD_V1 = (
    (
        "CREATE OR REPLACE FUNCTION clinic_app.scheduling_appointment_guard_v1()"
        + INTEGRITY_SQL.split(
            "CREATE FUNCTION clinic_app.scheduling_appointment_guard_v1()", 1
        )[1].split("REVOKE ALL", 1)[0]
    )
    .replace(
        "        SELECT clinic.timezone",
        "        IF NULLIF(current_setting('app.current_patient_session', true), '') "
        "IS NOT NULL THEN\n"
        "            SELECT s.timezone INTO clinic_timezone\n"
        "              FROM clinic_app.patient_booking_scope() s\n"
        "             WHERE s.clinic_id = NEW.clinic_id\n"
        "               AND s.organization_id = NEW.organization_id;\n"
        "        ELSE\n"
        "        SELECT clinic.timezone",
    )
    .replace(
        "AND clinic.id = NEW.clinic_id;",
        "AND clinic.id = NEW.clinic_id;\n        END IF;",
    )
)
_GUARD_V1_V2 = _GUARD_V1.replace(
    "    IF NEW.status = 'scheduled' THEN\n",
    "    IF NEW.status IN ('requested', 'held', 'scheduled') THEN\n",
    1,
)

# Patient sessions may create a request (policy) or a hold/booking; receipts
# name the lifecycle transition instead of calling every update a reschedule.
_PATIENT_GUARD = PATIENT_SQL[
    PATIENT_SQL.index(
        "CREATE FUNCTION clinic_app.patient_booking_guard()"
    ) : PATIENT_SQL.index("CREATE FUNCTION clinic_app.patient_booking_receipt()")
].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
_PATIENT_GUARD_V2 = _PATIENT_GUARD.replace(
    "(TG_OP = 'INSERT' AND NEW.status <> 'scheduled')",
    "(TG_OP = 'INSERT' AND NEW.status NOT IN ('scheduled', 'held', 'requested'))",
    1,
)
_PATIENT_RECEIPT = PATIENT_SQL[
    PATIENT_SQL.index(
        "CREATE FUNCTION clinic_app.patient_booking_receipt()"
    ) : PATIENT_SQL.index(
        "CREATE FUNCTION clinic_app.patient_booking_event_immutable()"
    )
].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
_PATIENT_RECEIPT_V2 = _PATIENT_RECEIPT.replace(
    "   CASE WHEN TG_OP = 'INSERT' THEN 'created'\n"
    "        WHEN NEW.status = 'cancelled' THEN 'cancelled' ELSE 'rescheduled' END,",
    "   CASE WHEN TG_OP = 'INSERT' AND NEW.status = 'scheduled' THEN 'created'\n"
    "        WHEN TG_OP = 'INSERT' THEN NEW.status\n"
    "        WHEN NEW.status = 'cancelled' THEN 'cancelled'\n"
    "        WHEN NEW.status IS DISTINCT FROM OLD.status AND NEW.status = 'scheduled'\n"
    "          THEN 'booked'\n"
    "        WHEN NEW.status IS DISTINCT FROM OLD.status THEN NEW.status\n"
    "        ELSE 'rescheduled' END,",
    1,
)

# Status transitions are authorized by the lifecycle guard; capacity keeps the
# move permission for range and other same-status updates, and never re-derives
# units or rules when a row only moves between two occupying states.
_CAPACITY = RESOURCE_SQL[
    RESOURCE_SQL.index(
        "CREATE FUNCTION clinic_app.scheduling_capacity_guard()"
    ) : RESOURCE_SQL.index(
        "REVOKE ALL ON FUNCTION clinic_app.scheduling_definition_guard(),"
    )
].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
_CAPACITY_V2 = (
    _CAPACITY.replace(
        "  IF TG_OP='UPDATE' AND NEW.service_type_id IS NOT NULL AND NOT (",
        "  IF TG_OP='UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status\n"
        "   AND NEW.service_type_id IS NOT NULL AND NOT (",
        1,
    )
    .replace(
        "  IF NOT consumes THEN RETURN NEW; END IF;\n",
        "  IF TG_OP='UPDATE' AND consumes\n"
        "   AND OLD.status IN ('held','scheduled','arrived','in_progress')\n"
        "   AND ROW(NEW.start_at,NEW.end_at)=ROW(OLD.start_at,OLD.end_at) THEN\n"
        "   RETURN NEW;\n"
        "  END IF;\n"
        "  IF NOT consumes THEN RETURN NEW; END IF;\n",
        1,
    )
    .replace(
        " IF NOT consumes THEN\n  UPDATE clinic_app.scheduling_appointmentresource",
        " IF TG_OP='UPDATE' AND consumes\n"
        "  AND OLD.status IN ('held','scheduled','arrived','in_progress')\n"
        "  AND ROW(NEW.start_at,NEW.end_at)=ROW(OLD.start_at,OLD.end_at) THEN\n"
        "  RETURN NEW;\n"
        " END IF;\n"
        " IF NOT consumes THEN\n  UPDATE clinic_app.scheduling_appointmentresource",
        1,
    )
)

# The comms reminder trigger names ``status`` in its column list, so PostgreSQL
# refuses the varchar widening while it exists; it is recreated verbatim.
DROP_REMINDER_TRIGGER_SQL = """
DROP TRIGGER comms_schedule_reminders ON clinic_app.scheduling_appointment;
"""
CREATE_REMINDER_TRIGGER_SQL = """
SET LOCAL ROLE clinic_resolver;
GRANT EXECUTE ON FUNCTION clinic_app.comms_schedule_reminders_v1() TO clinic_owner;
RESET ROLE;
CREATE TRIGGER comms_schedule_reminders
 AFTER INSERT OR UPDATE OF start_at, end_at, status ON clinic_app.scheduling_appointment
 FOR EACH ROW EXECUTE FUNCTION clinic_app.comms_schedule_reminders_v1();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.comms_schedule_reminders_v1() FROM clinic_owner;
RESET ROLE;
"""

CONSTRAINT_SQL = """
ALTER TABLE clinic_app.scheduling_appointment
 DROP CONSTRAINT scheduling_appointment_lifecycle_check;
ALTER TABLE clinic_app.scheduling_appointment
 ADD CONSTRAINT scheduling_appointment_lifecycle_check CHECK (
  (status IN ('requested','held','scheduled','arrived','in_progress','completed',
    'expired','no_show') AND cancellation_reason IS NULL AND cancelled_at IS NULL)
  OR (status = 'cancelled' AND cancellation_reason IN ('patient_request',
    'clinic_request','practitioner_unavailable','duplicate','other')
    AND cancelled_at IS NOT NULL));
ALTER TABLE clinic_app.scheduling_appointment
 ADD CONSTRAINT scheduling_appointment_hold_check
 CHECK (status <> 'held' OR hold_expires_at IS NOT NULL);
ALTER TABLE clinic_app.scheduling_appointment
 ADD CONSTRAINT scheduling_appointment_authorization_reference_check
 CHECK (authorization_reference = ''
  OR authorization_reference ~ '^[A-Za-z0-9./-]{1,64}$');
ALTER TABLE clinic_app.scheduling_appointment
 ADD CONSTRAINT scheduling_appointment_series_binding
 FOREIGN KEY (organization_id, clinic_id, series_id)
 REFERENCES clinic_app.scheduling_appointmentseries (organization_id, clinic_id, id);
ALTER TABLE clinic_app.scheduling_appointmentseries
 ADD CONSTRAINT scheduling_series_clinic_fk FOREIGN KEY (organization_id, clinic_id)
 REFERENCES clinic_app.identity_clinic (organization_id, id);
ALTER TABLE clinic_app.scheduling_appointmentseries
 ADD CONSTRAINT scheduling_series_enrollment_fk
 FOREIGN KEY (organization_id, clinic_id, patient_id)
 REFERENCES clinic_app.intake_patientclinicenrollment
  (organization_id, clinic_id, patient_id);
ALTER TABLE clinic_app.scheduling_seriesexception
 ADD CONSTRAINT scheduling_series_exception_binding
 FOREIGN KEY (organization_id, clinic_id, series_id)
 REFERENCES clinic_app.scheduling_appointmentseries (organization_id, clinic_id, id);
ALTER TABLE clinic_app.scheduling_appointmenttransition
 ADD CONSTRAINT scheduling_transition_binding
 FOREIGN KEY (organization_id, clinic_id, appointment_id)
 REFERENCES clinic_app.scheduling_appointment (organization_id, clinic_id, id);
"""

# Reversed first (last operation): refuse before any work once v2 data exists.
ROLLBACK_GUARD_SQL = """
SET LOCAL ROLE clinic_resolver;
DO $guard$
BEGIN
 IF EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment
   WHERE status NOT IN ('scheduled','cancelled') OR series_id IS NOT NULL)
  OR EXISTS (SELECT 1 FROM clinic_app.scheduling_appointmentseries) THEN
  RAISE EXCEPTION 'appointment lifecycle v2 is populated; rollback requires restore'
   USING ERRCODE='23514', CONSTRAINT='scheduling_lifecycle_rollback';
 END IF;
END $guard$;
RESET ROLE;
"""

REVERSE_CONSTRAINT_SQL = """
ALTER TABLE clinic_app.scheduling_appointmenttransition
 DROP CONSTRAINT scheduling_transition_binding;
ALTER TABLE clinic_app.scheduling_seriesexception
 DROP CONSTRAINT scheduling_series_exception_binding;
ALTER TABLE clinic_app.scheduling_appointmentseries
 DROP CONSTRAINT scheduling_series_enrollment_fk,
 DROP CONSTRAINT scheduling_series_clinic_fk;
ALTER TABLE clinic_app.scheduling_appointment
 DROP CONSTRAINT scheduling_appointment_series_binding,
 DROP CONSTRAINT scheduling_appointment_authorization_reference_check,
 DROP CONSTRAINT scheduling_appointment_hold_check,
 DROP CONSTRAINT scheduling_appointment_lifecycle_check;
ALTER TABLE clinic_app.scheduling_appointment
 ADD CONSTRAINT scheduling_appointment_lifecycle_check CHECK (
  (status = 'scheduled' AND cancellation_reason IS NULL AND cancelled_at IS NULL)
  OR (status = 'cancelled' AND cancellation_reason IN ('patient_request',
    'clinic_request','practitioner_unavailable','duplicate','other')
    AND cancelled_at IS NOT NULL));
"""

# The single scheduling clock reader for lifecycle v2 and the single hold-expiry
# equality rule: a hold is due (expired) exactly when its deadline <= DB time.
CLOCK_SQL = """
CREATE FUNCTION clinic_app.scheduling_clock()
RETURNS timestamptz LANGUAGE sql STABLE
SET search_path=pg_catalog AS $f$
 SELECT statement_timestamp()
$f$;
CREATE FUNCTION clinic_app.scheduling_hold_due(deadline timestamptz)
RETURNS boolean LANGUAGE sql STABLE
SET search_path=pg_catalog,clinic_app AS $f$
 SELECT deadline <= clinic_app.scheduling_clock()
$f$;
REVOKE ALL ON FUNCTION clinic_app.scheduling_clock(),
 clinic_app.scheduling_hold_due(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.scheduling_clock(),
 clinic_app.scheduling_hold_due(timestamptz) TO clinic_app, clinic_resolver;
"""

REVERSE_CLOCK_SQL = """
DROP FUNCTION clinic_app.scheduling_hold_due(timestamptz);
DROP FUNCTION clinic_app.scheduling_clock();
"""

_GUARD_SQL_PART_0 = """
GRANT SELECT ON clinic_app.scheduling_appointmentseries,
 clinic_app.scheduling_seriesexception, clinic_app.scheduling_appointmenttransition
 TO clinic_resolver;
GRANT INSERT ON clinic_app.scheduling_appointmenttransition TO clinic_resolver;
GRANT UPDATE (status, last_command_id) ON clinic_app.scheduling_appointment
 TO clinic_resolver;
"""
_GUARD_SQL_PART_2 = """
SET LOCAL ROLE clinic_resolver;
"""
_GUARD_SQL_PART_4 = """
"""
_GUARD_SQL_PART_6 = """
"""
_GUARD_SQL_PART_8 = """
CREATE FUNCTION clinic_app.scheduling_release_due_holds(
 booking uuid, org uuid, practitioner uuid, patient uuid, resources uuid[],
 starts timestamptz, ends timestamptz)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE
 prior_user text := coalesce(current_setting('app.current_user_id',true),'');
 prior_patient text :=
  coalesce(current_setting('app.current_patient_session',true),'');
BEGIN
 -- Due holds expire as the machine (W) actor: clear, then restore, the human
 -- actor settings. An aborted statement rolls these back with it.
 PERFORM set_config('app.current_user_id','',true);
 PERFORM set_config('app.current_patient_session','',true);
 UPDATE clinic_app.scheduling_appointment a SET status='expired'
 WHERE a.organization_id=org AND a.status='held' AND a.id<>booking
  AND clinic_app.scheduling_hold_due(a.hold_expires_at)
  AND (a.practitioner_id=practitioner OR a.patient_id=patient
   OR a.resource_ids && resources)
  AND clinic_app.scheduling_effective_interval(a.start_at,a.end_at,a.buffer_before,
   a.buffer_after) && tstzrange(starts - interval '240 minutes',
   ends + interval '240 minutes','[)');
 PERFORM set_config('app.current_user_id',prior_user,true);
 PERFORM set_config('app.current_patient_session',prior_patient,true);
END $f$;
CREATE FUNCTION clinic_app.scheduling_lifecycle_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE
 actor uuid;
 patient boolean;
 moment timestamptz := clinic_app.scheduling_clock();
 approval boolean;
 ttl integer;
 edge text;
 allowed boolean := false;
BEGIN
 BEGIN
  actor := NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 EXCEPTION WHEN invalid_text_representation THEN
  RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';
 END;
 patient := NULLIF(current_setting('app.current_patient_session',true),'')
  IS NOT NULL;
 SELECT c.self_booking_requires_approval, c.hold_ttl_minutes INTO approval, ttl
  FROM clinic_app.identity_clinicconfiguration c
  WHERE c.clinic_id=NEW.clinic_id AND c.organization_id=NEW.organization_id
  ORDER BY c.version DESC LIMIT 1;
 approval := coalesce(approval,false);
 ttl := coalesce(ttl,10);
 IF TG_OP='INSERT' THEN
  NEW.revision := 1;
  NEW.transitioned_at := moment;
  NEW.hold_expires_at := NULL;
  NEW.last_command_id := NULL;
  -- A born-cancelled row is terminal and non-occupying (legacy insert shape).
  IF NEW.status NOT IN ('requested','held','scheduled','cancelled') THEN
   RAISE EXCEPTION 'illegal appointment transition' USING ERRCODE='23514',
    CONSTRAINT='scheduling_appointment_transition_check';
  END IF;
  IF patient THEN
   IF NOT EXISTS (SELECT 1 FROM clinic_app.patient_booking_scope() s
    WHERE s.organization_id=NEW.organization_id AND s.clinic_id=NEW.clinic_id
     AND s.patient_id=NEW.patient_id) THEN
    RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';
   END IF;
   IF (NEW.status='requested') <> approval THEN
    RAISE EXCEPTION 'self-booking policy' USING ERRCODE='23514',
     CONSTRAINT='scheduling_appointment_request_policy_check';
   END IF;
  ELSIF NEW.status='requested' THEN
   RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';
  ELSIF NEW.status='held' THEN
   IF NOT (clinic_app.has_permission('appointment.book',NEW.clinic_id,NULL)
    OR (NEW.practitioner_id=actor
     AND clinic_app.has_permission('appointment.book_own',NEW.clinic_id,NULL))) THEN
    RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';
   END IF;
  END IF;
  IF NEW.status='held' THEN
   NEW.hold_expires_at := moment + make_interval(mins => ttl);
  END IF;
  IF NEW.status IN ('held','scheduled') AND (patient OR NEW.organization_id::text
    = current_setting('app.current_tenant',true)) THEN
   PERFORM clinic_app.scheduling_release_due_holds(NEW.id,NEW.organization_id,
    NEW.practitioner_id,NEW.patient_id,NEW.resource_ids,NEW.start_at,NEW.end_at);
  END IF;
  RETURN NEW;
 END IF;
 IF ROW(NEW.id,NEW.organization_id,NEW.clinic_id,NEW.patient_id,NEW.practitioner_id,
   NEW.idempotency_key,NEW.create_fingerprint,NEW.series_id,NEW.series_index,
   NEW.payer_membership_id,NEW.authorization_reference)
  IS DISTINCT FROM ROW(OLD.id,OLD.organization_id,OLD.clinic_id,OLD.patient_id,
   OLD.practitioner_id,OLD.idempotency_key,OLD.create_fingerprint,OLD.series_id,
   OLD.series_index,OLD.payer_membership_id,OLD.authorization_reference) THEN
  RAISE EXCEPTION 'appointment identity is immutable' USING ERRCODE='23514',
   CONSTRAINT='scheduling_appointment_identity_check';
 END IF;
 NEW.revision := OLD.revision + 1;
 NEW.hold_expires_at := OLD.hold_expires_at;
 NEW.transitioned_at := OLD.transitioned_at;
 -- Cancelled rows stay owned by the legacy terminal guard (terminal_check).
 IF OLD.status='cancelled' THEN
  RETURN NEW;
 END IF;
 IF NEW.status=OLD.status THEN
  IF OLD.status<>'scheduled' AND ROW(NEW.start_at,NEW.end_at)
    IS DISTINCT FROM ROW(OLD.start_at,OLD.end_at) THEN
   RAISE EXCEPTION 'illegal appointment transition' USING ERRCODE='23514',
    CONSTRAINT='scheduling_appointment_transition_check';
  END IF;
  RETURN NEW;
 END IF;
 edge := OLD.status || '>' || NEW.status;
 IF edge NOT IN ('requested>held','requested>scheduled','requested>cancelled',
   'held>scheduled','held>cancelled','held>expired','scheduled>arrived',
   'scheduled>cancelled','scheduled>no_show','arrived>in_progress',
   'arrived>cancelled','in_progress>completed')
  OR (edge<>'scheduled>cancelled' AND ROW(NEW.start_at,NEW.end_at)
   IS DISTINCT FROM ROW(OLD.start_at,OLD.end_at)) THEN
  RAISE EXCEPTION 'illegal appointment transition' USING ERRCODE='23514',
   CONSTRAINT='scheduling_appointment_transition_check';
 END IF;
 IF edge IN ('requested>held','requested>scheduled','held>scheduled') THEN
  allowed := clinic_app.has_permission('appointment.book',OLD.clinic_id,NULL)
   OR (OLD.practitioner_id=actor
    AND clinic_app.has_permission('appointment.book_own',OLD.clinic_id,NULL));
 ELSIF edge IN ('requested>cancelled','held>cancelled','scheduled>arrived',
   'scheduled>no_show','arrived>cancelled') THEN
  allowed := clinic_app.has_permission('appointment.move',OLD.clinic_id,NULL)
   OR (OLD.practitioner_id=actor
    AND clinic_app.has_permission('appointment.move_own',OLD.clinic_id,NULL));
 ELSIF edge IN ('arrived>in_progress','in_progress>completed') THEN
  allowed := OLD.practitioner_id=actor
   AND clinic_app.has_permission('appointment.move_own',OLD.clinic_id,NULL);
 ELSIF edge='held>expired' THEN
  allowed := actor IS NULL AND NOT patient;
 ELSIF edge='scheduled>cancelled' THEN
  -- Legacy authority is retained unchanged (todo 6 parity): the legacy
  -- cancellation service and the patient guard decide this edge.
  allowed := true;
 END IF;
 IF edge IN ('held>scheduled','held>cancelled','requested>cancelled') AND patient
  AND EXISTS (SELECT 1 FROM clinic_app.patient_booking_scope() s
   WHERE s.organization_id=OLD.organization_id AND s.clinic_id=OLD.clinic_id
    AND s.patient_id=OLD.patient_id) THEN
  allowed := true;
 END IF;
 IF NOT coalesce(allowed,false) THEN
  RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';
 END IF;
 IF edge='held>scheduled' AND clinic_app.scheduling_hold_due(OLD.hold_expires_at)
 THEN
  RAISE EXCEPTION 'hold expired' USING ERRCODE='23514',
   CONSTRAINT='scheduling_appointment_hold_expired';
 END IF;
 IF edge='held>expired'
  AND NOT clinic_app.scheduling_hold_due(OLD.hold_expires_at) THEN
  RAISE EXCEPTION 'illegal appointment transition' USING ERRCODE='23514',
   CONSTRAINT='scheduling_appointment_transition_check';
 END IF;
 IF edge='scheduled>no_show' AND OLD.start_at>moment THEN
  RAISE EXCEPTION 'no-show before start' USING ERRCODE='23514',
   CONSTRAINT='scheduling_appointment_no_show_deadline';
 END IF;
 NEW.transitioned_at := moment;
 IF NEW.status='cancelled' AND NEW.cancelled_at IS NULL THEN
  NEW.cancelled_at := moment;
 END IF;
 IF NEW.status='held' THEN
  NEW.hold_expires_at := moment + make_interval(mins => ttl);
 END IF;
 IF OLD.status='requested' AND NEW.status IN ('held','scheduled') THEN
  PERFORM clinic_app.scheduling_release_due_holds(NEW.id,NEW.organization_id,
   NEW.practitioner_id,NEW.patient_id,NEW.resource_ids,NEW.start_at,NEW.end_at);
 END IF;
 RETURN NEW;
END $f$;
CREATE FUNCTION clinic_app.scheduling_lifecycle_receipt()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF TG_OP='UPDATE' AND NEW.status IS NOT DISTINCT FROM OLD.status THEN
  RETURN NEW;
 END IF;
 INSERT INTO clinic_app.scheduling_appointmenttransition
  (id,organization_id,clinic_id,appointment_id,from_status,to_status,revision,
   command_id,actor_kind,occurred_at)
 VALUES (gen_random_uuid(),NEW.organization_id,NEW.clinic_id,NEW.id,
  CASE WHEN TG_OP='UPDATE' THEN OLD.status ELSE '' END,NEW.status,NEW.revision,
  CASE WHEN TG_OP='UPDATE' AND NEW.last_command_id IS DISTINCT FROM
   OLD.last_command_id THEN NEW.last_command_id END,
  CASE WHEN NULLIF(current_setting('app.current_patient_session',true),'')
    IS NOT NULL THEN 'patient'
   WHEN NULLIF(current_setting('app.current_user_id',true),'') IS NOT NULL
    THEN 'staff' ELSE 'machine' END,
  NEW.transitioned_at);
 RETURN NEW;
END $f$;
CREATE FUNCTION clinic_app.scheduling_history_immutable()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 RAISE EXCEPTION 'scheduling history is immutable' USING ERRCODE='23514';
END $f$;
CREATE FUNCTION clinic_app.patient_booking_requires_approval()
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT coalesce((SELECT c.self_booking_requires_approval
  FROM clinic_app.patient_booking_scope() s
  JOIN clinic_app.identity_clinicconfiguration c ON c.clinic_id=s.clinic_id
   AND c.organization_id=s.organization_id
  ORDER BY c.version DESC LIMIT 1), false)
$f$;
CREATE FUNCTION clinic_app.scheduling_due_holds(batch integer)
RETURNS TABLE(clinic_id uuid, appointment_id uuid, revision integer)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT a.clinic_id, a.id, a.revision::integer
 FROM clinic_app.scheduling_appointment a
 WHERE NULLIF(current_setting('app.current_user_id',true),'') IS NULL
  AND NULLIF(current_setting('app.current_patient_session',true),'') IS NULL
  AND a.status='held' AND clinic_app.scheduling_hold_due(a.hold_expires_at)
 ORDER BY a.hold_expires_at, a.id LIMIT least(greatest(coalesce(batch,1),1),500)
$f$;
CREATE FUNCTION clinic_app.scheduling_expire_hold(requested_clinic uuid,
 requested_appointment uuid, expected_revision integer, command uuid)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE
 held_id uuid;
 held_organization uuid;
 held_revision integer;
 receipt_appointment uuid;
 receipt_status text;
 receipt_revision integer;
BEGIN
 IF NULLIF(current_setting('app.current_user_id',true),'') IS NOT NULL
  OR NULLIF(current_setting('app.current_patient_session',true),'') IS NOT NULL
  OR command IS NULL OR expected_revision IS NULL THEN
  RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';
 END IF;
 SELECT a.id, a.organization_id, a.revision INTO held_id, held_organization,
  held_revision FROM clinic_app.scheduling_appointment a
  WHERE a.id=requested_appointment AND a.clinic_id=requested_clinic FOR UPDATE;
 IF held_id IS NULL THEN
  RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';
 END IF;
 SELECT t.appointment_id, t.to_status, t.revision
  INTO receipt_appointment, receipt_status, receipt_revision
  FROM clinic_app.scheduling_appointmenttransition t
  WHERE t.organization_id=held_organization AND t.command_id=command;
 IF receipt_appointment IS NOT NULL THEN
  IF receipt_appointment=held_id AND receipt_status='expired' THEN
   RETURN receipt_revision;
  END IF;
  RAISE EXCEPTION 'command reused' USING ERRCODE='23505',
   CONSTRAINT='scheduling_transition_command_uniq';
 END IF;
 IF held_revision<>expected_revision THEN
  RAISE EXCEPTION 'revision conflict' USING ERRCODE='23514',
   CONSTRAINT='scheduling_appointment_revision_check';
 END IF;
 UPDATE clinic_app.scheduling_appointment SET status='expired',
  last_command_id=command WHERE id=held_id;
 RETURN held_revision + 1;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.scheduling_release_due_holds(uuid,uuid,uuid,uuid,
 uuid[],timestamptz,timestamptz), clinic_app.scheduling_lifecycle_guard(),
 clinic_app.scheduling_lifecycle_receipt(), clinic_app.scheduling_history_immutable(),
 clinic_app.scheduling_due_holds(integer),
 clinic_app.patient_booking_requires_approval(),
 clinic_app.scheduling_expire_hold(uuid,uuid,integer,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.scheduling_due_holds(integer),
 clinic_app.patient_booking_requires_approval(),
 clinic_app.scheduling_expire_hold(uuid,uuid,integer,uuid) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.scheduling_lifecycle_guard(),
 clinic_app.scheduling_lifecycle_receipt(), clinic_app.scheduling_history_immutable()
 TO clinic_owner;
RESET ROLE;
CREATE TRIGGER scheduling_appointment_a_lifecycle BEFORE INSERT OR UPDATE
 ON clinic_app.scheduling_appointment FOR EACH ROW
 EXECUTE FUNCTION clinic_app.scheduling_lifecycle_guard();
CREATE TRIGGER scheduling_appointment_lifecycle_receipt AFTER INSERT OR UPDATE
 ON clinic_app.scheduling_appointment FOR EACH ROW
 EXECUTE FUNCTION clinic_app.scheduling_lifecycle_receipt();
CREATE TRIGGER scheduling_history_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.scheduling_appointmenttransition FOR EACH ROW
 EXECUTE FUNCTION clinic_app.scheduling_history_immutable();
CREATE TRIGGER scheduling_history_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.scheduling_seriesexception FOR EACH ROW
 EXECUTE FUNCTION clinic_app.scheduling_history_immutable();
CREATE TRIGGER scheduling_history_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.scheduling_appointmentseries FOR EACH ROW
 EXECUTE FUNCTION clinic_app.scheduling_history_immutable();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.scheduling_lifecycle_guard(),
 clinic_app.scheduling_lifecycle_receipt(), clinic_app.scheduling_history_immutable()
 FROM clinic_owner;
RESET ROLE;
"""
_GUARD_SQL_ORDER = (
    _GUARD_SQL_PART_0,
    _GUARD_V1_V2,
    _GUARD_SQL_PART_2,
    _PATIENT_GUARD_V2,
    _GUARD_SQL_PART_4,
    _PATIENT_RECEIPT_V2,
    _GUARD_SQL_PART_6,
    _CAPACITY_V2,
    _GUARD_SQL_PART_8,
)
GUARD_SQL = "".join(_GUARD_SQL_ORDER)

_REVERSE_GUARD_SQL_PART_0 = """
DROP TRIGGER scheduling_history_immutable ON clinic_app.scheduling_appointmentseries;
DROP TRIGGER scheduling_history_immutable ON clinic_app.scheduling_seriesexception;
DROP TRIGGER scheduling_history_immutable
 ON clinic_app.scheduling_appointmenttransition;
DROP TRIGGER scheduling_appointment_lifecycle_receipt
 ON clinic_app.scheduling_appointment;
DROP TRIGGER scheduling_appointment_a_lifecycle ON clinic_app.scheduling_appointment;
"""
_REVERSE_GUARD_SQL_PART_2 = """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.scheduling_expire_hold(uuid,uuid,integer,uuid);
DROP FUNCTION clinic_app.scheduling_due_holds(integer);
DROP FUNCTION clinic_app.patient_booking_requires_approval();
DROP FUNCTION clinic_app.scheduling_history_immutable();
DROP FUNCTION clinic_app.scheduling_lifecycle_receipt();
DROP FUNCTION clinic_app.scheduling_lifecycle_guard();
DROP FUNCTION clinic_app.scheduling_release_due_holds(uuid,uuid,uuid,uuid,uuid[],
 timestamptz,timestamptz);
"""
_REVERSE_GUARD_SQL_PART_4 = """
"""
_REVERSE_GUARD_SQL_PART_6 = """
"""
_REVERSE_GUARD_SQL_PART_8 = """
RESET ROLE;
REVOKE UPDATE (status, last_command_id) ON clinic_app.scheduling_appointment
 FROM clinic_resolver;
REVOKE INSERT ON clinic_app.scheduling_appointmenttransition FROM clinic_resolver;
REVOKE SELECT ON clinic_app.scheduling_appointmentseries,
 clinic_app.scheduling_seriesexception, clinic_app.scheduling_appointmenttransition
 FROM clinic_resolver;
"""
_REVERSE_GUARD_SQL_ORDER = (
    _REVERSE_GUARD_SQL_PART_0,
    _GUARD_V1,
    _REVERSE_GUARD_SQL_PART_2,
    _CAPACITY,
    _REVERSE_GUARD_SQL_PART_4,
    _PATIENT_RECEIPT,
    _REVERSE_GUARD_SQL_PART_6,
    _PATIENT_GUARD,
    _REVERSE_GUARD_SQL_PART_8,
)
REVERSE_GUARD_SQL = "".join(_REVERSE_GUARD_SQL_ORDER)

# Runtime grants: series and exceptions are written by the series services;
# lifecycle receipts are database-owned (SELECT only).
ACL_SQL = """
REVOKE ALL ON clinic_app.scheduling_appointmentseries,
 clinic_app.scheduling_seriesexception, clinic_app.scheduling_appointmenttransition
 FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.scheduling_appointmentseries,
 clinic_app.scheduling_seriesexception TO clinic_app;
GRANT SELECT ON clinic_app.scheduling_appointmenttransition TO clinic_app;
CREATE POLICY patient_booking_read ON clinic_app.scheduling_appointmenttransition
 FOR SELECT TO clinic_app USING (EXISTS (
 SELECT 1 FROM clinic_app.patient_booking_scope() s
 JOIN clinic_app.scheduling_appointment a ON a.patient_id=s.patient_id
  AND a.clinic_id=s.clinic_id AND a.organization_id=s.organization_id
 WHERE a.id=scheduling_appointmenttransition.appointment_id));
GRANT UPDATE (last_command_id) ON clinic_app.scheduling_appointment TO clinic_app;
"""

REVERSE_ACL_SQL = """
DROP POLICY IF EXISTS patient_booking_read
 ON clinic_app.scheduling_appointmenttransition;
REVOKE UPDATE (last_command_id) ON clinic_app.scheduling_appointment FROM clinic_app;
"""
