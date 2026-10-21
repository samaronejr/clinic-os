"""Raw-GUC capture admission refuses inactive staff without a Python guard."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction

from identity.scribe_authority_census import _context_switch_scope
from patient_service_support import runtime_role
from scribe.test_ingest import media, world

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

__all__ = ("media",)
pytestmark = pytest.mark.django_db(transaction=True)


def test_capture_switch_is_owner_scoped_and_never_retargets_recording(
    rbac_graph: RbacGraph,
) -> None:
    _context_switch_scope(rbac_graph)


def test_native_capture_authority_refuses_inactive_actor(
    rbac_graph: RbacGraph,
) -> None:
    graph = rbac_graph
    scope = world(graph)
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT to_jsonb(s)::text FROM clinic_app.scribe_recordingsession s "
            "WHERE s.id=%s",
            [scope.session],
        )
        row = cursor.fetchone()
        assert row is not None
        invalid = json.loads(row[0])
    invalid.update(id=str(uuid4()), state="recording")
    phases = []
    for active in (True, False, True):
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE clinic_app.identity_user SET is_active=%s WHERE id=%s",
                [active, graph.physician],
            )
            assert cursor.rowcount == 1
        with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant',%s,true),"
                "set_config('app.current_user_id',%s,true)",
                [str(graph.organization_a), str(graph.physician)],
            )
            cursor.execute("SELECT clinic_app.scribe_assigned(%s)", [scope.session])
            phases.append(cursor.fetchone())
            with pytest.raises(DatabaseError) as denied, transaction.atomic():
                cursor.execute(
                    "INSERT INTO clinic_app.scribe_recordingsession "
                    "SELECT item.* FROM jsonb_populate_record("
                    "NULL::clinic_app.scribe_recordingsession,%s::jsonb) item",
                    [json.dumps(invalid)],
                )
            assert getattr(denied.value.__cause__, "sqlstate", None) == (
                "23514" if active else "42501"
            )
            assert (
                "invalid recording binding" if active else "recording authority denied"
            ) in str(denied.value)
    assert phases == [(True,), (False,), (True,)]
