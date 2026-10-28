"""Distinguish crypto-shredded chunks from completed object deletion."""

from typing import ClassVar

from django.db import migrations, models


class Migration(migrations.Migration):
    """Keep all recording/chunk identities and original upload deadlines."""

    dependencies: ClassVar = [("scribe", "0002_ai_disclosure_capture")]
    operations: ClassVar = [
        migrations.AddField(
            model_name="audiochunk",
            name="storage_location",
            field=models.CharField(max_length=64, default=""),
        ),
        migrations.RemoveConstraint(model_name="audiochunk", name="scribe_chunk_state"),
        migrations.AddConstraint(
            model_name="audiochunk",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    state__in=(
                        "buffered",
                        "accepted",
                        "quarantined",
                        "rejected",
                        "purged",
                        "purging",
                    )
                ),
                name="scribe_chunk_state",
            ),
        ),
    ]
