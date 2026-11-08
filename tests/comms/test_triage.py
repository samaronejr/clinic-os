"""Clinical-advice triage routes conservatively and stores nothing."""

from __future__ import annotations

from typing import Final

import pytest
from apps.comms.triage import (
    CLINICAL_TERMS,
    classify_inbound,
    clinical_content_in_reply,
)


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


def test_names_and_administrative_words_are_not_urgent_or_clinical() -> None:
    assert classify_inbound("O Samuel quer remarcar").category == "administrative"
    assert classify_inbound("Samuel pediu o boleto").category == "administrative"
    assert classify_inbound("Falei com o vendedor sobre o boleto").category == (
        "clinical"
    ), "substring clinical matching is deliberate: a false positive fails safe"


@pytest.mark.parametrize(
    "text",
    [
        "Estou com uma dorzinha, posso remarcar?",
        "Estou com febres, quero remarcar",
        "Tenho tossido muito, mas quero agendar",
        "Meu pé está com inchaço, qual o horário?",
        "Posso mudar a dosagem? Preciso do boleto",
        "Estou grávida? Fiz o exame, segunda via do recibo",
        "Tomei os remédios hoje e quero remarcar",
        "Sinto muitas dores, pode confirmar consulta?",
        "Dorzinha no braço, obrigada",
        "Senti efeitos colaterais, quero remarcar",
        "Minhas medicações acabaram, posso remarcar?",
        "Sou alérgico a dipirona, quero agendar",
        "Tive uma reação alérgica, quero remarcar",
        "Fui diagnosticado ontem, quero agendar",
        "Tomei um remedinho, posso remarcar?",
    ],
)
def test_inflections_and_diminutives_of_clinical_terms_still_escalate(
    text: str,
) -> None:
    verdict = classify_inbound(text)
    assert verdict.needs_clinician, text
    assert not verdict.allow_ai_suggestion, text


@pytest.mark.parametrize("term", CLINICAL_TERMS)
def test_every_clinical_term_escalates_inside_a_larger_word(term: str) -> None:
    # Substring matching keeps every hit the original matcher had; embedding
    # the term in a longer word must never route the thread to staff.
    assert classify_inbound(f"quero remarcar x{term}zinho").needs_clinician


@pytest.mark.parametrize(
    "stem",
    ["sangrando", "emergencias", "infartou", "overdoses", "avcs", "avci", "avch"],
)
def test_inflected_urgent_terms_escalate_as_urgent(stem: str) -> None:
    assert classify_inbound(f"preciso ajuda, {stem} aqui").category == "urgent"


def test_a_clinical_draft_is_still_detected_in_an_inflected_form() -> None:
    assert clinical_content_in_reply("Tome a dosagem dobrada")
    assert clinical_content_in_reply("Sua dorzinha deve passar")


@pytest.mark.parametrize(
    "text",
    [
        "Sobre os efeitos colaterais, fale com o médico",
        "Suas medicações podem ser ajustadas",
        "Isso pode ser uma reação alérgica",
        "O diagnóstico foi confirmado",
        "Esse remedinho serve",
    ],
)
def test_clinical_drafts_with_inflected_terms_are_dropped(text: str) -> None:
    assert clinical_content_in_reply(text)


URGENT_PHRASES: Final = (
    "Às vezes acho que me mataria",
    "Um dia me matarei",
    "Eu me mataria, mas quero remarcar",
    "Eu me mato",
    "Vou me matar",
    "Ele tentou se matar",
    "Quero morrer",
    "Quero morrer, mas preciso remarcar",
    "Queria morrer hoje",
    "Estou faltando ar",
    "Estou com falta de ar, quero agendar",
    "Meu pai teve dois AVCs, quero agendar consulta",
    "Minha mãe teve um AVCi, quero agendar",
    "Ele teve um choque anafilático, quero remarcar",
    "Foi anafilaxia, pode agendar?",
    "Ele se matou",
    "Quase me matei",
    "Tenho vontade de morrer",
    "Não quero mais viver",
    "Nao quero mais viver, mas preciso remarcar",
    "Vou matar-me",
    "Está faltando o ar",
    "Me falta o ar, quero agendar",
)
CLINICAL_PHRASES: Final = (
    "Meu anticonvulsivante acabou, quero agendar",
    "O anticonvulsivo acabou, remarcar",
    "Meu pai teve um reinfarto, quero agendar",
    "Posso mudar as dosagens? Preciso do boleto",
    "Qual a dosagem? Quero remarcar",
    "Quero uma receitinha, qual o horário?",
    "Preciso das receitas, pode agendar?",
    "Ele sangrou muito, quero remarcar",
    "Estou grávida, quero agendar",
    "Minha mão está inchada, qual o horário?",
    "Estou piorando, quero remarcar",
    "Fiquei tonta, posso remarcar?",
    "Estou com a pressão baixinha, quero agendar",
    "Minha pressão baixa hoje, pode confirmar consulta?",
    "Sangrou de novo, preciso do boleto",
    "Ela está grávida e quer a segunda via",
    "Ficou tonto e inchado, pode remarcar?",
)


