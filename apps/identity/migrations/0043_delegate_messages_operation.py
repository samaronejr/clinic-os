"""Widen the delegate operation vocabulary to the portal messages operation."""

from typing import ClassVar

from django.db import migrations

from ._delegate_messages_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Additive check widening; a reverse needs every grant without messages."""

    # Only the delegate grant's own check changes. An intake edge would make the
    # identity leaf pull the late intake chain into historical migration states.
    dependencies: ClassVar = [("identity", "0042_privacy_request_permission")]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
