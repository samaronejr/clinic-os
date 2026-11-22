"""Keep native retained-output effects inside the original invocation permission."""

from apps.ai.migrations import _extraction_review_sql, _extraction_sql, _rollout_sql
from apps.ai.migrations._care_sql import SQL as CARE
from apps.ai.migrations._care_sql import _edit, _resolver
from apps.ai.migrations._front_desk_authority_sql import _function
from apps.analytics.migrations._analytics_v1_sql import SQL as ANALYTICS
from apps.retention.migrations._purge_guard_sql import SQL as LEDGER
from apps.scribe.migrations._draft_sql import SQL as SCRIBE

_SOURCE = r"""
CREATE FUNCTION clinic_app.ai_effect_source(requested uuid,clinic uuid)
RETURNS TABLE(configuration_id uuid,purpose text,approval_id uuid)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT i.configuration_id,i.purpose,i.rollout_approval_id
 FROM clinic_app.ai_aiinvocation i
 JOIN clinic_app.ai_aiartifact source ON source.id=i.inputs_ref_id
 WHERE i.id=requested AND i.clinic_id=clinic
 AND i.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND (clinic_app.ai_staff(clinic,i.purpose,source.patient_enrollment_id)
  OR (i.purpose='care_coordination' AND clinic_app.ai_care_staff(clinic)))
$f$;
"""

_ALLOWED = r"""
CREATE FUNCTION clinic_app.ai_effect_allowed(
 requested uuid,clinic uuid,observed jsonb)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.ai_aiinvocation i
 JOIN clinic_app.ai_modelconfiguration m ON m.id=i.configuration_id
 JOIN clinic_app.ai_modelchangerequest permission ON permission.id=i.rollout_approval_id
 JOIN clinic_app.ai_aiartifact source ON source.id=i.inputs_ref_id
 JOIN clinic_app.ai_gatewayswitch gs ON gs.id=1 AND gs.enabled
 WHERE i.id=requested AND i.clinic_id=clinic
 AND i.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND current_setting('transaction_isolation')='read committed'
 AND permission.configuration_id=i.configuration_id
 AND permission.organization_id=i.organization_id AND permission.clinic_id=i.clinic_id
 AND clinic_app.ai_rollout_bound(permission.id,clinic,false)
 AND i.config_version=m.version AND i.purpose=m.purpose
 AND m.state='active' AND m.provider IN('synthetic','recorded_fixture')
 AND m.approval_ref<>'' AND m.retention_terms<>'' AND m.training_terms<>''
 AND gs.approved_regions ? m.region
 AND observed=jsonb_build_object('prompt',m.prompt_sha256,
  'retrieval',m.retrieval_sha256,'schema',m.schema_sha256)
 AND coalesce((clinic_app.ai_organization_policy()->>'enabled')::boolean,false)
 AND coalesce((clinic_app.ai_organization_policy()->'purposes') ? i.purpose,false)
 AND (clinic_app.ai_staff(clinic,i.purpose,source.patient_enrollment_id)
  OR (i.purpose='care_coordination' AND clinic_app.ai_care_staff(clinic)))
 AND NOT EXISTS(SELECT 1 FROM clinic_app.ai_aicircuit circuit
  WHERE circuit.organization_id=i.organization_id AND circuit.configuration_id=m.id
  AND circuit.closed_at IS NULL)
 AND EXISTS(SELECT 1 FROM clinic_app.providers_capabilityversion provider
  WHERE provider.id=(SELECT capability.current_version_id
   FROM clinic_app.providers_providercapability capability
   WHERE capability.key=m.capability_key
   AND (capability.clinic_id=clinic OR capability.clinic_id IS NULL)
   ORDER BY (capability.clinic_id IS NULL),capability.id LIMIT 1)
  AND provider.state NOT IN('degraded','revoked')
  AND provider.region IN('', 'synthetic',m.region)))
$f$;
"""

_CURRENT = r"""
CREATE FUNCTION clinic_app.ai_effect_current(requested uuid,clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.ai_aiinvocation i
 JOIN clinic_app.ai_modelconfiguration m ON m.id=i.configuration_id
 WHERE i.id=requested AND i.clinic_id=clinic AND i.status='succeeded'
 AND i.effect_xid=pg_current_xact_id()::text
 AND clinic_app.ai_effect_allowed(i.id,clinic,
  jsonb_build_object('prompt',m.prompt_sha256,'retrieval',m.retrieval_sha256,
   'schema',m.schema_sha256)))
$f$;
"""

