"""Same-clinic insurance references and DB-owned appointment check pointers."""

from psycopg import sql

from apps.scheduling.migrations._patient_service_sql import (
    _CAPACITY_V3 as PRIOR_CAPACITY,
)
from apps.scheduling.migrations._patient_service_sql import (
    _LIFECYCLE_V2 as PRIOR_LIFECYCLE,
)

from ._membership_sql import foreign_key

SCOPED_FOREIGN_KEYS = (
    (
        "insurance_insurancemembership",
        "insurance_registration_org_fk",
        "organization_id,registration_id",
        "intake_insurancemembership",
        "organization_id,id",
    ),
    (
        "insurance_payerrequest",
        "insurance_request_appointment_scope_fk",
        "organization_id,clinic_id,appointment_id",
        "scheduling_appointment",
        "organization_id,clinic_id,id",
    ),
    (
        "scheduling_appointment",
        "scheduling_membership_scope_fk",
        "organization_id,clinic_id,payer_membership_id",
        "insurance_insurancemembership",
        "organization_id,clinic_id,id",
    ),
    (
        "scheduling_appointment",
        "scheduling_contract_scope_fk",
        "organization_id,clinic_id,payer_contract_id",
        "insurance_payercontract",
        "organization_id,clinic_id,id",
    ),
    (
        "scheduling_appointment",
        "scheduling_eligibility_scope_fk",
        "organization_id,clinic_id,eligibility_check_id",
        "insurance_payerrequest",
        "organization_id,clinic_id,id",
    ),
    (
        "scheduling_appointment",
        "scheduling_authorization_scope_fk",
        "organization_id,clinic_id,authorization_check_id",
        "insurance_payerrequest",
        "organization_id,clinic_id,id",
    ),
)
APPOINTMENT_REFERENCES = {
    "payer_membership": "insurance_insurancemembership",
    "payer_contract": "insurance_payercontract",
    "eligibility_check": "insurance_payerrequest",
    "authorization_check": "insurance_payerrequest",
}

SCHEMA_SQL = "".join(
    (
        """
ALTER TABLE clinic_app.scheduling_appointment
 ADD COLUMN IF NOT EXISTS payer_contract_id uuid NULL,
 ADD COLUMN IF NOT EXISTS eligibility_check_id uuid NULL,
 ADD COLUMN IF NOT EXISTS authorization_check_id uuid NULL;
CREATE UNIQUE INDEX IF NOT EXISTS insurance_membership_scope_id
 ON clinic_app.insurance_insurancemembership(organization_id,clinic_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS insurance_contract_scope_id
 ON clinic_app.insurance_payercontract(organization_id,clinic_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS insurance_request_scope_id
 ON clinic_app.insurance_payerrequest(organization_id,clinic_id,id);
SET LOCAL ROLE clinic_resolver;
DO $preflight$
DECLARE invalid bigint;
BEGIN
 SELECT count(*) INTO invalid FROM clinic_app.scheduling_appointment a
 WHERE (a.payer_membership_id IS NULL)<>(a.payer_contract_id IS NULL)
  OR (a.payer_membership_id IS NOT NULL AND NOT EXISTS(
   SELECT 1 FROM clinic_app.insurance_insurancemembership m
   JOIN clinic_app.intake_patientclinicenrollment e ON e.id=m.enrollment_id
   JOIN clinic_app.insurance_payercontract c ON c.id=a.payer_contract_id
   WHERE m.id=a.payer_membership_id AND m.organization_id=a.organization_id
    AND m.clinic_id=a.clinic_id AND e.patient_id=a.patient_id
    AND c.organization_id=a.organization_id AND c.clinic_id=a.clinic_id
    AND c.payer_id=m.payer_id))
  OR (a.authorization_reference<>'' AND a.authorization_check_id IS NULL);
 IF invalid<>0 THEN
  RAISE EXCEPTION 'insurer linkage requires explicit review: % unresolved appointments',
   invalid USING ERRCODE='23514',
   CONSTRAINT='scheduling_insurance_upgrade_preflight';
 END IF;
END $preflight$;
RESET ROLE;
""",
        "".join(
            sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} ({});\n")
            .format(
                sql.Identifier("scheduling_" + field + "_id"),
                sql.Identifier("clinic_app", "scheduling_appointment"),
                sql.Identifier(field + "_id"),
            )
            .as_string()
            + foreign_key(
                "scheduling_appointment",
                f"scheduling_{field}_fk",
                f"{field}_id",
                target,
                "id",
            )
            for field, target in APPOINTMENT_REFERENCES.items()
        ),
        "".join(
            foreign_key(table, name, columns, target, keys)
            for table, name, columns, target, keys in SCOPED_FOREIGN_KEYS
        ),
    )
)

