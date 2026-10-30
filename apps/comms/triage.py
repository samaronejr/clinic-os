"""Deterministic clinical-advice triage for inbound patient messages.

Terms match whole words; a trailing ``*`` marks a stem (a prefix of a word), so
"samu" never matches a name such as "Samuel" nor "dor" a word such as "adorei".
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
    "nao consigo respirar",
    "desmai*",
    "convuls*",
    "sangramento*",
    "sangrando",
    "overdose",
    "suicid*",
    "me matar",
    "emergencia",
    "samu",
    "avc",
    "infarto",
    "anafilax*",
    "reacao alergica grave",
)
CLINICAL_TERMS: Final = (
    "dor",
    "dores",
    "febre",
    "tosse",
    "enjoo*",
    "vomit*",
    "tontura*",
    "sintoma*",
    "efeito colateral",
    "efeitos colaterais",
    "alergi*",
    "pressao alta",
    "pressao baixa",
    "diabet*",
    "remedi*",
    "medicament*",
    "medicac*",
    "dose",
    "doses",
    "dosagem",
    "receita*",
    "posso tomar",
    "devo tomar",
    "parar de tomar",
    "exame*",
    "resultado*",
    "laudo*",
    "diagnostic*",
    "tratamento*",
    "gravidez",
    "gestante*",
    "ferida*",
    "inchaco",
    "piorou",
    "melhorou",
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


def classify_inbound(text: str) -> TriageVerdict:
    """Route one inbound message; urgent beats clinical beats administrative."""
    folded = _fold(text)
    if _has(folded, URGENT_TERMS):
        return TriageVerdict("clinician", "urgent", allow_ai_suggestion=False)
    if _has(folded, CLINICAL_TERMS):
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
    return _has(folded, URGENT_TERMS) or _has(folded, CLINICAL_TERMS)
