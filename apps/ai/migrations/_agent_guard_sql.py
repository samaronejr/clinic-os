"""Retained immutable terms, fenced transitions, and mutation/send serialization."""

SQL = """
CREATE FUNCTION clinic_app.agent_mutation_lock()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('agent-send-v1',0));
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.agent_mutation_lock() FROM PUBLIC;
CREATE FUNCTION clinic_app.agent_transition(previous text,following text)
RETURNS boolean LANGUAGE sql IMMUTABLE
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT CASE previous
 WHEN 'proposal' THEN following IN ('validated','rejected')
 WHEN 'validated' THEN following IN ('approved','rejected')
 WHEN 'approved' THEN following IN ('executing','rejected')
 WHEN 'executing' THEN following IN ('receipt','unknown','failure','rejected')
 WHEN 'unknown' THEN following IN ('reconciled','exception')
 WHEN 'failure' THEN following='compensating'
 WHEN 'compensating' THEN following IN ('compensated','manual_required')
 ELSE false END
$f$;
REVOKE ALL ON FUNCTION clinic_app.agent_transition(text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.agent_transition(text,text)
 TO clinic_app,clinic_agent;
CREATE FUNCTION clinic_app.agent_record_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE allowed text[]; p record; r record; e record;
BEGIN
 IF TG_OP='DELETE' THEN
  RAISE EXCEPTION 'agent history is immutable' USING ERRCODE='23514';
 END IF;
 IF TG_OP='UPDATE' THEN
  allowed:=CASE TG_TABLE_NAME
   WHEN 'ai_agentrun' THEN ARRAY['outcome','reason_code','cost_micros','latency_ms']
   WHEN 'ai_actionproposal' THEN ARRAY['state','reason_code']
   WHEN 'ai_actionexecution' THEN
    ARRAY['state','reason_code','fencing_token','send_started_at']
   WHEN 'ai_compensation' THEN ARRAY['state','reason_code']
   ELSE ARRAY[]::text[] END;
  IF cardinality(allowed)=0
   OR (to_jsonb(OLD)-allowed) IS DISTINCT FROM (to_jsonb(NEW)-allowed) THEN
   RAISE EXCEPTION 'agent terms are immutable' USING ERRCODE='23514';
  END IF;
  IF TG_TABLE_NAME IN ('ai_actionproposal','ai_compensation','ai_actionexecution') THEN
   IF OLD.state<>NEW.state
    AND NOT clinic_app.agent_transition(OLD.state,NEW.state) THEN
    RAISE EXCEPTION 'agent transition refused' USING ERRCODE='23514';
   END IF;
  END IF;
  IF TG_TABLE_NAME='ai_actionexecution' THEN
   IF NEW.fencing_token<OLD.fencing_token OR NEW.fencing_token>OLD.fencing_token+1
    OR (NEW.fencing_token<>OLD.fencing_token AND OLD.state<>'unknown')
    OR (OLD.send_started_at IS NOT NULL
     AND NEW.send_started_at IS DISTINCT FROM OLD.send_started_at) THEN
    RAISE EXCEPTION 'agent fence refused' USING ERRCODE='23514';
   END IF;
  END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='ai_agentpolicyversion' THEN
  IF NOT (clinic_app.has_permission('automation.admin',NEW.clinic_id,NULL)
    OR clinic_app.has_permission('automation.organization',NEW.clinic_id,NULL))
   OR NEW.created_by_id IS DISTINCT FROM
    NULLIF(current_setting('app.current_user_id',true),'')::uuid
   OR (NEW.principal_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM clinic_app.identity_serviceprincipal sp
    WHERE sp.id=NEW.principal_id AND sp.organization_id=NEW.organization_id
     AND sp.clinic_id=NEW.clinic_id AND sp.grant_set_version=2 AND sp.active)) THEN
   RAISE EXCEPTION 'agent policy authority refused' USING ERRCODE='42501';
  END IF;
 ELSIF TG_TABLE_NAME='ai_agentrun' THEN
  SELECT * INTO p FROM clinic_app.ai_agentpolicyversion WHERE id=NEW.policy_id;
  IF NEW.user_id IS DISTINCT FROM
   NULLIF(current_setting('app.current_user_id',true),'')::uuid
   OR NEW.principal_id IS DISTINCT FROM p.principal_id
   OR NOT p.enabled OR NEW.outcome<>'proposal' THEN
   RAISE EXCEPTION 'agent run refused' USING ERRCODE='23514';
  END IF;
 ELSIF TG_TABLE_NAME='ai_actionproposal' THEN
  IF NEW.state<>'proposal' OR NEW.digest !~ '^[0-9a-f]{64}$' THEN
   RAISE EXCEPTION 'agent proposal refused' USING ERRCODE='23514';
  END IF;
 ELSIF TG_TABLE_NAME='ai_approval' THEN
  SELECT * INTO p FROM clinic_app.ai_actionproposal WHERE id=NEW.proposal_id;
  IF NEW.approver_id IS DISTINCT FROM
   NULLIF(current_setting('app.current_user_id',true),'')::uuid
   OR NEW.digest<>p.digest OR p.state NOT IN ('validated','approved')
   OR NEW.key_id<>'agent-attestation-v1' OR NEW.attestation !~ '^[0-9a-f]{64}$'
   OR NEW.authority_snapshot IS DISTINCT FROM clinic_app.agent_snapshot(
    NEW.clinic_id,NEW.authority_snapshot->>'permission') THEN
   RAISE EXCEPTION 'agent approval refused' USING ERRCODE='42501';
  END IF;
 ELSIF TG_TABLE_NAME='ai_actionexecution' THEN
  SELECT * INTO p FROM clinic_app.ai_actionproposal WHERE id=NEW.proposal_id;
  SELECT * INTO r FROM clinic_app.ai_agentrun WHERE id=p.run_id;
  IF p.state<>'approved' OR NEW.digest<>p.digest
   OR NEW.fencing_token<>1 OR NEW.state<>'executing'
   OR NEW.reserved_micros<>1
   OR NEW.deadline<=NEW.intent_at OR NEW.send_started_at IS NOT NULL THEN
   RAISE EXCEPTION 'agent execution refused' USING ERRCODE='23514';
  END IF;
 ELSIF TG_TABLE_NAME='ai_actionreceipt' THEN
  SELECT * INTO e FROM clinic_app.ai_actionexecution WHERE id=NEW.execution_id;
  IF NEW.digest<>e.digest OR NEW.fencing_token<>e.fencing_token
   OR NEW.key_id<>'agent-attestation-v1' OR NEW.attestation !~ '^[0-9a-f]{64}$' THEN
   RAISE EXCEPTION 'agent receipt refused' USING ERRCODE='23514';
  END IF;
 ELSIF TG_TABLE_NAME='ai_compensation' THEN
  SELECT * INTO e FROM clinic_app.ai_actionexecution WHERE id=NEW.execution_id;
  IF e.state<>'failure' OR NEW.state<>'compensating' OR NEW.policy_version<>1 THEN
   RAISE EXCEPTION 'agent compensation refused' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.agent_record_guard() FROM PUBLIC;
"""

for _table in (
    "ai_agentpolicyversion",
    "ai_agentrun",
    "ai_actionproposal",
    "ai_approval",
    "ai_actionexecution",
    "ai_actionreceipt",
    "ai_compensation",
):
    SQL += f"""
CREATE TRIGGER agent_record_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.{_table} FOR EACH ROW EXECUTE FUNCTION clinic_app.agent_record_guard();
"""

# One bounded send lock deliberately serializes authorization/source mutations
# with synthetic dispatch. No database transaction spans the external adapter.
for _table in (
    "identity_user",
    "identity_userclinicrole",
    "identity_rolegrant",
    "identity_serviceprincipal",
    "identity_serviceprincipalgrant",
    "identity_clinicconfiguration",
    "workflows_workflowdefinitionversion",
    "ai_agentpolicyversion",
    "ai_gatewayswitch",
    "ai_modelconfiguration",
    "providers_providercapability",
    "providers_capabilityversion",
):
    SQL += f"""
CREATE TRIGGER agent_send_mutation BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.{_table} FOR EACH ROW EXECUTE FUNCTION clinic_app.agent_mutation_lock();
"""
