"""Serialize organization-wide export projections with every source clinic."""

from typing import ClassVar

from django.db import migrations

from apps.ai.migrations._care_sql import _resolver
from apps.ai.migrations._pending_decision_sql import _CHOICE, _LOCK

# Keep all clinic writer order, admission decisions, owners and ACLs. The
# additional key is only for projections whose audience spans several clinics.
_ORG = (
    "   PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended("
    "'ai-pending-export:'||NEW.organization_id::text,0));\n"
)


class Migration(migrations.Migration):
    """Reverse restores the exact 0024 function and changes no schema state."""

    dependencies: ClassVar = [("ai", "0024_pending_decision_fence")]
    operations: ClassVar = [
        migrations.RunSQL(
            _resolver((_CHOICE.replace(_LOCK, _LOCK + _ORG),)),
            reverse_sql=_resolver((_CHOICE,)),
        )
    ]
