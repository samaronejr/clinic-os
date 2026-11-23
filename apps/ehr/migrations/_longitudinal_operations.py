"""Publish the longitudinal schema and its authority contract together."""

from apps.ai.migrations._state_batch import CheckpointedStateBatch


class ProtectedSchema(CheckpointedStateBatch):
    """Keep the declared database operations atomic and restore-only."""

    atomic = True
    reversible = False
