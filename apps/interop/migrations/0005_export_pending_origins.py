"""Private origin references for point-of-download consent checks."""

from typing import ClassVar

from django.db import migrations, models


class Migration(migrations.Migration):
    """Old archive bytes have no proven dependency list; require a fresh build."""

    dependencies: ClassVar = [("interop", "0004_rnds_submission")]
    operations: ClassVar = [
        migrations.AddField(
            model_name="organizationexport",
            name="pending_origins",
            field=models.JSONField(null=True, editable=False),
        )
    ]
