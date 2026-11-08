"""Install the provider-principal audit binding for inbox ingest."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.comms.migrations._inbound_audit_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Additive SQL functions; no table or grant shape changes for the runtime."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("audit", "0005_clinic_metadata_v2"),
        ("comms", "0008_omnichannel_inbox"),
    ]
    operations: ClassVar[Sequence[Operation]] = [
        migrations.RunSQL(SQL, reverse_sql=REVERSE_SQL),
    ]
