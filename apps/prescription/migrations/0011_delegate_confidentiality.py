"""Keep unreviewed prescription PDFs out of delegated lists and exports."""

from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.prescription.migrations._delegate_confidentiality_sql import SQL


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
