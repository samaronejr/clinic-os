"""Explicit record audience; an unreviewed release stays private from delegates."""

from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.retention_patient_releases()
RETURNS TABLE(release_id uuid,version_id uuid,document_id uuid,encounter_id uuid,
 version_number integer,state text,content_digest text,finalized_at timestamptz,
 amendment_of_version integer,author_label text,content bytea,released_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT r.id,v.id,d.id,e.id,v.version,v.state::text,v.content_digest::text,
   v.finalized_at,base.version,u.username::text,v.content,r.created_at
 FROM clinic_app.retention_recordrelease r
 JOIN clinic_app.ehr_clinicaldocumentversion v ON v.id=r.version_id
 JOIN clinic_app.ehr_clinicaldocument d ON d.id=v.document_id
 JOIN clinic_app.ehr_encounter e ON e.id=d.encounter_id
 JOIN clinic_app.identity_user u ON u.id=v.author_id
 LEFT JOIN clinic_app.ehr_clinicaldocumentversion base ON base.id=v.amendment_of_id
 WHERE r.revoked_at IS NULL
   AND (NOT clinic_app.patient_is_delegate() OR NOT r.restricted_from_delegates)
   AND EXISTS (SELECT 1 FROM clinic_app.retention_records_session() s
   WHERE s.organization_id=r.organization_id AND s.clinic_id=r.clinic_id
     AND s.patient_id=r.patient_id)
 ORDER BY e.created_at,d.id,v.version
$f$;
CREATE FUNCTION clinic_app.delegate_confidentiality_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF NEW.restricted_from_delegates
 IS DISTINCT FROM OLD.restricted_from_delegates THEN
   RAISE EXCEPTION 'record audience is immutable' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.delegate_confidentiality_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.delegate_confidentiality_guard() TO clinic_owner;
RESET ROLE;
CREATE TRIGGER delegate_release_confidentiality BEFORE UPDATE
 ON clinic_app.retention_recordrelease FOR EACH ROW
 EXECUTE FUNCTION clinic_app.delegate_confidentiality_guard();
CREATE TRIGGER delegate_form_confidentiality BEFORE UPDATE
 ON clinic_app.intake_questionnaireresponse FOR EACH ROW
 EXECUTE FUNCTION clinic_app.delegate_confidentiality_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.delegate_confidentiality_guard()
 FROM clinic_owner;
RESET ROLE;
CREATE POLICY delegate_confidentiality ON clinic_app.retention_recordrelease
 AS RESTRICTIVE TO clinic_app
 USING (NOT clinic_app.patient_is_delegate() OR NOT restricted_from_delegates)
 WITH CHECK (NOT clinic_app.patient_is_delegate() OR NOT restricted_from_delegates);
"""


class Migration(migrations.Migration):
    """Add the audience flag and prevent history from being silently widened."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("retention", "0003_protected_fields"),
        ("intake", "0014_delegate_sessions"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="recordrelease",
            name="restricted_from_delegates",
            field=models.BooleanField(default=True),
        ),
        migrations.RunSQL(SQL, reverse_sql=None),
    ]
