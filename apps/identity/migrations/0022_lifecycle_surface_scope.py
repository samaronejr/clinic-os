"""Install exact-user authenticator scope and a narrow staff list resolver."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._lifecycle_surface_sql import FORWARD_SQL


class Migration(migrations.Migration):
    """Add policies without disabling FORCE RLS or granting runtime user access."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0021_invitation_claim_type"),
        ("otp_static", "0003_add_timestamps"),
    ]
    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(FORWARD_SQL)]
