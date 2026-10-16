"""Freeze the AI configuration guard as finite, auditable migration SQL."""

# Copied from identity.0009 with the AI policy checks and organization lock.
# Keep the immutable SQL literal visible to the migration clock census.
SQL = r"""SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.configuration_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE last_version integer;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('ai-budget:'||NEW.organization_id,0));
 IF TG_OP<>'INSERT' THEN
   RAISE EXCEPTION 'configuration history is immutable' USING ERRCODE='23514';
 END IF;
 IF NOT (
 clinic_app.questionnaire_staff(NEW.clinic_id,ARRAY['owner','clinic_admin'])
 OR (clinic_app.has_permission('configuration.organization',NEW.clinic_id,NULL)
  AND EXISTS (
   SELECT 1 FROM clinic_app.identity_clinicconfiguration prior
   WHERE prior.id=(SELECT c.id FROM clinic_app.identity_clinicconfiguration c
    WHERE c.clinic_id=NEW.clinic_id ORDER BY c.version DESC LIMIT 1)
   AND (to_jsonb(prior)-
    ARRAY['id','version','published_by_id','created_at','ai_policy'])
    = (to_jsonb(NEW)-ARRAY['id','version','published_by_id','created_at','ai_policy'])
  ))
)
 OR NEW.published_by_id IS DISTINCT FROM
   NULLIF(current_setting('app.current_user_id',true),'')::uuid
 OR btrim(NEW.display_name)='' OR NEW.display_name ~ '[<>]'
 OR (NEW.contact_phone<>'' AND NEW.contact_phone !~ '^\+[1-9][0-9]{7,14}$')
 OR (NEW.contact_email<>'' AND NEW.contact_email !~ '^[^ <>@]+@[^ <>@]+\.[^ <>@]+$')
 THEN RAISE EXCEPTION 'invalid configuration' USING ERRCODE='23514'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
   'clinic-configuration:'||NEW.clinic_id,0));
 SELECT coalesce(max(version),0) INTO last_version
 FROM clinic_app.identity_clinicconfiguration WHERE clinic_id=NEW.clinic_id;
 IF NEW.version<>last_version+1 THEN
   RAISE EXCEPTION 'stale configuration' USING ERRCODE='23514'; END IF;

 IF NOT clinic_app.ai_policy_valid(NEW.ai_policy) THEN
  RAISE EXCEPTION 'invalid AI policy' USING ERRCODE='23514';
 END IF;
 IF NEW.ai_policy IS DISTINCT FROM
   coalesce(clinic_app.ai_organization_policy(),'{}'::jsonb)
 AND NOT (coalesce(clinic_app.ai_organization_policy(),'{}'::jsonb)='{}'::jsonb
  AND NEW.ai_policy='{"enabled":false,"purposes":[],
   "monthly_budget_micros":0,"purpose_budgets":{}}'::jsonb)
 THEN
  IF NOT clinic_app.has_permission('configuration.organization',NEW.clinic_id,NULL)
  OR EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
   WHERE c.organization_id=NEW.organization_id
    AND NOT clinic_app.has_permission('configuration.organization',c.id,NULL))
  THEN RAISE EXCEPTION 'AI organization policy denied' USING ERRCODE='23514';
  END IF;
 END IF;

 NEW.created_at:=statement_timestamp();
 RETURN NEW;
END $f$;
RESET ROLE;

DROP POLICY configuration_read ON clinic_app.identity_clinicconfiguration;
CREATE POLICY configuration_read ON clinic_app.identity_clinicconfiguration
 FOR SELECT TO clinic_app USING (
  clinic_app.questionnaire_staff(clinic_id,
   ARRAY['owner','clinic_admin','physician','receptionist'])
  OR clinic_app.has_permission('configuration.organization',clinic_id,NULL));
DROP POLICY configuration_insert ON clinic_app.identity_clinicconfiguration;
CREATE POLICY configuration_insert ON clinic_app.identity_clinicconfiguration
 FOR INSERT TO clinic_app WITH CHECK (
  clinic_app.questionnaire_staff(clinic_id,ARRAY['owner','clinic_admin'])
  OR clinic_app.has_permission('configuration.organization',clinic_id,NULL));
"""
