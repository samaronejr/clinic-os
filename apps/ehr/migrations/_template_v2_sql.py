"""Version-aware template trigger; never replace the shared encounter guard."""

SQL = r"""
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ehr_template_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 p jsonb;
 item jsonb;
 content_text text;
BEGIN
 IF TG_TABLE_NAME = 'ehr_clinicaldocumentversion' THEN
   SELECT prompts INTO p FROM clinic_app.ehr_specialtytemplate
    WHERE id = NEW.template_id AND organization_id = NEW.organization_id;
   IF p ? 'schema_version' AND p->>'state' <> 'published' THEN
     RAISE EXCEPTION 'template is not published' USING ERRCODE = '23514';
   END IF;
   RETURN NEW;
 END IF;
 p := NEW.prompts;
 IF NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
   WHERE c.id = NEW.clinic_id AND c.organization_id = NEW.organization_id)
   OR jsonb_typeof(p) IS DISTINCT FROM 'object'
   OR NOT (p ?& ARRAY['subjective','objective','assessment','plan']) THEN
   RAISE EXCEPTION 'invalid template binding' USING ERRCODE = '23514';
 END IF;
 IF p ? 'schema_version' THEN
   IF p->'schema_version' IS DISTINCT FROM '2'::jsonb
      OR NOT (p ?& ARRAY['state','fields','phrases','optional_sections'])
      OR (p - ARRAY['schema_version','state','fields','phrases',
         'optional_sections','subjective','objective','assessment','plan'])
           <> '{}'::jsonb
      OR p->>'state' NOT IN ('draft','published','retired')
      OR jsonb_typeof(p->'state') IS DISTINCT FROM 'string'
      OR jsonb_typeof(p->'fields') IS DISTINCT FROM 'array'
      OR jsonb_typeof(p->'phrases') IS DISTINCT FROM 'array'
      OR jsonb_typeof(p->'optional_sections') IS DISTINCT FROM 'array'
   THEN RAISE EXCEPTION 'invalid template v2' USING ERRCODE = '23514'; END IF;
   IF jsonb_array_length(p->'fields') > 80
      OR jsonb_array_length(p->'phrases') > 80
      OR jsonb_array_length(p->'optional_sections') > 4 THEN
     RAISE EXCEPTION 'template limit' USING ERRCODE = '23514';
   END IF;
   FOR item IN SELECT value FROM jsonb_array_elements(p->'fields') LOOP
     IF jsonb_typeof(item) IS DISTINCT FROM 'object'
       OR NOT (item ?& ARRAY['id','section','label','type','required'])
       OR item->>'type' NOT IN
         ('text','number','choice','multichoice','date','boolean','scale-ref')
       OR jsonb_typeof(item->'required') IS DISTINCT FROM 'boolean'
     THEN RAISE EXCEPTION 'invalid template field' USING ERRCODE = '23514';
     END IF;
   END LOOP;
 ELSE
   IF (p - ARRAY['subjective','objective','assessment','plan']) <> '{}'::jsonb THEN
     RAISE EXCEPTION 'invalid template v1' USING ERRCODE = '23514';
   END IF;
 END IF;
 FOR content_text IN SELECT unnest(ARRAY[
   'subjective','objective','assessment','plan']) LOOP
   IF jsonb_typeof(p->content_text) IS DISTINCT FROM 'string'
      OR length(p->>content_text) > 1000 THEN
     RAISE EXCEPTION 'invalid SOAP prompt' USING ERRCODE = '23514';
   END IF;
 END LOOP;
 -- Inspect leaf strings, never JSON punctuation. The former overlay guard
 -- rejected nested JSON's braces even when every text value was plain.
 FOR content_text IN
   SELECT value #>> '{}' FROM jsonb_path_query(p, '$.** ? (@.type() == "string")')
     AS leaves(value)
   UNION ALL SELECT NEW.title
 LOOP
   IF content_text ~* ('[<>]|(javascript|vbscript|data)\s*:|/\*|\*/|[{}]|'
       || '(display|visibility|position|color|background)\s*:') THEN
     RAISE EXCEPTION 'overlay requires plain text' USING ERRCODE = '23514';
   END IF;
 END LOOP;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.ehr_template_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ehr_template_guard() TO clinic_owner;
RESET ROLE;
DROP TRIGGER ehr_binding ON clinic_app.ehr_specialtytemplate;
DROP TRIGGER template_plain_text ON clinic_app.ehr_specialtytemplate;
CREATE TRIGGER ehr_binding BEFORE INSERT ON clinic_app.ehr_specialtytemplate
 FOR EACH ROW EXECUTE FUNCTION clinic_app.ehr_template_guard();
CREATE TRIGGER template_published BEFORE INSERT
 ON clinic_app.ehr_clinicaldocumentversion
 FOR EACH ROW EXECUTE FUNCTION clinic_app.ehr_template_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ehr_template_guard() FROM clinic_owner;
RESET ROLE;
ALTER POLICY clinical_insert ON clinic_app.ehr_specialtytemplate
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND clinic_app.questionnaire_staff(
   clinic_id, ARRAY['clinic_admin','owner','physician']));
"""

REVERSE_SQL = """
SET LOCAL ROLE clinic_resolver;
DO $f$ BEGIN
 IF EXISTS (SELECT 1 FROM clinic_app.ehr_specialtytemplate
   WHERE prompts ? 'schema_version') THEN
   RAISE EXCEPTION 'template_v2_rollback_is_restore' USING ERRCODE = '23514';
 END IF;
END $f$;
GRANT EXECUTE ON FUNCTION clinic_app.ehr_binding_guard(),
 clinic_app.overlay_plain_text_guard() TO clinic_owner;
RESET ROLE;
DROP TRIGGER template_published ON clinic_app.ehr_clinicaldocumentversion;
DROP TRIGGER ehr_binding ON clinic_app.ehr_specialtytemplate;
CREATE TRIGGER ehr_binding BEFORE INSERT ON clinic_app.ehr_specialtytemplate
 FOR EACH ROW EXECUTE FUNCTION clinic_app.ehr_binding_guard();
CREATE TRIGGER template_plain_text BEFORE INSERT ON clinic_app.ehr_specialtytemplate
 FOR EACH ROW EXECUTE FUNCTION clinic_app.overlay_plain_text_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ehr_binding_guard(),
 clinic_app.overlay_plain_text_guard() FROM clinic_owner;
DROP FUNCTION clinic_app.ehr_template_guard();
RESET ROLE;
ALTER POLICY clinical_insert ON clinic_app.ehr_specialtytemplate
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND clinic_app.questionnaire_staff(clinic_id, ARRAY['clinic_admin','owner']));
"""
