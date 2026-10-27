"""Independent success vectors transcribed from manual v5 pages 11 and 14."""

import json

import pytest
from apps.prescription.sncr import (
    AllocationRequest,
    RecordedSncrAdapter,
    SncrUnavailableError,
)
from django.test import override_settings


@pytest.mark.parametrize("retained", [False, True])
@override_settings(PRESCRIPTION_SYNTHETIC_ISSUANCE=True)
def test_manual_v5_success_examples(retained: bool) -> None:
    # Response literals are the published examples, not produced by the parser.
    body = (
        {
            "inicio": "2602.6-53.0000001",
            "fim": "2602.6-53.0001000",
            "quantidade": 1000,
            "mensagem": "Numera\u00e7\u00e3o gerada com sucesso.",
        }
        if retained
        else {
            "numeracoesReceita": "[2411.1-00.0000001, 2411.1-00.0000002]",
            "saldoReceitas": 49,
            "mensagem": "Saldo inferior a 50 receitas dispon\u00edveis",
        }
    )
    request = AllocationRequest(
        category="controle_especial" if retained else "notificacao_a",
        council="CRM",
        jurisdiction="SP",
        registration="999999999999",
        quantity=1 if retained else 25,
        cnpj="11111111111111" if retained else "",
    )
    response = json.dumps(body).encode()
    result = RecordedSncrAdapter(
        request=request, response=response, status=201 if retained else 200
    ).allocate(request)
    assert (result.first, result.last, result.quantity) == (
        ("2602.6-53.0000001", "2602.6-53.0001000", 1000)
        if retained
        else ("2411.1-00.0000001", "2411.1-00.0000002", 2)
    )
    assert result.response == response


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 500])
@override_settings(PRESCRIPTION_SYNTHETIC_ISSUANCE=True)
def test_error_status_never_returns_numbers(status: int) -> None:
    request = AllocationRequest(
        category="notificacao_a",
        council="CRM",
        jurisdiction="SP",
        registration="999999999999",
        quantity=25,
    )
    with pytest.raises(SncrUnavailableError):
        RecordedSncrAdapter(
            request=request, response=b'{"mensagem":"erro"}', status=status
        ).allocate(request)


@pytest.mark.parametrize(
    ("category", "kind"),
    [
        ("notificacao_a", "NRA"),
        ("notificacao_b", "NRB"),
        ("notificacao_b2", "NRB2"),
        ("retinoid_systemic", "NRR"),
        ("thalidomide", "NRT"),
    ],
)
def test_manual_notification_codes(category: str, kind: str) -> None:
    request = AllocationRequest(
        category=category,
        council="CRM",
        jurisdiction="SP",
        registration="999999999999",
        quantity=25,
    )
    assert request.payload()["receita"] == kind


@pytest.mark.parametrize("quantity", [0, 1, 9, 51, True])
def test_manual_notification_request_bounds(quantity: int) -> None:
    request = AllocationRequest(
        category="notificacao_a",
        council="CRM",
        jurisdiction="SP",
        registration="999999999999",
        quantity=quantity,
    )
    with pytest.raises(SncrUnavailableError):
        request.payload()
