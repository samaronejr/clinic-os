"""Payment and fiscal projections remain distinct, including in rehearsal."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from apps.billing.fiscal_presentation import FiscalSummary
from django.template.loader import render_to_string
from django.utils.translation import gettext, override


@pytest.mark.parametrize("audience", ["staff", "patient"])
def test_receipt_surface_is_never_labelled_as_a_fiscal_invoice(audience: str) -> None:
    with override("pt-br"):
        html = render_to_string(
            f"billing/partials/{audience}_status.html",
            {
                "clinic_timezone": "America/Sao_Paulo",
                "view": SimpleNamespace(
                    paid=True,
                    amount=10000,
                    receipt_reference=UUID("00000000-0000-4000-8000-000000000058"),
                ),
            },
        )
        assert 'data-document-kind="receipt"' in html
        assert gettext("Receipt (not a tax invoice)") in html


def test_fiscal_authorization_keeps_the_synthetic_disclosure() -> None:
    row = FiscalSummary(
        reference=UUID("00000000-0000-4000-8000-000000000058"),
        status="authorized",
        rehearsal=True,
        rejection_code="",
    )
    with override("pt-br"):
        html = render_to_string(
            "billing/partials/finance_fiscal.html",
            {"fiscal_rows": (row,), "fiscal_finance": True},
        )
        assert 'data-fiscal-state="authorized"' in html
        assert 'data-fiscal-rehearsal="true"' in html
        assert gettext("NFS-e rehearsal (not a tax invoice)") in html


def test_fiscal_rejection_displays_safe_code_and_translated_recovery() -> None:
    row = FiscalSummary(
        reference=UUID("00000000-0000-4000-8000-000000000058"),
        status="rejected",
        rehearsal=True,
        rejection_code="E0004",
    )
    with override("pt-br"):
        html = render_to_string(
            "billing/partials/finance_fiscal.html",
            {"fiscal_rows": (row,), "fiscal_finance": True},
        )
        assert "E0004" in html
        assert row.rejection_message in html


def test_fiscal_surface_without_finance_authority_has_no_document_rows() -> None:
    html = render_to_string(
        "billing/partials/finance_fiscal.html",
        {"fiscal_finance": False},
    )
    assert 'data-fiscal-permission="denied"' in html
    assert "data-fiscal-state=" not in html
