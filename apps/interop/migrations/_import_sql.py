"""Frozen interop import v1 authority, state machine, visibility and recovery SQL.

Import authority is the clinic staff-management bundle: ``staff.clinic``
(clinic manager, legacy clinic admin) or ``staff.organization`` (organization
admin, owner) at the exact clinic, never a machine principal. One guard
trigger binds every row to its tenant, clinic and job, refuses UPDATE except
the batch ``pending -> committed`` step and refuses DELETE and TRUNCATE.

Rollback never deletes: ``interop_hide_reversible`` records hide markers for
imported enrollments that nothing else references yet, and the restrictive
``interop_import_visibility`` policies hide exactly those enrollments (and
their review hints) from every staff and owner read.
"""

from typing import Final

TABLES: Final = (
    "interop_importmapping",
    "interop_importjob",
    "interop_importfile",
    "interop_importtransition",
    "interop_importbatch",
    "interop_importedrecord",
)
HIDDEN: Final = "interop_hiddenenrollment"
TENANT: Final = "NULLIF(current_setting('app.current_tenant',true),'')::uuid"
NO_MACHINE_CONTEXT: Final = (
    "NULLIF(current_setting('app.current_principal',true),'') IS NULL"
)
# Relations whose rows mean the imported patient is already in use. The live
# catalog test (tests/interop/test_posture.py) requires every foreign key to a
# patient or enrollment identity to be listed here or in IMPORT_OWNED/DERIVED.
PATIENT_ACTIVITY: Final = (
    ("billing_invoice", "patient_id"),
    ("consent_consentacceptance", "patient_id"),
    ("consent_refusalrecord", "patient_id"),
    ("ehr_clinicalattachment", "patient_id"),
    ("ehr_encounter", "patient_id"),
    ("ehr_encounteraddendum", "patient_id"),
    ("ehr_episode", "patient_id"),
    ("ehr_historyassessment", "patient_id"),
    ("ehr_medicationstatement", "patient_id"),
    ("ehr_observation", "patient_id"),
    ("ehr_timelineprojection", "patient_id"),
    ("ehr_timelinerefresh", "patient_id"),
    ("intake_datasubjectrequest", "patient_id"),
    ("intake_emergencycontact", "patient_id"),
    ("intake_insurancemembership", "patient_id"),
    ("intake_mergedecision", "merged_patient_id"),
    ("intake_mergedecision", "survivor_patient_id"),
    ("intake_patientaccessgrant", "patient_id"),
    ("intake_patientaddress", "patient_id"),
    ("intake_patientalias", "merged_patient_id"),
    ("intake_patientalias", "survivor_patient_id"),
    ("intake_patientchannelpreference", "patient_id"),
    ("intake_patientcontact", "patient_id"),
    ("intake_patientsession", "patient_id"),
    ("intake_questionnaireresponse", "patient_id"),
    ("prescription_prescriptiondocument", "patient_id"),
    ("prescription_prescriptiondocumentrelease", "patient_id"),
    ("prescription_prescriptiondocumentrevocation", "patient_id"),
    ("prescription_prescriptiondraft", "patient_id"),
    ("prescription_signatureoperation", "patient_id"),
    ("retention_recordexport", "patient_id"),
    ("retention_recordrelease", "patient_id"),
    ("scheduling_appointment", "patient_id"),
    ("scheduling_appointmentseries", "patient_id"),
    ("scheduling_waitlistentry", "patient_id"),
    ("teleconsult_teleconsultsession", "patient_id"),
)
ENROLLMENT_ACTIVITY: Final = (
    ("ai_aiartifact", "patient_enrollment_id"),
    ("ai_careproposal", "enrollment_id"),
    ("ai_extractionjob", "patient_enrollment_id"),
    ("ai_frontdeskthread", "verified_enrollment_id"),
    ("comms_conversation", "enrollment_id"),
    ("comms_participant", "enrollment_id"),
    ("consent_consentacceptance", "enrollment_id"),
    ("consent_refusalrecord", "enrollment_id"),
    ("crm_campaignsend", "enrollment_id"),
    ("crm_lead", "enrollment_id"),
    ("crm_marketingpreference", "enrollment_id"),
    ("identity_careteammembership", "patient_enrollment_id"),
    ("identity_patientdelegategrant", "delegate_enrollment_id"),
    ("identity_patientdelegategrant", "represented_enrollment_id"),
    ("insurance_insurancemembership", "enrollment_id"),
    ("intake_datasubjectrequest", "enrollment_id"),
    ("intake_datasubjectrequest", "requester_enrollment_id"),
    ("intake_patientaccessgrant", "enrollment_id"),
    ("intake_patientsession", "acting_for_enrollment_id"),
    ("intake_patientsession", "enrollment_id"),
    ("intake_questionnaireresponse", "enrollment_id"),
    ("orders_order", "enrollment_id"),
    ("scheduling_waitlistentry", "enrollment_id"),
    ("scribe_recordingsession", "enrollment_id"),
    ("workflows_careplan", "enrollment_id"),
    ("workflows_proassignment", "enrollment_id"),
    ("workflows_recall", "enrollment_id"),
    ("workflows_releasedelivery", "enrollment_id"),
    ("workflows_urgentconcern", "enrollment_id"),
)
# Rows the import itself writes: activity is only what exceeds the import.
IMPORT_OWNED: Final = (
    ("intake_patientclinicenrollment", "patient_id"),
    ("intake_patientdemographics", "patient_id"),
    ("intake_patientdemographics", "enrollment_id"),
    ("intake_patientidentifier", "patient_id"),
    ("interop_importedrecord", "patient_id"),
    ("interop_importedrecord", "enrollment_id"),
    ("interop_hiddenenrollment", "enrollment_id"),
)
# Rows that cannot exist without a checked relation, or that are review hints.
DERIVED: Final = (
    # One correction receipt per demographics version (versions are checked).
    ("intake_demographicscorrection", "patient_id"),
    # Appended only by identity/contact changes, which are checked themselves.
    ("intake_patientduplicateindex", "patient_id"),
    # Review hints; the visibility policy hides them with the enrollment.
    ("intake_duplicatecandidate", "enrollment_a_id"),
    ("intake_duplicatecandidate", "enrollment_b_id"),
    # Contact events reference a checked contact or channel preference.
    ("intake_patientcontactevent", "patient_id"),
    # AI-use disclosures require a (checked) encounter.
    ("consent_aiusedisclosure", "patient_id"),
)
# The activity check reads these through the resolver; everything else it
# reads already carries a resolver SELECT grant from its owning app.
RESOLVER_ACTIVITY_GRANTS: Final = (
    "comms_participant",
    "intake_emergencycontact",
    "intake_patientaddress",
)