_AUTHORIZE = r"""
CREATE FUNCTION clinic_app.ai_authorize_effect(
 requested uuid,clinic uuid,receipt bytea,kek text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE i clinic_app.ai_aiinvocation; source clinic_app.ai_aiartifact;
 facts jsonb; admitted boolean; stamp text;
BEGIN
 PERFORM pg_advisory_xact_lock_shared(hashtextextended('ai-global-rollout-v1',0));
 SELECT * INTO i FROM clinic_app.ai_aiinvocation WHERE id=requested;
 SELECT * INTO source FROM clinic_app.ai_aiartifact WHERE id=i.inputs_ref_id;
 IF i.id IS NULL OR i.clinic_id IS DISTINCT FROM clinic
 OR i.organization_id IS DISTINCT FROM
  NULLIF(current_setting('app.current_tenant',true),'')::uuid
 OR NOT (clinic_app.ai_staff(clinic,i.purpose,source.patient_enrollment_id)
  OR (i.purpose='care_coordination' AND clinic_app.ai_care_staff(clinic)))
 THEN RETURN false; END IF;
 facts:=convert_from(clinic_app.protected_decrypt(
  kek,'ai.effect.sources',receipt),'UTF8')::jsonb;
 admitted:=facts=jsonb_build_object('subject',requested::text,
  'transaction',pg_current_xact_id()::text,'sources',facts->'sources')
  AND clinic_app.ai_effect_allowed(requested,clinic,facts->'sources');
 stamp:=CASE WHEN admitted THEN pg_current_xact_id()::text ELSE '' END;
 UPDATE clinic_app.ai_aiinvocation SET effect_xid=stamp
  WHERE id=i.id AND effect_xid IS DISTINCT FROM stamp;
 RETURN coalesce(admitted,false);
END $f$;
"""

_STAMP_UPDATE = """
 IF TG_TABLE_NAME='ai_aiinvocation' AND TG_OP='UPDATE' THEN
  IF NEW.effect_xid IS DISTINCT FROM OLD.effect_xid
  AND (to_jsonb(NEW)-'effect_xid') IS NOT DISTINCT FROM
      (to_jsonb(OLD)-'effect_xid') THEN RETURN NEW; END IF;
 END IF;
"""
_LEDGER_PRIOR = _function(LEDGER, "ai_ledger_guard")
_LEDGER_STAMP = _edit(
    _LEDGER_PRIOR,
    " IF TG_TABLE_NAME='ai_aiartifact' AND TG_OP='UPDATE' THEN",
    _STAMP_UPDATE + " IF TG_TABLE_NAME='ai_aiartifact' AND TG_OP='UPDATE' THEN",
)
_LEDGER = _edit(
    _LEDGER_STAMP,
    "  IF NEW.status<>'reserved' OR NEW.operation_id IS NOT NULL",
    "  IF NEW.effect_xid<>'' OR NEW.status<>'reserved' OR NEW.operation_id IS NOT NULL",
)

_ROLLOUT_PRIOR = _function(_rollout_sql.SQL, "ai_rollout_guard")
_ROLLOUT_STAMP = _edit(
    _ROLLOUT_PRIOR,
    " IF TG_TABLE_NAME='ai_aiinvocation' THEN",
    _STAMP_UPDATE + " IF TG_TABLE_NAME='ai_aiinvocation' THEN",
)
_ROLLOUT = _edit(
    _ROLLOUT_STAMP,
    " IF TG_TABLE_NAME='ai_agentrun' THEN",
    """ IF TG_TABLE_NAME='ai_agentrun' THEN
  IF EXISTS(SELECT 1 FROM clinic_app.ai_agentpolicyversion p
   WHERE p.id=NEW.policy_id AND p.capability='care_coordination')
  AND NOT EXISTS(SELECT 1 FROM clinic_app.ai_aiinvocation source
   WHERE source.id=NEW.invocation_id AND source.purpose='care_coordination'
   AND source.configuration_id=NEW.configuration_id
   AND clinic_app.ai_effect_current(source.id,NEW.clinic_id))
  THEN RAISE EXCEPTION 'care invocation permission required' USING ERRCODE='42501';
  END IF;""",
)
_AGENT_PRIOR = _function(_rollout_sql.SQL, "ai_rollout_agent_allowed")
_AGENT = _edit(
    _AGENT_PRIOR,
    " SELECT coalesce(bool_or(c.configuration_id=r.configuration_id",
    " SELECT coalesce(bool_or((p.capability<>'care_coordination'\n"
    " OR clinic_app.ai_effect_current(r.invocation_id,r.clinic_id))\n"
    " AND c.configuration_id=r.configuration_id",
)

_SCRIBE_PRIOR = _function(SCRIBE, "scribe_draft_guard")
_SCRIBE = _edit(
    _SCRIBE_PRIOR,
    "   SELECT * INTO c FROM clinic_app.ai_modelconfiguration "
    "WHERE id=i.configuration_id;",
    """   SELECT * INTO c FROM clinic_app.ai_modelconfiguration
    WHERE id=i.configuration_id;
   IF NEW.action<>'rejected'
   AND NOT clinic_app.ai_effect_current(i.id,e.clinic_id) THEN
    RAISE EXCEPTION 'draft invocation permission required' USING ERRCODE='42501';
   END IF;""",
)
_CARE_PRIOR = _function(CARE, "ai_care_guard")
_CARE_INSERT = _edit(
    _CARE_PRIOR,
    "  OR i.purpose<>'care_coordination' OR i.status<>'succeeded'",
    "  OR i.purpose<>'care_coordination' OR i.status<>'succeeded'\n"
    "  OR NOT clinic_app.ai_effect_current(i.id,NEW.clinic_id)",
)
_CARE_ACCEPT = _edit(
    _CARE_INSERT,
    " IF NEW.state IN ('approved','accepted') THEN",
    """ IF NEW.state IN ('approved','accepted') THEN
  IF NOT clinic_app.ai_effect_current(NEW.invocation_id,NEW.clinic_id) THEN
   RAISE EXCEPTION 'care invocation permission required' USING ERRCODE='42501';
  END IF;""",
)
_CARE = _edit(
    _CARE_ACCEPT,
    "  OR (r.organization_id,r.clinic_id,r.state) IS DISTINCT FROM",
    "  OR NOT EXISTS(SELECT 1 FROM clinic_app.ai_agentrun source\n"
    "   WHERE source.id=r.run_id AND source.invocation_id=NEW.invocation_id)\n"
    "  OR (r.organization_id,r.clinic_id,r.state) IS DISTINCT FROM",
)

