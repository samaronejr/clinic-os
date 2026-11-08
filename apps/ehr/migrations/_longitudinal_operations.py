"""Publish the longitudinal schema and its authority contract together."""

from apps.ai.migrations._state_batch import SchemaStateBatch


class ProtectedSchema(SchemaStateBatch):
    """Keep the declared database operations atomic and restore-only."""

    atomic = True
    reversible = False
