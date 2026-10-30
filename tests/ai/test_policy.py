"""Default-off and non-synthetic refusal paths without external evidence claims."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ai.artifacts import create_input
from apps.ai.contracts import GatewayRefusedError, Purpose
from apps.ai.models import AIArtifact
from apps.tenancy.db import tenant_context
from django.test import override_settings

from ai.support import SENTINEL, seed
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_default_kill_switch_refuses_before_content_is_persisted(
    rbac_graph: RbacGraph,
) -> None:
    # Given: a migrated app with no owner enablement or organization opt-in.
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        # When/Then: capture refuses without retaining content.
        with pytest.raises(GatewayRefusedError) as refused:
            create_input(
                clinic_id=rbac_graph.clinic_a,
                purpose=Purpose.FRONT_DESK,
                content=SENTINEL,
            )
        assert refused.value.reason == "disabled"
        assert not AIArtifact.objects.exists()


@pytest.mark.parametrize("mode", ["live", "invalid"])
def test_non_synthetic_data_mode_never_runs_a_synthetic_provider(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    # Given: synthetic opt-ins, deliberately invalid non-synthetic runtime settings.
    seed(rbac_graph, monkeypatch)
    with (
        override_settings(CLINIC_DATA_MODE=mode),
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        # When/Then: this is refusal evidence, never a valid live/EG receipt.
        with pytest.raises(GatewayRefusedError) as refused:
            create_input(
                clinic_id=rbac_graph.clinic_a,
                purpose=Purpose.FRONT_DESK,
                content=SENTINEL,
            )
        assert refused.value.reason == "disabled"
