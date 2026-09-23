"""Bounded append-only settings without granting writes to identity or history."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

SQL = r"""
ALTER TABLE clinic_app.identity_clinicconfiguration ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_clinicconfiguration FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_clinicconfiguration ADD CONSTRAINT config_clinic_org_fk
 FOREIGN KEY (organization_id,clinic_id)
 REFERENCES clinic_app.identity_clinic(organization_id,id);
ALTER TABLE clinic_app.identity_clinicconfiguration ADD CONSTRAINT config_logo_bound
 CHECK (octet_length(logo_png)<=262144 AND (octet_length(logo_png)=0
 OR substring(logo_png from 1 for 8)=decode('89504e470d0a1a0a','hex')));
REVOKE ALL ON clinic_app.identity_clinicconfiguration FROM PUBLIC,clinic_app;
GRANT SELECT,INSERT ON clinic_app.identity_clinicconfiguration TO clinic_app;
GRANT SELECT ON clinic_app.identity_clinicconfiguration TO clinic_resolver;
CREATE POLICY setup_tenant ON clinic_app.identity_clinicconfiguration TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY configuration_read ON clinic_app.identity_clinicconfiguration
 FOR SELECT TO clinic_app USING (clinic_app.questionnaire_staff(clinic_id,
 ARRAY['owner','clinic_admin','physician','receptionist']));
CREATE POLICY configuration_insert ON clinic_app.identity_clinicconfiguration
 FOR INSERT TO clinic_app WITH CHECK (clinic_app.questionnaire_staff(clinic_id,
 ARRAY['owner','clinic_admin']));
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.configuration_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE last_version integer;
BEGIN
 IF TG_OP<>'INSERT' THEN
   RAISE EXCEPTION 'configuration history is immutable' USING ERRCODE='23514';
 END IF;
 IF NOT clinic_app.questionnaire_staff(NEW.clinic_id,ARRAY['owner','clinic_admin'])
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
 NEW.created_at:=statement_timestamp();
 RETURN NEW;
END $f$;
CREATE FUNCTION clinic_app.overlay_plain_text_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE content_text text;
BEGIN
 IF TG_TABLE_NAME='consent_consenttext' THEN content_text:=NEW.text;
 ELSIF TG_TABLE_NAME='identity_clinicconfiguration' THEN content_text:=NEW.display_name;
 ELSE
   SELECT NEW.title||string_agg(p.value,' ') INTO content_text
   FROM jsonb_each_text(NEW.prompts) p;
 END IF;
 IF content_text ~* ('[<>]|(javascript|vbscript|data)\s*:|/\*|\*/|[{}]|'
   || '(display|visibility|position|color|background)\s*:')
 THEN RAISE EXCEPTION 'overlay requires plain text' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.configuration_guard(),
 clinic_app.overlay_plain_text_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.configuration_guard(),
 clinic_app.overlay_plain_text_guard() TO clinic_owner;
RESET ROLE;
CREATE TRIGGER configuration_immutable BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.identity_clinicconfiguration FOR EACH ROW
 EXECUTE FUNCTION clinic_app.configuration_guard();
CREATE TRIGGER configuration_plain_text BEFORE INSERT
 ON clinic_app.identity_clinicconfiguration FOR EACH ROW
 EXECUTE FUNCTION clinic_app.overlay_plain_text_guard();
CREATE TRIGGER template_plain_text BEFORE INSERT
 ON clinic_app.ehr_specialtytemplate FOR EACH ROW
 EXECUTE FUNCTION clinic_app.overlay_plain_text_guard();
CREATE TRIGGER consent_plain_text BEFORE INSERT
 ON clinic_app.consent_consenttext FOR EACH ROW
 EXECUTE FUNCTION clinic_app.overlay_plain_text_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.configuration_guard(),
 clinic_app.overlay_plain_text_guard() FROM clinic_owner;
RESET ROLE;
"""

REVERSE_SQL = """
DROP TRIGGER consent_plain_text ON clinic_app.consent_consenttext;
DROP TRIGGER template_plain_text ON clinic_app.ehr_specialtytemplate;
DROP TRIGGER configuration_plain_text ON clinic_app.identity_clinicconfiguration;
DROP TRIGGER configuration_immutable ON clinic_app.identity_clinicconfiguration;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.configuration_guard();
DROP FUNCTION clinic_app.overlay_plain_text_guard();
RESET ROLE;
DROP POLICY configuration_read ON clinic_app.identity_clinicconfiguration;
DROP POLICY configuration_insert ON clinic_app.identity_clinicconfiguration;
DROP POLICY setup_tenant ON clinic_app.identity_clinicconfiguration;
REVOKE SELECT ON clinic_app.identity_clinicconfiguration FROM clinic_resolver;
ALTER TABLE clinic_app.identity_clinicconfiguration DROP CONSTRAINT config_logo_bound;
ALTER TABLE clinic_app.identity_clinicconfiguration
 DROP CONSTRAINT config_clinic_org_fk;
ALTER TABLE clinic_app.identity_clinicconfiguration DISABLE ROW LEVEL SECURITY;
"""


class Migration(migrations.Migration):
    """Install independent clinic predicates and immutable publication guards."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0008_clinic_configuration"),
        ("consent", "0002_consent_policy"),
        ("ehr", "0008_finalization_policy"),
    ]
    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(SQL, REVERSE_SQL)]
