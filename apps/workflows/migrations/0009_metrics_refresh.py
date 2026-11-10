"""Admit only the reviewed catalog's typed local refresh step."""

from typing import ClassVar

from django.db import migrations

from apps.workflows.migrations._workflow_sql import SQL as WORKFLOW_SQL

_original = WORKFLOW_SQL.split(
    "CREATE FUNCTION clinic_app.workflows_steps_valid(steps jsonb)", 1
)[1].split("REVOKE ALL ON FUNCTION clinic_app.workflows_steps_valid(jsonb)", 1)[0]
SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    "CREATE OR REPLACE FUNCTION clinic_app.workflows_steps_valid(steps jsonb)"
    + _original.replace(
        '  ELSIF step<>\'{"handler":"external",'
        '"provider":"workflow-synthetic-v1"}\'::jsonb',
        """  ELSIF step->>'handler'='metrics_refresh' THEN
   IF NOT step ?& ARRAY['handler','metric_id','days']
   OR step-ARRAY['handler','metric_id','days']<>'{}'
   OR jsonb_typeof(step->'metric_id')<>'string'
   OR step->>'metric_id' NOT IN ('utilization','no_show','waiting_time',
    'task_turnaround','receivables','verified_collections','net_collections',
    'denials','payout_liabilities','result_ack_latency','follow_up_overdue',
    'support_load','ai_correction_time','activation','retained_usage')
   OR jsonb_typeof(step->'days')<>'number' OR step->>'days' !~ '^[0-9]{1,3}$'
   THEN RETURN false; END IF;
   IF (step->>'days')::int NOT BETWEEN 1 AND 366 THEN RETURN false; END IF;
  ELSIF step<>'{"handler":"external","provider":"workflow-synthetic-v1"}'::jsonb""",
    )
    + "RESET ROLE;"
)


class Migration(migrations.Migration):
    """Existing workflow runs keep their original immutable version."""

    dependencies: ClassVar = [
        ("workflows", "0008_release_and_urgent"),
        ("analytics", "0001_initial"),
    ]
    operations: ClassVar = [
        migrations.RunSQL(
            SQL,
            reverse_sql=(
                "SET LOCAL ROLE clinic_resolver;\n"
                "CREATE OR REPLACE FUNCTION clinic_app.workflows_steps_valid"
                "(steps jsonb)" + _original + "RESET ROLE;"
            ),
        ),
        migrations.RunSQL(
            """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.analytics_workflow_receipt()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE run clinic_app.workflows_workflowrun; item jsonb;
BEGIN
 IF NEW.state<>'completed' THEN RETURN NEW; END IF;
 SELECT * INTO run FROM clinic_app.workflows_workflowrun WHERE id=NEW.run_id;
 SELECT steps->NEW.position INTO item
 FROM clinic_app.workflows_workflowdefinitionversion
 WHERE id=run.definition_version_id;
 IF item->>'handler'='metrics_refresh' AND NOT EXISTS (
  SELECT 1 FROM clinic_app.analytics_readmodelrefresh r
  WHERE r.command_id=NEW.id AND r.clinic_id=NEW.clinic_id
  AND r.organization_id=NEW.organization_id AND r.user_id=run.started_by_id
  AND r.authorization_version=clinic_app.analytics_authorization(NEW.clinic_id)
  AND r.descriptor->>'metric_id'=item->>'metric_id'
  AND r.descriptor->'version'='1'
  AND r.descriptor->'filters'='{}' AND r.descriptor->'dimensions'='[]'
  AND r.window_end=run.created_at
  AND r.window_start=run.created_at-(item->>'days')::int*interval '24 hours'
 ) THEN RAISE EXCEPTION 'workflow effect is missing' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.analytics_workflow_receipt()
 FROM PUBLIC,clinic_app,clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.analytics_workflow_receipt() TO clinic_owner;
RESET ROLE;
CREATE TRIGGER analytics_workflow_receipt BEFORE UPDATE OF state
 ON clinic_app.workflows_workflowstep FOR EACH ROW
 EXECUTE FUNCTION clinic_app.analytics_workflow_receipt();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.analytics_workflow_receipt() FROM clinic_owner;
RESET ROLE;
""",
            reverse_sql="""
SET LOCAL ROLE clinic_resolver;
DO $f$
BEGIN
 IF EXISTS(SELECT 1 FROM clinic_app.workflows_workflowdefinitionversion
 WHERE steps @> '[{"handler":"metrics_refresh"}]'::jsonb) THEN
  RAISE EXCEPTION 'metric workflows require restore' USING ERRCODE='55000';
 END IF;
END $f$;
RESET ROLE;
DROP TRIGGER analytics_workflow_receipt ON clinic_app.workflows_workflowstep;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.analytics_workflow_receipt();
RESET ROLE;
""",
        ),
    ]