# Only a nested insert receipt can replace current check pointers. Replaying an
# older immutable request never fires that receipt and cannot restore a refusal.
CHECK_UPDATE = """
pg_trigger_depth()>1
AND clinic_app.has_permission('tiss.manage',NEW.clinic_id,NULL)
AND (to_jsonb(NEW)-ARRAY['eligibility_check_id','authorization_check_id',
 'authorization_reference','revision','updated_at'])=
 (to_jsonb(OLD)-ARRAY['eligibility_check_id','authorization_check_id',
 'authorization_reference','revision','updated_at'])
AND (NEW.eligibility_check_id IS DISTINCT FROM OLD.eligibility_check_id
 OR NEW.authorization_check_id IS DISTINCT FROM OLD.authorization_check_id)
"""

_LIFECYCLE = PRIOR_LIFECYCLE.replace(
    "NEW.payer_membership_id,NEW.authorization_reference",
    "NEW.payer_membership_id,NEW.payer_contract_id,NEW.authorization_reference",
).replace(
    "OLD.series_index,OLD.payer_membership_id,OLD.authorization_reference) THEN",
    "OLD.series_index,OLD.payer_membership_id,OLD.payer_contract_id,"
    "OLD.authorization_reference) AND NOT (" + CHECK_UPDATE + ") THEN",
)
_CAPACITY = PRIOR_CAPACITY.replace(
    "   AND NOT (", "   AND NOT (" + CHECK_UPDATE + ")\n   AND NOT (", 1
)

# Bind the exact booking and current source revisions. Protected dates enter
# this digest only as ciphertext, never as a plaintext or comparable shadow.
_COVERAGE_BINDING = """
encode(clinic_app.digest(ROW(
 __APPOINTMENT__,__PATIENT__,m.id,m.registration_id,m.fingerprint,
 encode(m.valid_from,'hex'),r.version,r.payer_id,
 coalesce(encode(r.valid_until,'hex'),''),
 coalesce(extract(epoch FROM r.retired_at)::text,''),
 c.id,c.version,c.fingerprint,to_char(c.starts_on,'YYYY-MM-DD'),
 to_char(c.ends_on,'YYYY-MM-DD'),c.eligibility_supported,c.authorization_supported,
 to_char(__START__ AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US'),
 to_char(__END__ AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US')
)::text,'sha256'),'hex')
"""

# The clock census decoder refuses module-level reassignment: one name per value.
_FUNCTIONS_TEMPLATE = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.insurance_booking_coverage(
 clinic uuid,patient uuid,membership uuid,contract uuid)
RETURNS TABLE(membership_id uuid,contract_id uuid,clinic_id uuid,patient_id uuid,
 payer_id uuid,contract_payer_id uuid,valid_from bytea,valid_to bytea,
 starts_on date,ends_on date,eligibility_supported boolean,
 authorization_supported boolean)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT m.id,c.id,m.clinic_id,e.patient_id,m.payer_id,c.payer_id,
  m.valid_from,r.valid_until,c.starts_on,c.ends_on,
  c.eligibility_supported,c.authorization_supported
 FROM clinic_app.insurance_insurancemembership m
 JOIN clinic_app.intake_patientclinicenrollment e ON e.id=m.enrollment_id
  AND e.organization_id=m.organization_id AND e.clinic_id=m.clinic_id
 JOIN clinic_app.intake_insurancemembership r ON r.id=m.registration_id
  AND r.organization_id=m.organization_id AND r.patient_id=e.patient_id
 JOIN clinic_app.insurance_payer canonical ON canonical.id=r.payer_id
  AND canonical.organization_id=r.organization_id
 JOIN clinic_app.insurance_payer local_payer ON local_payer.id=m.payer_id
  AND local_payer.clinic_id=m.clinic_id
  AND local_payer.ans_registry=canonical.ans_registry
 JOIN clinic_app.insurance_payercontract c ON c.id=contract
  AND c.clinic_id=m.clinic_id AND c.organization_id=m.organization_id
  AND c.payer_id=m.payer_id
 WHERE m.id=membership AND m.clinic_id=clinic AND e.patient_id=patient
  AND r.retired_at IS NULL AND NOT EXISTS(
   SELECT 1 FROM clinic_app.intake_insurancemembership newer
   WHERE newer.organization_id=r.organization_id AND newer.patient_id=r.patient_id
    AND newer.sequence=r.sequence AND newer.version>r.version)
  AND NOT EXISTS(SELECT 1 FROM clinic_app.insurance_payercontract newer
   WHERE newer.organization_id=c.organization_id AND newer.clinic_id=c.clinic_id
    AND newer.payer_id=c.payer_id AND newer.version>c.version)
  AND m.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND (clinic_app.has_permission('appointment.book',clinic,NULL)
   OR clinic_app.has_permission('appointment.book_own',clinic,NULL)
   OR clinic_app.has_permission('appointment.move',clinic,NULL)
   OR clinic_app.has_permission('appointment.move_own',clinic,NULL))
