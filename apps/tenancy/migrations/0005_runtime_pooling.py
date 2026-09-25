"""Give transaction-pooled runtime backends stable, bounded role defaults."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    """Alter only the runtime role via its existing owner SET membership."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("tenancy", "0004_protected_fields")
    ]
    operations: ClassVar[Sequence[Operation]] = [
        migrations.RunSQL(
            "SET ROLE clinic_app; "
            "ALTER ROLE clinic_app SET search_path = clinic_app, public; "
            "ALTER ROLE clinic_app SET idle_in_transaction_session_timeout = '15s'; "
            "RESET ROLE;",
            "SET ROLE clinic_app; "
            "ALTER ROLE clinic_app RESET search_path; "
            "ALTER ROLE clinic_app RESET idle_in_transaction_session_timeout; "
            "RESET ROLE;",
        ),
    ]
