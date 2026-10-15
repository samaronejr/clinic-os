"""Execute AI authority witnesses before issuing merged-census certificates."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Protocol

from ai import test_controls, test_gateway, test_posture

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from rbac_fixtures import RbacGraph


class _ProofRecorder(Protocol):
    def run(self, label: str, oracle: Callable[[], None]) -> None: ...


def exercise_ai_authority(
    proofs: _ProofRecorder, graph: Callable[[], RbacGraph], patch: pytest.MonkeyPatch
) -> None:
    """Bind source candidates to executed scope, role, owner and transport checks."""
    for oracle in (
        test_gateway.test_fake_call_is_metered_once_and_all_content_stays_encrypted,
        test_gateway.test_unknown_foreign_and_other_user_inputs_have_identical_denial,
        test_controls.test_owner_cli_changes_only_synthetic_model_and_global_controls,
        test_controls.test_native_controls_execute_valid_invalid_stale_and_forbidden_requests,
        test_controls.test_ai_policy_requires_organization_permission_in_every_clinic,
        test_controls.test_unknown_and_foreign_clinic_control_reads_refuse_equally,
        test_posture.test_closed_circuit_can_reopen_without_losing_history,
    ):
        with patch.context() as local:
            proofs.run(
                f"AI executed authority:{oracle.__name__}",
                partial(oracle, graph(), local),
            )