$f$;
REVOKE ALL ON FUNCTION clinic_app.insurance_booking_coverage(uuid,uuid,uuid,uuid)
 FROM PUBLIC,clinic_agent,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.insurance_booking_coverage(uuid,uuid,uuid,uuid)
 TO clinic_app;

CREATE OR REPLACE FUNCTION clinic_app.insurance_record_booking_coverage(
 clinic uuid,patient uuid,membership uuid,contract uuid,appointment uuid,
 starts timestamptz,ends timestamptz,kek text)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE m clinic_app.insurance_insurancemembership;
 c clinic_app.insurance_payercontract; r clinic_app.intake_insurancemembership;
 covered_from date; covered_until date; first_day date; last_day date;
 binding text; receipt uuid; zone text;
BEGIN
 PERFORM 1 FROM clinic_app.insurance_booking_coverage(
  clinic,patient,membership,contract);
 IF NOT FOUND OR appointment IS NULL OR starts IS NULL OR ends IS NULL
  OR ends<=starts THEN
  RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
  'insurance-clinic-v1:'||clinic::text,0));
 SELECT * INTO m FROM clinic_app.insurance_insurancemembership WHERE id=membership;
 SELECT * INTO c FROM clinic_app.insurance_payercontract WHERE id=contract;
 SELECT * INTO r FROM clinic_app.intake_insurancemembership WHERE id=m.registration_id;
 SELECT timezone INTO zone FROM clinic_app.identity_clinic WHERE id=clinic;
 first_day:=(starts AT TIME ZONE zone)::date;
 last_day:=((ends-interval '1 microsecond') AT TIME ZONE zone)::date;
 BEGIN
  covered_from:=convert_from(clinic_app.protected_decrypt(
   kek,'insurance.membership.valid_from',m.valid_from),'UTF8')::date;
  IF r.valid_until IS NOT NULL THEN
   covered_until:=convert_from(clinic_app.protected_decrypt(
    kek,'intake.insurancemembership.valid_until',r.valid_until),'UTF8')::date;
  END IF;
 EXCEPTION WHEN OTHERS THEN
  -- Do not expose decryption or date-parser diagnostics containing payloads.
  RAISE EXCEPTION 'insurance coverage unavailable' USING ERRCODE='42501';
 END;
 IF first_day<covered_from OR (covered_until IS NOT NULL AND last_day>covered_until)
  OR first_day<c.starts_on OR (c.ends_on IS NOT NULL AND last_day>c.ends_on) THEN
  RAISE EXCEPTION 'payer refused' USING ERRCODE='23514',
   CONSTRAINT='scheduling_payer_refused'; END IF;
 binding:=__RECORD_BINDING__;
 receipt:=gen_random_uuid();
 INSERT INTO clinic_app.insurance_commandreceipt
  (id,organization_id,clinic_id,command_id,fingerprint,created_at,
   kind,subject_id,next_state)
 VALUES(receipt,m.organization_id,clinic,receipt,binding,statement_timestamp(),
  'coverage',appointment,'covered');
 RETURN receipt;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.insurance_record_booking_coverage(
 uuid,uuid,uuid,uuid,uuid,timestamptz,timestamptz,text)
 FROM PUBLIC,clinic_agent,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.insurance_record_booking_coverage(
 uuid,uuid,uuid,uuid,uuid,timestamptz,timestamptz,text) TO clinic_app;

