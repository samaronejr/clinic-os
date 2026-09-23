"""Read-only patient projection of one released charge and its live PIX request.

The resolver repeats the exact session predicate of
``billing_patient_charges``: a live, unrevoked session bound to the same
organization, clinic and patient, carrying the ``billing`` operation. It adds
only the payment instructions a patient needs - the stored copy text, the
stored QR bytes, the request expiry and the clinic time zone every displayed
time is read in - plus a boolean saying that a recorded payment event still
requires clinic verification. No clinical column, staff identifier, settlement
evidence or provider implementation field is exposed, and nothing here can
mark an invoice paid.
"""

from typing import ClassVar

from django.db import migrations

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.billing_patient_charge(requested_invoice uuid)
RETURNS TABLE(invoice_id uuid, reference uuid, amount_minor bigint,
 currency varchar, state varchar, issued_at timestamptz,
 receipt_reference uuid, receipt_issued_at timestamptz,
 copy_code text, qr_base64 text, expires_at timestamptz, flagged boolean,
 clinic_timezone varchar)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT i.id, i.reference, i.amount_minor, i.currency, i.state, i.issued_at,
   r.reference, r.issued_at, pix.copy_code, pix.qr_base64, pix.expires_at,
   COALESCE(pix.flagged, false), clinic.timezone
 FROM clinic_app.billing_invoice i
 JOIN clinic_app.identity_clinic clinic ON clinic.id = i.clinic_id
   AND clinic.organization_id = i.organization_id
 LEFT JOIN clinic_app.billing_receipt r ON r.invoice_id = i.id
 LEFT JOIN LATERAL (
   SELECT c.copy_code, c.qr_base64, o.expires_at,
     EXISTS (SELECT 1 FROM clinic_app.billing_paymentevent e
       WHERE e.operation_id = o.id
       AND e.resolution = 'operator_required') AS flagged
   FROM clinic_app.billing_pixoperation o
   JOIN clinic_app.billing_pixcharge c ON c.operation_id = o.id
   WHERE o.invoice_id = i.id
     AND NOT EXISTS (SELECT 1 FROM clinic_app.billing_pixoperation n
       WHERE n.previous_id = o.id)
   ORDER BY o.created_at DESC, o.id DESC
   LIMIT 1
 ) pix ON true
 WHERE i.id = requested_invoice
   AND i.released_at IS NOT NULL AND i.issued_at IS NOT NULL
   AND EXISTS (SELECT 1 FROM clinic_app.intake_patientsession s
     JOIN clinic_app.intake_patientaccessgrant g ON g.id = s.grant_id
     WHERE s.id =
       NULLIF(current_setting('app.current_patient_session', true), '')::uuid
     AND s.organization_id = i.organization_id AND s.clinic_id = i.clinic_id
     AND s.patient_id = i.patient_id
     AND s.revoked_at IS NULL AND g.revoked_at IS NULL
     AND s.expires_at > statement_timestamp()
     AND s.idle_expires_at > statement_timestamp()
     AND 'billing' = ANY(s.operations))
$f$;
REVOKE ALL ON FUNCTION clinic_app.billing_patient_charge(uuid)
 FROM PUBLIC, clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.billing_patient_charge(uuid) TO clinic_app;
RESET ROLE;
"""

REVERSE_SQL = """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.billing_patient_charge(uuid);
RESET ROLE;
"""


class Migration(migrations.Migration):
    """Add the patient payment-instruction resolver; no table or column changes."""

    dependencies: ClassVar = [("billing", "0006_payment_event_superseded")]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
