"""Execute AI authority witnesses before issuing merged-census certificates."""

from __future__ import annotations

import time
from contextlib import contextmanager
from functools import partial
from hashlib import sha256
from secrets import token_urlsafe
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

import psycopg
import pytest
from apps.ai.action_contracts import ActionAccessDeniedError
from apps.ai.approvals import reject_action
from apps.ai.contracts import GatewayAccessDeniedError, Purpose
from apps.ai.models import ActionProposal, AIArtifact
from apps.ai.pending_access import PendingReadOutsideVisibilityError
from apps.ai.pending_outputs import RenderSelection, ai_pending_visible, key
from apps.ai.revenue_cycle import _reference
from apps.identity import stepup
from django.conf import settings
from django.db import connections, transaction
from psycopg import sql

from ai import (
    source_visit_reads,
    test_agent_execution,
    test_agent_guards,
    test_brief,
    test_cancellation,
    test_chart_consent,
    test_controls,
    test_decision_support,
    test_evaluation_authority,
    test_evaluation_cli,
    test_evaluation_native,
    test_evaluation_runtime,
    test_extraction_rollout,
    test_gateway,
    test_guide_consent,
    test_manual_appeal,
    test_pending_output_boundary,
    test_posture,
    test_revenue_rollout,
    test_rollout,
)
from ai.agent_support import agent_world
from ai.brief_support import seeded
from ai.chart_consent_support import accept_ai
from ai.decision_support_helpers import enable, world_for
from ai.pending_display_support import withdraw
from ai.revenue_support import request_and_collect, revenue_world
from ai.test_revenue_cycle import proposal_for
from billing.journal_support import finance_scope, finance_setup

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from ai.revenue_support import RevenueWorld
    from rbac_fixtures import RbacGraph


class _ProofRecorder(Protocol):
    def run(self, label: str, oracle: Callable[[], None]) -> None: ...


def _retained_finance_reference(world: RevenueWorld) -> None:
    """The command cannot cite bytes it held before a committed withdrawal."""
    setup = world.setup
    patient, receipt = accept_ai(setup.graph, setup.appointment.enrollment_id)
    review = request_and_collect(world)
    assert review.output_id is not None
    with finance_scope(setup):
        held = AIArtifact.objects.get(pk=review.output_id)
        citation = _reference(held)
        assert citation["id"] == str(held.pk)
        assert citation["content_sha256"] == sha256(held.content.encode()).hexdigest()
        proposal = proposal_for(world, review)
    with finance_scope(setup):
        selector = key(proposal)
        digest = proposal.digest
        with transaction.atomic():
            reject_action(
                clinic_id=setup.clinic_id,
                proposal_id=selector.pk,
                expected_digest=digest,
            )
            with pytest.raises(PendingReadOutsideVisibilityError):
                ActionProposal.objects.filter(pk=selector.pk, state="rejected").exists()

            def rejected(batch: RenderSelection) -> bool:
                checked = batch.get(selector)
                return checked is not None and checked.record.state == "rejected"

            assert ai_pending_visible((selector,), rejected, clinic_id=setup.clinic_id)
            transaction.set_rollback(True)
    withdraw(patient, receipt)
    with finance_scope(setup):
        assert not AIArtifact.objects.filter(pk=held.pk).exists()
        with pytest.raises(GatewayAccessDeniedError):
            _reference(held)
        with pytest.raises(ActionAccessDeniedError):
            reject_action(
                clinic_id=setup.clinic_id,
                proposal_id=proposal.pk,
                expected_digest=proposal.digest,
            )