CREATE OR REPLACE FUNCTION clinic_app.insurance_registration_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF TG_TABLE_NAME='insurance_insurancemembership' THEN
  IF NOT EXISTS(SELECT 1 FROM clinic_app.intake_insurancemembership r
   JOIN clinic_app.intake_patientclinicenrollment e
    ON e.patient_id=r.patient_id AND e.organization_id=r.organization_id
   JOIN clinic_app.insurance_payer canonical ON canonical.id=r.payer_id
    AND canonical.organization_id=r.organization_id
   JOIN clinic_app.insurance_payer p ON p.id=NEW.payer_id
    AND p.clinic_id=NEW.clinic_id AND p.organization_id=NEW.organization_id
    AND p.ans_registry=canonical.ans_registry
   WHERE r.id=NEW.registration_id AND r.organization_id=NEW.organization_id
    AND r.retired_at IS NULL AND e.id=NEW.enrollment_id
    AND e.clinic_id=NEW.clinic_id) THEN
   RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501'; END IF;
 ELSIF TG_TABLE_NAME='insurance_guide' THEN
  IF EXISTS(SELECT 1 FROM clinic_app.ehr_encounter e
   JOIN clinic_app.scheduling_appointment a ON a.id=e.appointment_id
    AND a.clinic_id=NEW.clinic_id AND a.organization_id=NEW.organization_id
   WHERE e.id=NEW.encounter_id AND a.payer_membership_id IS NOT NULL
    AND (a.payer_membership_id<>NEW.membership_id
     OR a.payer_contract_id<>NEW.contract_id)) THEN
   RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501'; END IF;
 ELSE
  IF NOT EXISTS(SELECT 1 FROM clinic_app.scheduling_appointment a
   WHERE a.id=NEW.appointment_id AND a.organization_id=NEW.organization_id
    AND a.clinic_id=NEW.clinic_id AND a.payer_membership_id=NEW.membership_id
    AND a.payer_contract_id=NEW.contract_id
    AND a.status NOT IN ('cancelled','expired','no_show'))
   OR (NEW.kind='authorization' AND NEW.outcome='authorized'
    AND NEW.authorization_reference<>'SYN-'||replace(NEW.command_id::text,'-',''))
   OR ((NEW.kind<>'authorization' OR NEW.outcome<>'authorized')
    AND NEW.authorization_reference<>'') THEN
   RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501'; END IF;
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.insurance_registration_guard()
 FROM PUBLIC,clinic_app,clinic_agent,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.insurance_registration_guard() TO clinic_owner;
RESET ROLE;
DROP TRIGGER IF EXISTS insurance_registration_guard
 ON clinic_app.insurance_insurancemembership;
CREATE TRIGGER insurance_registration_guard
 BEFORE INSERT ON clinic_app.insurance_insurancemembership
 FOR EACH ROW EXECUTE FUNCTION clinic_app.insurance_registration_guard();
DROP TRIGGER IF EXISTS insurance_request_appointment_guard
 ON clinic_app.insurance_payerrequest;
CREATE TRIGGER insurance_request_appointment_guard
 BEFORE INSERT ON clinic_app.insurance_payerrequest
 FOR EACH ROW EXECUTE FUNCTION clinic_app.insurance_registration_guard();
DROP TRIGGER IF EXISTS insurance_guide_coverage_guard ON clinic_app.insurance_guide;
CREATE TRIGGER insurance_guide_coverage_guard BEFORE INSERT
 ON clinic_app.insurance_guide
 FOR EACH ROW EXECUTE FUNCTION clinic_app.insurance_registration_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.insurance_registration_guard() FROM clinic_owner;

CREATE OR REPLACE FUNCTION clinic_app.insurance_payer_check_applied()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF NEW.kind='eligibility' THEN
  UPDATE clinic_app.scheduling_appointment SET eligibility_check_id=NEW.id,
   updated_at=statement_timestamp()
   WHERE id=NEW.appointment_id AND clinic_id=NEW.clinic_id
    AND organization_id=NEW.organization_id
    AND payer_membership_id=NEW.membership_id AND payer_contract_id=NEW.contract_id;
 ELSE
  UPDATE clinic_app.scheduling_appointment SET authorization_check_id=NEW.id,
   authorization_reference=NEW.authorization_reference,
   updated_at=statement_timestamp()
   WHERE id=NEW.appointment_id AND clinic_id=NEW.clinic_id
    AND organization_id=NEW.organization_id
    AND payer_membership_id=NEW.membership_id AND payer_contract_id=NEW.contract_id;
 END IF;
 IF NOT FOUND THEN
  RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501'; END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.insurance_payer_check_applied()
 FROM PUBLIC,clinic_app,clinic_agent,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.insurance_payer_check_applied() TO clinic_owner;
