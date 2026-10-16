"""Keep saved views private when clinic memberships are retained after revocation."""

from collections.abc import Sequence
from typing import ClassVar, Final

from django.db import migrations
from django.db.migrations.operations.base import Operation

_OWNER: Final = """
        organization_id = NULLIF(
            pg_catalog.current_setting('app.current_tenant', true), ''
        )::pg_catalog.uuid
        AND user_id = NULLIF(
            pg_catalog.current_setting('app.current_user_id', true), ''
        )::pg_catalog.uuid
        AND EXISTS (
            SELECT 1 FROM clinic_app.identity_userclinicrole AS role_row
             WHERE role_row.user_id = identity_savedview.user_id
               AND role_row.clinic_id = identity_savedview.clinic_id
               AND role_row.organization_id = identity_savedview.organization_id
               AND role_row.revoked_at IS NULL
        )
"""

SQL: Final = f"""
ALTER POLICY savedview_owner_only ON clinic_app.identity_savedview
    USING ({_OWNER})
    WITH CHECK ({_OWNER});
"""


class Migration(migrations.Migration):
    """Require current membership for reads, inserts and archive transitions."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0027_session_revoke_scope"),
    ]

    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(SQL)]
