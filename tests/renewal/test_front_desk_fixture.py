"""The front-desk browser fixture provisions the authority its policy needs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from apps.ai.evaluation_models import ModelChangeRequest
from apps.ai.models import ModelConfiguration
from apps.ai.owner import set_global_switch, transition_model
from apps.identity.models import Clinic, User, UserClinicRole
from apps.providers.lifecycle import ApprovalInput, revoke_version
from django.conf import settings as django_settings
from django.db import connection

from ai.test_front_desk import desk_world
from ai.test_front_desk_portal import portal
from ai.test_front_desk_portal import say as portal_say
from identity.permission_support import owner_context
from renewal.front_desk_fixture import seed, seed_environment

if TYPE_CHECKING:
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _browser_administrator(graph: RbacGraph) -> User:
    """Administer clinics A and B, as seed_ai_admin does for the browser."""
    actor = User.objects.create(username=f"synthetic-desk-admin-{uuid4().hex}")
    with owner_context(graph.organization_a):
        for clinic in (graph.clinic_a, graph.clinic_b):
            for role in ("clinic_admin", "org_admin"):
                UserClinicRole.objects.create(
                    organization_id=graph.organization_a,
                    clinic_id=clinic,
                    user=actor,
                    role=role,
                )
    return actor


def test_seed_publishes_the_policy_after_another_scenario_added_a_clinic(
    rbac_graph: RbacGraph,
) -> None:
    actor = _browser_administrator(rbac_graph)
    with owner_context(rbac_graph.organization_a):
        # An earlier scenario adds a unit the administrator holds no role in.
        added = Clinic.objects.create(
            organization_id=rbac_graph.organization_a,
            name="Synthetic added unit",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
    receipt = seed(actor.pk, rbac_graph.clinic_a, rbac_graph.organization_a)
    assert receipt["synthetic"] is True
    with owner_context(rbac_graph.organization_a):
        assert UserClinicRole.objects.filter(
            user=actor, clinic=added, role="org_admin"
        ).exists()


def _run_seed(
    actor: UUID, graph: RbacGraph, *, drill_cleared: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run the module as test_messages._enable_portal_assistant does."""
    owner = urlsplit(os.environ["MIGRATION_DATABASE_URL"])
    database = str(connection.settings_dict["NAME"])
    environment = seed_environment(urlunsplit(owner._replace(path=f"/{database}")))
    # The runner hands the server and the fixture one secret backend.
    environment["CLINIC_SECRET_BACKEND"] = str(django_settings.CLINIC_SECRET_BACKEND)
    environment["CLINIC_SECRET_DIR"] = str(django_settings.CLINIC_SECRET_DIR)
    if not drill_cleared:
        environment["CLINIC_SYNTHETIC_FAULTS"] = os.environ["CLINIC_SYNTHETIC_FAULTS"]
    return subprocess.run(  # noqa: S603 - fixed module and UUID-only arguments.
        [
            sys.executable,
            "-m",
            "renewal.front_desk_fixture",
            str(actor),
            str(graph.clinic_a),
            str(graph.organization_a),
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _browser_seed(actor: UUID, graph: RbacGraph) -> None:
    result = _run_seed(actor, graph)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["synthetic"] is True


def _setup_rows() -> tuple[int, ...]:
    """Every row family the seed writes."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT (SELECT count(*) FROM clinic_app.providers_capabilityversion),"
            "(SELECT count(*) FROM clinic_app.ai_modelconfiguration),"
            "(SELECT count(*) FROM clinic_app.ai_agentpolicyversion),"
            "(SELECT count(*) FROM clinic_app.identity_serviceprincipal),"
            "(SELECT count(*) FROM clinic_app.identity_userclinicrole)"
        )
        row = cursor.fetchone()
    assert row is not None
    return tuple(int(value) for value in row)


def test_browser_seed_entry_point_makes_the_portal_assistant_answer(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A linked portal patient with the agent policy unpublished. As in the
    # browser database there is no active front-desk model and no usable
    # llm_inference provider version.
    desk = desk_world(rbac_graph, settings, monkeypatch, enabled=False)
    for active in ModelConfiguration.objects.filter(
        purpose="front_desk", state="active"
    ):
        transition_model(configuration_id=active.pk, state="retired")
    revoke_version(
        "llm_inference",
        decision=ApprovalInput(
            "Synthetic fixture",
            "Synthetic owner",
            "https://synthetic.invalid/browser-database",
        ),
        reason="Synthetic browser database has no provider version",
    )
    actor = _browser_administrator(rbac_graph)
    # The resilience runner exports its drill to the whole browser process.
    monkeypatch.setenv("CLINIC_SYNTHETIC_FAULTS", "ai=down,messaging=down")
    with owner_context(rbac_graph.organization_a):
        before = _setup_rows()
    # Run under the drill, the seed refuses before writing anything.
    refused = _run_seed(actor.pk, rbac_graph, drill_cleared=False)
    assert refused.returncode != 0
    with owner_context(rbac_graph.organization_a):
        assert _setup_rows() == before
    assert "AI drill cleared" in refused.stderr
    # The browser helper clears the drill for this setup step only.
    _browser_seed(actor.pk, rbac_graph)
    assert os.environ["CLINIC_SYNTHETIC_FAULTS"] == "ai=down,messaging=down"
    session, thread = portal(desk)
    replies = portal_say(desk, session, thread, "Qual o horário de funcionamento?")
    # test_messages asserts the same text with to_contain_text.
    assert replies, "the portal assistant published no reply"
    assert "Atendemos de segunda a sexta, das 8h às 18h." in replies[-1]
    # The reply was admitted by a recorded promotion of the active model.
    rollout = ModelConfiguration.objects.get(purpose="front_desk", state="active")
    with owner_context(rbac_graph.organization_a):
        stages = set(
            ModelChangeRequest.objects.filter(
                configuration=rollout, approved_by__isnull=False
            ).values_list("stage", flat=True)
        )
    assert stages == {"shadow", "reviewed_live", "monitored_autonomy"}
    # AI off: the same admitted desk stays with people.
    set_global_switch(enabled=False, approved_regions=("br-synthetic",))
    after = portal_say(desk, session, thread, "Qual o horário de funcionamento?")
    assert after == replies
