"""RNDS laboratory-result (REL) bundle: typed exam input, builder and validator.

The only RNDS use case the guide supports is the laboratory-result document
(Resultado de Exame Laboratorial, COVID-19 and Monkeypox). The definitions
package linked from the guide returned HTTP 404 on 2026-11-10, so the rules here
are recorded from the guide's documentation example and nothing else
(``DEFINITIONS``). A code is accepted only when that example documents it; any
other code is ``code_not_documented`` until a downloaded package extends the
sets. The builder takes a typed exam document entered for the exam and never
reads the encrypted free text of an orders result.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final, cast

from apps.interop.errors import RndsValidationError, ValidationIssue

if TYPE_CHECKING:
    from uuid import UUID

type JsonValue = (
    bool | int | str | float | None | Sequence[JsonValue] | Mapping[str, JsonValue]
)

FHIR_BASE: Final = "http://www.saude.gov.br/fhir/r4"
PROFILE: Final = "BRResultadoExameLaboratorial-1.1"
PROFILES: Final = {
    "Composition": f"{FHIR_BASE}/StructureDefinition/BRResultadoExameLaboratorial-1.1",
    "Observation": (
        f"{FHIR_BASE}/StructureDefinition/BRDiagnosticoLaboratorioClinico-1.0"
    ),
    "Specimen": f"{FHIR_BASE}/StructureDefinition/BRAmostraBiologica-1.0",
}
SYSTEMS: Final = {
    "document_type": f"{FHIR_BASE}/CodeSystem/BRTipoDocumento",
    "individual": f"{FHIR_BASE}/StructureDefinition/BRIndividuo-1.0",
    "establishment": f"{FHIR_BASE}/StructureDefinition/BREstabelecimentoSaude-1.0",
    "sus_subgroup": f"{FHIR_BASE}/CodeSystem/BRSubgrupoTabelaSUS",
    "exam": f"{FHIR_BASE}/CodeSystem/BRNomeExameLOINC",
    "performer": (
        f"{FHIR_BASE}/StructureDefinition/BRPessoaJuridicaProfissionalLiberal-1.0"
    ),
    "result": f"{FHIR_BASE}/CodeSystem/BRResultadoQualitativoExame",
    "specimen": f"{FHIR_BASE}/CodeSystem/BRTipoAmostraGAL",
}
REQUESTER_SYSTEM: Final = f"{FHIR_BASE}/NamingSystem/BRRNDS-"
DOCUMENT_TYPE: Final = "REL"
TITLE: Final = "Resultado de Exame Laboratorial"
# Where every rule and code set below comes from, and when it was read.
DEFINITIONS: Final = {
    "guide": "https://rnds-guia.saude.gov.br/docs/publico-alvo/ti/homologar/",
    "errors": "https://rnds-guia.saude.gov.br/docs/publico-alvo/ti/erros/",
    "definitions": "https://rnds-guia.saude.gov.br/docs/rnds/definicoes/",
    "package": "unavailable: the definitions zip returned HTTP 404 on 2026-11-10",
    "example_date": "2020-10-02",
    "fetched": "2026-11-10",
}
DOCUMENTED_CODES: Final[Mapping[str, frozenset[str]]] = {
    "exam_code": frozenset({"94507-1"}),
    "sus_subgroup": frozenset({"0214"}),
    # The example's reference range documents (1) detectable, (2) not detectable.
    "result_code": frozenset({"1", "2"}),
    "specimen_type": frozenset({"SGHEM"}),
}
EXAM_FIELDS: Final = (
    "exam_code",
    "sus_subgroup",
    "result_code",
    "method",
    "reference_range",
    "specimen_type",
    "issued_at",
)
_FORMATS: Final = {
    "exam_code": re.compile(r"[0-9]{1,7}-[0-9]"),
    "sus_subgroup": re.compile(r"[0-9]{4}"),
    "result_code": re.compile(r"[0-9]{1,2}"),
    "specimen_type": re.compile(r"[A-Z0-9]{2,10}"),
}
_TEXT_LIMITS: Final = {"method": 100, "reference_range": 200}
_INSTANT: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?"
    r"(Z|[+-][0-9]{2}:[0-9]{2})"
)
_REQUESTER: Final = re.compile(r"[0-9]{1,10}")
_CNES: Final = re.compile(r"[0-9]{7}")
_UUID_TEXT: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_CLOCK_SKEW: Final = timedelta(minutes=5)
_ISSUED_FLOOR: Final = datetime.fromisoformat("2020-01-01T00:00:00+00:00")
_CPF_LENGTH: Final = 11
_CNS_LENGTH: Final = 15
_CNPJ_LENGTH: Final = 14
_MOD11: Final = 11
_MOD11_FLOOR: Final = 2
_DOUBLED_ABOVE: Final = 9


@dataclass(frozen=True, slots=True)
class LabExam:
    """The typed exam document a clinician enters for one result version."""

    exam_code: str
    sus_subgroup: str
    result_code: str
    method: str
    reference_range: str
    specimen_type: str
    issued_at: datetime

    def canonical(self) -> dict[str, JsonValue]:
        """Return the exact input terms for the request digest."""
        return {
            "exam_code": self.exam_code,
            "sus_subgroup": self.sus_subgroup,
            "result_code": self.result_code,
            "method": self.method,
            "reference_range": self.reference_range,
            "specimen_type": self.specimen_type,
            "issued_at": self.issued_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class Establishment:
    """The credentialed establishment: RNDS requester id, CNES and legal entity."""

    requester_id: str
    cnes: str
    performer: str
    requester_cns: str


def _luhn(digits: str) -> bool:
    total = 0
    for position, character in enumerate(reversed(digits)):
        value = int(character) * (2 if position % 2 == 1 else 1)
        total += value - _DOUBLED_ABOVE if value > _DOUBLED_ABOVE else value
    return total % 10 == 0


def loinc_valid(code: str) -> bool:
    """Check a LOINC code's mod-10 check digit (``94507-1``)."""
    return bool(_FORMATS["exam_code"].fullmatch(code)) and _luhn(code.replace("-", ""))


