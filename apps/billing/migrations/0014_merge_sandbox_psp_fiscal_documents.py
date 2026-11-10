"""Join the independent sandbox PSP and NFS-e fiscal migration branches."""

from typing import ClassVar

from django.db import migrations


class Migration(migrations.Migration):
    """Preserve both reviewed branches without changing their operations."""

    dependencies: ClassVar = [
        ("billing", "0012_sandbox_psp"),
        ("billing", "0013_fiscal_documents"),
    ]
    operations: ClassVar = []