@pytest.mark.parametrize("text", URGENT_PHRASES)
def test_suicide_breathing_and_stroke_phrases_are_urgent(text: str) -> None:
    verdict = classify_inbound(text)
    assert verdict.category == "urgent", text
    assert verdict.needs_clinician
    assert not verdict.allow_ai_suggestion


@pytest.mark.parametrize("text", CLINICAL_PHRASES)
def test_mid_word_and_inflected_clinical_phrases_reach_a_clinician(text: str) -> None:
    verdict = classify_inbound(text)
    assert verdict.needs_clinician, text
    assert not verdict.allow_ai_suggestion, text
    assert clinical_content_in_reply(text), text


@pytest.mark.parametrize(
    "text",
    [
        "A gravidade me preocupa, quero remarcar",
        "O pior é a espera, quero remarcar",
        "Pediram uma sangria no evento, qual o horário?",
    ],
)
def test_broad_stems_accept_fail_safe_false_positives(text: str) -> None:
    # An unrelated word that contains a clinical stem only reaches a clinician.
    assert classify_inbound(text).needs_clinician, text


@pytest.mark.parametrize(
    "text",
    [
        "Isso me matou de rir",
        "Quero morrer de rir",
        "Está faltando ar-condicionado na recepção",
        "Meu carro foi guinchado no estacionamento",
        "O valor pode ser pechinchado?",
        "Qual o preceito da clínica? Quero remarcar",
    ],
)
def test_idioms_and_lookalike_words_accept_a_fail_safe_escalation(text: str) -> None:
    # Harmless text that contains an urgent phrase or a broad stem only reaches
    # a clinician sooner; it is never routed to staff with a draft.
    verdict = classify_inbound(text)
    assert verdict.needs_clinician, text
    assert not verdict.allow_ai_suggestion, text


# Every clinical term any earlier version used, written out so the guard does not
# depend on the current list: 9d2c48e (substring terms) and da430a1 (stems,
# written without their asterisk; there they matched as word prefixes).
CLINICAL_AT_9D2C48E: Final = (
    "dor",
    "febre",
    "tosse",
    "enjoo",
    "vomit",
    "tontura",
    "sintoma",
    "efeito colateral",
    "alergia",
    "pressao alta",
    "pressao baixa",
    "diabet",
    "remedio",
    "medicamento",
    "medicacao",
    "dose",
    "dosagem",
    "receita",
    "posso tomar",
    "devo tomar",
    "parar de tomar",
    "exame",
    "resultado",
    "laudo",
    "diagnostico",
    "tratamento",
    "gravidez",
    "gestante",
    "ferida",
    "inchaco",
    "piorou",
    "melhorou",
)

CLINICAL_AT_DA430A1: Final = (
    "dor",
    "dores",
    "febre",
    "tosse",
    "enjoo",
    "vomit",
    "tontura",
    "sintoma",
    "efeito colateral",
    "efeitos colaterais",
    "alergi",
    "pressao alta",
    "pressao baixa",
    "diabet",
    "remedi",
    "medicament",
    "medicac",
    "dose",
    "doses",
    "dosagem",
    "receita",
    "posso tomar",
    "devo tomar",
    "parar de tomar",
    "exame",
    "resultado",
    "laudo",
    "diagnostic",
    "tratamento",
    "gravidez",
    "gestante",
    "ferida",
    "inchaco",
    "piorou",
    "melhorou",
)
KNOWN_CLINICAL: Final = tuple(sorted({*CLINICAL_AT_9D2C48E, *CLINICAL_AT_DA430A1}))


@pytest.mark.parametrize("term", KNOWN_CLINICAL)
def test_no_term_from_an_earlier_clinical_list_stops_escalating(term: str) -> None:
    for text in (term, f"quero remarcar, {term}", f"quero remarcar {term}zinho"):
        verdict = classify_inbound(text)
        assert verdict.needs_clinician, text
        assert verdict.category in {"clinical", "urgent"}, text
        assert not verdict.allow_ai_suggestion, text
        assert clinical_content_in_reply(text), text


# The urgent terms of 9d2c48e. Each one, alone, must still be an urgent match,
# so narrowing the matcher can never silently drop one.
URGENT_AT_9D2C48E: Final = (
    "dor no peito",
    "falta de ar",
    "nao consigo respirar",
    "desmai",
    "convuls",
    "sangramento",
    "sangrando",
    "overdose",
    "suicid",
    "me matar",
    "emergencia",
    "samu",
    "avc",
    "infarto",
    "anafilax",
    "reacao alergica grave",
)


@pytest.mark.parametrize("term", URGENT_AT_9D2C48E)
def test_no_urgent_term_from_the_original_list_stops_being_urgent(term: str) -> None:
    for text in (term, f"quero remarcar, {term}", f"quero remarcar {term}"):
        assert classify_inbound(text).category == "urgent", text
