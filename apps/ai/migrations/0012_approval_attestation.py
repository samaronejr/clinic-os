"""Count only votes whose step-up PostgreSQL verified; approve only with quorum.

clinic_app loses INSERT on votes; ``agent_record_approval`` records one only for
the approver's persisted fresh TOTP or passkey session and stores that instant.
Proposals declare their payload quorum, and approval, execution claims and sends
need that many current votes. Votes recorded earlier carry no verified instant
and no longer count. Reversing is supported on an empty synthetic database;
retained history requires restore instead.
"""

from typing import ClassVar

from django.db import migrations, models

from apps.ai.migrations import _approval_attestation_sql


class Migration(migrations.Migration):
    """Columns, the vote writer, the counted-vote rule and the guard change together."""

    dependencies: ClassVar = [("ai", "0011_revenue_finance_bound")]
    operations: ClassVar = [
        migrations.AddField(
            model_name="actionproposal",
            name="quorum",
            field=models.PositiveSmallIntegerField(null=True),
        ),
        migrations.AddField(
            model_name="approval",
            name="step_up_verified_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.RunSQL(
            _approval_attestation_sql.SQL,
            reverse_sql=_approval_attestation_sql.REVERSE_SQL,
        ),
    ]
