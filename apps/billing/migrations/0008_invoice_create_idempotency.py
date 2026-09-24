"""Bind every charge creation to one replayable organization-scoped key.

The unique key is what makes a resubmitted create form converge instead of
opening a second charge, so the database - not the browser and not the view -
owns that identity. The two columns are added in SQL because
``billing_invoice_guard`` rejects any row update that does not follow the
charge lifecycle: PostgreSQL evaluates the volatile ``gen_random_uuid()``
default once per existing row during the add, and both defaults are dropped
immediately so every later insert must state its own key and fingerprint.
Neither column is granted to ``clinic_app`` or ``clinic_resolver`` for UPDATE,
so a stored charge can never be rebound to a different create submission.
"""

from typing import ClassVar

from django.db import migrations, models

SQL = """
ALTER TABLE clinic_app.billing_invoice
 ADD COLUMN idempotency_key uuid NOT NULL DEFAULT gen_random_uuid(),
 ADD COLUMN create_fingerprint bytea NOT NULL DEFAULT ''::bytea;
ALTER TABLE clinic_app.billing_invoice
 ALTER COLUMN idempotency_key DROP DEFAULT,
 ALTER COLUMN create_fingerprint DROP DEFAULT;
"""

REVERSE_SQL = """
ALTER TABLE clinic_app.billing_invoice
 DROP COLUMN create_fingerprint,
 DROP COLUMN idempotency_key;
"""


class Migration(migrations.Migration):
    """Add the charge-create key and its exact-terms fingerprint."""

    dependencies: ClassVar = [("billing", "0007_patient_charge_detail")]
    operations: ClassVar = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.RunSQL(SQL, REVERSE_SQL)],
            state_operations=[
                migrations.AddField(
                    model_name="invoice",
                    name="idempotency_key",
                    field=models.UUIDField(),
                ),
                migrations.AddField(
                    model_name="invoice",
                    name="create_fingerprint",
                    field=models.BinaryField(editable=False, max_length=32),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="billing_invoice_org_idempotency_uniq",
            ),
        ),
    ]
