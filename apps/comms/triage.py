"""Deterministic clinical-advice triage for inbound patient messages.

Clinical terms match as substrings of the folded text, so a listed term also hits
every longer word that contains it, and the list is the union of every clinical
term any earlier version used. Coverage of inflections is only as wide as the
listed substrings: a form that does not contain one ("dosagens" without
"dosage") is not matched until its stem is listed. A false positive inside an
unrelated word only sends a harmless thread to a clinician, while a miss would
withhold escalation, so every added term errs toward matching. Urgent terms
match whole words or marked stems (a trailing ``*`` is a word prefix; a
substring there would flag names such as "Samuel"); "convuls" and "infarto" are
also clinical substrings, so mid-word forms such as "anticonvulsivante" and
"reinfarto" reach a clinician even though they are not urgent matches. The
administrative list is whole-word, since a miss there only withholds a draft.
The broad clinical stems accept these fail-safe false positives, which only send
a thread to a clinician: "gravid" ("gravidade"), "pior" ("o pior dia"), "sangr"
("sangria"), "tont" ("atontado"), "inchad" ("guinchado", "pechinchado") and
"receit" ("preceito"). The urgent idioms do too, as urgent: "me mato*" and
"quero morrer*" match "isso me matou de rir" and "quero morrer de rir", and the
whole phrase "faltando ar" matches "faltando ar-condicionado". All of them fail
safe, since a harmless message only reaches a clinician sooner.
The classifier is a conservative router, never a clinician: any clinical or
urgent term, or any text it cannot positively recognise as administrative,
withholds AI drafting and (for clinical/urgent text) routes the thread to the
clinician queue. It reads pt-BR text with accents and case folded away, stores
nothing and returns only fixed codes, so a verdict can be logged and audited
without ever carrying message content.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

type Route = Literal["staff", "clinician"]

URGENT_TERMS: Final = (
    "dor no peito",
    "falta de ar",
    "faltando ar",
    "faltando o ar",
    "me falta o ar",
    "nao consigo respirar",
    "desmai*",
    "convuls*",
    "sangramento*",
    "sangrando*",
    "overdose*",
    "suicid*",
    "me matar*",
    "me mato*",
    "se matar*",
    "quero morrer*",
    "queria morrer*",
    "se matou",
    "me matei*",
    "matar me*",
    "vontade de morrer*",
    "nao quero mais viver",
    "emergencia*",
    "samu",
    "avc*",
    "infarto*",
    "anafil*",
    "reacao alergica grave",
)
CLINICAL_TERMS: Final = (
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
    "receit",
    "dosage",
    "sangr",
    "anafil",
    "gravid",
    "inchad",
    "pior",
    "tont",
    "pressao baix",
    "convuls",
    "infarto",
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
    "dores",
    "efeitos colaterais",
    "alergi",
    "remedi",
    "medicament",
    "medicac",
    "doses",
    "diagnostic",
    "toss",
)
ADMINISTRATIVE_TERMS: Final = (
    "horario*",
    "remarcar",
    "reagendar",
    "agendar",
    "cancelar consulta",
    "confirmar consulta",
    "endereco",
    "estacionamento",
    "boleto*",
    "pagamento*",
    "fatura*",
    "recibo*",
    "nota fiscal",
    "segunda via",
    "convenio*",
    "obrigad*",
)
_NON_WORD = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class TriageVerdict:
    """Fixed-code routing decision; it never repeats any part of the text."""

    route: Route
    category: Literal["urgent", "clinical", "administrative", "unclassified"]
    allow_ai_suggestion: bool

    @property
    def needs_clinician(self) -> bool:
        """Whether the thread belongs in the clinician queue."""
        return self.route == "clinician"


def _fold(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in stripped if not unicodedata.combining(char))
    return " " + _NON_WORD.sub(" ", plain).strip() + " "


def _matches(folded: str, term: str) -> bool:
    if term.endswith("*"):
        return f" {term[:-1]}" in folded
    return f" {term} " in folded


def _has(folded: str, terms: tuple[str, ...]) -> bool:
    return any(_matches(folded, term) for term in terms)


def _contains(folded: str, terms: tuple[str, ...]) -> bool:
    """Match as substrings: a term hits every word that contains it.

    Inflections are hits only where they still contain a listed term; stems such
    as "alergi", "remedi", "receit" and "dosage" are listed for that reason, and
    a form outside every listed term is not matched.
    """
    return any(term in folded for term in terms)


def classify_inbound(text: str) -> TriageVerdict:
    """Route one inbound message; urgent beats clinical beats administrative."""
    folded = _fold(text)
    if _has(folded, URGENT_TERMS):
        return TriageVerdict("clinician", "urgent", allow_ai_suggestion=False)
    if _contains(folded, CLINICAL_TERMS):
        return TriageVerdict("clinician", "clinical", allow_ai_suggestion=False)
    if _has(folded, ADMINISTRATIVE_TERMS):
        return TriageVerdict("staff", "administrative", allow_ai_suggestion=True)
    return TriageVerdict("staff", "unclassified", allow_ai_suggestion=False)


def clinical_content_in_reply(text: str) -> bool:
    """Return whether a drafted reply itself carries clinical advice.

    A suggestion is dropped unless it is positively administrative, so a model
    that drifts into dosage, diagnosis or symptom advice never reaches a human
    as a ready-to-send draft.
    """
    folded = _fold(text)
    return _has(folded, URGENT_TERMS) or _contains(folded, CLINICAL_TERMS)
