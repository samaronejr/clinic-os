"""Observation borrows the caller backend and leaves no new backend behind."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.core import telemetry
from apps.identity.models import User
from django.db import connection, connections

from identity.authority_catalog import Catalog
from identity.authority_observer import AuthorityObserver
from identity.nonstaff_differential import DifferentialProbe, _invoke

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("invocation_fails", [False, True])
def test_observation_leaves_no_owned_backend(
    monkeypatch: pytest.MonkeyPatch, rbac_graph: RbacGraph, invocation_fails: bool
) -> None:
    application = "t6_cleanup_" + uuid4().hex
    actor = User.objects.create(username="synthetic-backend-" + uuid4().hex)
    monkeypatch.setenv("PGAPPNAME", application)
    monkeypatch.setenv(telemetry.OPS_METRICS_NETWORKS_ENV, "192.0.2.0/24")
    connections.close_all()
    monkeypatch.setitem(
        connection.settings_dict,
        "OPTIONS",
        {**connection.settings_dict["OPTIONS"], "application_name": application},
    )
    catalog = Catalog()
    observer = AuthorityObserver(catalog, catalog.derive())

    def invoke() -> object:
        value = telemetry._allowed_networks()
        if invocation_fails:
            message = "synthetic-observer-failure"
            raise RuntimeError(message)
        return value

    probe = DifferentialProbe("apps.core.telemetry._allowed_networks", invoke, bool)
    previous_profile = sys.getprofile()
    previous_tools = tuple(sys.monitoring.get_tool(i) for i in range(6))
    with psycopg.connect(
        os.environ["TEST_SUPERUSER_DATABASE_URL"],
        dbname="postgres",
        autocommit=True,
        application_name="t6_cleanup_observer",
    ) as monitor:
        query = (
            "SELECT pid, datname, application_name FROM pg_stat_activity "
            "WHERE application_name=%s ORDER BY pid"
        )
        before = monitor.execute(query, [application]).fetchall()
        assert len(before) == 1
        try:
            if invocation_fails:
                with pytest.raises(RuntimeError, match="synthetic-observer-failure"):
                    _invoke(probe, actor, rbac_graph.organization_a, authority=observer)
            else:
                assert _invoke(
                    probe, actor, rbac_graph.organization_a, authority=observer
                )
                observer.assert_nonstaff(probe.symbol)
            # No sleeps/polling: the existing caller connection must be the only
            # backend both before and after success or exception propagation.
            assert monitor.execute(query, [application]).fetchall() == before
            assert sys.getprofile() is previous_profile
            assert tuple(sys.monitoring.get_tool(i) for i in range(6)) == previous_tools
        finally:
            connections.close_all()
