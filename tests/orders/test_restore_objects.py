"""Logical recovery must also retain encrypted result source objects."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.core.secrets import secret_store
from apps.orders.services import ingest_result
from django.db import connection
from ops.testing.restore_contract import RestoreContractError
from ops.testing.restore_verification import verify_object_store

from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from orders.support import ordered, submission

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


class _Database:
    def sql(self, statement: str) -> str:
        with connection.cursor() as cursor:
            cursor.execute(statement)
            if statement.startswith("SET "):
                assert cursor.nextset()
            return "\n".join(str(row[0]) for row in cursor.fetchall())


def test_result_objects_are_verified_and_missing_source_breaks_restore(
    rbac_graph: RbacGraph, settings: SettingsWrapper, tmp_path: Path
) -> None:
    root = tmp_path / "objects"
    root.mkdir(mode=0o700)
    settings.EHR_ATTACHMENT_ROOT = root
    world = ordered(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.physician):
        ingest_result(clinic_id=rbac_graph.clinic_a, submission=submission(world))
    kek = secret_store().get_secret("tenant-kek")
    with owner_context(rbac_graph.organization_a):
        assert verify_object_store(_Database(), root, kek) == 1
    objects = [
        path for path in (root / "orders").iterdir() if path.suffix != ".pending"
    ]
    assert len(objects) == 1
    objects[0].unlink()
    with (
        owner_context(rbac_graph.organization_a),
        pytest.raises(
            RestoreContractError, match="does not equal the attachment manifest"
        ),
    ):
        verify_object_store(_Database(), root, kek)
