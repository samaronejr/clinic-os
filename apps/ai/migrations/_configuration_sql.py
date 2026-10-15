"""Extend the installed immutable configuration guard without editing history."""

from importlib import import_module

_BASE = import_module("apps.identity.migrations.0009_clinic_configuration_policy").SQL
_START = _BASE.index("CREATE FUNCTION clinic_app.configuration_guard()")
_END = _BASE.index("END $f$;", _START) + len("END $f$;")
_ORIGINAL = _BASE[_START:_END]

_AI_ONLY = r"""(
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
)"""
_CHECK = r"""
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
"""
_GUARD = (
    _ORIGINAL.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    .replace(
        "BEGIN\n IF TG_OP",
        "BEGIN\n PERFORM pg_advisory_xact_lock(hashtextextended("
        "'ai-budget:'||NEW.organization_id,0));\n IF TG_OP",
        1,
    )
    .replace(
        "clinic_app.questionnaire_staff(NEW.clinic_id,ARRAY['owner','clinic_admin'])",
        _AI_ONLY,
        1,
    )
    .replace(
        " NEW.created_at:=statement_timestamp();",
        _CHECK + "\n NEW.created_at:=statement_timestamp();",
        1,
    )
)

SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _GUARD
    + "\nRESET ROLE;\n"
    + r"""
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
)
