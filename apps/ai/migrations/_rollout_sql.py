"""Stage evidence extends the released evaluation ledger and native admission."""

from apps.ai.migrations import _evaluation_sql
from apps.ai.migrations._care_sql import _edit, _function, _resolver
from apps.ai.migrations._revenue_sql import _EVAL as _PRIOR_RECORD

_RECORD = _edit(
    _PRIOR_RECORD,
    " IF TG_TABLE_NAME='ai_evaluationprotocol' THEN",
    """
 IF TG_TABLE_NAME='ai_modelchangerequest' THEN
  IF TG_OP='UPDATE' AND OLD.status='approved'
  AND NEW.status IN('killed','invalidated') THEN
   IF NOT clinic_app.ai_eval_staff(NEW.clinic_id,'owner')
   OR (to_jsonb(NEW)-'status') IS DISTINCT FROM (to_jsonb(OLD)-'status')
   THEN RAISE EXCEPTION 'rollout stop denied' USING ERRCODE='42501'; END IF;
   RETURN NEW;
  END IF;
 END IF;
 IF TG_TABLE_NAME='ai_evaluationprotocol' THEN""",
)

SQL = r"""
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ai_rollout_authority(
 subject uuid, requester uuid, approver uuid)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 WITH evidence AS (
 SELECT r.*,p.registered_by_id FROM clinic_app.ai_evaluationrun r
 JOIN clinic_app.ai_evaluationprotocol p ON p.id=r.protocol_id WHERE r.id=subject
 ), required AS (
 SELECT DISTINCT human,role FROM evidence r CROSS JOIN LATERAL (
 VALUES (r.registered_by_id,'owner'),(r.registered_by_id,'physician'),
 (r.created_by_id,'owner'),(r.created_by_id,'physician'),
 (r.rater_one_id,'physician'),(r.rater_two_id,'physician'),
 (r.adjudicator_id,'physician'),(requester,'owner'),(approver,'owner')
 ) needed(human,role)
 ), members AS (
 SELECT m.id FROM evidence r JOIN required n ON true
 JOIN clinic_app.identity_userclinicrole m
 ON m.user_id=n.human AND m.role=n.role AND m.clinic_id=r.clinic_id
 AND m.organization_id=r.organization_id AND m.revoked_at IS NULL
 JOIN clinic_app.identity_user u ON u.id=m.user_id AND u.is_active
 )
 SELECT CASE WHEN count(*)=(SELECT count(*) FROM required) AND count(*)>0
 THEN encode(clinic_app.digest(convert_to(
 string_agg(id::text,',' ORDER BY id::text),'UTF8')::bytea,'sha256'::text),'hex')
 END FROM members
$f$;
CREATE FUNCTION clinic_app.ai_rollout_status(subject uuid)
RETURNS text LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE c clinic_app.ai_modelchangerequest; r clinic_app.ai_evaluationrun;
 p clinic_app.ai_evaluationprotocol; m clinic_app.ai_modelconfiguration;
BEGIN
 SELECT * INTO c FROM clinic_app.ai_modelchangerequest WHERE id=subject;
 IF c.id IS NULL THEN RETURN 'invalidated'; END IF;
 IF c.status<>'approved' THEN RETURN c.status; END IF;
 SELECT * INTO r FROM clinic_app.ai_evaluationrun WHERE id=c.run_id;
 SELECT * INTO p FROM clinic_app.ai_evaluationprotocol WHERE id=r.protocol_id;
 SELECT * INTO m FROM clinic_app.ai_modelconfiguration WHERE id=c.configuration_id;
 IF c.switch_generation<>coalesce((
 SELECT generation FROM clinic_app.ai_gatewayswitch WHERE id=1),0)
 OR EXISTS(SELECT 1 FROM clinic_app.identity_clinicconfiguration cfg
 WHERE cfg.organization_id=c.organization_id AND cfg.created_at>c.created_at
 AND cfg.ai_policy<>'{}'::jsonb AND (
 NOT coalesce((cfg.ai_policy->>'enabled')::boolean,false)
 OR NOT coalesce((cfg.ai_policy->'purposes') ? m.purpose,false))
 AND coalesce((SELECT coalesce((previous.ai_policy->>'enabled')::boolean,false)
  AND coalesce((previous.ai_policy->'purposes') ? m.purpose,false)
  FROM clinic_app.identity_clinicconfiguration previous
  WHERE previous.organization_id=cfg.organization_id
  AND previous.created_at<cfg.created_at
  ORDER BY previous.created_at DESC,previous.id DESC LIMIT 1),false))
 THEN RETURN 'killed'; END IF;
 IF r.id IS NULL OR p.id IS NULL OR m.id IS NULL OR m.state='retired'
 OR c.configuration_id<>r.configuration_id OR r.configuration_version<>m.version
 OR r.versions->>'prompt' IS DISTINCT FROM m.prompt_sha256
 OR r.versions->>'retrieval' IS DISTINCT FROM m.retrieval_sha256
 OR r.versions->>'schema' IS DISTINCT FROM m.schema_sha256
 OR r.status<>'passed' OR clinic_app.ai_eval_verdict(r.id)<>'passed'
 OR c.authority_sha256 IS DISTINCT FROM
  clinic_app.ai_rollout_authority(r.id,c.requested_by_id,c.approved_by_id)
 OR c.authority_sha256=''
 OR EXISTS(SELECT 1 FROM clinic_app.ai_evaluationprotocol newer
 WHERE newer.clinic_id=p.clinic_id AND newer.capability=p.capability
 AND newer.version>p.version)
 THEN RETURN 'invalidated'; END IF;
 IF c.stage='shadow' AND r.stage='offline' AND r.rollout_approval_id IS NULL
 AND c.exposure='none' THEN RETURN c.stage; END IF;
 IF ((c.stage='reviewed_live' AND r.stage='shadow' AND c.exposure='human_review')
 OR (c.stage='monitored_autonomy' AND r.stage='reviewed_live'
  AND c.exposure='bounded_admin' AND m.purpose IN('front_desk','analyst')))
 AND clinic_app.ai_rollout_status(r.rollout_approval_id)=r.stage
 THEN RETURN c.stage; END IF;
 RETURN 'invalidated';
END $f$;
CREATE FUNCTION clinic_app.ai_rollout_status_for(clinic uuid, subject uuid)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT coalesce((SELECT clinic_app.ai_rollout_status(c.id)
 FROM clinic_app.ai_modelchangerequest c
 WHERE c.id=subject AND c.clinic_id=clinic
 AND c.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.ai_eval_staff(clinic,'owner')),'invalidated')
$f$;
CREATE OR REPLACE FUNCTION clinic_app.ai_eval_activation_allowed(
 candidate uuid, clinic uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.ai_modelchangerequest c
 WHERE c.configuration_id=candidate AND c.clinic_id=clinic
 AND c.stage='reviewed_live' AND clinic_app.ai_rollout_status(c.id)=c.stage)
$f$;
CREATE FUNCTION clinic_app.ai_rollout_bound(
 subject uuid, clinic uuid, autonomy boolean)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.ai_modelchangerequest c
 JOIN clinic_app.ai_gatewayswitch g ON g.id=1 AND g.enabled
 WHERE c.id=subject AND c.clinic_id=clinic
 AND c.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND clinic_app.ai_rollout_status(c.id)=c.stage
 AND CASE WHEN autonomy THEN c.stage='monitored_autonomy'
 ELSE c.stage IN('reviewed_live','monitored_autonomy') END)
$f$;
CREATE FUNCTION clinic_app.ai_rollout_admission(
 candidate uuid, clinic uuid, autonomy boolean)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT c.id FROM clinic_app.ai_modelchangerequest c
 WHERE c.configuration_id=candidate
 AND clinic_app.ai_rollout_bound(c.id,clinic,autonomy)
 ORDER BY c.decided_at DESC,c.id LIMIT 1
$f$;
CREATE FUNCTION clinic_app.ai_rollout_agent_allowed(subject uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT coalesce(bool_or(c.configuration_id=r.configuration_id
 AND clinic_app.ai_rollout_bound(c.id,r.clinic_id,
 p.autonomy_ceiling IN('A2','A3'))),false)
 FROM clinic_app.ai_agentrun r
 JOIN clinic_app.ai_agentpolicyversion p ON p.id=r.policy_id
 JOIN clinic_app.ai_modelchangerequest c ON c.id=r.rollout_approval_id
 WHERE r.id=subject
$f$;
CREATE FUNCTION clinic_app.ai_rollout_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE r clinic_app.ai_evaluationrun; c clinic_app.ai_modelchangerequest;
 m clinic_app.ai_modelconfiguration; expected text; exposure text; valid boolean;
BEGIN
 IF TG_TABLE_NAME='ai_gatewayswitch' THEN
  IF TG_OP='INSERT' THEN NEW.generation:=0;
  ELSE
   NEW.generation:=OLD.generation+CASE WHEN NOT NEW.enabled
    OR NOT NEW.approved_regions @> OLD.approved_regions THEN 1 ELSE 0 END;
  END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='ai_aiinvocation' THEN
  IF TG_OP='UPDATE' AND NEW.status<>'succeeded' THEN RETURN NEW; END IF;
  SELECT * INTO c FROM clinic_app.ai_modelchangerequest
   WHERE id=NEW.rollout_approval_id;
  IF c.configuration_id IS DISTINCT FROM NEW.configuration_id
  OR NOT clinic_app.ai_rollout_bound(c.id,NEW.clinic_id,false)
  THEN RAISE EXCEPTION 'reviewed rollout required' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='ai_agentrun' THEN
  SELECT clinic_app.ai_rollout_bound(NEW.rollout_approval_id,NEW.clinic_id,
   p.autonomy_ceiling IN('A2','A3')) AND approved.configuration_id=NEW.configuration_id
  INTO valid FROM clinic_app.ai_agentpolicyversion p
  JOIN clinic_app.ai_modelchangerequest approved ON approved.id=NEW.rollout_approval_id
  WHERE p.id=NEW.policy_id;
  IF NOT coalesce(valid,false) THEN
   RAISE EXCEPTION 'agent rollout permission required' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='ai_actionexecution' THEN
  IF TG_OP='UPDATE' AND NEW.send_started_at IS NOT DISTINCT FROM OLD.send_started_at
  THEN RETURN NEW; END IF;
  SELECT clinic_app.ai_rollout_agent_allowed(p.run_id) INTO valid
   FROM clinic_app.ai_actionproposal p WHERE p.id=NEW.proposal_id;
  IF NOT coalesce(valid,false) THEN
   RAISE EXCEPTION 'agent rollout stopped' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='ai_modelchangerequest' THEN
  IF TG_OP='UPDATE' AND NEW.status IN('killed','invalidated') THEN RETURN NEW; END IF;
  SELECT * INTO r FROM clinic_app.ai_evaluationrun WHERE id=NEW.run_id;
  SELECT * INTO m FROM clinic_app.ai_modelconfiguration WHERE id=NEW.configuration_id;
  expected:=CASE r.stage WHEN 'offline' THEN 'shadow'
   WHEN 'shadow' THEN 'reviewed_live'
   WHEN 'reviewed_live' THEN 'monitored_autonomy' END;
  exposure:=CASE expected WHEN 'shadow' THEN 'none'
   WHEN 'reviewed_live' THEN 'human_review'
   WHEN 'monitored_autonomy' THEN 'bounded_admin' END;
  IF expected IS NULL OR NEW.stage IS DISTINCT FROM expected
  OR NEW.exposure IS DISTINCT FROM exposure
  OR (expected='monitored_autonomy' AND m.purpose NOT IN('front_desk','analyst'))
  OR NEW.switch_generation<>coalesce((
   SELECT generation FROM clinic_app.ai_gatewayswitch WHERE id=1),0)
  OR (r.stage<>'offline' AND
   clinic_app.ai_rollout_status(r.rollout_approval_id)<>r.stage)
  THEN RAISE EXCEPTION 'rollout stage evidence required' USING ERRCODE='23514';
  END IF;
  IF TG_OP='INSERT' AND NEW.authority_sha256<>'' THEN
   RAISE EXCEPTION 'rollout authority denied' USING ERRCODE='23514'; END IF;
  IF TG_OP='UPDATE' AND NEW.status='approved' THEN
   NEW.authority_sha256:=clinic_app.ai_rollout_authority(
    r.id,NEW.requested_by_id,NEW.approved_by_id);
   IF NEW.authority_sha256 IS NULL THEN
    RAISE EXCEPTION 'rollout authority revoked' USING ERRCODE='42501'; END IF;
  END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='ai_evaluationrating' THEN
  SELECT * INTO r FROM clinic_app.ai_evaluationrun WHERE id=NEW.run_id;
 ELSE r:=NEW; END IF;
 IF r.stage='offline' AND r.rollout_approval_id IS NULL THEN RETURN NEW; END IF;
 SELECT * INTO c FROM clinic_app.ai_modelchangerequest WHERE id=r.rollout_approval_id;
 IF (r.organization_id,r.clinic_id,r.configuration_id) IS DISTINCT FROM
 (c.organization_id,c.clinic_id,c.configuration_id)
 OR r.stage NOT IN('shadow','reviewed_live') OR c.stage IS DISTINCT FROM r.stage
 OR clinic_app.ai_rollout_status(c.id)<>r.stage
 OR NOT EXISTS(SELECT 1 FROM clinic_app.ai_gatewayswitch WHERE id=1 AND enabled)
 OR NOT EXISTS(SELECT 1 FROM clinic_app.ai_evaluationrun prior
 WHERE prior.id=c.run_id AND prior.protocol_id=r.protocol_id
 AND prior.versions=r.versions AND prior.created_at<=c.decided_at)
 THEN RAISE EXCEPTION 'rollout receipt denied' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.ai_rollout_authority(uuid,uuid,uuid),
 clinic_app.ai_rollout_status(uuid),clinic_app.ai_rollout_status_for(uuid,uuid),
 clinic_app.ai_rollout_bound(uuid,uuid,boolean),
 clinic_app.ai_rollout_admission(uuid,uuid,boolean),
 clinic_app.ai_rollout_agent_allowed(uuid),clinic_app.ai_rollout_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_rollout_status_for(uuid,uuid),
 clinic_app.ai_rollout_bound(uuid,uuid,boolean),
 clinic_app.ai_rollout_admission(uuid,uuid,boolean)
 TO clinic_app,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.ai_rollout_agent_allowed(uuid)
 TO clinic_app,clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.ai_rollout_guard() TO clinic_owner;
RESET ROLE;
"""

