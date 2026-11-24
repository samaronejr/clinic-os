"""The machine grant is justified by the worker's actual FORCE-RLS read plans."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from apps.ai import tasks
from django.db import connections

from ai.agent_support import approve, propose
from ai.test_agent_execution import agent_password, machine_login, world

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.backends.utils import _ExecuteParameters

    from ai.agent_support import AgentWorld

__all__ = ("agent_password", "machine_login", "world")
pytestmark = pytest.mark.django_db(transaction=True, databases={"default", "agent"})


def test_authenticated_worker_executes_the_pending_predicate_on_both_reads(
    world: AgentWorld,
) -> None:
    proposal = propose(world)
    approve(world, proposal)
    agent = connections["agent"]
    observed: set[str] = set()

    def observe(
        execute: Callable[..., object],
        statement: str,
        params: _ExecuteParameters,
        many: bool,
        context: dict[str, object],
    ) -> object:
        if statement.startswith("SELECT "):
            for table in ("ai_actionproposal", "ai_agentrun"):
                if f'FROM "{table}"' not in statement:
                    continue
                # Explain this worker's exact ORM SELECT, with its actual
                # parameters, on the same bound agent transaction and login.
                with agent.cursor() as cursor:
                    cursor.execute(
                        "EXPLAIN (ANALYZE, TIMING OFF, COSTS OFF) " + statement, params
                    )
                    plan = "\n".join(row[0] for row in cursor.fetchall())
                assert f"ai_pending_visible('{table}'::text," in plan
                assert "actual rows=1 loops=1" in plan
                observed.add(table)
        return execute(statement, params, many, context)

    with (
        agent.execute_wrapper(observe),
        patch.object(tasks, "fair_acquire", return_value=False),
        patch.object(tasks.execute_agent, "apply_async"),
    ):
        result = tasks.run_agent(
            clinic_id=str(world.gateway.graph.clinic_a),
            principal_id=str(world.principal.pk),
            proposal_id=str(proposal.pk),
        )
    assert result == "deferred"
    assert observed == {"ai_actionproposal", "ai_agentrun"}
