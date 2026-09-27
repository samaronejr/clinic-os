"""Synthetic kill cut point after the real task effect, before its commit."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING
from uuid import UUID

import django

if TYPE_CHECKING:
    from apps.workflows.engine import Result
    from apps.workflows.models import WorkflowStep


def main() -> None:
    django.setup()
    from apps.workflows import engine  # noqa: PLC0415

    original = engine.HANDLERS["task"]

    def cut_point(step: WorkflowStep) -> Result:
        result = original(step)
        sys.stdout.write("effect-uncommitted\n")
        sys.stdout.flush()
        # The parent kills this process at the subscribed cut point; no sleeps.
        assert sys.stdin.readline() == "continue\n"
        return result

    engine.HANDLERS = {**engine.HANDLERS, "task": cut_point}
    sys.stdout.write("worker-ready\n")
    sys.stdout.flush()
    assert sys.stdin.readline() == "execute\n"
    engine.execute_step(step_id=UUID(sys.argv[1]))


if __name__ == "__main__":
    main()
