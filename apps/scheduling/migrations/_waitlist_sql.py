"""Waitlist scope bindings, closed grants and patient enrollment RLS."""

from psycopg import sql

SQL = """
GRANT SELECT ON clinic_app.scheduling_waitlistentry,
 clinic_app.scheduling_waitlistoffer TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.waitlist_staff(requested_clinic uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT EXISTS (
 SELECT 1 FROM clinic_app.identity_userclinicrole r
 JOIN clinic_app.identity_user u ON u.id = r.user_id AND u.is_active
 WHERE r.clinic_id = requested_clinic
 AND r.organization_id = NULLIF(current_setting('app.current_tenant', true),'')::uuid
 AND r.user_id = NULLIF(current_setting('app.current_user_id', true),'')::uuid
 AND r.role IN ('owner','clinic_admin','receptionist'))
$f$;
CREATE FUNCTION clinic_app.waitlist_binding()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF TG_TABLE_NAME = 'scheduling_waitlistentry' THEN
   IF NOT EXISTS (SELECT 1 FROM clinic_app.intake_patientclinicenrollment e
     WHERE e.id = NEW.enrollment_id AND e.patient_id = NEW.patient_id
     AND e.clinic_id = NEW.clinic_id AND e.organization_id = NEW.organization_id)
   THEN RAISE EXCEPTION 'waitlist enrollment mismatch' USING ERRCODE = '23514'; END IF;
 ELSE
   IF NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_waitlistentry e
     WHERE e.id = NEW.entry_id AND e.clinic_id = NEW.clinic_id
     AND e.organization_id = NEW.organization_id
     AND e.practitioner_id = NEW.practitioner_id
     AND e.start_at <= NEW.start_at AND e.end_at >= NEW.end_at)
   THEN RAISE EXCEPTION 'waitlist offer mismatch' USING ERRCODE = '23514'; END IF;
   IF NEW.appointment_id IS NOT NULL AND NOT EXISTS (
     SELECT 1 FROM clinic_app.scheduling_appointment a
     JOIN clinic_app.scheduling_waitlistentry e ON e.id = NEW.entry_id
     WHERE a.id = NEW.appointment_id AND a.patient_id = e.patient_id
       AND a.clinic_id = NEW.clinic_id AND a.organization_id = NEW.organization_id
       AND a.practitioner_id = NEW.practitioner_id
       AND a.start_at = NEW.start_at AND a.end_at = NEW.end_at)
   THEN RAISE EXCEPTION 'waitlist booking mismatch' USING ERRCODE = '23514'; END IF;
   IF TG_OP = 'UPDATE' AND OLD.state <> 'pending'
      AND NEW IS DISTINCT FROM OLD
   THEN RAISE EXCEPTION 'waitlist offer is terminal' USING ERRCODE = '23514'; END IF;
 END IF;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.waitlist_staff(uuid),
 clinic_app.waitlist_binding() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.waitlist_staff(uuid) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.waitlist_binding() TO clinic_owner;
RESET ROLE;
CREATE TRIGGER waitlist_entry_binding BEFORE INSERT OR UPDATE
 ON clinic_app.scheduling_waitlistentry FOR EACH ROW
 EXECUTE FUNCTION clinic_app.waitlist_binding();
CREATE TRIGGER waitlist_offer_binding BEFORE INSERT OR UPDATE
 ON clinic_app.scheduling_waitlistoffer FOR EACH ROW
 EXECUTE FUNCTION clinic_app.waitlist_binding();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.waitlist_binding() FROM clinic_owner;
RESET ROLE;
"""

for table in ("waitlistentry", "waitlistoffer"):
    patient = (
        f"s.enrollment_id = scheduling_{table}.enrollment_id"
        if table == "waitlistentry"
        else "EXISTS (SELECT 1 FROM clinic_app.scheduling_waitlistentry e "
        "WHERE e.id = scheduling_waitlistoffer.entry_id "
        "AND e.enrollment_id = s.enrollment_id)"
    )
    scope = """organization_id =
      NULLIF(current_setting('app.current_tenant', true),'')::uuid
      AND clinic_app.waitlist_staff(clinic_id)"""
    access = (
        sql.SQL("""({scope}) OR EXISTS (
      SELECT 1 FROM clinic_app.patient_booking_scope() s
      WHERE s.organization_id = {table}.organization_id
      AND s.clinic_id = {table}.clinic_id AND {patient})""")
        .format(
            scope=sql.SQL(scope),
            table=sql.Identifier("scheduling_" + table),
            patient=sql.SQL(patient),
        )
        .as_string()
    )
    SQL += f"""
ALTER TABLE clinic_app.scheduling_{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.scheduling_{table} FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.scheduling_{table} TO clinic_owner
 USING (organization_id = NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id =
 NULLIF(current_setting('app.current_tenant',true),'')::uuid);
REVOKE ALL ON clinic_app.scheduling_{table} FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.scheduling_{table} TO clinic_app;
CREATE POLICY waitlist_read ON clinic_app.scheduling_{table}
 FOR SELECT TO clinic_app USING ({access});
CREATE POLICY waitlist_insert ON clinic_app.scheduling_{table}
 FOR INSERT TO clinic_app WITH CHECK ({scope});
CREATE POLICY waitlist_update ON clinic_app.scheduling_{table}
 FOR UPDATE TO clinic_app USING ({access}) WITH CHECK ({access});
"""

SQL += """
GRANT UPDATE (state) ON clinic_app.scheduling_waitlistentry TO clinic_app;
GRANT UPDATE (state, responded_at, appointment_id)
 ON clinic_app.scheduling_waitlistoffer TO clinic_app;
GRANT USAGE, SELECT ON SEQUENCE
 clinic_app.scheduling_waitlistentry_id_seq TO clinic_app;
"""

REVERSE_SQL = """
DROP TRIGGER waitlist_entry_binding ON clinic_app.scheduling_waitlistentry;
DROP TRIGGER waitlist_offer_binding ON clinic_app.scheduling_waitlistoffer;
DROP POLICY waitlist_read ON clinic_app.scheduling_waitlistentry;
DROP POLICY waitlist_insert ON clinic_app.scheduling_waitlistentry;
DROP POLICY waitlist_update ON clinic_app.scheduling_waitlistentry;
DROP POLICY waitlist_read ON clinic_app.scheduling_waitlistoffer;
DROP POLICY waitlist_insert ON clinic_app.scheduling_waitlistoffer;
DROP POLICY waitlist_update ON clinic_app.scheduling_waitlistoffer;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.waitlist_binding();
DROP FUNCTION clinic_app.waitlist_staff(uuid);
RESET ROLE;
"""
