"""Versioned pt-BR document templates for the fixed synthetic PDF profile."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


@dataclass(frozen=True, slots=True)
class DocumentTemplate:
    """Only authored text is interpolated; no clinical conclusions are inferred."""

    title: str
    content_label: str


TEMPLATE_VERSION: Final = "clinical-documents-pt-BR-v1"
TEMPLATES: Final = MappingProxyType(
    {
        "prescription": DocumentTemplate("Prescrição", "Medicamentos"),
        "certificate": DocumentTemplate("Atestado médico", "Declaração médica"),
        "attestation": DocumentTemplate(
            "Declaração de comparecimento", "Comparecimento registrado"
        ),
        "referral": DocumentTemplate("Encaminhamento", "Motivo e informações clínicas"),
        "exam_request": DocumentTemplate(
            "Solicitação de exames", "Indicação registrada"
        ),
        "instruction": DocumentTemplate(
            "Orientações ao paciente", "Orientações aprovadas"
        ),
    }
)
