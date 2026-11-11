"""The emergency projection returns real finalized content and then revokes it."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from apps.identity.enterprise_access import (
    EmergencyScope,
    GovernanceDeniedError,
    grant_break_glass,
    read_break_glass,
    revoke_break_glass,
)
from django.test import override_settings
from django.utils import timezone

from ehr.longitudinal_support import chart_world, finalized
from identity.enterprise_support import enterprise_setup, verified_actor_client
from identity.lifecycle_test_support import session_key
from identity.permission_support import permission_actor, permission_context

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
def test_emergency_reader_returns_a_finalized_version_and_stops_on_revoke(
    rbac_graph: RbacGraph,
) -> None:
    enterprise_setup(rbac_graph)
    world = chart_world(rbac_graph)
    version = finalized(rbac_graph, world.version)
    reader, _own = permission_actor(rbac_graph, "physician")
    client = verified_actor_client(reader)
    with permission_context(rbac_graph, reader):
        grant = grant_break_glass(
            clinic_id=rbac_graph.clinic_a,
            scope=EmergencyScope(world.enrollment),
            reason="Synthetic emergency read",
            expires_at=timezone.now() + timedelta(hours=1),
            session_key=session_key(client),
        )
        snapshot = read_break_glass(
            clinic_id=rbac_graph.clinic_a,
            grant_id=grant.pk,
        )
        assert {note.pk for note in snapshot.notes} == {version.pk}
        assert snapshot.notes[0].content == version.content
        revoke_break_glass(
            clinic_id=rbac_graph.clinic_a,
            grant_id=grant.pk,
            session_key=session_key(client),
        )
        with pytest.raises(GovernanceDeniedError):
            read_break_glass(
                clinic_id=rbac_graph.clinic_a,
                grant_id=grant.pk,
            )
