"""Exact source authority, encrypted drafts, and a private observation bridge."""

from apps.orders.migrations._access_sql import render
from apps.orders.migrations._guard_sql import CURRENT_GUARD_SQL

TABLES = ("ai_extractionjob", "ai_extractioncandidate")

ACCESS = r"""
GRANT SELECT ON clinic_app.ai_extractionjob,clinic_app.ai_extractioncandidate
 TO clinic_resolver;
GRANT INSERT ON clinic_app.orders_resultobservation TO clinic_resolver;
GRANT UPDATE(status,observation_id,reviewed_by_id,reviewed_at)
 ON clinic_app.ai_extractioncandidate TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ai_extraction_source_allowed(
 clinic uuid,enrollment uuid,version uuid,attachment uuid,digest text)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.has_permission('clinical.read',clinic,enrollment)
 AND ((version IS NOT NULL AND attachment IS NULL AND EXISTS(
  SELECT 1 FROM clinic_app.orders_resultversion v
  JOIN clinic_app.orders_resultmatch m ON m.version_id=v.id
   AND m.clinic_id=v.clinic_id AND m.organization_id=v.organization_id
  JOIN clinic_app.orders_order o ON o.id=m.order_id
   AND o.clinic_id=v.clinic_id AND o.organization_id=v.organization_id
  WHERE v.id=version AND v.clinic_id=clinic AND v.organization_id=__TENANT__
   AND v.sha256=digest AND o.enrollment_id=enrollment
   AND clinic_app.orders_is_current(v.id)
   AND clinic_app.has_permission('result.read',clinic,enrollment)))
 OR (version IS NULL AND attachment IS NOT NULL AND EXISTS(
  SELECT 1 FROM clinic_app.ehr_clinicalattachment a
  JOIN clinic_app.intake_patientclinicenrollment e ON e.patient_id=a.patient_id
   AND e.clinic_id=a.clinic_id AND e.organization_id=a.organization_id
  WHERE a.id=attachment AND a.clinic_id=clinic AND a.organization_id=__TENANT__
   AND a.state='available' AND a.sha256=digest AND e.id=enrollment
   AND clinic_app.ehr_care(a.encounter_id))))
$f$;
CREATE FUNCTION clinic_app.ai_extraction_job_allowed(job uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.ai_extractionjob j
 WHERE j.id=job AND j.organization_id=__TENANT__
 AND clinic_app.ai_extraction_source_allowed(j.clinic_id,j.patient_enrollment_id,
  j.result_version_id,j.attachment_id,j.sha256))
$f$;
CREATE FUNCTION clinic_app.ai_imported_observation(
 observation uuid,version uuid,clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.ai_extractioncandidate c
 JOIN clinic_app.ai_extractionjob j ON j.id=c.job_id
 JOIN clinic_app.orders_resultversion v ON v.id=version
 JOIN clinic_app.orders_resultmatch m ON m.version_id=v.id
 JOIN clinic_app.orders_order o ON o.id=m.order_id
 WHERE c.observation_id=observation AND c.status='confirmed'
 AND c.reviewed_by_id=__ACTOR__ AND c.clinic_id=clinic
 AND c.organization_id=__TENANT__ AND j.clinic_id=clinic
 AND j.organization_id=__TENANT__ AND v.clinic_id=clinic
 AND v.organization_id=__TENANT__ AND m.clinic_id=clinic
 AND m.organization_id=__TENANT__ AND o.clinic_id=clinic
 AND o.organization_id=__TENANT__ AND o.enrollment_id=j.patient_enrollment_id
 AND v.sha256=j.sha256 AND (j.result_version_id IS NULL OR j.result_version_id=v.id)
 AND clinic_app.ai_extraction_job_allowed(j.id)
 AND clinic_app.has_permission('clinical.write',clinic,j.patient_enrollment_id)
 AND clinic_app.orders_is_current(v.id)
 AND NOT EXISTS(SELECT 1 FROM clinic_app.orders_resultacknowledgment a
  WHERE a.version_id=v.id))
$f$;
CREATE FUNCTION clinic_app.ai_extraction_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE j clinic_app.ai_extractionjob; i clinic_app.ai_aiinvocation;
 a clinic_app.ai_aiartifact;
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION 'extraction history retained' USING ERRCODE='23514'; END IF;
 IF NEW.organization_id IS DISTINCT FROM __TENANT__ THEN
  RAISE EXCEPTION 'extraction denied' USING ERRCODE='42501'; END IF;
 IF TG_TABLE_NAME='ai_extractionjob' THEN
  SELECT * INTO i FROM clinic_app.ai_aiinvocation WHERE id=NEW.invocation_id;
  SELECT * INTO a FROM clinic_app.ai_aiartifact WHERE id=i.inputs_ref_id;
  IF TG_OP<>'INSERT' OR NEW.user_id IS DISTINCT FROM __ACTOR__
  OR (NEW.organization_id,NEW.clinic_id,NEW.user_id) IS DISTINCT FROM
   (i.organization_id,i.clinic_id,i.user_id)
  OR a.patient_enrollment_id IS DISTINCT FROM NEW.patient_enrollment_id
  OR i.purpose<>'extraction' OR a.method<>'extract'
  OR NEW.sha256!~'^[0-9a-f]{64}$'
  OR jsonb_typeof(NEW.rotations)<>'array'
  OR jsonb_array_length(NEW.rotations) NOT BETWEEN 1 AND 32
  OR EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.rotations) x
   WHERE x::text NOT IN ('0','90','180','270'))
  OR NOT clinic_app.ai_extraction_source_allowed(NEW.clinic_id,
   NEW.patient_enrollment_id,NEW.result_version_id,NEW.attachment_id,NEW.sha256)
  THEN RAISE EXCEPTION 'extraction denied' USING ERRCODE='42501'; END IF;
 ELSE
  SELECT * INTO j FROM clinic_app.ai_extractionjob WHERE id=NEW.job_id;
  IF (NEW.organization_id,NEW.clinic_id) IS DISTINCT FROM
   (j.organization_id,j.clinic_id) OR NOT clinic_app.ai_extraction_job_allowed(j.id)
  THEN RAISE EXCEPTION 'extraction denied' USING ERRCODE='42501'; END IF;
  IF TG_OP='INSERT' THEN
   PERFORM pg_advisory_xact_lock(hashtextextended('ai-extraction:'||j.id,0));
   SELECT * INTO a FROM clinic_app.ai_aiartifact
    WHERE invocation_id=j.invocation_id AND kind='output';
   SELECT * INTO i FROM clinic_app.ai_aiinvocation WHERE id=j.invocation_id;
   IF j.user_id IS DISTINCT FROM __ACTOR__ OR i.status<>'succeeded'
   OR NEW.status NOT IN ('candidate','ambiguous') OR a.id IS NULL
   OR NEW.expires_at IS DISTINCT FROM a.expires_at
   OR NEW.expires_at<=statement_timestamp()
   OR octet_length(NEW.payload) NOT BETWEEN 68 AND 16384
   OR NEW.observation_id IS NOT NULL OR NEW.reviewed_by_id IS NOT NULL
   OR NEW.reviewed_at IS NOT NULL OR NEW.position NOT BETWEEN 0 AND 127
   OR NEW.position<>(SELECT count(*) FROM clinic_app.ai_extractioncandidate c
    WHERE c.job_id=j.id)
   THEN RAISE EXCEPTION 'extraction proposal denied' USING ERRCODE='23514'; END IF;
  ELSE
   IF (to_jsonb(NEW)-ARRAY['status','observation_id','reviewed_by_id','reviewed_at'])
    IS DISTINCT FROM
    (to_jsonb(OLD)-ARRAY['status','observation_id','reviewed_by_id','reviewed_at'])
   OR OLD.status NOT IN ('candidate','ambiguous')
   OR NOT (NEW.status='rejected' OR (OLD.status='candidate' AND NEW.status='confirmed'))
   OR NEW.reviewed_by_id IS DISTINCT FROM __ACTOR__
   OR NEW.reviewed_at IS NULL OR NEW.expires_at<=statement_timestamp()
   OR NOT clinic_app.has_permission(
    'clinical.write',j.clinic_id,j.patient_enrollment_id)
   THEN RAISE EXCEPTION 'extraction decision denied' USING ERRCODE='23514'; END IF;
   NEW.reviewed_at:=statement_timestamp();
  END IF;
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.ai_extraction_source_allowed(uuid,uuid,uuid,uuid,text)
 FROM PUBLIC;
REVOKE ALL ON FUNCTION clinic_app.ai_extraction_job_allowed(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION clinic_app.ai_imported_observation(uuid,uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
 clinic_app.ai_extraction_source_allowed(uuid,uuid,uuid,uuid,text),
 clinic_app.ai_extraction_job_allowed(uuid),clinic_app.ai_imported_observation(uuid,uuid,uuid)
 TO clinic_app,clinic_owner;
REVOKE ALL ON FUNCTION clinic_app.ai_extraction_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_extraction_guard() TO clinic_owner;
RESET ROLE;
"""

