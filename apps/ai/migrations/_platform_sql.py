"""Owner-only configuration transitions and retained global controls."""

SQL = r"""
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ai_platform_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION 'AI configuration history is retained' USING ERRCODE='23514';
 END IF;
 IF TG_TABLE_NAME='ai_gatewayswitch' THEN
  IF jsonb_typeof(NEW.approved_regions)<>'array'
  OR EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.approved_regions) x
   WHERE jsonb_typeof(x)<>'string')
  OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(NEW.approved_regions) x
   WHERE x<>ALL(ARRAY['br-synthetic','br-synthetic-secondary',
    'eu-synthetic','us-synthetic','sa-east-1'])) THEN
   RAISE EXCEPTION 'AI regions denied' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
 END IF;
 IF TG_OP='INSERT' THEN
  IF NEW.state<>'draft' OR NEW.approval_ref<>'' THEN
   RAISE EXCEPTION 'AI model must start draft' USING ERRCODE='23514';
  END IF;
 ELSIF (
  (to_jsonb(NEW)-ARRAY['state','approval_ref']) IS DISTINCT FROM
  (to_jsonb(OLD)-ARRAY['state','approval_ref'])
  OR NOT ((OLD.state='draft' AND NEW.state='approved' AND NEW.approval_ref<>'')
   OR (OLD.state='approved' AND NEW.state='active'
    AND NEW.approval_ref=OLD.approval_ref)
   OR (OLD.state IN ('draft','approved','active') AND NEW.state='retired'
    AND NEW.approval_ref=OLD.approval_ref))) THEN
  RAISE EXCEPTION 'AI model transition denied' USING ERRCODE='23514';
 END IF;
 IF NEW.retention_terms='' OR NEW.training_terms='' THEN
  RAISE EXCEPTION 'AI terms required' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.ai_platform_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_platform_guard() TO clinic_owner;
RESET ROLE;
CREATE TRIGGER ai_model_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.ai_modelconfiguration FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ai_platform_guard();
CREATE TRIGGER ai_switch_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.ai_gatewayswitch FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ai_platform_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ai_platform_guard() FROM clinic_owner;
RESET ROLE;
"""
