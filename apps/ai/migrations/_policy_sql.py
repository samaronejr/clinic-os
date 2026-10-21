"""Hardened metadata resolvers; no function returns plaintext AI content."""

SQL = r"""
GRANT SELECT ON clinic_app.ai_aiartifact, clinic_app.ai_aiinvocation,
 clinic_app.ai_aicircuit, clinic_app.ai_modelconfiguration,
 clinic_app.ai_gatewayswitch TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ai_staff(clinic uuid, purpose text, enrollment uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT CASE purpose
 WHEN 'front_desk' THEN clinic_app.has_permission('appointment.read',clinic,NULL)
 WHEN 'analyst' THEN clinic_app.has_permission('charge.read',clinic,NULL)
 WHEN 'scribe_asr' THEN clinic_app.has_permission('clinical.read',clinic,enrollment)
 WHEN 'scribe_draft' THEN clinic_app.has_permission('clinical.read',clinic,enrollment)
 WHEN 'brief' THEN clinic_app.has_permission('clinical.read',clinic,enrollment)
 WHEN 'extraction' THEN clinic_app.has_permission('clinical.read',clinic,enrollment)
 WHEN 'decision_support' THEN
  clinic_app.has_permission('clinical.read',clinic,enrollment)
 ELSE false END
$f$;
CREATE FUNCTION clinic_app.ai_organization_policy()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT ai_policy FROM clinic_app.identity_clinicconfiguration
 WHERE organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 ORDER BY created_at DESC,id DESC LIMIT 1
$f$;
CREATE FUNCTION clinic_app.ai_budget_usage(requested_purpose text)
RETURNS TABLE(total_micros numeric,purpose_micros numeric)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT coalesce(sum(CASE WHEN status='reserved'
   THEN reserved_micros ELSE cost_micros END),0),
 coalesce(sum(CASE WHEN purpose=requested_purpose THEN
   CASE WHEN status='reserved' THEN reserved_micros ELSE cost_micros END
   ELSE 0 END),0)
 FROM clinic_app.ai_aiinvocation
 WHERE organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND (status='reserved' OR
  coalesce(settled_at,created_at)>=
  date_trunc('month',statement_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')
$f$;
CREATE FUNCTION clinic_app.ai_policy_valid(policy jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE item record; p text;
BEGIN
 IF policy='{}'::jsonb THEN RETURN true; END IF;
 IF jsonb_typeof(policy)<>'object'
 OR policy-ARRAY['enabled','purposes','monthly_budget_micros','purpose_budgets']
   <>'{}'::jsonb
 OR NOT policy ?& ARRAY['enabled','purposes','monthly_budget_micros',
   'purpose_budgets']
 OR jsonb_typeof(policy->'enabled')<>'boolean'
 OR jsonb_typeof(policy->'purposes')<>'array'
 OR jsonb_typeof(policy->'purpose_budgets')<>'object'
 OR policy->>'monthly_budget_micros' !~ '^(0|[1-9][0-9]{0,15})$'
 OR (policy->>'monthly_budget_micros')::numeric>1000000000000000
 THEN RETURN false; END IF;
 IF EXISTS (SELECT 1 FROM jsonb_array_elements(policy->'purposes') x
  WHERE jsonb_typeof(x)<>'string')
 OR (SELECT count(*) FROM jsonb_array_elements_text(policy->'purposes'))
 <> (SELECT count(DISTINCT x) FROM jsonb_array_elements_text(policy->'purposes') x)
 THEN RETURN false; END IF;
 FOR p IN SELECT jsonb_array_elements_text(policy->'purposes') LOOP
  IF p<>ALL(ARRAY['scribe_asr','scribe_draft','brief','extraction',
   'front_desk','analyst','decision_support'])
  OR NOT (policy->'purpose_budgets') ? p THEN RETURN false; END IF;
 END LOOP;
 FOR item IN SELECT * FROM jsonb_each(policy->'purpose_budgets') LOOP
  IF item.key<>ALL(ARRAY['scribe_asr','scribe_draft','brief','extraction',
   'front_desk','analyst','decision_support'])
  OR jsonb_typeof(item.value)<>'number'
  OR item.value::text !~ '^(0|[1-9][0-9]{0,15})$'
  OR item.value::numeric>1000000000000000 THEN RETURN false; END IF;
 END LOOP;
 RETURN jsonb_typeof(policy->'monthly_budget_micros')='number';
EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range
 OR invalid_parameter_value THEN RETURN false;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.ai_staff(uuid,text,uuid),
 clinic_app.ai_organization_policy(),clinic_app.ai_budget_usage(text),
 clinic_app.ai_policy_valid(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_staff(uuid,text,uuid)
 TO clinic_app,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.ai_organization_policy(),
 clinic_app.ai_budget_usage(text) TO clinic_app,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.ai_policy_valid(jsonb)
 TO clinic_app,clinic_owner;
RESET ROLE;

ALTER TABLE clinic_app.ai_aiartifact ADD CONSTRAINT ai_artifact_clinic_fk
 FOREIGN KEY(organization_id,clinic_id)
 REFERENCES clinic_app.identity_clinic(organization_id,id);
ALTER TABLE clinic_app.ai_aiartifact ADD CONSTRAINT ai_artifact_enrollment_fk
 FOREIGN KEY(organization_id,clinic_id,patient_enrollment_id)
 REFERENCES clinic_app.intake_patientclinicenrollment(organization_id,clinic_id,id);
ALTER TABLE clinic_app.ai_aiinvocation ADD CONSTRAINT ai_invocation_clinic_fk
 FOREIGN KEY(organization_id,clinic_id)
 REFERENCES clinic_app.identity_clinic(organization_id,id);
ALTER TABLE clinic_app.ai_aiinvocation ADD CONSTRAINT ai_invocation_input_fk
 FOREIGN KEY(organization_id,clinic_id,inputs_ref_id)
 REFERENCES clinic_app.ai_aiartifact(organization_id,clinic_id,id);

REVOKE ALL ON clinic_app.ai_modelconfiguration,clinic_app.ai_gatewayswitch,
 clinic_app.ai_aiinvocation,clinic_app.ai_aiartifact,clinic_app.ai_aicircuit
 FROM PUBLIC,clinic_app,clinic_agent;
GRANT SELECT ON clinic_app.ai_modelconfiguration,clinic_app.ai_gatewayswitch
 TO clinic_app;
GRANT SELECT,INSERT ON clinic_app.ai_aiartifact,clinic_app.ai_aiinvocation,
 clinic_app.ai_aicircuit TO clinic_app;
GRANT UPDATE(operation_id,status,reason_code,tokens_in,tokens_out,tokens_cached,
 audio_seconds,cost_micros,cost_minor,latency_ms,settled_at)
 ON clinic_app.ai_aiinvocation TO clinic_app;

ALTER TABLE clinic_app.ai_aiartifact ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.ai_aiartifact FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.ai_aiinvocation ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.ai_aiinvocation FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.ai_aicircuit ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.ai_aicircuit FORCE ROW LEVEL SECURITY;
CREATE POLICY ai_owner ON clinic_app.ai_aiartifact TO clinic_owner
 USING(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK(organization_id=
  NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY ai_owner ON clinic_app.ai_aiinvocation TO clinic_owner
 USING(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK(organization_id=
  NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY ai_owner ON clinic_app.ai_aicircuit TO clinic_owner
 USING(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK(organization_id=
  NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY ai_artifact_user ON clinic_app.ai_aiartifact TO clinic_app
 USING(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND user_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
  AND clinic_app.ai_staff(clinic_id,purpose,patient_enrollment_id))
 WITH CHECK(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND user_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
  AND clinic_app.ai_staff(clinic_id,purpose,patient_enrollment_id));
CREATE POLICY ai_invocation_user ON clinic_app.ai_aiinvocation TO clinic_app
 USING(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND user_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid)
 WITH CHECK(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND user_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid);
CREATE POLICY ai_circuit_tenant ON clinic_app.ai_aicircuit TO clinic_app
 USING(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK(organization_id=
  NULLIF(current_setting('app.current_tenant',true),'')::uuid);
"""
