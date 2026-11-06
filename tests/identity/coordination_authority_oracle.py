"""Execute the todo 55 urgent-queue and result-release witnesses.

The workflow guard cases already execute the staff pages and the permission
services. These acceptance oracles add the boundaries no guard case reaches:
the patient bridge (``report_urgent_message``), the patient coverage text, the
responsible-role decision with its revision check and the send-time release
eligibility recheck, each on the real runtime roles.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from workflows import test_urgent_queue
from workflows.test_urgent_queue import (
    test_urgent_reply_opens_one_concern_and_escalates_when_overdue as escalation,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from identity.cut1_authority_census import ExecutedProofs
    from rbac_fixtures import RbacGraph


def run_coordination_census(
    proofs: ExecutedProofs,
    graph: Callable[[], RbacGraph],
    patch: pytest.MonkeyPatch,
    root: Path,
) -> None:
    """Require the queue, coverage and release boundaries to run their oracles."""
    for oracle in (
        test_urgent_queue.test_only_responsible_roles_acknowledge_and_resolve,
        test_urgent_queue.test_portal_states_only_the_configured_coverage,
    ):
        proofs.run(oracle.__name__, partial(oracle, graph()))
    for release_oracle in (
        test_urgent_queue.test_release_queues_the_approved_notice_without_result_content,
        test_urgent_queue.test_guardian_access_change_re_evaluates_release_scope,
    ):
        proofs.run(release_oracle.__name__, partial(release_oracle, graph(), root))
    # The escalation and failure oracles pin the clock or the adapter; each gets
    # its own patch context so nothing leaks into the next proof.
    with patch.context() as local:
        proofs.run(escalation.__name__, partial(escalation, graph(), local))
    failure = test_urgent_queue.test_failed_notice_delivery_opens_one_follow_up_task
    with patch.context() as local:
        proofs.run(failure.__name__, partial(failure, graph(), root, local))