SQL += _resolver((_RECORD,))
SQL += "".join(
    f"""
CREATE TRIGGER evaluation_rollout_guard BEFORE {operations}
 ON clinic_app.{table} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ai_rollout_guard();
"""
    for table, operations in (
        ("ai_gatewayswitch", "INSERT OR UPDATE"),
        ("ai_evaluationrun", "INSERT OR UPDATE"),
        ("ai_evaluationrating", "INSERT"),
        ("ai_modelchangerequest", "INSERT OR UPDATE"),
        ("ai_aiinvocation", "INSERT OR UPDATE"),
        ("ai_agentrun", "INSERT"),
        ("ai_actionexecution", "INSERT OR UPDATE"),
    )
)
SQL += """
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ai_rollout_guard() FROM clinic_owner;
RESET ROLE;
ALTER TABLE clinic_app.ai_modelchangerequest ADD CONSTRAINT ai_rollout_change_scope
 UNIQUE(organization_id,clinic_id,id);
ALTER TABLE clinic_app.ai_evaluationrun ADD CONSTRAINT ai_rollout_run_scope
 FOREIGN KEY(organization_id,clinic_id,rollout_approval_id)
 REFERENCES clinic_app.ai_modelchangerequest(organization_id,clinic_id,id);
ALTER TABLE clinic_app.ai_aiinvocation ADD CONSTRAINT ai_rollout_invocation_scope
 FOREIGN KEY(organization_id,clinic_id,rollout_approval_id)
 REFERENCES clinic_app.ai_modelchangerequest(organization_id,clinic_id,id);
ALTER TABLE clinic_app.ai_agentrun ADD CONSTRAINT ai_rollout_agent_scope
 FOREIGN KEY(organization_id,clinic_id,rollout_approval_id)
 REFERENCES clinic_app.ai_modelchangerequest(organization_id,clinic_id,id);
"""