RESET ROLE;
GRANT UPDATE(eligibility_check_id,authorization_check_id,authorization_reference,
 updated_at) ON clinic_app.scheduling_appointment TO clinic_resolver;
DROP TRIGGER IF EXISTS insurance_payer_check_applied
 ON clinic_app.insurance_payerrequest;
CREATE TRIGGER insurance_payer_check_applied AFTER INSERT
 ON clinic_app.insurance_payerrequest
 FOR EACH ROW EXECUTE FUNCTION clinic_app.insurance_payer_check_applied();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.insurance_payer_check_applied() FROM clinic_owner;

CREATE OR REPLACE FUNCTION clinic_app.insurance_appointment_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE m clinic_app.insurance_insurancemembership;
 c clinic_app.insurance_payercontract; e clinic_app.intake_patientclinicenrollment;
 r clinic_app.intake_insurancemembership;
 eligible clinic_app.insurance_payerrequest;
 authorized clinic_app.insurance_payerrequest;
 day date; confirming boolean; booking boolean; binding text;
BEGIN
 IF TG_OP='UPDATE' AND ROW(NEW.eligibility_check_id,NEW.authorization_check_id,
  NEW.authorization_reference) IS DISTINCT FROM
  ROW(OLD.eligibility_check_id,OLD.authorization_check_id,OLD.authorization_reference)
  AND NOT (__CHECK_UPDATE__) THEN
  RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501'; END IF;
 IF NEW.payer_membership_id IS NULL THEN
  IF NEW.payer_contract_id IS NOT NULL OR NEW.eligibility_check_id IS NOT NULL
   OR NEW.authorization_check_id IS NOT NULL OR NEW.authorization_reference<>'' THEN
   RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501'; END IF;
  RETURN NEW;
 END IF;
 IF TG_OP='INSERT' AND (NEW.eligibility_check_id IS NOT NULL
  OR NEW.authorization_check_id IS NOT NULL OR NEW.authorization_reference<>'') THEN
  RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501'; END IF;
 SELECT * INTO m FROM clinic_app.insurance_insurancemembership
  WHERE id=NEW.payer_membership_id AND clinic_id=NEW.clinic_id
   AND organization_id=NEW.organization_id;
 SELECT * INTO c FROM clinic_app.insurance_payercontract
  WHERE id=NEW.payer_contract_id AND clinic_id=NEW.clinic_id
   AND organization_id=NEW.organization_id AND payer_id=m.payer_id;
 SELECT * INTO e FROM clinic_app.intake_patientclinicenrollment
  WHERE id=m.enrollment_id AND patient_id=NEW.patient_id
   AND clinic_id=NEW.clinic_id AND organization_id=NEW.organization_id;
 SELECT * INTO r FROM clinic_app.intake_insurancemembership
  WHERE id=m.registration_id AND organization_id=NEW.organization_id
   AND patient_id=NEW.patient_id;
 IF m.id IS NULL OR c.id IS NULL OR e.id IS NULL OR r.id IS NULL THEN
  RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501'; END IF;
 SELECT (NEW.start_at AT TIME ZONE timezone)::date INTO day
  FROM clinic_app.identity_clinic WHERE id=NEW.clinic_id;
 binding:=__APPOINTMENT_BINDING__;
 IF day<c.starts_on OR (c.ends_on IS NOT NULL AND day>c.ends_on)
  OR NOT EXISTS(SELECT 1 FROM clinic_app.insurance_commandreceipt proof
   WHERE proof.organization_id=NEW.organization_id AND proof.clinic_id=NEW.clinic_id
    AND proof.kind='coverage' AND proof.subject_id=NEW.id
    AND proof.next_state='covered' AND proof.fingerprint=binding) THEN
  RAISE EXCEPTION 'payer refused' USING ERRCODE='23514',
   CONSTRAINT='scheduling_payer_refused'; END IF;
 SELECT * INTO eligible FROM clinic_app.insurance_payerrequest
  WHERE id=NEW.eligibility_check_id AND kind='eligibility'
   AND appointment_id=NEW.id AND membership_id=m.id AND contract_id=c.id;
 SELECT * INTO authorized FROM clinic_app.insurance_payerrequest
  WHERE id=NEW.authorization_check_id AND kind='authorization'
   AND appointment_id=NEW.id AND membership_id=m.id AND contract_id=c.id;
 IF (NEW.eligibility_check_id IS NOT NULL AND eligible.id IS NULL)
  OR (NEW.authorization_check_id IS NOT NULL AND authorized.id IS NULL)
  OR NEW.authorization_reference IS DISTINCT FROM
   coalesce(authorized.authorization_reference,'') THEN
  RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501'; END IF;
 confirming:=NEW.status='scheduled' AND
  (TG_OP='INSERT' OR OLD.status IN ('held','requested'));
 booking:=TG_OP='INSERT' OR confirming OR
  (TG_OP='UPDATE' AND ROW(NEW.start_at,NEW.end_at) IS DISTINCT FROM
   ROW(OLD.start_at,OLD.end_at));
 IF booking AND (r.retired_at IS NOT NULL
   OR EXISTS(SELECT 1 FROM clinic_app.intake_insurancemembership newer
    WHERE newer.organization_id=r.organization_id AND newer.patient_id=r.patient_id
     AND newer.sequence=r.sequence AND newer.version>r.version)
   OR EXISTS(SELECT 1 FROM clinic_app.insurance_payercontract newer
    WHERE newer.organization_id=c.organization_id AND newer.clinic_id=c.clinic_id
     AND newer.payer_id=c.payer_id AND newer.version>c.version)) THEN
  RAISE EXCEPTION 'payer refused' USING ERRCODE='23514',
   CONSTRAINT='scheduling_payer_refused'; END IF;
 IF confirming THEN
  IF (c.eligibility_supported AND eligible.outcome='denied')
   OR (c.authorization_supported AND authorized.outcome='denied') THEN
   RAISE EXCEPTION 'payer refused' USING ERRCODE='23514',
    CONSTRAINT='scheduling_payer_refused'; END IF;
  IF (c.eligibility_supported AND eligible.outcome IS DISTINCT FROM 'eligible')
   OR (c.authorization_supported AND authorized.outcome IS DISTINCT FROM 'authorized')
  THEN RAISE EXCEPTION 'payer check required' USING ERRCODE='23514',
   CONSTRAINT='scheduling_payer_pending'; END IF;
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.insurance_appointment_guard()
 FROM PUBLIC,clinic_app,clinic_agent,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.insurance_appointment_guard() TO clinic_owner;