_REVIEW_PRIOR = _function(_extraction_review_sql.SQL, "ai_review_extraction")
_REVIEW = _edit(
    _REVIEW_PRIOR,
    " IF decision='confirmed' THEN",
    """ IF decision='confirmed' THEN
  IF NOT clinic_app.ai_effect_current(j.invocation_id,j.clinic_id) THEN
   RAISE EXCEPTION 'extraction invocation permission required' USING ERRCODE='42501';
  END IF;""",
)
_OBSERVATION_PRIOR = _function(_extraction_sql.SQL, "ai_imported_observation")
_OBSERVATION = _edit(
    _OBSERVATION_PRIOR,
    " AND clinic_app.ai_extraction_job_allowed(j.id)",
    " AND clinic_app.ai_extraction_job_allowed(j.id)\n"
    " AND clinic_app.ai_effect_current(j.invocation_id,clinic)",
)
_EXTRACTION_PRIOR = _function(_extraction_sql.SQL, "ai_extraction_guard")
_EXTRACTION = _edit(
    _EXTRACTION_PRIOR,
    "   NEW.reviewed_at:=statement_timestamp();",
    """   IF NEW.status='confirmed'
   AND NOT clinic_app.ai_effect_current(j.invocation_id,j.clinic_id) THEN
    RAISE EXCEPTION 'extraction invocation permission required' USING ERRCODE='42501';
   END IF;
   NEW.reviewed_at:=statement_timestamp();""",
)

_ANALYST_PRIOR = _function(ANALYTICS, "analytics_guard")
_ANALYST = _edit(
    _ANALYST_PRIOR,
    "  IF NEW.decision NOT IN ('accepted','rejected')",
    "  IF NOT clinic_app.ai_effect_current(NEW.invocation_id,NEW.clinic_id)\n"
    "  OR NEW.decision NOT IN ('accepted','rejected')",
)

SQL = "".join(
    (
        "GRANT UPDATE(effect_xid) ON clinic_app.ai_aiinvocation TO clinic_resolver;\n",
        _resolver(
            (
                _SOURCE,
                _ALLOWED,
                _CURRENT,
                _AUTHORIZE,
                _LEDGER,
                _ROLLOUT,
                _AGENT,
                _SCRIBE,
                _CARE,
                _REVIEW,
                _OBSERVATION,
                _EXTRACTION,
                _ANALYST,
            )
        ),
        """
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.ai_effect_allowed(uuid,uuid,jsonb),
 clinic_app.ai_effect_current(uuid,uuid),
 clinic_app.ai_authorize_effect(uuid,uuid,bytea,text),
 clinic_app.ai_effect_source(uuid,uuid) FROM PUBLIC,clinic_app,clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.ai_authorize_effect(uuid,uuid,bytea,text),
 clinic_app.ai_effect_source(uuid,uuid)
 TO clinic_app;
RESET ROLE;
""",
    )
)

REVERSE_SQL = "".join(
    (
        """
SET LOCAL ROLE clinic_resolver;
DO $rollback$
BEGIN
 IF EXISTS(SELECT 1 FROM clinic_app.ai_aiinvocation WHERE effect_xid<>'')
 THEN RAISE EXCEPTION 'retained effect evidence requires restore'
 USING ERRCODE='23514'; END IF;
END $rollback$;
RESET ROLE;
""",
        _resolver(
            (
                _LEDGER_PRIOR,
                _ROLLOUT_PRIOR,
                _AGENT_PRIOR,
                _SCRIBE_PRIOR,
                _CARE_PRIOR,
                _REVIEW_PRIOR,
                _OBSERVATION_PRIOR,
                _EXTRACTION_PRIOR,
                _ANALYST_PRIOR,
            )
        ),
        """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.ai_authorize_effect(uuid,uuid,bytea,text);
DROP FUNCTION clinic_app.ai_effect_current(uuid,uuid);
DROP FUNCTION clinic_app.ai_effect_allowed(uuid,uuid,jsonb);
DROP FUNCTION clinic_app.ai_effect_source(uuid,uuid);
RESET ROLE;
REVOKE UPDATE(effect_xid) ON clinic_app.ai_aiinvocation FROM clinic_resolver;
""",
    )
)