REVERSE_SQL = """
SET LOCAL ROLE clinic_resolver;
DO $rollback$
BEGIN
 IF EXISTS(SELECT 1 FROM clinic_app.ai_evaluationrun
  WHERE stage<>'offline' OR rollout_approval_id IS NOT NULL)
 OR EXISTS(SELECT 1 FROM clinic_app.ai_modelchangerequest WHERE authority_sha256<>'')
 OR EXISTS(SELECT 1 FROM clinic_app.ai_aiinvocation
  WHERE rollout_approval_id IS NOT NULL)
 OR EXISTS(SELECT 1 FROM clinic_app.ai_agentrun WHERE rollout_approval_id IS NOT NULL)
 THEN RAISE EXCEPTION 'retained rollout evidence requires restore'
  USING ERRCODE='23514'; END IF;
END $rollback$;
RESET ROLE;
ALTER TABLE clinic_app.ai_agentrun DROP CONSTRAINT ai_rollout_agent_scope;
ALTER TABLE clinic_app.ai_aiinvocation DROP CONSTRAINT ai_rollout_invocation_scope;
ALTER TABLE clinic_app.ai_evaluationrun DROP CONSTRAINT ai_rollout_run_scope;
ALTER TABLE clinic_app.ai_modelchangerequest DROP CONSTRAINT ai_rollout_change_scope;
"""
REVERSE_SQL += "".join(
    f"DROP TRIGGER evaluation_rollout_guard ON clinic_app.{table};\n"
    for table in (
        "ai_gatewayswitch",
        "ai_evaluationrun",
        "ai_evaluationrating",
        "ai_modelchangerequest",
        "ai_aiinvocation",
        "ai_agentrun",
        "ai_actionexecution",
    )
)
REVERSE_SQL += _resolver(
    (
        _PRIOR_RECORD,
        _function(_evaluation_sql.SQL, "ai_eval_activation_allowed"),
    )
)
REVERSE_SQL += """
DROP FUNCTION clinic_app.ai_rollout_guard();
DROP FUNCTION clinic_app.ai_rollout_agent_allowed(uuid);
DROP FUNCTION clinic_app.ai_rollout_admission(uuid,uuid,boolean);
DROP FUNCTION clinic_app.ai_rollout_bound(uuid,uuid,boolean);
DROP FUNCTION clinic_app.ai_rollout_status_for(uuid,uuid);
DROP FUNCTION clinic_app.ai_rollout_status(uuid);
DROP FUNCTION clinic_app.ai_rollout_authority(uuid,uuid,uuid);
"""
