"""Upload rollback and post-commit recovery never erase a committed source."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.orders.models import ResultVersion
from apps.orders.services import ingest_result
from apps.orders.storage import (
    reconcile_result_uploads,
    result_storage,
    source_bytes,
)
from django.core.exceptions import ValidationError
from django.db import transaction

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from orders.support import SOURCE, ordered, submission
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _source_root(settings: SettingsWrapper, tmp_path: Path) -> None:
    settings.EHR_ATTACHMENT_ROOT = tmp_path / "objects"


def test_rolled_back_source_is_tracked_until_owner_reconciliation(
    rbac_graph: RbacGraph,
) -> None:
    world = ordered(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.physician), transaction.atomic():
        ingest_result(clinic_id=rbac_graph.clinic_a, submission=submission(world))
        transaction.set_rollback(True)
    backend = result_storage()
    pending = backend.pending_uploads()
    assert len(pending) == 1
    assert backend.get(pending[0].key)
    with runtime_role(), pytest.raises(PermissionError):
        reconcile_result_uploads()
    with owner_context(rbac_graph.organization_a):
        assert not ResultVersion.objects.exists()
        assert reconcile_result_uploads() == 1
    assert not backend.pending_uploads()


def test_pending_commit_receipt_retains_the_original_source(
    rbac_graph: RbacGraph,
) -> None:
    world = ordered(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.physician):
        receipt = ingest_result(
            clinic_id=rbac_graph.clinic_a, submission=submission(world)
        )
        version = ResultVersion.objects.get(pk=receipt.version_id)
    backend = result_storage()
    backend.mark_pending(version.storage_key, str(rbac_graph.organization_a))
    with owner_context(rbac_graph.organization_a):
        assert reconcile_result_uploads() == 0
        assert source_bytes(version) == SOURCE
    assert not backend.pending_uploads()


def test_source_descriptor_digest_mismatch_is_not_downloaded(
    rbac_graph: RbacGraph,
) -> None:
    world = ordered(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.physician):
        receipt = ingest_result(
            clinic_id=rbac_graph.clinic_a, submission=submission(world)
        )
        version = ResultVersion.objects.get(pk=receipt.version_id)
        version.sha256 = "0" * 64
        with pytest.raises(ValidationError):
            source_bytes(version)
