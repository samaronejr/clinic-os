"""Independent RP action oracle, exercised through the real service/SQL boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from apps.identity.current_context import CurrentActorError, require_permission
from apps.identity.permissions import BUNDLES_V1, PERMISSIONS

from identity.permission_support import permission_actor, permission_context

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

# RP columns are expanded into actions: a partial capability must not become full.
# Patient/delegate, support and service-principal cells are separate authorities,
# not staff roles; their conditional grants ship in todos 7, 19 and 65.
RP = {
    "agenda": {
        "physician": "appointment.read_own appointment.book_own appointment.move_own",
        "nurse": "appointment.read",
        "receptionist": "appointment.read appointment.book appointment.move",
        "clinic_manager": "appointment.read appointment.book appointment.move",
        "finance": "appointment.read",
        "org_admin": "appointment.read",
    },
    "demographics": {
        "physician": "demographics.read demographics.write",
        "nurse": "demographics.read",
        "receptionist": "demographics.read demographics.write",
        "clinic_manager": "demographics.read",
        "finance": "demographics.billing_read",
    },
    "narrative": {
        "physician": "clinical.read clinical.write clinical.finalize clinical.amend",
        "nurse": "observation.write",
    },
    "restricted": {},
    "orders": {
        "physician": "order.place result.read result.acknowledge result.release",
        "nurse": "order.observe order.task",
        "receptionist": "order.route",
    },
    "prescribe": {"physician": "prescription.prepare prescription.sign"},
    "charges": {
        "physician": "charge.read",
        "receptionist": "charge.read charge.create charge.collect",
        "clinic_manager": "charge.read",
        "finance": "charge.read charge.create charge.collect settlement.post",
        "org_admin": "charge.read",
    },
    "refunds": {
        "clinic_manager": "refund.request writeoff.request payout.request",
        "finance": "refund.approve writeoff.approve payout.approve",
        "org_admin": "finance.policy",
    },
    "tiss": {
        "physician": "tiss.clinical_read",
        "clinic_manager": "tiss.read",
        "finance": "tiss.read tiss.manage",
        "org_admin": "tiss.read",
    },
    "configuration": {
        "physician": "configuration.propose",
        "clinic_manager": "configuration.clinic",
        "finance": "configuration.clinic",
        "org_admin": "configuration.organization",
    },
    "staff": {
        "clinic_manager": "staff.clinic",
        "org_admin": "staff.organization",
    },
    "automation": {
        "physician": "automation.propose_clinical",
        "clinic_manager": "automation.admin",
        "finance": "automation.finance",
        "org_admin": "automation.organization",
    },
    "break_glass": {
        "physician": "break_glass.request",
        "nurse": "break_glass.request_scoped",
    },
}
COLUMNS = (
    "physician",
    "nurse",
    "receptionist",
    "clinic_manager",
    "finance",
    "org_admin",
    "patient_delegate",
    "support",
    "service_principal",
)


def test_registry_is_exact_and_bundles_cannot_be_mutated() -> None:
    expected = frozenset(
        permission
        for row in RP.values()
        for actions in row.values()
        for permission in actions.split()
    ) | {"restricted.read"}
    assert expected == PERMISSIONS
    assert len(RP) * len(COLUMNS) == 117
    for role in COLUMNS[:6]:
        assert BUNDLES_V1[role] == frozenset(
            permission
            for row in RP.values()
            for permission in row.get(role, "").split()
        )
    assert BUNDLES_V1["allied_professional"] == BUNDLES_V1["nurse"]
    assert BUNDLES_V1["scheduler"] == BUNDLES_V1["receptionist"]
    assert BUNDLES_V1["clinic_admin"] == BUNDLES_V1["clinic_manager"]
    assert BUNDLES_V1["owner"] == BUNDLES_V1["org_admin"]
    with pytest.raises(TypeError):
        cast("dict[str, frozenset[str]]", BUNDLES_V1)["finance"] = frozenset(
            {"clinical.read"}
        )


@pytest.mark.parametrize("column", COLUMNS)
@pytest.mark.parametrize("capability", RP)
def test_every_rp_cell_at_service_boundary(
    rbac_graph: RbacGraph,
    capability: str,
    column: str,
) -> None:
    actor, enrollment = permission_actor(rbac_graph, column)
    expected = set(RP[capability].get(column, "").split())
    actions = {
        action for cell in RP[capability].values() for action in cell.split()
    } or {"restricted.read"}
    with permission_context(rbac_graph, actor):
        for permission in sorted(actions):
            if permission in expected:
                assert (
                    require_permission(
                        permission,
                        clinic_id=rbac_graph.clinic_a,
                        patient_enrollment_id=enrollment,
                    )
                    == actor
                )
            else:
                with pytest.raises(CurrentActorError):
                    require_permission(
                        permission,
                        clinic_id=rbac_graph.clinic_a,
                        patient_enrollment_id=enrollment,
                    )
