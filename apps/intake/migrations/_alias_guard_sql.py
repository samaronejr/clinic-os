"""Adjudication and reversible-edge guards; historical bindings never change."""

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.intake_merge_decision_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    candidate clinic_app.intake_duplicatecandidate;
    first_patient uuid;
    second_patient uuid;
    locked_patient uuid;
    verified_at timestamptz;
BEGIN
    SELECT * INTO candidate FROM clinic_app.intake_duplicatecandidate
    WHERE id=NEW.candidate_id AND organization_id=NEW.organization_id
      AND clinic_id=NEW.clinic_id FOR UPDATE;
    IF candidate.id IS NULL OR candidate.state<>'open'
       OR NEW.adjudicator_id::text IS DISTINCT FROM current_setting
           ('app.current_user_id',true)
       OR NOT clinic_app.intake_merge_authorized(NEW.clinic_id,NEW.verified_session_id)
    THEN
        RAISE EXCEPTION USING ERRCODE='42501',
            MESSAGE='duplicate adjudication authority required';
    END IF;
    SELECT e.patient_id INTO first_patient
    FROM clinic_app.intake_patientclinicenrollment e WHERE
        e.id=candidate.enrollment_a_id;
    SELECT e.patient_id INTO second_patient
    FROM clinic_app.intake_patientclinicenrollment e WHERE
        e.id=candidate.enrollment_b_id;
    SELECT s.verified_at INTO verified_at FROM clinic_app.identity_sessionrecord s
    WHERE s.id=NEW.verified_session_id;
    IF NEW.merged_patient_id NOT IN (first_patient,second_patient)
       OR NEW.survivor_patient_id NOT IN (first_patient,second_patient)
       OR NEW.merged_patient_id=NEW.survivor_patient_id
       OR NEW.step_up_verified_at IS DISTINCT FROM verified_at
       OR NEW.merged_version IS DISTINCT FROM COALESCE((
           SELECT max(v.version) FROM clinic_app.intake_patientdemographics v
           WHERE v.patient_id=NEW.merged_patient_id AND
               v.organization_id=NEW.organization_id),0)
       OR NEW.survivor_version IS DISTINCT FROM COALESCE((
           SELECT max(v.version) FROM clinic_app.intake_patientdemographics v
           WHERE v.patient_id=NEW.survivor_patient_id AND
               v.organization_id=NEW.organization_id),0)
       OR NEW.reason IS NULL OR NEW.field_choices IS NULL
       OR NEW.decision NOT IN ('merge','dismiss')
       OR (NEW.decision='merge' AND NEW.result_version IS DISTINCT FROM
           NEW.survivor_version+1)
       OR (NEW.decision='dismiss' AND NEW.result_version IS NOT NULL)
    THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='duplicate adjudication binding is invalid';
    END IF;
    IF NEW.decision='merge' THEN
        FOR locked_patient IN
            SELECT patient_id
            FROM unnest(ARRAY[first_patient,second_patient]) patient_id
            ORDER BY patient_id
        LOOP
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'intake-alias-write-v1:'||NEW.organization_id::text||':'||
                    locked_patient::text,0
            ));
        END LOOP;
    END IF;
    IF NEW.decision='merge' AND EXISTS (
        SELECT 1 FROM clinic_app.intake_patientalias a
        WHERE a.organization_id=NEW.organization_id AND a.active
          AND a.merged_patient_id IN (NEW.merged_patient_id,NEW.survivor_patient_id)
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='patient_aliased';
    END IF;
    NEW.created_at := transaction_timestamp();
    RETURN NEW;
END
$function$;
ALTER FUNCTION clinic_app.intake_merge_decision_guard() OWNER TO clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.intake_merge_decision_guard() FROM PUBLIC,clinic_app;

CREATE FUNCTION clinic_app.intake_duplicate_transition_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF (NEW.id,NEW.organization_id,NEW.clinic_id,NEW.enrollment_a_id,
        NEW.enrollment_b_id,NEW.signals,NEW.created_at)
       IS DISTINCT FROM
       (OLD.id,OLD.organization_id,OLD.clinic_id,OLD.enrollment_a_id,
        OLD.enrollment_b_id,OLD.signals,OLD.created_at)
       OR OLD.state<>'open' OR NEW.state NOT IN ('dismissed','merged')
       OR NOT EXISTS (
           SELECT 1 FROM clinic_app.intake_mergedecision d
           WHERE d.candidate_id=OLD.id AND d.organization_id=OLD.organization_id
             AND d.created_at=transaction_timestamp()
             AND NEW.state=CASE d.decision WHEN 'merge' THEN 'merged' ELSE
                 'dismissed' END
             AND clinic_app.intake_merge_authorized(d.clinic_id,d.verified_session_id)
       )
    THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='duplicate candidate transition is invalid';
    END IF;
    RETURN NEW;
END
$function$;
ALTER FUNCTION clinic_app.intake_duplicate_transition_guard() OWNER TO clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.intake_duplicate_transition_guard() FROM PUBLIC,
    clinic_app;

CREATE FUNCTION clinic_app.intake_patient_alias_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    decision clinic_app.intake_mergedecision;
BEGIN
    SELECT * INTO decision FROM clinic_app.intake_mergedecision
    WHERE id=NEW.merge_decision_id AND organization_id=NEW.organization_id;
    IF decision.id IS NULL OR decision.decision<>'merge'
       OR (NEW.merged_patient_id,NEW.survivor_patient_id)
          IS DISTINCT FROM (decision.merged_patient_id,decision.survivor_patient_id)
    THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='patient alias binding is invalid';
    END IF;
    IF TG_OP='INSERT' THEN
        IF NOT NEW.active OR NEW.deactivated_at IS NOT NULL
           OR NEW.deactivated_by_id IS NOT NULL OR NEW.deactivation_session_id IS
               NOT NULL
           OR NEW.deactivation_reason IS NOT NULL
           OR decision.created_at<>transaction_timestamp()
           OR NOT clinic_app.intake_merge_authorized(decision.clinic_id,
               decision.verified_session_id)
           OR EXISTS (SELECT 1 FROM clinic_app.intake_patientalias a
                      WHERE a.organization_id=NEW.organization_id AND a.active
                        AND a.merged_patient_id IN (NEW.merged_patient_id,
                            NEW.survivor_patient_id))
        THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='patient alias transition is invalid';
        END IF;
        NEW.created_at := transaction_timestamp();
    ELSE
        IF (NEW.id,NEW.organization_id,NEW.merged_patient_id,NEW.survivor_patient_id,
            NEW.merge_decision_id,NEW.created_at)
           IS DISTINCT FROM
           (OLD.id,OLD.organization_id,OLD.merged_patient_id,OLD.survivor_patient_id,
            OLD.merge_decision_id,OLD.created_at)
           OR NOT OLD.active OR NEW.active
           OR NEW.deactivation_reason IS NULL
           OR NEW.deactivated_by_id::text IS DISTINCT FROM current_setting
               ('app.current_user_id',true)
           OR NOT clinic_app.intake_merge_authorized(decision.clinic_id,
               NEW.deactivation_session_id)
        THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='patient alias transition is invalid';
        END IF;
        NEW.deactivated_at := transaction_timestamp();
    END IF;
    RETURN NEW;
END
$function$;
ALTER FUNCTION clinic_app.intake_patient_alias_guard() OWNER TO clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.intake_patient_alias_guard() FROM PUBLIC,clinic_app;

CREATE FUNCTION clinic_app.intake_merge_receipt_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM clinic_app.intake_duplicatecandidate c
        WHERE c.id=NEW.candidate_id AND c.organization_id=NEW.organization_id
          AND c.state=CASE NEW.decision WHEN 'merge' THEN 'merged' ELSE 'dismissed' END
    ) OR (NEW.decision='merge' AND (
        NOT EXISTS (SELECT 1 FROM clinic_app.intake_patientalias a WHERE
            a.merge_decision_id=NEW.id)
        OR NOT EXISTS (
            SELECT 1 FROM clinic_app.intake_patientdemographics v
            JOIN clinic_app.intake_demographicscorrection r ON r.demographics_id=v.id
            WHERE v.organization_id=NEW.organization_id AND
                v.patient_id=NEW.survivor_patient_id
              AND v.version=NEW.result_version AND v.created_at=NEW.created_at
              AND r.actor_id=NEW.adjudicator_id
        )
    )) OR (NEW.decision='dismiss' AND EXISTS (
        SELECT 1 FROM clinic_app.intake_patientalias a WHERE a.merge_decision_id=NEW.id
    ))
    THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='duplicate adjudication requires its receipts';
    END IF;
    RETURN NULL;
END
$function$;
ALTER FUNCTION clinic_app.intake_merge_receipt_guard() OWNER TO clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.intake_merge_receipt_guard() FROM PUBLIC,clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.intake_duplicate_transition_guard(),
    clinic_app.intake_merge_decision_guard(),clinic_app.intake_merge_receipt_guard(),
    clinic_app.intake_patient_alias_guard() TO clinic_owner;
RESET ROLE;
"""

FINALIZE_SQL = """
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.intake_duplicate_transition_guard(),
    clinic_app.intake_merge_decision_guard(),clinic_app.intake_merge_receipt_guard(),
    clinic_app.intake_patient_alias_guard() FROM clinic_owner;
RESET ROLE;
"""