def _check_digit(digits: str, weights: Sequence[int]) -> int:
    remainder = sum(int(d) * w for d, w in zip(digits, weights, strict=True)) % _MOD11
    return 0 if remainder < _MOD11_FLOOR else _MOD11 - remainder


def cpf_valid(value: str) -> bool:
    """Check an 11-digit CPF and both check digits."""
    if len(value) != _CPF_LENGTH or not value.isdigit() or len(set(value)) == 1:
        return False
    first = _check_digit(value[:9], range(10, 1, -1))
    second = _check_digit(value[:10], range(11, 1, -1))
    return value[9:] == f"{first}{second}"


def cns_valid(value: str) -> bool:
    """Check a 15-digit CNS: definitive (1, 2) or provisional (7, 8, 9) series."""
    if len(value) != _CNS_LENGTH or not value.isdigit() or value[0] not in "12789":
        return False
    return sum(int(d) * (15 - i) for i, d in enumerate(value)) % _MOD11 == 0


def cnpj_valid(value: str) -> bool:
    """Check a 14-digit CNPJ and both check digits."""
    if len(value) != _CNPJ_LENGTH or not value.isdigit() or len(set(value)) == 1:
        return False
    weights = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    first = _check_digit(value[:12], weights)
    second = _check_digit(value[:13], (6, *weights))
    return value[12:] == f"{first}{second}"


def individual_valid(value: str) -> bool:
    """Accept a valid CNS or CPF as a BRIndividuo identifier."""
    return cns_valid(value) or cpf_valid(value)


def cnes_valid(value: str) -> bool:
    """Accept a 7-digit CNES establishment code."""
    return bool(_CNES.fullmatch(value))


def performer_valid(value: str) -> bool:
    """Accept a CNPJ (legal entity) or CPF (liberal professional) performer."""
    return cnpj_valid(value) or cpf_valid(value)


_IDENTIFIER_RULES: Final[Mapping[str, Callable[[str], bool]]] = {
    "individual": individual_valid,
    "establishment": cnes_valid,
    "performer": performer_valid,
}


def _text_issue(field: str, value: object) -> ValidationIssue | None:
    limit = _TEXT_LIMITS[field]
    if (
        type(value) is not str
        or not 1 <= len(value) <= limit
        or value != value.strip()
        or any(not character.isprintable() for character in value)
    ):
        return ValidationIssue(f"exam.{field}", "invalid_text")
    return None


def _code_issue(field: str, value: object) -> ValidationIssue | None:
    if type(value) is not str or not _FORMATS[field].fullmatch(value):
        return ValidationIssue(f"exam.{field}", "invalid_format")
    if field == "exam_code" and not loinc_valid(value):
        return ValidationIssue(f"exam.{field}", "invalid_check_digit")
    if value not in DOCUMENTED_CODES[field]:
        return ValidationIssue(f"exam.{field}", "code_not_documented")
    return None


