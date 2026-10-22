"""Opt the workflow test population into its unissued synthetic candidate."""

from __future__ import annotations

from secrets import token_hex
from typing import TYPE_CHECKING

import pytest
from apps.identity.models import UserClinicRole

from identity.permission_support import owner_context

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph


@pytest.fixture
def rbac_graph(rbac_graph: RbacGraph) -> RbacGraph:
    for organization in (rbac_graph.organization_a, rbac_graph.organization_b):
        with owner_context(organization):
            UserClinicRole.objects.filter(
                organization_id=organization,
            ).update(workflow_bundle_version=2)
    return rbac_graph


@pytest.fixture
def synthetic_workflow_transport(
    synthetic_secret_backend: Path, settings: SettingsWrapper
) -> Iterator[Path]:
    """Explicitly provision one managed fixture secret; defaults remain off."""
    secret = synthetic_secret_backend / "workflow-synthetic-callback.secret"
    assert not secret.exists()
    secret.write_text(token_hex(32), encoding="ascii")
    secret.chmod(0o600)
    settings.WORKFLOWS_SYNTHETIC_ENABLED = True
    try:
        yield secret
    finally:
        secret.unlink()
