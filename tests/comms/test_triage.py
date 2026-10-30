"""Clinical-advice triage routes conservatively and stores nothing."""

from __future__ import annotations

import pytest
from apps.comms.triage import classify_inbound, clinical_content_in_reply


@pytest.mark.parametrize(
    "text",
    [
        "Estou com dor no peito agora",
        "TENHO FALTA DE AR!",
        "ele desmaiou",
        "pensando em me matar",
    ],
)
def test_urgent_text_routes_to_a_clinician_without_ai(text: str) -> None:
    verdict = classify_inbound(text)
    assert (verdict.category, verdict.route) == ("urgent", "clinician")
    assert verdict.needs_clinician
    assert not verdict.allow_ai_suggestion


@pytest.mark.parametrize(
    "text",
    [
        "Posso tomar o remédio junto com café?",
        "Qual a dose?",
        "o resultado do exame saiu?",
        "Estou com febre",
    ],
)
def test_clinical_text_routes_to_a_clinician_without_ai(text: str) -> None:
    verdict = classify_inbound(text)
    assert (verdict.category, verdict.route) == ("clinical", "clinician")
    assert not verdict.allow_ai_suggestion


@pytest.mark.parametrize(
    "text",
    [
        "Qual o horário de funcionamento?",
        "Preciso remarcar minha consulta",
        "Pode enviar o boleto?",
    ],
)
def test_administrative_text_may_receive_a_reviewable_suggestion(text: str) -> None:
    verdict = classify_inbound(text)
    assert (verdict.category, verdict.route) == ("administrative", "staff")
    assert verdict.allow_ai_suggestion


def test_mixed_text_is_clinical_and_unrecognised_text_gets_no_ai() -> None:
    assert (
        classify_inbound("Quero remarcar porque estou com dor").category == "clinical"
    )
    unknown = classify_inbound("ok")
    assert (unknown.category, unknown.route, unknown.allow_ai_suggestion) == (
        "unclassified",
        "staff",
        False,
    )


def test_accents_case_and_punctuation_do_not_hide_terms() -> None:
    assert classify_inbound("DOR... no PEITO!!!").category == "urgent"
    assert classify_inbound("Remédio").category == "clinical"


def test_a_drifting_draft_is_detected() -> None:
    assert clinical_content_in_reply("Pode tomar a dose dobrada")
    assert not clinical_content_in_reply("Atendemos de segunda a sexta, das 8h às 18h.")


def test_terms_match_whole_words_not_substrings() -> None:
    assert classify_inbound("O Samuel quer remarcar").category == "administrative"
    assert classify_inbound("Adorei o atendimento, obrigado").category == (
        "administrative"
    )
    assert classify_inbound("Falei com o vendedor sobre o boleto").category == (
        "administrative"
    )
    assert classify_inbound("Estou com dores fortes").category == "clinical"
    assert classify_inbound("Tomei os remédios hoje").category == "clinical"