def _issued(value: object, now: datetime) -> datetime | ValidationIssue:
    if isinstance(value, str) and _INSTANT.fullmatch(value):
        value = datetime.fromisoformat(value)
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or not _ISSUED_FLOOR <= value <= now + _CLOCK_SKEW
    ):
        return ValidationIssue("exam.issued_at", "invalid_datetime")
    return value


def parse_exam(document: object, *, now: datetime) -> LabExam:
    """Validate the closed exam document; report every issue, never the values."""
    if not isinstance(document, Mapping):
        raise RndsValidationError((ValidationIssue("exam", "invalid_document"),))
    issues = [
        ValidationIssue(f"exam.{key}", "unknown_field")
        for key in sorted(str(key) for key in document if key not in EXAM_FIELDS)
    ]
    issues.extend(
        ValidationIssue(f"exam.{field}", "required")
        for field in EXAM_FIELDS
        if field not in document
    )
    checks = (
        *(
            _code_issue(field, document[field])
            for field in ("exam_code", "sus_subgroup", "result_code", "specimen_type")
            if field in document
        ),
        *(
            _text_issue(field, document[field])
            for field in _TEXT_LIMITS
            if field in document
        ),
    )
    issues.extend(issue for issue in checks if issue is not None)
    issued = _issued(document.get("issued_at"), now)
    if "issued_at" in document and isinstance(issued, ValidationIssue):
        issues.append(issued)
    if issues:
        raise RndsValidationError(tuple(issues))
    return LabExam(
        exam_code=cast("str", document["exam_code"]),
        sus_subgroup=cast("str", document["sus_subgroup"]),
        result_code=cast("str", document["result_code"]),
        method=cast("str", document["method"]),
        reference_range=cast("str", document["reference_range"]),
        specimen_type=cast("str", document["specimen_type"]),
        issued_at=cast("datetime", issued),
    )


def _coding(system: str, code: str) -> dict[str, JsonValue]:
    return {"coding": [{"system": SYSTEMS[system], "code": code}]}


def _identifier(system: str, value: str) -> dict[str, JsonValue]:
    return {"identifier": {"system": SYSTEMS[system], "value": value}}