RESET ROLE;
DROP TRIGGER IF EXISTS scheduling_insurance_guard ON clinic_app.scheduling_appointment;
CREATE TRIGGER scheduling_insurance_guard BEFORE INSERT OR UPDATE
 ON clinic_app.scheduling_appointment
 FOR EACH ROW EXECUTE FUNCTION clinic_app.insurance_appointment_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.insurance_appointment_guard() FROM clinic_owner;
RESET ROLE;
"""
FUNCTIONS_SQL = (
    _FUNCTIONS_TEMPLATE.replace("__CHECK_UPDATE__", CHECK_UPDATE)
    .replace(
        "__RECORD_BINDING__",
        _COVERAGE_BINDING.replace("__APPOINTMENT__", "appointment")
        .replace("__PATIENT__", "patient")
        .replace("__START__", "starts")
        .replace("__END__", "ends"),
    )
    .replace(
        "__APPOINTMENT_BINDING__",
        _COVERAGE_BINDING.replace("__APPOINTMENT__", "NEW.id")
        .replace("__PATIENT__", "NEW.patient_id")
        .replace("__START__", "NEW.start_at")
        .replace("__END__", "NEW.end_at"),
    )
)

SQL = (
    SCHEMA_SQL
    + FUNCTIONS_SQL
    + "SET LOCAL ROLE clinic_resolver;\n"
    + _LIFECYCLE
    + _CAPACITY
    + """
REVOKE ALL ON FUNCTION clinic_app.scheduling_lifecycle_guard(),
 clinic_app.scheduling_capacity_guard()
 FROM PUBLIC,clinic_app,clinic_agent,clinic_owner;
RESET ROLE;
"""
)
