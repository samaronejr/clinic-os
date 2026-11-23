"""Frozen SQL for result source tasks."""

from apps.orders.migrations._task_sql import WORKFLOW_BRIDGE

SQL = "SET LOCAL ROLE clinic_resolver;" + WORKFLOW_BRIDGE + "RESET ROLE;"