def _instant(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def build_bundle(
    *,
    submission_id: UUID,
    composed_at: datetime,
    exam: LabExam,
    patient: str,
    establishment: Establishment,
) -> dict[str, JsonValue]:
    """Build the REL document bundle in the documentation example's exact shape."""
    reference = str(submission_id)
    subject = _identifier("individual", patient)
    return {
        "resourceType": "Bundle",
        "id": reference,
        "type": "document",
        "timestamp": _instant(composed_at),
        "meta": {"lastUpdated": _instant(composed_at)},
        "identifier": {
            "system": REQUESTER_SYSTEM + establishment.requester_id,
            "value": reference,
        },
        "entry": [
            {
                "fullUrl": "urn:uuid:transient-0",
                "resource": {
                    "resourceType": "Composition",
                    "meta": {"profile": [PROFILES["Composition"]]},
                    "status": "final",
                    "type": _coding("document_type", DOCUMENT_TYPE),
                    "subject": subject,
                    "date": _instant(composed_at),
                    "author": [_identifier("establishment", establishment.cnes)],
                    "title": TITLE,
                    "section": [{"entry": [{"reference": "urn:uuid:transient-1"}]}],
                },
            },
            {
                "fullUrl": "urn:uuid:transient-1",
                "resource": {
                    "resourceType": "Observation",
                    "meta": {"profile": [PROFILES["Observation"]]},
                    "status": "final",
                    "category": [_coding("sus_subgroup", exam.sus_subgroup)],
                    "code": _coding("exam", exam.exam_code),
                    "subject": subject,
                    "issued": _instant(exam.issued_at),
                    "performer": [_identifier("performer", establishment.performer)],
                    "valueCodeableConcept": _coding("result", exam.result_code),
                    "method": {"text": exam.method},
                    "referenceRange": [{"text": exam.reference_range}],
                    "specimen": {"reference": "urn:uuid:transient-2"},
                },
            },
            {
                "fullUrl": "urn:uuid:transient-2",
                "resource": {
                    "resourceType": "Specimen",
                    "meta": {"profile": [PROFILES["Specimen"]]},
                    "type": _coding("specimen", exam.specimen_type),
                },
            },
        ],
    }


class _Checker:
    """Walk one bundle against the recorded profile shape, collecting issues."""

    def __init__(self) -> None:
        self.issues: list[ValidationIssue] = []

    def fail(self, path: str, code: str) -> None:
        self.issues.append(ValidationIssue(path, code))

    def node(
        self, value: object, path: str, keys: tuple[str, ...]
    ) -> Mapping[str, object]:
        """Require an object holding exactly ``keys``; report missing and extra."""
        if not isinstance(value, Mapping):
            self.fail(path, "invalid_element")
            return {}
        for key in keys:
            if key not in value:
                self.fail(f"{path}.{key}", "required")
        for key in sorted(str(key) for key in value if key not in keys):
            self.fail(f"{path}.{key}", "unexpected_element")
        return value

    def single(self, value: object, path: str) -> object:
        if not isinstance(value, list) or len(value) != 1:
            self.fail(path, "invalid_cardinality")
            return None
        return value[0]

    def equal(self, value: object, path: str, expected: object, code: str) -> None:
        if value != expected:
            self.fail(path, code)

    def text(self, value: object, path: str, limit: int) -> None:
        if (
            type(value) is not str
            or not 1 <= len(value) <= limit
            or any(not character.isprintable() for character in value)
        ):
            self.fail(path, "invalid_text")

    def instant(self, value: object, path: str) -> None:
        if type(value) is not str or not _INSTANT.fullmatch(value):
            self.fail(path, "invalid_instant")

    def profile(self, resource: Mapping[str, object], path: str, kind: str) -> None:
        meta = self.node(resource.get("meta"), f"{path}.meta", ("profile",))
        profile = self.single(meta.get("profile"), f"{path}.meta.profile")
        self.equal(profile, f"{path}.meta.profile", PROFILES[kind], "invalid_profile")

    def coding(self, value: object, path: str, system: str, field: str) -> None:
        concept = self.node(value, path, ("coding",))
        coding = self.node(
            self.single(concept.get("coding"), f"{path}.coding"),
            f"{path}.coding",
            ("system", "code"),
        )
        self.equal(
            coding.get("system"),
            f"{path}.coding.system",
            SYSTEMS[system],
            "invalid_system",
        )
        code = coding.get("code")
        if field == "document_type":
            self.equal(code, f"{path}.coding.code", DOCUMENT_TYPE, "invalid_code")
        elif type(code) is not str or not _FORMATS[field].fullmatch(code):
            self.fail(f"{path}.coding.code", "invalid_format")
        elif field == "exam_code" and not loinc_valid(code):
            self.fail(f"{path}.coding.code", "invalid_check_digit")
        elif code not in DOCUMENTED_CODES[field]:
            self.fail(f"{path}.coding.code", "code_not_documented")

    def identifier(self, value: object, path: str, system: str) -> object:
        holder = self.node(value, path, ("identifier",))
        identifier = self.node(
            holder.get("identifier"), f"{path}.identifier", ("system", "value")
        )
        self.equal(
            identifier.get("system"),
            f"{path}.identifier.system",
            SYSTEMS[system],
            "invalid_system",
        )
        found = identifier.get("value")
        if type(found) is not str or not _IDENTIFIER_RULES[system](found):
            self.fail(f"{path}.identifier.value", "invalid_identifier")
        return found


def _composition(check: _Checker, resource: Mapping[str, object]) -> object:
    path = "Composition"
    check.node(
        resource,
        path,
        (
            "resourceType",
            "meta",
            "status",
            "type",
            "subject",
            "date",
            "author",
            "title",
            "section",
        ),
    )
    check.profile(resource, path, "Composition")
    # RNDS refuses any other document status (EHR-ERR924).
    check.equal(resource.get("status"), f"{path}.status", "final", "status_not_final")
    check.coding(resource.get("type"), f"{path}.type", "document_type", "document_type")
    subject = check.identifier(resource.get("subject"), f"{path}.subject", "individual")
    check.instant(resource.get("date"), f"{path}.date")
    check.identifier(
        check.single(resource.get("author"), f"{path}.author"),
        f"{path}.author",
        "establishment",
    )
    check.equal(resource.get("title"), f"{path}.title", TITLE, "invalid_title")
    section = check.node(
        check.single(resource.get("section"), f"{path}.section"),
        f"{path}.section",
        ("entry",),
    )
    entry = check.node(
        check.single(section.get("entry"), f"{path}.section.entry"),
        f"{path}.section.entry",
        ("reference",),
    )
    check.equal(
        entry.get("reference"),
        f"{path}.section.entry.reference",
        "urn:uuid:transient-1",
        "invalid_reference",
    )
    return subject


def _observation(
    check: _Checker, resource: Mapping[str, object], subject: object
) -> None:
    path = "Observation"
    check.node(
        resource,
        path,
        (
            "resourceType",
            "meta",
            "status",
            "category",
            "code",
            "subject",
            "issued",
            "performer",
            "valueCodeableConcept",
            "method",
            "referenceRange",
            "specimen",
        ),
    )
    check.profile(resource, path, "Observation")
    check.equal(resource.get("status"), f"{path}.status", "final", "status_not_final")
    check.coding(
        check.single(resource.get("category"), f"{path}.category"),
        f"{path}.category",
        "sus_subgroup",
        "sus_subgroup",
    )
    check.coding(resource.get("code"), f"{path}.code", "exam", "exam_code")
    observed = check.identifier(
        resource.get("subject"), f"{path}.subject", "individual"
    )
    check.equal(
        observed, f"{path}.subject.identifier.value", subject, "subject_mismatch"
    )
    check.instant(resource.get("issued"), f"{path}.issued")
    check.identifier(
        check.single(resource.get("performer"), f"{path}.performer"),
        f"{path}.performer",
        "performer",
    )
    check.coding(
        resource.get("valueCodeableConcept"),
        f"{path}.valueCodeableConcept",
        "result",
        "result_code",
    )
    method = check.node(resource.get("method"), f"{path}.method", ("text",))
    check.text(method.get("text"), f"{path}.method.text", _TEXT_LIMITS["method"])
    reference_range = check.node(
        check.single(resource.get("referenceRange"), f"{path}.referenceRange"),
        f"{path}.referenceRange",
        ("text",),
    )
    check.text(
        reference_range.get("text"),
        f"{path}.referenceRange.text",
        _TEXT_LIMITS["reference_range"],
    )
    specimen = check.node(resource.get("specimen"), f"{path}.specimen", ("reference",))
    check.equal(
        specimen.get("reference"),
        f"{path}.specimen.reference",
        "urn:uuid:transient-2",
        "invalid_reference",
    )


def _specimen(check: _Checker, resource: Mapping[str, object]) -> None:
    path = "Specimen"
    check.node(resource, path, ("resourceType", "meta", "type"))
    check.profile(resource, path, "Specimen")
    check.coding(resource.get("type"), f"{path}.type", "specimen", "specimen_type")


def validate_bundle(bundle: object) -> tuple[ValidationIssue, ...]:
    """Check one bundle against the recorded REL profile rules; empty means valid."""
    check = _Checker()
    root = check.node(
        bundle,
        "Bundle",
        ("resourceType", "id", "type", "timestamp", "meta", "identifier", "entry"),
    )
    check.equal(
        root.get("resourceType"), "Bundle.resourceType", "Bundle", "invalid_resource"
    )
    check.equal(root.get("type"), "Bundle.type", "document", "invalid_bundle_type")
    check.instant(root.get("timestamp"), "Bundle.timestamp")
    meta = check.node(root.get("meta"), "Bundle.meta", ("lastUpdated",))
    check.instant(meta.get("lastUpdated"), "Bundle.meta.lastUpdated")
    identifier = check.node(
        root.get("identifier"), "Bundle.identifier", ("system", "value")
    )
    system = identifier.get("system")
    if (
        type(system) is not str
        or not system.startswith(REQUESTER_SYSTEM)
        or not _REQUESTER.fullmatch(system.removeprefix(REQUESTER_SYSTEM))
    ):
        check.fail("Bundle.identifier.system", "invalid_requester")
    value = identifier.get("value")
    if type(value) is not str or not _UUID_TEXT.fullmatch(value):
        check.fail("Bundle.identifier.value", "invalid_identifier")
    check.equal(root.get("id"), "Bundle.id", value, "identifier_mismatch")
    entries = root.get("entry")
    expected = ("Composition", "Observation", "Specimen")
    if not isinstance(entries, list) or len(entries) != len(expected):
        check.fail("Bundle.entry", "invalid_cardinality")
        return tuple(check.issues)
    resources: list[Mapping[str, object]] = []
    for position, (entry, kind) in enumerate(zip(entries, expected, strict=True)):
        node = check.node(entry, f"Bundle.entry[{position}]", ("fullUrl", "resource"))
        check.equal(
            node.get("fullUrl"),
            f"Bundle.entry[{position}].fullUrl",
            f"urn:uuid:transient-{position}",
            "invalid_reference",
        )
        resource = node.get("resource")
        if not isinstance(resource, Mapping) or resource.get("resourceType") != kind:
            check.fail(f"Bundle.entry[{position}].resource", "invalid_resource")
            return tuple(check.issues)
        resources.append(resource)
    subject = _composition(check, resources[0])
    _observation(check, resources[1], subject)
    _specimen(check, resources[2])
    return tuple(check.issues)
