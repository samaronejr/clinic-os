"""An exemption is certified only when no authority input is observed."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.core import telemetry
from apps.identity import current_context
from apps.identity.models import User
from django.db import connection, transaction

from identity.authority_catalog import Catalog
from identity.authority_observer import AuthorityObservedError, AuthorityObserver
from identity.nonstaff_differential import DifferentialProbe, _invoke

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _query(statement: str) -> object:
    with connection.cursor() as cursor:
        cursor.execute(statement)
        return cursor.fetchone()


def _permission(clinic: object) -> bool:
    try:
        getattr(current_context, "require_" + "permission")(
            "clinical.read", clinic_id=clinic
        )
    except current_context.CurrentActorError:
        return True
    return False


def _inactive_attribute(actor: User) -> bool:
    return actor.is_active


def _raw_attribute(actor: User, direct: bool) -> object:
    if direct:
        return object.__getattribute__(actor, "is_active")
    return actor.__dict__["is_active"]


@pytest.mark.parametrize("direct", [False, True])
def test_raw_model_access_cannot_hide_active_status(
    rbac_graph: RbacGraph, direct: bool
) -> None:
    actor = User.objects.create(username="synthetic-raw-attribute-" + uuid4().hex)
    catalog = Catalog()
    observer = AuthorityObserver(catalog, catalog.derive())
    probe = DifferentialProbe(
        __name__ + "._raw_attribute", lambda: _raw_attribute(actor, direct)
    )
    assert _invoke(probe, actor, rbac_graph.organization_a, authority=observer)
    with pytest.raises(AuthorityObservedError):
        observer.assert_nonstaff(probe.symbol)


def test_channels_come_from_deployed_authority_gates() -> None:
    catalog = Catalog()
    channels = catalog.derive()
    names = {catalog.relations[oid].name for oid in channels.relations}
    assert {
        "clinic_app.identity_user",
        "clinic_app.identity_userclinicrole",
        "clinic_app.identity_rolegrant",
        "clinic_app.identity_professionalregistration",
        "clinic_app.identity_physicianprofile",
        "clinic_app.identity_careteammembership",
        "clinic_app.ehr_encounter",
    } <= names
    assert channels.settings == {"app.current_user_id", "app.current_tenant"}
    assert "is_active" in next(
        r.columns
        for r in catalog.relations.values()
        if r.name == "clinic_app.identity_user"
    )


@pytest.mark.parametrize("mode", ["relation", "guc", "accessor", "attribute"])
def test_executed_channels_refuse_even_an_always_allowing_guard(
    rbac_graph: RbacGraph, mode: str
) -> None:
    actor = User.objects.create(username="synthetic-observer-" + uuid4().hex)
    catalog = Catalog()
    observer = AuthorityObserver(catalog, catalog.derive())
    if mode == "accessor":
        probe = DifferentialProbe(
            __name__ + "._permission", lambda: _permission(rbac_graph.clinic_a)
        )
    elif mode == "attribute":
        probe = DifferentialProbe(
            __name__ + "._inactive_attribute", lambda: _inactive_attribute(actor)
        )
    else:
        query = (
            "SELECT count(*) FROM clinic_app.identity_userclinicrole"
            if mode == "relation"
            else "SELECT current_setting('app.current_user_id', true)"
        )
        probe = DifferentialProbe(__name__ + "._query", lambda: _query(query))
    assert _invoke(probe, actor, rbac_graph.organization_a, authority=observer)
    with pytest.raises(AuthorityObservedError):
        observer.assert_nonstaff(probe.symbol)
    assert any(kind != "opaque" for kind, _name in observer.touches)


def test_pure_operator_authority_has_no_staff_channel(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(telemetry.OPS_METRICS_NETWORKS_ENV, "192.0.2.0/24")
    actor = User.objects.create(username="synthetic-observer-" + uuid4().hex)
    catalog = Catalog()
    observer = AuthorityObserver(catalog, catalog.derive())
    probe = DifferentialProbe(
        "apps.core.telemetry._allowed_networks", telemetry._allowed_networks, bool
    )
    assert _invoke(probe, actor, rbac_graph.organization_a, authority=observer)
    observer.assert_nonstaff(probe.symbol)
    assert observer.touches == set()


def test_gate_growth_through_sql_body_and_view_expands_channels() -> None:
    before = Catalog()
    assert "clinic_app.t6_new_authority" not in {
        before.relations[oid].name for oid in before.derive().relations
    }
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("CREATE TABLE clinic_app.t6_new_authority (allowed boolean)")
        cursor.execute(
            "CREATE VIEW clinic_app.t6_authority_view AS "
            "SELECT allowed FROM clinic_app.t6_new_authority"
        )
        cursor.execute(
            "GRANT SELECT ON clinic_app.t6_authority_view TO clinic_resolver"
        )
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute("""
            CREATE FUNCTION clinic_app.t6_new_authority_reader()
            RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
            BEGIN ATOMIC
                SELECT EXISTS (SELECT 1 FROM clinic_app.t6_authority_view);
            END;
        """)
        cursor.execute("""
            CREATE OR REPLACE FUNCTION clinic_app.user_has_org(requested_org uuid)
            RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
            SET search_path=pg_catalog,clinic_app,pg_temp AS $body$
                SELECT clinic_app.t6_new_authority_reader()
            $body$
        """)
        cursor.execute("RESET ROLE")
        after = Catalog()
        channels = after.derive()
        assert "clinic_app.t6_new_authority" in {
            after.relations[oid].name for oid in channels.relations
        }
        assert "clinic_app.t6_new_authority_reader" in {
            after.functions[oid].name for oid in channels.functions
        }
        transaction.set_rollback(True)
