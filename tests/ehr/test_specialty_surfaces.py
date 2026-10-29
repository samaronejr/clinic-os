"""Native specialty surfaces enforce roles, input refusal and source-bound forms."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.pack_models import SpecialtyEntry, SpecialtyPack
from apps.ehr.packs import install_pack
from django.test import Client
from django.urls import reverse
from django.utils.translation import gettext

from ehr.longitudinal_support import chart_world
from ehr.test_addenda import signed_in
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context
from renewal.test_encounters import seed

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_admin_installs_pack_and_missing_license_is_visible(
    rbac_graph: RbacGraph,
) -> None:
    seed(rbac_graph)
    url = reverse("ehr:specialty-packs", kwargs={"clinic_id": rbac_graph.clinic_a})
    with signed_in(rbac_graph.clinic_admin) as client:
        response = client.post(
            url, {"action": "install", "pack_key": "pediatrics", "version": "1"}
        )
        assert response.status_code == 200
        assert "no-store" in response["Cache-Control"]
        assert gettext("Unavailable: license pending") in response.content.decode()
        again = client.post(
            url, {"action": "install", "pack_key": "pediatrics", "version": "1"}
        )
        assert again.status_code == 200
    with owner_context(rbac_graph.organization_a):
        assert SpecialtyPack.objects.filter(clinic_id=rbac_graph.clinic_a).count() == 1


@pytest.mark.parametrize("value", ["1,234.5", "1.234,5", "70,5", "NaN"])
def test_decimal_confusion_returns_validation_not_a_silent_result(
    rbac_graph: RbacGraph, value: str
) -> None:
    seed(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        install_pack(clinic_id=rbac_graph.clinic_a, pack_key="general", version=1)
    url = reverse("ehr:specialty-packs", kwargs={"clinic_id": rbac_graph.clinic_a})
    with signed_in(rbac_graph.physician) as client:
        response = client.post(
            url,
            {
                "action": "calculate",
                "pack_key": "general",
                "version": "1",
                "calculator_key": "bmi",
                "bmi-weight": value,
                "bmi-weight_unit": "kg",
                "bmi-height": "175",
                "bmi-height_unit": "cm",
            },
        )
        assert response.status_code == 400
        assert b"data-calculation-result" not in response.content
        assert (
            gettext("Enter valid values with a decimal point and explicit units.")
            in response.content.decode()
        )


def test_valid_calculation_and_uninstall_preserve_clinical_workspace(
    rbac_graph: RbacGraph,
) -> None:
    world = chart_world(rbac_graph)
    with as_actor(rbac_graph, rbac_graph.clinic_admin):
        install_pack(clinic_id=rbac_graph.clinic_a, pack_key="general", version=1)
        install_pack(clinic_id=rbac_graph.clinic_a, pack_key="pediatrics", version=1)
    url = reverse("ehr:specialty-packs", kwargs={"clinic_id": rbac_graph.clinic_a})
    with signed_in(rbac_graph.physician) as client:
        response = client.post(
            url,
            {
                "action": "calculate",
                "pack_key": "general",
                "version": "1",
                "calculator_key": "bmi",
                "bmi-weight": "70",
                "bmi-weight_unit": "kg",
                "bmi-height": "175",
                "bmi-height_unit": "cm",
            },
        )
        assert response.status_code == 200
        assert b"data-calculation-result" in response.content
        assert b"22.86 kg/m2" in response.content
        workspace = reverse(
            "ehr:patient-workspace", kwargs={"clinic_id": rbac_graph.clinic_a}
        )
        saved = client.post(
            workspace,
            {
                "action": "immunization",
                "tab": "specialty",
                "enrollment_id": world.enrollment,
                "immunization-encounter_id": world.version.document.encounter_id,
                "immunization-vaccine": "SINTETICO-SENTINELA-30",
                "immunization-administered_on": "02/01/2020",
                "immunization-dose": "Synthetic dose",
                "immunization-provenance": "reported_by_patient",
            },
        )
        assert saved.status_code == 200
        assert b"SINTETICO-SENTINELA-30" in saved.content
    with owner_context(rbac_graph.organization_a):
        assert SpecialtyEntry.objects.filter(clinic_id=rbac_graph.clinic_a).count() == 1


def test_role_foreign_selector_and_csrf_refusals_leave_no_installation(
    rbac_graph: RbacGraph,
) -> None:
    seed(rbac_graph)
    url = reverse("ehr:specialty-packs", kwargs={"clinic_id": rbac_graph.clinic_a})
    payload = {"action": "install", "pack_key": "pediatrics", "version": "1"}
    with signed_in(rbac_graph.physician) as client:
        assert client.post(url, payload).status_code == 403
    with signed_in(rbac_graph.clinic_admin) as client:
        csrf = Client(enforce_csrf_checks=True)
        csrf.cookies = client.cookies
        assert csrf.post(url, payload).status_code == 403
        for clinic in (rbac_graph.clinic_c, uuid4()):
            other = reverse("ehr:specialty-packs", kwargs={"clinic_id": clinic})
            assert client.post(other, payload).status_code == 403
    with owner_context(rbac_graph.organization_a):
        assert SpecialtyPack.objects.filter(clinic_id=rbac_graph.clinic_a).count() == 0


@pytest.mark.parametrize("htmx", [False, True])
@pytest.mark.parametrize(
    ("installed", "payload"),
    [
        pytest.param(
            False,
            {"action": "calculate", "calculator_key": "bmi"},
            id="uninstalled-calculator",
        ),
        pytest.param(
            True,
            {"action": "calculate", "calculator_key": "bmi", "version": "2"},
            id="stale-calculator-version",
        ),
        pytest.param(
            True,
            {"action": "calculate", "calculator_key": "unpublished"},
            id="unbound-calculator",
        ),
        pytest.param(False, {"action": "move_up"}, id="uninstalled-reorder"),
        pytest.param(True, {"action": "unknown"}, id="unknown-command"),
        pytest.param(
            True,
            {"action": "unknown", "version": "2"},
            id="unknown-command-stale-version",
        ),
        pytest.param(
            True,
            {"action": "install", "pack_key": "unpublished"},
            id="unknown-pack",
        ),
    ],
)
def test_refused_pack_commands_leave_installations_unchanged(
    rbac_graph: RbacGraph, installed: bool, payload: dict[str, str], htmx: bool
) -> None:
    seed(rbac_graph)
    if installed:
        with as_actor(rbac_graph, rbac_graph.clinic_admin):
            install_pack(clinic_id=rbac_graph.clinic_a, pack_key="general", version=1)
    with owner_context(rbac_graph.organization_a):
        before = list(
            SpecialtyPack.objects.filter(clinic_id=rbac_graph.clinic_a).values_list(
                "pk", "position", "uninstalled_at"
            )
        )
    url = reverse("ehr:specialty-packs", kwargs={"clinic_id": rbac_graph.clinic_a})
    with signed_in(rbac_graph.clinic_admin) as client:
        response = client.post(
            url,
            {"pack_key": "general", "version": "1", **payload},
            headers={"HX-Request": "true"} if htmx else {},
        )
        assert response.status_code == 403
        assert "no-store" in response["Cache-Control"]
        assert b"data-calculation-result" not in response.content
    with owner_context(rbac_graph.organization_a):
        assert (
            list(
                SpecialtyPack.objects.filter(clinic_id=rbac_graph.clinic_a).values_list(
                    "pk", "position", "uninstalled_at"
                )
            )
            == before
        )


def test_native_reordering_retirement_and_stale_commands(rbac_graph: RbacGraph) -> None:
    seed(rbac_graph)
    url = reverse("ehr:specialty-packs", kwargs={"clinic_id": rbac_graph.clinic_a})
    with signed_in(rbac_graph.clinic_admin) as client:
        for key in ("general", "pediatrics"):
            assert (
                client.post(
                    url, {"action": "install", "pack_key": key, "version": "1"}
                ).status_code
                == 200
            )
        moved = client.post(
            url, {"action": "move_up", "pack_key": "pediatrics", "version": "1"}
        )
        assert moved.status_code == 200
        assert [card.definition.key for card in moved.context["cards"]][:2] == [
            "pediatrics",
            "general",
        ]
        assert (
            client.post(
                url, {"action": "move_up", "pack_key": "pediatrics", "version": "1"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                url, {"action": "move_down", "pack_key": "pediatrics", "version": "1"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                url, {"action": "uninstall", "pack_key": "pediatrics", "version": "1"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                url, {"action": "install", "pack_key": "pediatrics", "version": "1"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                url, {"action": "install", "pack_key": "general", "version": "2"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                url, {"action": "unknown", "pack_key": "general", "version": "1"}
            ).status_code
            == 403
        )
