"""A killed OS worker loses both its session lock and uncommitted effect."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from apps.workflows import engine
from apps.workflows.models import Task, WorkflowStep
from django.db import connection
from django.utils import timezone

from database_urls import database_url_for_name
from identity.permission_support import owner_context
from workflows.test_engine import execute, publish, start, step_id, task_step

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
ROOT = Path(__file__).resolve().parents[2]


async def kill_at_cut_point(identifier: UUID, environment: dict[str, str]) -> None:
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "tests/workflows/crash_worker.py",
        str(identifier),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=ROOT,
        env=environment,
    )
    try:
        assert child.stdout is not None
        assert child.stdin is not None
        assert await asyncio.wait_for(child.stdout.readline(), 15) == b"worker-ready\n"
        # Subscribe to the effect signal before triggering execution.
        effect = asyncio.create_task(child.stdout.readline())
        child.stdin.write(b"execute\n")
        await child.stdin.drain()
        assert await asyncio.wait_for(effect, 15) == b"effect-uncommitted\n"
        assert await asyncio.to_thread(Path(f"/proc/{child.pid}/cwd").resolve) == ROOT
        child.kill()
        assert await asyncio.wait_for(child.wait(), 15) == -signal.SIGKILL
    finally:
        if child.returncode is None:
            assert (
                await asyncio.to_thread(Path(f"/proc/{child.pid}/cwd").resolve) == ROOT
            )
            child.kill()
            await asyncio.wait_for(child.wait(), 15)


def test_killed_effect_transaction_is_reclaimed_once(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = rbac_graph
    run = start(graph, publish(graph, [task_step()]))
    identifier = step_id(graph, run)
    environment = dict(os.environ)
    environment.update(
        DJANGO_SETTINGS_MODULE="config.settings.base",
        APP_DATABASE_URL=database_url_for_name(
            os.environ["APP_DATABASE_URL"], str(connection.settings_dict["NAME"])
        ),
        CELERY_BROKER_URL="memory://",
        CELERY_RESULT_BACKEND="",
        PYTHONPATH=str(ROOT),
    )
    asyncio.run(kill_at_cut_point(identifier, environment))
    with owner_context(graph.organization_a):
        assert Task.objects.count() == 0
        abandoned = WorkflowStep.objects.get(pk=identifier)
        assert abandoned.state == "running"
        assert abandoned.fencing_token == 1
    now = timezone.now() + timedelta(hours=1)
    monkeypatch.setattr(engine, "utc_now", lambda: now)
    assert execute(identifier) == "completed"
    assert execute(identifier) == "completed"
    with owner_context(graph.organization_a):
        assert Task.objects.count() == 1
        abandoned.refresh_from_db()
        assert abandoned.fencing_token == 2