def exercise_pending_read_authority(
    proofs: _ProofRecorder,
    graph: Callable[[], RbacGraph],
    patch: pytest.MonkeyPatch,
    secret_root: Path,
) -> None:
    """Exercise the checked display and the independent command-side reselect."""
    with patch.context() as local:
        local.setattr(settings, "INSURANCE_SYNTHETIC_ENABLED", True)
        finance = finance_setup._get_wrapped_function()(graph())
        proofs.run(
            "Finance command cannot cite a withdrawn retained source",
            partial(
                _retained_finance_reference, revenue_world(finance, local, secret_root)
            ),
        )
    with patch.context() as local:
        proofs.run(
            "Held extraction candidate cannot be projected after withdrawal",
            partial(
                test_pending_output_boundary.test_held_extraction_candidate_is_reselected_before_projection,
                graph(),
                local,
            ),
        )
    with patch.context() as local:
        proofs.run(
            "Owned refused chart turn keeps manual fallback",
            partial(
                test_chart_consent.test_revocation_fences_queued_work_and_retained_output,
                graph(),
                local,
                Purpose.BRIEF,
                "retained",
            ),
        )
    with patch.context() as local:
        proofs.run(
            "Staff preview withholds only after the care plan's own visit refuses",
            partial(
                source_visit_reads.care_source_visit,
                graph(),
                local,
                secret_root,
                "queue",
            ),
        )


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
    exercise_pending_read_authority(proofs, graph, patch, secret_root)
    with patch.context() as local:
        local.setattr(settings, "INSURANCE_SYNTHETIC_ENABLED", True)
        finance = finance_setup._get_wrapped_function()(graph())
        proofs.run(
            "Finance guide consent: isolated bindings, withdrawal and replacement",
            partial(
                test_guide_consent.test_finance_guide_binding_is_one_bit_and_never_rebinds,
                revenue_world(finance, local, secret_root),
            ),
        )
    with patch.context() as local:
        local.setattr(settings, "INSURANCE_SYNTHETIC_ENABLED", True)
        finance = finance_setup._get_wrapped_function()(graph())
        revenue = revenue_world(finance, local, secret_root)
        proofs.run(
            "AI executed authority:retained Finance source validation",
            partial(
                test_revenue_rollout.test_retained_revenue_reproposal_keeps_original_permission,
                revenue,
                local,
                "advanced_stage",
                native=False,
            ),
        )
        proofs.run(
            "Manual claim draft with AI off: scoped denial, idempotent, unsubmitted",
            partial(
                test_manual_appeal.test_manual_appeal_with_ai_off, revenue, "global"
            ),
        )
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
    for decision_oracle in (
        test_decision_support.test_fake_model_has_separate_facts_references_hypotheses_and_exact_licensed_sources,
        test_decision_support.test_direct_gateway_artifacts_cannot_bypass_gate_revocation,
    ):
        with patch.context() as local:
            decision_subject = world_for(graph(), local)
            enable(decision_subject)
            proofs.run(
                f"Decision support current authority:{decision_oracle.__name__}",
                partial(decision_oracle, decision_subject),
            )
    with patch.context() as local:
        decision_subject = world_for(graph(), local)
        enable(decision_subject)
        proofs.run(
            "Decision support native per-use opt-in and session-bound display",
            partial(
                test_decision_support.test_native_and_htmx_panel_require_session_bound_opt_in,
                decision_subject,
                enhanced=False,
            ),
        )
    for evaluation_oracle in (
        test_evaluation_authority.test_evaluation_sql_checks_every_stored_role_and_exact_clinic,
        test_evaluation_authority.test_evaluation_raw_readers_and_rating_guard_refuse_inactive_or_foreign_actor,
        test_evaluation_runtime.test_two_independent_reviews_and_explicit_owner_change_are_required,
        test_evaluation_runtime.test_assignments_scope_and_raw_verdict_writes_cannot_be_forged,
        test_evaluation_cli.test_cli_owner_scope_restores_role_after_service_failure_and_denies_runtime_entry,
        test_rollout.test_progression_requires_reviewed_preceding_stage_and_separate_autonomy,
        test_rollout.test_native_skipped_stage_and_foreign_receipt_inserts_are_refused,
        test_rollout.test_revoked_approval_membership_invalidates_the_stage_and_its_receipt,
        test_rollout.test_kill_is_durable_across_reenable_and_requires_new_stage_evidence,
    ):
        proofs.run(
            f"Evaluation current authority:{evaluation_oracle.__name__}",
            partial(evaluation_oracle, graph()),
        )
    proofs.run(
        "Rollout current owner stop and stale receipt",
        partial(
            test_rollout.test_stage_receipt_cannot_be_foreign_or_stale,
            graph(),
            foreign=False,
        ),
    )
    for native in (False, True):
        with patch.context() as local:
            proofs.run(
                f"Retained invocation cannot borrow a new permission:{native}",
                partial(
                    test_extraction_rollout.test_retained_extraction_cannot_file_after_rollout_invalidation,
                    graph(),
                    local,
                    "advanced_stage",
                    native,
                ),
            )
    with patch.context() as local:
        test_evaluation_native.fixed_review_freshness._get_wrapped_function()(local)
        proofs.run(
            "Evaluation native and HX independent required-answer authority",
            partial(
                test_evaluation_native.test_blinded_native_form_validates_every_answer_and_keeps_one_vote,
                graph(),
                enhanced=True,
            ),
        )
