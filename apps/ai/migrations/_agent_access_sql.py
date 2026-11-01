"""Narrow metadata and payload resolvers; machines never install a human GUC."""

SQL = """
GRANT SELECT ON clinic_app.providers_providercapability,
 clinic_app.providers_capabilityversion TO clinic_resolver;
GRANT SELECT ON clinic_app.ai_agentpolicyversion,clinic_app.ai_agentrun,
 clinic_app.ai_actionproposal,clinic_app.ai_approval,clinic_app.ai_actionexecution,
 clinic_app.ai_actionreceipt,clinic_app.ai_compensation TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.agent_staff(clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.has_permission('automation.admin',clinic,NULL)
 OR clinic_app.has_permission('automation.organization',clinic,NULL)
 OR clinic_app.has_permission('tasks.assign',clinic,NULL)
$f$;
CREATE FUNCTION clinic_app.agent_bound_snapshot(
 clinic uuid, actor uuid, perm text, tenant uuid)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT jsonb_build_object('user_id',actor::text,'permission',perm,
 'memberships',coalesce(jsonb_agg(jsonb_build_object('id',r.id::text,
 'role',r.role,'bundle_version',r.workflow_bundle_version)
 -- Action payload v1 requires RFC 8785 byte order: bundle_version, id, role.
 -- Bundle versions are restricted to 1/2 and UUID text has one ASCII spelling.
 ORDER BY r.workflow_bundle_version,r.id::text COLLATE pg_catalog."C",
 r.role COLLATE pg_catalog."C"),'[]'::jsonb))
 FROM clinic_app.identity_userclinicrole r
 WHERE r.user_id=actor AND r.organization_id=tenant AND r.clinic_id=clinic
 AND r.revoked_at IS NULL
 HAVING clinic_app.identity_actor_permission(perm,clinic,NULL,actor,tenant)
$f$;
CREATE FUNCTION clinic_app.agent_snapshot(clinic uuid,perm text)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.agent_bound_snapshot(clinic,
 NULLIF(current_setting('app.current_user_id',true),'')::uuid,perm,
 NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WHERE perm IN ('automation.admin','automation.organization','tasks.assign')
$f$;
CREATE FUNCTION clinic_app.agent_proposal_scope(requested uuid)
RETURNS TABLE(organization_id uuid,clinic_id uuid,principal_id uuid)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT p.organization_id,p.clinic_id,r.principal_id
 FROM clinic_app.ai_actionproposal p JOIN clinic_app.ai_agentrun r ON r.id=p.run_id
 WHERE p.id=requested
 AND p.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND r.principal_id=NULLIF(current_setting('app.current_principal',true),'')::uuid
 AND clinic_app.principal_has('agent.execute',p.clinic_id)
$f$;
CREATE FUNCTION clinic_app.agent_action_payload(requested uuid,kek text)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE value bytea;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM clinic_app.agent_proposal_scope(requested)) THEN
  RETURN NULL;
 END IF;
 SELECT payload INTO value FROM clinic_app.ai_actionproposal WHERE id=requested;
 RETURN convert_from(clinic_app.tenant_decrypt(
  kek,'ai.action_proposal.payload.v1',value),'UTF8');
END $f$;
CREATE FUNCTION clinic_app.agent_approval_current(requested uuid,approval_id uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.ai_approval a
 WHERE a.id=approval_id AND a.proposal_id=requested
 AND a.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND CASE WHEN pg_has_role(session_user,'clinic_agent','USAGE')
 THEN EXISTS(SELECT 1 FROM clinic_app.agent_proposal_scope(requested))
 ELSE clinic_app.agent_staff(a.clinic_id) END
 AND a.authority_snapshot=clinic_app.agent_bound_snapshot(a.clinic_id,a.approver_id,
 a.authority_snapshot->>'permission',a.organization_id))
$f$;
CREATE FUNCTION clinic_app.agent_source(requested uuid,source_id uuid)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT jsonb_build_object('version',d.version,'revision',d.revision,
 'content_sha256',d.fingerprint,'state',d.state,'current',
 NOT EXISTS(SELECT 1 FROM clinic_app.workflows_workflowdefinitionversion newer
 WHERE newer.clinic_id=d.clinic_id AND newer.key=d.key AND newer.version>d.version
 AND newer.state='published'))
 FROM clinic_app.workflows_workflowdefinitionversion d
 JOIN clinic_app.agent_proposal_scope(requested) s ON s.clinic_id=d.clinic_id
 WHERE d.id=source_id AND d.organization_id=s.organization_id
$f$;
CREATE FUNCTION clinic_app.agent_controls(requested uuid)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT jsonb_build_object('latest_policy_id',
 (SELECT latest.id::text FROM clinic_app.ai_agentpolicyversion latest
 WHERE latest.clinic_id=p.clinic_id AND latest.capability=p.capability
 ORDER BY latest.version DESC LIMIT 1),'enabled',
 coalesce((SELECT enabled FROM clinic_app.ai_gatewayswitch WHERE id=1),false)
 AND coalesce((org.policy->>'enabled')::boolean,false)
 AND coalesce((org.policy->'purposes') ? p.capability,false)
 AND EXISTS(SELECT 1 FROM clinic_app.ai_modelconfiguration m
 WHERE m.id=r.configuration_id AND m.state='active'
 AND m.approval_ref<>'' AND m.provider IN ('synthetic','recorded_fixture')
 AND EXISTS(SELECT 1 FROM clinic_app.ai_gatewayswitch gs
 WHERE gs.id=1 AND gs.approved_regions ? m.region)
 AND NOT EXISTS(SELECT 1 FROM clinic_app.ai_aicircuit circuit
 WHERE circuit.configuration_id=m.id AND circuit.organization_id=p.organization_id
 AND circuit.closed_at IS NULL)),
 'policy_authority',coalesce(
 clinic_app.identity_actor_permission('automation.admin',p.clinic_id,NULL,
 p.created_by_id,p.organization_id)
 OR clinic_app.identity_actor_permission('automation.organization',p.clinic_id,NULL,
 p.created_by_id,p.organization_id),false),
 'provider',(SELECT jsonb_build_object('capability_version',v.id::text,
 'account',v.account,'environment',v.environment,'api_version',v.api_version,
 'state',v.state,'provider',v.provider)
 FROM clinic_app.providers_capabilityversion v
 WHERE v.id=(SELECT c.current_version_id FROM clinic_app.providers_providercapability c
 WHERE c.key='email' AND (c.clinic_id=p.clinic_id OR c.clinic_id IS NULL)
 ORDER BY c.clinic_id NULLS LAST LIMIT 1)))
 FROM clinic_app.ai_actionproposal a
 JOIN clinic_app.ai_agentrun r ON r.id=a.run_id
 JOIN clinic_app.ai_agentpolicyversion p ON p.id=r.policy_id
 LEFT JOIN LATERAL(SELECT ai_policy AS policy
 FROM clinic_app.identity_clinicconfiguration
 WHERE organization_id=p.organization_id ORDER BY created_at DESC,id DESC LIMIT 1) org
 ON true WHERE a.id=requested
 AND a.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND CASE WHEN pg_has_role(session_user,'clinic_agent','USAGE')
 THEN EXISTS(SELECT 1 FROM clinic_app.agent_proposal_scope(requested))
 ELSE clinic_app.agent_staff(a.clinic_id) END
$f$;
REVOKE ALL ON FUNCTION clinic_app.agent_staff(uuid),
 clinic_app.agent_bound_snapshot(uuid,uuid,text,uuid),
 clinic_app.agent_snapshot(uuid,text),clinic_app.agent_proposal_scope(uuid),
 clinic_app.agent_action_payload(uuid,text),clinic_app.agent_approval_current(uuid,uuid),
 clinic_app.agent_source(uuid,uuid),clinic_app.agent_controls(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.agent_staff(uuid),
 clinic_app.agent_snapshot(uuid,text) TO clinic_app,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.agent_approval_current(uuid,uuid),
 clinic_app.agent_controls(uuid) TO clinic_app,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.agent_proposal_scope(uuid),
 clinic_app.agent_action_payload(uuid,text),clinic_app.agent_approval_current(uuid,uuid),
 clinic_app.agent_source(uuid,uuid),clinic_app.agent_controls(uuid) TO clinic_agent;
RESET ROLE;
"""
