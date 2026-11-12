"""Run browser-created operations through faulted synthetic provider boundaries."""

from __future__ import annotations

import itertools
import json
import sys
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from apps.core.integration import execute_operation
from django.db import connection
from django.utils import timezone


def main() -> None:
    """Drain bounded retries, or stop at an ambiguous unconfirmed acceptance."""
    operation = UUID(sys.argv[1])
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_app",)
    base = timezone.now()
    ticks = itertools.count()
    clock = SimpleNamespace(now=lambda: base + timedelta(minutes=2 * next(ticks)))
    outcomes: list[str] = []
    with patch("apps.core.integration.timezone", clock):
        for _ in range(8):
            outcomes.append(execute_operation(operation))
            if outcomes[-1] != "retry":
                break
    sys.stdout.write(
        json.dumps({"runtime_role": "clinic_app", "outcomes": outcomes}) + "\n"
    )
