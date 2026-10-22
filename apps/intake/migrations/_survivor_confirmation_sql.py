"""Bind pending successors and alias confirmations without changing final ACLs."""

from typing import Final

from apps.intake.migrations._merge_transfer_sql import SQL as _MERGE_TRANSFER_SQL
from apps.intake.migrations._survivor_write_sql import SQL as _SURVIVOR_WRITE_SQL

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.intake_survivor_write_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP='UPDATE' THEN
        IF OLD.status NOT IN ('requested','held')
           OR NEW.status NOT IN ('held','scheduled')
           OR NEW.status IS NOT DISTINCT FROM OLD.status THEN
            RETURN NEW;
        END IF;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'intake-alias-write-v1:'||NEW.organization_id::text||':'||NEW.patient_id::text,0
    ));
    IF EXISTS (
        SELECT 1 FROM clinic_app.intake_patientalias a
        WHERE a.organization_id=NEW.organization_id AND a.active
          AND a.merged_patient_id=NEW.patient_id
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            CONSTRAINT='intake_patient_aliased_check',MESSAGE='patient_aliased';
    END IF;
    RETURN NEW;
END
$function$;
GRANT EXECUTE ON FUNCTION clinic_app.intake_survivor_write_guard() TO clinic_owner;
RESET ROLE;

DROP TRIGGER zz_intake_survivor_binding ON clinic_app.scheduling_appointment;
CREATE TRIGGER zz_intake_survivor_binding
BEFORE INSERT OR UPDATE OF status ON clinic_app.scheduling_appointment
FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_survivor_write_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.intake_survivor_write_guard() FROM clinic_owner;
RESET ROLE;

-- Lifecycle cancellation records clinic_request; pending successors stay held.
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.intake_merge_transfer_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $function$
DECLARE
    decision clinic_app.intake_mergedecision;
    original_row record;
    replacement_row record;
BEGIN
    SELECT * INTO decision FROM clinic_app.intake_mergedecision d
    WHERE d.id=NEW.merge_decision_id AND d.organization_id=NEW.organization_id
      AND d.id::text=current_setting('app.current_merge_decision',true)
      AND d.created_at=transaction_timestamp() AND d.decision='merge';
    IF decision.id IS NULL OR NOT clinic_app.intake_merge_mutation_allowed(
        NEW.clinic_id,decision.merged_patient_id
    ) THEN
        RAISE EXCEPTION USING ERRCODE='42501',
            MESSAGE='merge transfer authority required';
    END IF;
    CASE NEW.kind
        WHEN 'appointment' THEN
            SELECT r.*,r.clinic_id AS transfer_clinic_id INTO original_row
            FROM clinic_app.scheduling_appointment r WHERE r.id::text=NEW.original_id;
            SELECT r.*,r.clinic_id AS transfer_clinic_id INTO replacement_row
            FROM clinic_app.scheduling_appointment r
            WHERE r.id::text=NEW.replacement_id;
        WHEN 'waitlist' THEN
            SELECT r.*,r.clinic_id AS transfer_clinic_id INTO original_row
            FROM clinic_app.scheduling_waitlistentry r WHERE r.id::text=NEW.original_id;
            SELECT r.*,r.clinic_id AS transfer_clinic_id INTO replacement_row
            FROM clinic_app.scheduling_waitlistentry r
            WHERE r.id::text=NEW.replacement_id;
        WHEN 'contact' THEN
            SELECT r.*,NULL::uuid AS transfer_clinic_id INTO original_row
            FROM clinic_app.intake_patientcontact r WHERE r.id::text=NEW.original_id;
            SELECT r.*,NULL::uuid AS transfer_clinic_id INTO replacement_row
            FROM clinic_app.intake_patientcontact r WHERE r.id::text=NEW.replacement_id;
        WHEN 'preference' THEN
            SELECT r.*,r.clinic_id AS transfer_clinic_id INTO original_row
            FROM clinic_app.intake_patientchannelpreference r
            WHERE r.id::text=NEW.original_id;
            SELECT r.*,r.clinic_id AS transfer_clinic_id INTO replacement_row
            FROM clinic_app.intake_patientchannelpreference r
            WHERE r.id::text=NEW.replacement_id;
        WHEN 'access_grant' THEN
            SELECT r.*,r.clinic_id AS transfer_clinic_id INTO original_row
            FROM clinic_app.intake_patientaccessgrant r
            WHERE r.id::text=NEW.original_id;
            SELECT r.*,r.clinic_id AS transfer_clinic_id INTO replacement_row
            FROM clinic_app.intake_patientaccessgrant r
            WHERE r.id::text=NEW.replacement_id;
        ELSE
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='merge transfer binding is invalid';
    END CASE;
    IF original_row.patient_id IS DISTINCT FROM decision.merged_patient_id
       OR replacement_row.patient_id IS DISTINCT FROM decision.survivor_patient_id
       OR original_row.organization_id IS DISTINCT FROM NEW.organization_id
       OR replacement_row.organization_id IS DISTINCT FROM NEW.organization_id
       OR (NEW.kind<>'contact' AND (
           original_row.transfer_clinic_id IS DISTINCT FROM NEW.clinic_id
           OR replacement_row.transfer_clinic_id IS DISTINCT FROM NEW.clinic_id
       ))
       OR NEW.action NOT IN ('recreated','kept_survivor','revoked_and_reissued')
    THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='merge transfer binding is invalid';
    END IF;
    IF NEW.kind='appointment' THEN
        IF NEW.action<>'recreated' OR original_row.status<>'cancelled'
           OR original_row.cancellation_reason NOT IN ('duplicate','clinic_request')
           OR replacement_row.status NOT IN ('scheduled','held')
           OR original_row.start_at<>replacement_row.start_at
           OR original_row.end_at<>replacement_row.end_at
           OR original_row.practitioner_id<>replacement_row.practitioner_id
           OR original_row.service_type_id IS DISTINCT FROM
               replacement_row.service_type_id
           OR original_row.resource_ids IS DISTINCT FROM replacement_row.resource_ids
        THEN RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='merge transfer binding is invalid'; END IF;
    ELSIF NEW.kind='waitlist' THEN
        IF NEW.action<>'recreated' OR original_row.state<>'declined'
           OR replacement_row.state<>'waiting'
           OR original_row.practitioner_id<>replacement_row.practitioner_id
           OR original_row.start_at<>replacement_row.start_at
           OR original_row.end_at<>replacement_row.end_at
        THEN RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='merge transfer binding is invalid'; END IF;
    ELSIF NEW.kind='contact' THEN
        IF original_row.channel<>replacement_row.channel
           OR NEW.action='revoked_and_reissued'
           OR (NEW.action='recreated' AND (
               replacement_row.verified_at IS NOT NULL
               OR replacement_row.verified_version<>0
           ))
        THEN RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='merge transfer binding is invalid'; END IF;
    ELSIF NEW.kind='preference' THEN
        IF original_row.opted_in OR original_row.channel<>replacement_row.channel
           OR original_row.purpose<>replacement_row.purpose
           OR NEW.action='revoked_and_reissued'
           OR (NEW.action='recreated' AND replacement_row.opted_in)
        THEN RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='merge transfer binding is invalid'; END IF;
    ELSIF NEW.kind='access_grant' THEN
        IF NEW.action<>'revoked_and_reissued' OR original_row.revoked_at IS NULL
           OR replacement_row.revoked_at IS NOT NULL
           OR replacement_row.consumed_at IS NOT NULL
           OR replacement_row.issued_by_id<>decision.adjudicator_id
           OR replacement_row.operations IS DISTINCT FROM original_row.operations
           OR replacement_row.secret_hash=original_row.secret_hash
        THEN RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='merge transfer binding is invalid'; END IF;
    END IF;
    NEW.created_at := transaction_timestamp();
    RETURN NEW;
END
$function$;
RESET ROLE;
"""

REVERSE_SQL: Final = (
    "SET LOCAL ROLE clinic_resolver;\n"
    "CREATE OR REPLACE FUNCTION clinic_app.intake_survivor_write_guard()"
    + _SURVIVOR_WRITE_SQL.split(
        "CREATE FUNCTION clinic_app.intake_survivor_write_guard()", 1
    )[1].split("ALTER FUNCTION clinic_app.intake_survivor_write_guard()", 1)[0]
    + "CREATE OR REPLACE FUNCTION clinic_app.intake_merge_transfer_guard()"
    + _MERGE_TRANSFER_SQL.split(
        "CREATE FUNCTION clinic_app.intake_merge_transfer_guard()", 1
    )[1].split("ALTER FUNCTION clinic_app.intake_merge_transfer_guard()", 1)[0]
    + """GRANT EXECUTE ON FUNCTION clinic_app.intake_survivor_write_guard()
    TO clinic_owner;
RESET ROLE;
DROP TRIGGER zz_intake_survivor_binding ON clinic_app.scheduling_appointment;
CREATE TRIGGER zz_intake_survivor_binding
BEFORE INSERT ON clinic_app.scheduling_appointment
FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_survivor_write_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.intake_survivor_write_guard() FROM clinic_owner;
RESET ROLE;
"""
)
