"""Released charges resolve against the represented patient, not posted ids."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.billing_patient_charges()
RETURNS TABLE(invoice_id uuid,reference uuid,amount_minor bigint,
 currency varchar,state varchar,issued_at timestamptz,
 receipt_reference uuid,receipt_issued_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT i.id,i.reference,i.amount_minor,i.currency,i.state,i.issued_at,
   r.reference,r.issued_at FROM clinic_app.billing_invoice i
 LEFT JOIN clinic_app.billing_receipt r ON r.invoice_id=i.id
 WHERE i.released_at IS NOT NULL AND i.issued_at IS NOT NULL
 AND EXISTS (SELECT 1 FROM clinic_app.current_patient_authority('billing') s
 WHERE s.organization_id=i.organization_id AND s.clinic_id=i.clinic_id
   AND s.patient_id=i.patient_id)
 ORDER BY i.created_at,i.id
$f$;
CREATE OR REPLACE FUNCTION clinic_app.billing_patient_charge(requested_invoice uuid)
RETURNS TABLE(invoice_id uuid,reference uuid,amount_minor bigint,
 currency varchar,state varchar,issued_at timestamptz,
 receipt_reference uuid,receipt_issued_at timestamptz,
 copy_code bytea,qr_base64 bytea,expires_at timestamptz,flagged boolean,
 clinic_timezone varchar)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT i.id,i.reference,i.amount_minor,i.currency,i.state,i.issued_at,
   r.reference,r.issued_at,pix.copy_code,pix.qr_base64,pix.expires_at,
   coalesce(pix.flagged,false),c.timezone
 FROM clinic_app.billing_invoice i
 JOIN clinic_app.identity_clinic c ON c.id=i.clinic_id
 LEFT JOIN clinic_app.billing_receipt r ON r.invoice_id=i.id
 LEFT JOIN LATERAL (
   SELECT charge.copy_code,charge.qr_base64,o.expires_at,
   EXISTS (SELECT 1 FROM clinic_app.billing_paymentevent e
     WHERE e.operation_id=o.id AND e.resolution='operator_required') flagged
   FROM clinic_app.billing_pixoperation o
   JOIN clinic_app.billing_pixcharge charge ON charge.operation_id=o.id
   WHERE o.invoice_id=i.id
   AND NOT EXISTS (SELECT 1 FROM clinic_app.billing_pixoperation n
     WHERE n.previous_id=o.id)
   ORDER BY o.created_at DESC,o.id DESC LIMIT 1
 ) pix ON true
 WHERE i.id=requested_invoice AND i.released_at IS NOT NULL
 AND i.issued_at IS NOT NULL
 AND EXISTS (SELECT 1 FROM clinic_app.current_patient_authority('billing') s
 WHERE s.organization_id=i.organization_id AND s.clinic_id=i.clinic_id
   AND s.patient_id=i.patient_id)
$f$;
RESET ROLE;
"""


class Migration(migrations.Migration):
    """Preserve charge output and all existing ACLs."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("billing", "0009_protected_fields"),
        ("intake", "0014_delegate_sessions"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(SQL, reverse_sql=None),
    ]
