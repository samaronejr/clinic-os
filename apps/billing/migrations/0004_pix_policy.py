"""Enforce exact invoice binding and append-only synthetic PIX history."""

from typing import ClassVar

from django.db import migrations

SQL = """
CREATE FUNCTION clinic_app.billing_pix_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 invoice clinic_app.billing_invoice;
 predecessor clinic_app.billing_pixoperation;
 operation clinic_app.billing_pixoperation;
BEGIN
 IF TG_OP <> 'INSERT' THEN
   RAISE EXCEPTION 'immutable PIX history' USING ERRCODE = '23514';
 END IF;
 SELECT * INTO invoice FROM clinic_app.billing_invoice
 WHERE id = NEW.invoice_id FOR UPDATE;
 IF invoice.id IS NULL OR invoice.organization_id <> NEW.organization_id
    OR invoice.state <> 'open' THEN
   RAISE EXCEPTION 'invalid PIX invoice' USING ERRCODE = '23514';
 END IF;
 IF TG_TABLE_NAME = 'billing_pixoperation' THEN
   IF ROW(NEW.amount_minor, NEW.currency, NEW.invoice_reference)
      IS DISTINCT FROM ROW(invoice.amount_minor, invoice.currency,
                           invoice.reference) THEN
     RAISE EXCEPTION 'invalid PIX terms' USING ERRCODE = '23514';
   END IF;
   IF NEW.previous_id IS NOT NULL THEN
     SELECT * INTO predecessor FROM clinic_app.billing_pixoperation
       WHERE id = NEW.previous_id;
     IF predecessor.id IS NULL OR predecessor.invoice_id <> invoice.id
        OR predecessor.organization_id <> NEW.organization_id
        OR predecessor.expires_at > statement_timestamp() THEN
       RAISE EXCEPTION 'invalid PIX regeneration' USING ERRCODE = '23514';
     END IF;
   END IF;
 ELSE
   SELECT * INTO operation FROM clinic_app.billing_pixoperation
     WHERE id = NEW.operation_id;
   IF operation.id IS NULL OR operation.invoice_id <> invoice.id
      OR operation.organization_id <> NEW.organization_id
      OR NEW.provider_reference <> 'synthetic-pix-' || operation.id::text
      OR NEW.copy_code NOT LIKE 'SYNTHETIC-NOT-PAYABLE|%'
      OR length(NEW.qr_base64) NOT BETWEEN 1 AND 65536 THEN
     RAISE EXCEPTION 'invalid synthetic PIX result' USING ERRCODE = '23514';
   END IF;
 END IF;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.billing_pix_guard() FROM PUBLIC, clinic_app;
"""

for table in ("pixoperation", "pixcharge"):
    SQL += f"""
REVOKE ALL ON clinic_app.billing_{table} FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.billing_{table} TO clinic_app;
ALTER TABLE clinic_app.billing_{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.billing_{table} FORCE ROW LEVEL SECURITY;
CREATE POLICY billing_staff ON clinic_app.billing_{table}
 USING (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
   AND clinic_app.billing_staff_invoice(invoice_id))
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
   AND clinic_app.billing_staff_invoice(invoice_id));
CREATE TRIGGER billing_pix_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.billing_{table} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.billing_pix_guard();
"""

REVERSE_SQL = ""
for table in ("pixcharge", "pixoperation"):
    REVERSE_SQL += f"""
DROP TRIGGER billing_pix_guard ON clinic_app.billing_{table};
DROP POLICY billing_staff ON clinic_app.billing_{table};
ALTER TABLE clinic_app.billing_{table} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.billing_{table} DISABLE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.billing_{table} FROM clinic_app;
"""
REVERSE_SQL += "DROP FUNCTION clinic_app.billing_pix_guard();"


class Migration(migrations.Migration):
    """Narrow runtime grants separately from creation of the model tables."""

    dependencies: ClassVar = [("billing", "0003_pixoperation_pixcharge_and_more")]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
