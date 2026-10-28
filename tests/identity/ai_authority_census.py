"""Execute AI authority witnesses before issuing merged-census certificates."""

from __future__ import annotations

import time
from contextlib import contextmanager
from functools import partial
from secrets import token_urlsafe
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

import psycopg
from apps.identity import stepup
from django.db import connections
from psycopg import sql

from ai import (
    test_agent_execution,
    test_agent_guards,
    test_brief,
    test_cancellation,
    test_controls,
    test_gateway,
    test_posture,
)
from ai.agent_support import agent_world
from ai.brief_support import seeded

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    import pytest

    from rbac_fixtures import RbacGraph


class _ProofRecorder(Protocol):
    def run(self, label: str, oracle: Callable[[], None]) -> None: ...


@contextmanager
def _agent_login(database_url: str, patch: pytest.MonkeyPatch) -> Iterator[str]:
    """Give each retained principal its own real login without rewriting history."""
    name = "clinic_agent_t39_" + uuid4().hex
    secret = token_urlsafe(32)
    agent = connections["agent"]
    agent.close()
    with psycopg.connect(database_url, autocommit=True) as owner:
        owner.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE clinic_agent").format(
                sql.Identifier(name),
                sql.Literal(secret),
            )
        )
        try:
            with patch.context() as local:
                local.setattr(stepup, "_utc_now_seconds", lambda: int(time.time()))
                local.setitem(agent.settings_dict, "USER", name)
                local.setitem(agent.settings_dict, "PASSWORD", secret)
                try:
                    yield name
                finally:
                    agent.close()
        finally:
            owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))


def exercise_ai_authority(
    proofs: _ProofRecorder,
    graph: Callable[[], RbacGraph],
    patch: pytest.MonkeyPatch,
    superuser_database_url: str,
    secret_root: Path,
) -> None:
    """Bind source candidates to executed scope, role, owner and transport checks."""
    with patch.context() as local:
        proofs.run(
            "AI executed authority:metering and protected sinks",
            partial(
                test_gateway.test_fake_call_is_metered_once_and_all_content_stays_encrypted,
                graph(),
                local,
                superuser_database_url,
            ),
        )
    for oracle in (
        test_gateway.test_unknown_foreign_and_other_user_inputs_have_identical_denial,
        test_controls.test_owner_cli_changes_only_synthetic_model_and_global_controls,
        test_controls.test_native_controls_execute_valid_invalid_stale_and_forbidden_requests,
        test_controls.test_http_controls_execute_denied_malformed_and_conflict_refusal_exits,
        test_controls.test_ai_policy_requires_organization_permission_in_every_clinic,
        test_controls.test_unknown_and_foreign_clinic_control_reads_refuse_equally,
        test_posture.test_closed_circuit_can_reopen_without_losing_history,
        test_posture.test_ledger_guard_refuses_inactive_admission_with_raw_actor_context,
    ):
        with patch.context() as local:
            proofs.run(
                f"AI executed authority:{oracle.__name__}",
                partial(oracle, graph(), local),
            )
    with patch.context() as local:
        proofs.run(
            "AI executed authority:inactive actor releases only reserved metadata",
            partial(
                test_cancellation.test_revoked_actor_cancellation_releases_the_unspent_reservation,
                graph(),
                local,
                inactive_actor=True,
            ),
        )
    for agent_oracle in (
        test_agent_guards.test_agent_scope_and_approval_refusals_are_side_effect_free,
        test_agent_guards.test_fabricated_step_up_and_double_approval_cannot_add_votes,
        test_agent_execution.test_revoked_approver_blocks_after_approval,
        test_agent_execution.test_superseded_source_publication_blocks_execution,
    ):
        with (
            patch.context() as local,
            _agent_login(superuser_database_url, local),
        ):
            subject = agent_world(graph(), local, secret_root)
            proofs.run(
                f"Agent executed authority:{agent_oracle.__name__}",
                partial(agent_oracle, subject),
            )
    with patch.context() as local, _agent_login(superuser_database_url, local):
        proofs.run(
            "Agent native exact-effect decisions and refusal exits",
            partial(
                test_agent_guards.test_native_agent_decisions_use_the_displayed_digest_and_preserve_refusals,
                graph(),
                local,
                secret_root,
            ),
        )
    for chart_oracle in (
        test_brief.test_brief_every_assertion_has_exact_authorized_source,
        test_brief.test_unauthorized_actor_gets_no_context_counts_or_artifact,
        test_brief.test_restricted_note_excluded_then_granted_and_hidden_after_revocation,
    ):
        with patch.context() as local:
            chart_subject = seeded(graph(), local)
            proofs.run(
                f"Chart permitted-source authority:{chart_oracle.__name__}",
                partial(chart_oracle, chart_subject),
            )
    with patch.context() as local:
        chart_subject = seeded(graph(), local)
        proofs.run(
            "Chart pre-send source revocation",
            partial(
                test_brief.test_revoke_before_dispatch_does_not_send_old_references,
                chart_subject,
                local,
            ),
        )
    with patch.context() as local:
        chart_subject = seeded(graph(), local)
        proofs.run(
            "Chart native session-bound display and current authority",
            partial(
                test_brief.test_native_and_htmx_drawer_generate_and_recheck,
                chart_subject,
                enhanced=False,
            ),
        )
