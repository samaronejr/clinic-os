"""Expose only verification metadata to narrow lifecycle resolvers."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    """Keep OTP seed/key columns inaccessible to the resolver role."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0022_lifecycle_surface_scope"),
    ]
    operations: ClassVar[Sequence[Operation]] = [
        migrations.RunSQL(
            """
GRANT SELECT (id, user_id, confirmed, last_t, last_used_at)
    ON clinic_app.otp_totp_totpdevice TO clinic_resolver;
GRANT SELECT (id, user_id, confirmed)
    ON clinic_app.otp_static_staticdevice TO clinic_resolver;
"""
        ),
    ]
