"""Share the Finance guide's complete read scope; no row or table grants."""

from typing import ClassVar

from django.db import migrations

from apps.ai.migrations import _guide_read_scope_sql


class Migration(migrations.Migration):
    """Keep the enterprise fence and the guide policy as shared decisions."""

    dependencies: ClassVar = [
        ("ai", "0021_guide_consent"),
        ("identity", "0051_enterprise_governance"),
    ]
    operations: ClassVar = [
        migrations.RunSQL(_guide_read_scope_sql.SQL, _guide_read_scope_sql.REVERSE_SQL)
    ]