def _exists(table: str, condition: str) -> str:
    # Identifiers come only from the frozen constants above, never from input.
    return f"EXISTS(SELECT 1 FROM clinic_app.{table} x WHERE {condition})"  # noqa: S608


def activity_predicate(alias: str) -> str:
    """Return the static SQL predicate: this imported patient is in use."""
    clauses = [
        _exists(table, f"x.{column}={alias}.patient_id")
        for table, column in PATIENT_ACTIVITY
    ]
    clauses += [
        _exists(table, f"x.{column}={alias}.enrollment_id")
        for table, column in ENROLLMENT_ACTIVITY
    ]
    clauses += [
        _exists(
            "intake_patientclinicenrollment",
            f"x.patient_id={alias}.patient_id AND x.id<>{alias}.enrollment_id",
        ),
        _exists(
            "intake_patientdemographics",
            f"x.patient_id={alias}.patient_id AND x.version>1",
        ),
        "(SELECT count(*) FROM clinic_app.intake_patientidentifier x"  # noqa: S608
        f" WHERE x.patient_id={alias}.patient_id)>{alias}.identifier_count",
    ]
    return "\n  OR ".join(clauses)


FUNCTIONS: Final = (
    """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.interop_allowed(requested_clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT __NO_MACHINE__
 AND EXISTS(SELECT 1 FROM clinic_app.identity_clinic c WHERE c.id=requested_clinic
  AND c.organization_id=__TENANT__)
 AND (clinic_app.has_permission('staff.clinic',requested_clinic,NULL)
  OR clinic_app.has_permission('staff.organization',requested_clinic,NULL))
$f$;

CREATE FUNCTION clinic_app.interop_record_visible(
 requested_job uuid, requested_clinic uuid, requested_enrollment uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT __NO_MACHINE__
 AND NOT EXISTS(SELECT 1 FROM clinic_app.interop_importtransition t
  WHERE t.job_id=requested_job AND t.state='rolled_back')
 AND clinic_app.has_permission('clinical.read',requested_clinic,requested_enrollment)
$f$;

CREATE FUNCTION clinic_app.interop_batch_scope(requested_batch uuid)
RETURNS TABLE(organization_id uuid, clinic_id uuid, actor_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT b.organization_id, b.clinic_id, b.requested_by_id
 FROM clinic_app.interop_importbatch b WHERE b.id=requested_batch
$f$;

CREATE FUNCTION clinic_app.interop_guard()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE tenant uuid := __TENANT__;
 runtime boolean := current_setting('role') IN ('clinic_app','clinic_agent')
 OR (current_setting('role')='none' AND session_user IN ('clinic_app','clinic_agent'));
 actor uuid; proof bytea; j clinic_app.interop_importjob;
 latest clinic_app.interop_importtransition;
 approved clinic_app.interop_importtransition;
 b clinic_app.interop_importbatch; m clinic_app.interop_importmapping;
BEGIN
 IF TG_OP='DELETE' OR (TG_OP='UPDATE' AND TG_TABLE_NAME<>'interop_importbatch') THEN
  RAISE EXCEPTION 'interop_immutable' USING ERRCODE='23514'; END IF;
 IF NEW.organization_id IS DISTINCT FROM tenant OR NOT EXISTS(
  SELECT 1 FROM clinic_app.identity_clinic c WHERE c.id=NEW.clinic_id
  AND c.organization_id=tenant) THEN
  RAISE EXCEPTION 'interop_denied' USING ERRCODE='42501'; END IF;
 -- One clinic lock orders every import command, batch and rollback.
 PERFORM pg_advisory_xact_lock(hashtextextended('interop:'||NEW.clinic_id::text,0));
 IF TG_TABLE_NAME NOT IN ('interop_importmapping','interop_importjob') THEN
  SELECT * INTO j FROM clinic_app.interop_importjob x WHERE x.id=NEW.job_id;
  IF j.id IS NULL OR j.organization_id<>tenant OR j.clinic_id<>NEW.clinic_id THEN
   RAISE EXCEPTION 'interop_denied' USING ERRCODE='42501'; END IF;
  SELECT * INTO latest FROM clinic_app.interop_importtransition t
   WHERE t.job_id=j.id ORDER BY t.sequence DESC LIMIT 1;
 END IF;
 IF TG_TABLE_NAME='interop_hiddenenrollment' THEN
  -- Only the rollback resolver holds INSERT; it hides this job's own rows.
  IF latest.state IS DISTINCT FROM 'rolled_back' OR NOT EXISTS(
   SELECT 1 FROM clinic_app.interop_importedrecord r WHERE r.job_id=j.id
   AND r.kind='patient' AND r.enrollment_id=NEW.enrollment_id) THEN
   RAISE EXCEPTION 'interop_denied' USING ERRCODE='42501'; END IF;
  NEW.hidden_at:=statement_timestamp();
  RETURN NEW;
 END IF;
 IF runtime THEN
  actor:=(SELECT u.id FROM clinic_app.load_current_user() u WHERE u.is_active);
  IF actor IS NULL OR NOT clinic_app.interop_allowed(NEW.clinic_id) THEN
   RAISE EXCEPTION 'interop_denied' USING ERRCODE='42501'; END IF;
  -- Comparing with or writing patient identity also needs demographics access.
  IF TG_TABLE_NAME IN ('interop_importbatch','interop_importedrecord')
   AND NOT clinic_app.has_permission('demographics.read',NEW.clinic_id,NULL) THEN
   RAISE EXCEPTION 'interop_denied' USING ERRCODE='42501';
  ELSIF TG_TABLE_NAME='interop_importtransition' THEN
   IF NEW.state='dry_run'
    AND NOT clinic_app.has_permission('demographics.read',NEW.clinic_id,NULL) THEN
    RAISE EXCEPTION 'interop_denied' USING ERRCODE='42501'; END IF;
  END IF;
 END IF;
 IF TG_TABLE_NAME='interop_importmapping' THEN
  IF runtime AND NEW.created_by_id IS DISTINCT FROM actor THEN
   RAISE EXCEPTION 'interop_denied' USING ERRCODE='42501'; END IF;
  IF NEW.version<>coalesce((SELECT max(x.version)
   FROM clinic_app.interop_importmapping x
   WHERE x.clinic_id=NEW.clinic_id AND x.code=NEW.code),0)+1 THEN
   RAISE EXCEPTION 'interop_stale' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='interop_importjob' THEN
  IF runtime AND NEW.created_by_id IS DISTINCT FROM actor THEN
   RAISE EXCEPTION 'interop_denied' USING ERRCODE='42501'; END IF;
 ELSIF TG_TABLE_NAME='interop_importfile' THEN
  -- Sources and quarantined attachments arrive only with the upload.
  IF latest.id IS NOT NULL THEN
   RAISE EXCEPTION 'interop_transition' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='interop_importtransition' THEN
  IF runtime AND NEW.actor_id IS DISTINCT FROM actor THEN
   RAISE EXCEPTION 'interop_denied' USING ERRCODE='42501'; END IF;
  IF NEW.sequence<>coalesce(latest.sequence,0)+1 THEN
   RAISE EXCEPTION 'interop_stale' USING ERRCODE='23514'; END IF;
  IF NOT ((latest.id IS NULL AND NEW.state='uploaded')
   OR (latest.state='uploaded' AND NEW.state='profiled')
   OR (latest.state IN ('profiled','mapped','dry_run') AND NEW.state='mapped')
   OR (latest.state='mapped' AND NEW.state='dry_run')
   OR (latest.state='dry_run' AND NEW.state='approved')
   OR (latest.state='approved' AND NEW.state='committed')
   OR (latest.state IN ('uploaded','profiled','mapped','dry_run','approved','committed')
    AND NEW.state='rolled_back')) THEN
   RAISE EXCEPTION 'interop_transition' USING ERRCODE='23514'; END IF;
  IF (NEW.state IN ('profiled','dry_run','committed') AND NEW.report IS NULL)
   OR (NEW.state='uploaded' AND NOT EXISTS(SELECT 1 FROM clinic_app.interop_importfile f
    WHERE f.job_id=j.id AND f.role='source'))
   OR (NEW.state IN ('uploaded','profiled') AND NEW.mapping_id IS NOT NULL)
   OR (NEW.state IN ('dry_run','approved','committed')
    AND NEW.mapping_id IS DISTINCT FROM latest.mapping_id)
   OR (NEW.state='dry_run' AND (NEW.digest=''
    OR NEW.valid_count+NEW.rejected_count<>NEW.row_count))
   OR (NEW.state IN ('approved','committed') AND (NEW.digest=''
    OR NEW.row_count<>latest.row_count OR NEW.valid_count<>latest.valid_count
    OR NEW.rejected_count<>latest.rejected_count
    OR NEW.duplicate_count<>latest.duplicate_count))
   OR (NEW.state='approved' AND (NEW.rollback_window_hours IS NULL
    OR NEW.rollback_window_hours NOT BETWEEN 1 AND 720))
   OR (NEW.state<>'approved' AND NEW.rollback_window_hours IS NOT NULL)
   OR ((NEW.state='rolled_back')<>(NEW.reason<>'')) THEN
   RAISE EXCEPTION 'interop_invalid' USING ERRCODE='23514'; END IF;
  IF NEW.state='mapped' THEN
   SELECT * INTO m FROM clinic_app.interop_importmapping x WHERE x.id=NEW.mapping_id;
   IF m.id IS NULL OR m.organization_id<>tenant OR m.clinic_id<>NEW.clinic_id
    OR m.source_kind<>j.source_kind THEN
    RAISE EXCEPTION 'interop_invalid' USING ERRCODE='23514'; END IF;
  ELSIF NEW.state='committed' AND (NOT EXISTS(
   SELECT 1 FROM clinic_app.interop_importbatch x WHERE x.job_id=j.id) OR EXISTS(
   SELECT 1 FROM clinic_app.interop_importbatch x WHERE x.job_id=j.id
   AND x.state<>'committed')) THEN
   RAISE EXCEPTION 'interop_batches_pending' USING ERRCODE='23514';
  ELSIF NEW.state='rolled_back' AND latest.state IN ('approved','committed') THEN
   proof:=decode(NULLIF(current_setting('app.current_import_session',true),''),
    'hex');
   IF runtime AND (proof IS NULL
    OR NOT clinic_app.identity_session_verified(proof)) THEN
    RAISE EXCEPTION 'interop_step_up' USING ERRCODE='42501'; END IF;
   SELECT * INTO approved FROM clinic_app.interop_importtransition t
    WHERE t.job_id=j.id AND t.state='approved' ORDER BY t.sequence DESC LIMIT 1;
   IF latest.state='committed' AND statement_timestamp()>latest.created_at
    +make_interval(hours=>approved.rollback_window_hours) THEN
    RAISE EXCEPTION 'interop_rollback_window' USING ERRCODE='23514'; END IF;
  END IF;
 ELSIF TG_TABLE_NAME='interop_importbatch' THEN
  IF TG_OP='INSERT' THEN
   IF latest.state IS DISTINCT FROM 'approved' OR NEW.state<>'pending'
    OR NEW.committed_count<>0 OR NEW.committed_at IS NOT NULL THEN
    RAISE EXCEPTION 'interop_transition' USING ERRCODE='23514'; END IF;
   IF runtime THEN
    proof:=decode(NULLIF(current_setting('app.current_import_session',true),''),'hex');
    IF NEW.requested_by_id IS DISTINCT FROM actor OR proof IS NULL
     OR NOT clinic_app.identity_session_verified(proof) THEN
     RAISE EXCEPTION 'interop_step_up' USING ERRCODE='42501'; END IF;
   END IF;
  ELSE
   IF (to_jsonb(NEW)-ARRAY['state','committed_count','committed_at'])
    IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','committed_count','committed_at'])
    OR OLD.state<>'pending' OR NEW.state<>'committed'
    OR latest.state IS DISTINCT FROM 'approved' OR NEW.committed_count<>(
     SELECT count(*) FROM clinic_app.interop_importedrecord r
     WHERE r.batch_id=NEW.id) THEN
    RAISE EXCEPTION 'interop_transition' USING ERRCODE='23514'; END IF;
   NEW.committed_at:=statement_timestamp();
  END IF;
 ELSIF TG_TABLE_NAME='interop_importedrecord' THEN
  SELECT * INTO b FROM clinic_app.interop_importbatch x WHERE x.id=NEW.batch_id;
  IF b.id IS NULL OR b.job_id<>j.id OR b.state<>'pending' OR b.dataset<>NEW.dataset
   OR NEW.row_number NOT BETWEEN b.first_row AND b.last_row
   OR latest.state IS DISTINCT FROM 'approved' THEN
   RAISE EXCEPTION 'interop_transition' USING ERRCODE='23514'; END IF;
  -- A patient record binds only the enrollment this exact job row created.
  IF (NEW.kind='patient' AND NOT EXISTS(
    SELECT 1 FROM clinic_app.intake_patientclinicenrollment e
    WHERE e.id=NEW.enrollment_id AND e.patient_id=NEW.patient_id
    AND e.clinic_id=NEW.clinic_id AND e.organization_id=tenant
    AND e.idempotency_key=substr(encode(sha256(convert_to(
     NEW.job_id::text||':patients:'||NEW.row_number::text,'UTF8')),'hex'),1,32)::uuid))
   OR (NEW.kind<>'patient' AND NOT EXISTS(
    SELECT 1 FROM clinic_app.interop_importedrecord p WHERE p.job_id=j.id
    AND p.kind='patient' AND p.enrollment_id=NEW.enrollment_id
    AND p.patient_id=NEW.patient_id))
   OR (NEW.file_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM clinic_app.interop_importfile f WHERE f.id=NEW.file_id
    AND f.job_id=j.id AND f.role='attachment' AND f.scan_verdict='clean')) THEN
   RAISE EXCEPTION 'interop_denied' USING ERRCODE='42501'; END IF;
 END IF;
 RETURN NEW;
END $f$;

CREATE FUNCTION clinic_app.interop_no_truncate()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 RAISE EXCEPTION 'interop_immutable' USING ERRCODE='23514';
END $f$;

CREATE FUNCTION clinic_app.interop_hide_reversible(requested_job uuid)
RETURNS TABLE(imported_enrollment uuid, hidden boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE j clinic_app.interop_importjob; latest text;
BEGIN
 SELECT * INTO j FROM clinic_app.interop_importjob x WHERE x.id=requested_job
  AND x.organization_id=__TENANT__;
 SELECT t.state INTO latest FROM clinic_app.interop_importtransition t
  WHERE t.job_id=j.id ORDER BY t.sequence DESC LIMIT 1;
 IF j.id IS NULL OR NOT clinic_app.interop_allowed(j.clinic_id)
  OR latest IS DISTINCT FROM 'rolled_back' OR EXISTS(
  SELECT 1 FROM clinic_app.interop_hiddenenrollment h WHERE h.job_id=j.id) THEN
  RAISE EXCEPTION 'interop_denied' USING ERRCODE='42501'; END IF;
 RETURN QUERY
 WITH candidates AS (
  SELECT r.patient_id, r.enrollment_id, r.identifier_count
  FROM clinic_app.interop_importedrecord r
  WHERE r.job_id=j.id AND r.kind='patient'),
 reversible AS (
  SELECT c.enrollment_id FROM candidates c WHERE NOT (
  __ACTIVITY__)),
 inserted AS (
  INSERT INTO clinic_app.interop_hiddenenrollment
   (id, organization_id, clinic_id, enrollment_id, job_id, hidden_at)
  SELECT gen_random_uuid(), j.organization_id, j.clinic_id, v.enrollment_id, j.id,
   statement_timestamp() FROM reversible v
  RETURNING clinic_app.interop_hiddenenrollment.enrollment_id AS kept)
 SELECT c.enrollment_id, EXISTS(SELECT 1 FROM inserted i WHERE i.kept=c.enrollment_id)
 FROM candidates c ORDER BY c.enrollment_id;
END $f$;
RESET ROLE;
""".replace("__NO_MACHINE__", NO_MACHINE_CONTEXT)
    .replace("__TENANT__", TENANT)
    .replace("__ACTIVITY__", activity_predicate("c"))
)
TENANT_CONDITION: Final = (
    "organization_id = NULLIF(pg_catalog.current_setting('app.current_tenant', true),"
    " '')::pg_catalog.uuid"
)
VISIBILITY: Final = (
    ("intake_patientclinicenrollment", "intake_patientclinicenrollment.id"),
    (
        "intake_duplicatecandidate",
        "intake_duplicatecandidate.enrollment_a_id,"
        "intake_duplicatecandidate.enrollment_b_id",
    ),
)


