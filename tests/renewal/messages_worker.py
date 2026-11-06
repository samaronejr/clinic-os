"""Browser QA worker for the inbox outbox: real clinic_app role, synthetic provider."""

from __future__ import annotations

import itertools
import json
import sys
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from apps.comms.tasks import execute_operation
from django.db import connection
from django.test import override_settings
from django.utils import timezone

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "skipped"})
MAX_ROUNDS = 8


def main() -> None:
    """Run one stored operation: ``sent`` once, or ``fail`` until it is terminal."""
    operation_id, outcome = UUID(sys.argv[1]), sys.argv[2]
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_app",)
    base = timezone.now()
    ticks = itertools.count()
    # Retry waits are database due times; advance a synthetic clock, never sleep.
    clock = SimpleNamespace(now=lambda: base + timedelta(minutes=2 * next(ticks)))
    with (
        override_settings(
            COMMS_SYNTHETIC_CHANNELS=("email", "sms", "whatsapp"),
            COMMS_SYNTHETIC_FAILURE_CHANNELS=("whatsapp",) if outcome == "fail" else (),
        ),
        patch("apps.core.integration.timezone", clock),
    ):
        results: list[str] = []
        for _ in range(MAX_ROUNDS):
            results.append(execute_operation(operation_id=str(operation_id)))
            if outcome == "sent" or results[-1] in TERMINAL:
                break
    sys.stdout.write(
        json.dumps(
            {"runtime_role": "clinic_app", "outcomes": results, "synthetic": True}
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
