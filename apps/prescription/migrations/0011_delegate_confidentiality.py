"""Keep unreviewed prescription PDFs out of delegated lists and exports."""

from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.prescription_patient_documents()
RETURNS TABLE(document_id uuid,document_version integer,state text,
 issued_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT d.id,d.document_version,o.state::text,o.completed_at
 FROM clinic_app.prescription_prescriptiondocumentrelease r
 JOIN clinic_app.prescription_prescriptiondocument d ON d.id=r.document_id
 JOIN clinic_app.prescription_signatureoperation o ON o.document_id=d.id
   AND o.state IN ('issued','rehearsal_complete')
 WHERE r.revoked_at IS NULL
   AND (NOT clinic_app.patient_is_delegate() OR NOT r.restricted_from_delegates)
   AND NOT EXISTS (
     SELECT 1 FROM clinic_app.prescription_prescriptiondocumentrevocation rv
     WHERE rv.document_id=d.id)
   AND EXISTS (SELECT 1 FROM clinic_app.retention_records_session() s
     WHERE s.organization_id=r.organization_id
       AND s.clinic_id=r.clinic_id AND s.patient_id=r.patient_id)
 ORDER BY d.document_version,d.id
$f$;
CREATE OR REPLACE FUNCTION clinic_app.prescription_patient_documents(kek text)
RETURNS TABLE(document_id uuid,document_version integer,state text,
 issued_at timestamptz,verification_url text)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE
 release_row RECORD;
 frozen_plain bytea;
BEGIN
 FOR release_row IN
   SELECT allowed.*,d.frozen_input,d.organization_id
   FROM clinic_app.prescription_patient_documents() allowed
   JOIN clinic_app.prescription_prescriptiondocument d
     ON d.id=allowed.document_id
 LOOP
   frozen_plain := clinic_app.prescription_document_open(
     kek,release_row.organization_id,
     'prescription.prescriptiondocument.frozen_input',release_row.frozen_input);
   document_id := release_row.document_id;
   document_version := release_row.document_version;
   state := release_row.state;
   issued_at := release_row.issued_at;
   verification_url := pg_catalog.convert_from(frozen_plain,'UTF8')
     ::jsonb->>'verification_url';
   RETURN NEXT;
 END LOOP;
 RETURN;
END
$f$;
CREATE OR REPLACE FUNCTION clinic_app.prescription_patient_document_bytes(
 kek text,requested_document uuid)
RETURNS bytea
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE
 o clinic_app.prescription_signatureoperation;
 signed_plain bytea;
BEGIN
 SELECT op.* INTO o
   FROM clinic_app.prescription_patient_documents() allowed
   JOIN clinic_app.prescription_signatureoperation op
     ON op.document_id=allowed.document_id
     AND op.state IN ('issued','rehearsal_complete')
   WHERE allowed.document_id=requested_document;
 IF o.id IS NULL OR o.signed_bytes IS NULL THEN
   RETURN NULL;
 END IF;
 signed_plain := clinic_app.prescription_document_open(
   kek,o.organization_id,'prescription.signatureoperation.signed_bytes',
   o.signed_bytes);
 IF pg_catalog.encode(clinic_app.digest(signed_plain,'sha256'),'hex')
   <> o.signed_digest THEN
   RETURN NULL;
 END IF;
 RETURN signed_plain;
END
$f$;
GRANT EXECUTE ON FUNCTION clinic_app.delegate_confidentiality_guard()
 TO clinic_owner;
RESET ROLE;
CREATE TRIGGER delegate_prescription_confidentiality BEFORE UPDATE
 ON clinic_app.prescription_prescriptiondocumentrelease FOR EACH ROW
 EXECUTE FUNCTION clinic_app.delegate_confidentiality_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.delegate_confidentiality_guard()
 FROM clinic_owner;
RESET ROLE;
CREATE POLICY delegate_confidentiality
 ON clinic_app.prescription_prescriptiondocumentrelease
 AS RESTRICTIVE TO clinic_app
 USING (NOT clinic_app.patient_is_delegate() OR NOT restricted_from_delegates)
 WITH CHECK (NOT clinic_app.patient_is_delegate() OR NOT restricted_from_delegates);
"""


class Migration(migrations.Migration):
    """Preserve signed bytes, public verification, and existing runtime grants."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("prescription", "0010_protected_documents"),
        ("retention", "0004_delegate_confidentiality"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.AddField(
            model_name="prescriptiondocumentrelease",
            name="restricted_from_delegates",
            field=models.BooleanField(default=True),
        ),
        migrations.RunSQL(SQL, reverse_sql=None),
    ]