def contract_sql() -> str:
    """Publish the import tables with exact grants, policies and guards."""
    sql = "".join(
        f"REVOKE ALL ON clinic_app.{table} FROM PUBLIC,clinic_app,clinic_agent;"
        f"GRANT SELECT,INSERT ON clinic_app.{table} TO clinic_app;"
        f"GRANT SELECT ON clinic_app.{table} TO clinic_resolver;"
        for table in TABLES
    )
    sql += f"""
REVOKE ALL ON clinic_app.{HIDDEN} FROM PUBLIC,clinic_app,clinic_agent;
GRANT SELECT ON clinic_app.{HIDDEN} TO clinic_app;
GRANT SELECT,INSERT ON clinic_app.{HIDDEN} TO clinic_resolver;
GRANT UPDATE(state,committed_count,committed_at)
 ON clinic_app.interop_importbatch TO clinic_app;
"""
    sql += "".join(
        f"GRANT SELECT ON clinic_app.{table} TO clinic_resolver;"
        for table in RESOLVER_ACTIVITY_GRANTS
    )
    sql += FUNCTIONS
    record_read = "clinic_app.interop_record_visible(job_id,clinic_id,enrollment_id)"
    for table in TABLES:
        read = "clinic_app.interop_allowed(clinic_id)"
        if table == "interop_importedrecord":
            read = f"({read} OR {record_read})"
        elif table == "interop_importfile":
            read = (
                f"({read} OR EXISTS(SELECT 1 FROM clinic_app.interop_importedrecord r"  # noqa: S608
                " WHERE r.file_id=interop_importfile.id"
                " AND clinic_app.interop_record_visible("
                "r.job_id,r.clinic_id,r.enrollment_id)))"
            )
        sql += f"""
ALTER TABLE clinic_app.{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{table} FORCE ROW LEVEL SECURITY;
CREATE POLICY interop_owner ON clinic_app.{table} TO clinic_owner
 USING(organization_id={TENANT}) WITH CHECK(organization_id={TENANT});
CREATE POLICY interop_read ON clinic_app.{table} FOR SELECT TO clinic_app
 USING(organization_id={TENANT} AND {read});
CREATE POLICY interop_insert ON clinic_app.{table} FOR INSERT TO clinic_app
 WITH CHECK(organization_id={TENANT} AND clinic_app.interop_allowed(clinic_id));
"""
    sql += f"""
CREATE POLICY interop_update ON clinic_app.interop_importbatch FOR UPDATE TO clinic_app
 USING(organization_id={TENANT} AND clinic_app.interop_allowed(clinic_id))
 WITH CHECK(organization_id={TENANT} AND clinic_app.interop_allowed(clinic_id));
ALTER TABLE clinic_app.{HIDDEN} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{HIDDEN} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON clinic_app.{HIDDEN}
 AS PERMISSIVE FOR ALL TO PUBLIC
 USING ({TENANT_CONDITION}) WITH CHECK ({TENANT_CONDITION});
"""
    for table in (*TABLES, HIDDEN):
        sql += f"""
CREATE TRIGGER interop_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.{table} FOR EACH ROW EXECUTE FUNCTION clinic_app.interop_guard();
CREATE TRIGGER interop_no_truncate BEFORE TRUNCATE ON clinic_app.{table}
 FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.interop_no_truncate();
"""
    for table, columns in VISIBILITY:
        # Fixed table/column constants only; nothing here comes from input.
        sql += f"""
CREATE POLICY interop_import_visibility ON clinic_app.{table}
 AS RESTRICTIVE FOR SELECT TO clinic_app,clinic_owner
 USING (NOT EXISTS(SELECT 1 FROM clinic_app.{HIDDEN} h
  WHERE h.enrollment_id IN ({columns})));
"""  # noqa: S608
    sql += "SET LOCAL ROLE clinic_resolver;"
    for name, signature, app_execute in (
        ("interop_allowed", "uuid", True),
        ("interop_record_visible", "uuid,uuid,uuid", True),
        ("interop_batch_scope", "uuid", True),
        ("interop_guard", "", False),
        ("interop_no_truncate", "", False),
        ("interop_hide_reversible", "uuid", True),
    ):
        sql += f"REVOKE ALL ON FUNCTION clinic_app.{name}({signature}) FROM PUBLIC;"
        if app_execute:
            sql += (
                f"GRANT EXECUTE ON FUNCTION clinic_app.{name}({signature})"
                " TO clinic_app;"
            )
    sql += "RESET ROLE;"
    return sql


SQL: Final = contract_sql()