POSTURE = "".join(
    f"""
ALTER TABLE clinic_app.{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{table} FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.{table} FROM PUBLIC,clinic_app,clinic_agent;
GRANT SELECT,INSERT ON clinic_app.{table} TO clinic_app;
CREATE POLICY extraction_owner ON clinic_app.{table} TO clinic_owner
 USING(organization_id=__TENANT__) WITH CHECK(organization_id=__TENANT__);
CREATE POLICY extraction_staff ON clinic_app.{table} TO clinic_app
 USING(organization_id=__TENANT__ AND {predicate})
 WITH CHECK(organization_id=__TENANT__ AND {predicate});
CREATE TRIGGER extraction_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.{table} FOR EACH ROW EXECUTE FUNCTION clinic_app.ai_extraction_guard();
CREATE TRIGGER extraction_no_truncate BEFORE TRUNCATE
 ON clinic_app.{table} FOR EACH STATEMENT
 EXECUTE FUNCTION clinic_app.ai_extraction_guard();
"""
    for table, predicate in (
        (
            "ai_extractionjob",
            "clinic_app.ai_extraction_source_allowed(clinic_id,"
            "patient_enrollment_id,result_version_id,attachment_id,sha256)",
        ),
        (
            "ai_extractioncandidate",
            "clinic_app.ai_extraction_job_allowed(job_id) AND "
            "(status IN ('confirmed','rejected') OR expires_at>statement_timestamp())",
        ),
    )
)
POSTURE += """
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ai_extraction_guard() FROM clinic_owner;
RESET ROLE;
ALTER TABLE clinic_app.ai_extractionjob ADD CONSTRAINT extraction_job_scope
 UNIQUE(organization_id,clinic_id,id);
ALTER TABLE clinic_app.ai_extractionjob ADD CONSTRAINT extraction_invocation_scope
 FOREIGN KEY(organization_id,clinic_id,invocation_id)
 REFERENCES clinic_app.ai_aiinvocation(organization_id,clinic_id,id);
ALTER TABLE clinic_app.ai_extractioncandidate
 ADD CONSTRAINT extraction_candidate_job_scope
 FOREIGN KEY(organization_id,clinic_id,job_id)
 REFERENCES clinic_app.ai_extractionjob(organization_id,clinic_id,id);
"""

# The bridge is resolver-written, so it needs no new runtime observation policy.
# Existing observation ACLs and immutable-source ingestion remain unchanged.
OBSERVATION_GUARD = (
    "SET LOCAL ROLE clinic_resolver;"
    + CURRENT_GUARD_SQL.replace(
        " WHEN 'orders_resultobservation' THEN",
        """ WHEN 'orders_resultobservation' THEN
  IF octet_length(NEW.source)>=68 AND clinic_app.ai_imported_observation(
    NEW.id,NEW.version_id,NEW.clinic_id) THEN RETURN NEW; END IF;""",
    )
    + "RESET ROLE;"
)

SQL = render(ACCESS + POSTURE + OBSERVATION_GUARD)
