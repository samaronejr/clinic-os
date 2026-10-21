"""Publish the longitudinal schema and its authority contract together."""

from django.db import migrations


class ProtectedSchema(migrations.SeparateDatabaseAndState):
    """Keep the declared database operations atomic and restore-only."""

    atomic = True
    reversible = False
