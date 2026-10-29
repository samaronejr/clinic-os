"""Cross-Origin-Opener-Policy: the app sends exactly ``same-origin``.

The browser harness turns COOP processing off in Firefox only, to avoid a
Juggler event loss in the pinned Playwright (tests/renewal/browser/engines.py,
Cross-origin opener policy; https://github.com/microsoft/playwright/issues/42731).
These requests run through the full middleware stack without any browser, so
that harness setting cannot hide a regression in the header the app sends.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from django.conf import settings
from django.test import Client

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

COOP_HEADER = "Cross-Origin-Opener-Policy"
EXPECTED_POLICY = "same-origin"
SETTINGS_PACKAGE = Path(__file__).resolve().parents[2] / "config" / "settings"


def test_no_settings_module_changes_the_opener_policy() -> None:
    overrides = sorted(
        path.name
        for path in SETTINGS_PACKAGE.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Name) and node.id == "SECURE_CROSS_ORIGIN_OPENER_POLICY"
    )

    assert overrides == []
    assert settings.SECURE_CROSS_ORIGIN_OPENER_POLICY == EXPECTED_POLICY
    assert "django.middleware.security.SecurityMiddleware" in settings.MIDDLEWARE


def test_public_entry_page_sends_the_exact_opener_policy() -> None:
    response = Client().get("/")

    assert response.status_code == 200
    assert response.headers[COOP_HEADER] == EXPECTED_POLICY


@pytest.mark.django_db(transaction=True)
def test_staff_sign_in_and_refusal_send_the_exact_opener_policy() -> None:
    client = Client()
    sign_in = client.get("/auth/login/")
    refusal = client.get("/auth/protected/")

    assert sign_in.status_code == 200
    assert sign_in.headers[COOP_HEADER] == EXPECTED_POLICY
    assert refusal.status_code == 403
    assert refusal.headers[COOP_HEADER] == EXPECTED_POLICY


@pytest.mark.django_db(transaction=True)
def test_patient_access_page_sends_the_exact_opener_policy(
    rbac_graph: RbacGraph,
) -> None:
    response = Client().get(f"/patient/access/{rbac_graph.clinic_a}/")

    assert response.status_code == 200
    assert response.headers[COOP_HEADER] == EXPECTED_POLICY
