"""Material use binds the retained procedure entry, not a client patient claim."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.attachments import scan_attachment
from apps.ehr.packs import install_pack
from apps.ehr.specialty_records import ProcedureInput, record_procedure
from apps.inventory.models import StockMovement
from apps.inventory.services import InventoryAccessDeniedError, use

from ehr.longitudinal_support import chart_world
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from inventory.support import stocked
from renewal.test_attachments import attachment_root, upload

if TYPE_CHECKING:
    from pathlib import Path

    from rbac_fixtures import RbacGraph

__all__ = ("attachment_root",)
pytestmark = pytest.mark.django_db(transaction=True)


def test_procedure_use_retains_exact_specialty_entry(
    rbac_graph: RbacGraph, attachment_root: Path
) -> None:
    chart = chart_world(rbac_graph)
    encounter = chart.version.document.encounter
    stock = stocked(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        install_pack(clinic_id=rbac_graph.clinic_a, pack_key="procedures", version=1)
    with as_actor(rbac_graph, rbac_graph.physician):
        consent = upload(rbac_graph, encounter)
        scan_attachment(clinic_id=rbac_graph.clinic_a, attachment_id=consent.pk)
        procedure = record_procedure(
            clinic_id=rbac_graph.clinic_a,
            encounter_id=encounter.pk,
            record=ProcedureInput("Synthetic procedure", consent.pk),
        )
    with as_actor(rbac_graph, stock.nurse):
        movement = use(
            clinic_id=rbac_graph.clinic_a,
            batch_id=stock.batch,
            location_id=stock.location,
            quantity=1,
            procedure_entry_id=procedure.pk,
            command_id=uuid4(),
        )
        assert movement.procedure_entry_id == procedure.pk
    with owner_context(rbac_graph.organization_a):
        assert StockMovement.objects.get(pk=movement.pk).procedure_entry_id == (
            procedure.pk
        )


@pytest.mark.parametrize("foreign_scope", [False, True])
def test_unresolvable_procedure_does_not_consume_stock(
    rbac_graph: RbacGraph, foreign_scope: bool
) -> None:
    stock = stocked(rbac_graph)
    with (
        as_actor(rbac_graph, stock.nurse),
        pytest.raises(InventoryAccessDeniedError),
    ):
        use(
            clinic_id=rbac_graph.clinic_c if foreign_scope else rbac_graph.clinic_a,
            batch_id=stock.batch,
            location_id=stock.location,
            quantity=1,
            procedure_entry_id=uuid4(),
            command_id=uuid4(),
        )
    with owner_context(rbac_graph.organization_a):
        assert StockMovement.objects.count() == 1
