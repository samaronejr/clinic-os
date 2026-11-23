"""Link every referral document that predates care plans, once (todo 52, B7).

Each existing ``PrescriptionDraft`` of kind referral without a coordination row
gets one ``documented`` referral with an unspecified target: nothing about its
destination or progress is inferred, and its creator is the document's issuer.
Runtime code never creates plan-less referrals, so this runs as trusted operator
code: the care guard is disabled for this one statement, inside the migration
transaction, and the resolver's insert grant is temporary. Its foreign keys are
checked when the statement ends: a deferred check still queued would forbid
re-enabling the guard in the same transaction. Re-running links only documents
still unlinked. The reverse keeps the rows; rollback = restore.
"""

from typing import ClassVar

from django.db import migrations

from apps.workflows.migrations._referral_backfill_sql import BACKFILL


class Migration(migrations.Migration):
    """Additive data step; the reverse leaves the coordination rows in place."""

    dependencies: ClassVar = [("workflows", "0006_care_plans")]
    operations: ClassVar = [migrations.RunSQL(BACKFILL, migrations.RunSQL.noop)]
