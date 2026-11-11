"""Import profile/mapping contract (docs/interop/import-mapping-v1.json).

``SCHEMA`` is the committed contract; ``tests/interop/test_contract.py``
requires the docs file to equal it byte for byte after canonical JSON
encoding. Mapping suggestions are advisory only: they are derived from a
fixed header vocabulary, never from patient values, and nothing is applied
until an authorized operator saves a mapping version.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

import rfc8785

from apps.interop.access import Code, ImportInputError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from apps.interop.sources import Dataset

SCHEMA_NAME: Final = "import-mapping-v1"
PROFILE_NAME: Final = "import-profile-v1"
MIN_PARTIAL_SYNONYM: Final = 4
SOURCE_KINDS: Final = ("csv", "xlsx", "zip", "api")
PATIENT_FIELDS: Final = (
    "source_id",
    "legal_name",
    "social_name",
    "birth_date",
    "cpf",
    "cns",
    "sex_at_birth",
    "registered_at",
    "registered_by",
)
HISTORY_FIELDS: Final = (
    "source_id",
    "patient_source_id",
    "kind",
    "recorded_at",
    "author",
    "text",
    "code",
    "value",
    "unit",
    "file",
    "title",
)
HISTORY_REQUIRED: Final = (
    "source_id",
    "patient_source_id",
    "kind",
    "recorded_at",
    "author",
)
SEX_VALUES: Final = ("declined", "female", "intersex", "male", "not_informed")
HISTORY_KINDS: Final = ("note", "observation", "attachment")
DATE_FORMATS: Final = ("DD/MM/YYYY", "YYYY-MM-DD", "excel_serial")
DATETIME_FORMATS: Final = ("DD/MM/YYYY HH:MM", "YYYY-MM-DDTHH:MM")


def _columns(fields: tuple[str, ...], required: list[str]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": {field: {"$ref": "#/$defs/column"} for field in fields},
    }


def _value_map(values: tuple[str, ...]) -> dict[str, object]:
    return {
        "type": "object",
        "maxProperties": 64,
        "propertyNames": {"minLength": 1, "maxLength": 64},
        "additionalProperties": {"enum": list(values)},
    }


def _csv_options() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["delimiter", "encoding"],
        "properties": {
            "delimiter": {"enum": [",", ";", "\t", "|"]},
            "encoding": {"enum": ["utf-8", "cp1252"]},
        },
    }


SCHEMA: Final[dict[str, object]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://clinic-ops.invalid/interop/import-mapping-v1.json",
    "title": "Clinic Ops import profile and mapping contract v1",
    "description": (
        "A profile describes an uploaded source without patient values; a "
        "mapping binds source columns to import fields. Competitor-specific "
        "layouts are out of scope until EG-15 samples exist."
    ),
    "oneOf": [{"$ref": "#/$defs/mapping"}, {"$ref": "#/$defs/profile"}],
    "$defs": {
        "column": {"type": "string", "minLength": 1, "maxLength": 128},
        "mapping": {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema", "source_kind", "datasets"],
            "properties": {
                "schema": {"const": SCHEMA_NAME},
                "source_kind": {"enum": list(SOURCE_KINDS)},
                "date_format": {"enum": list(DATE_FORMATS)},
                "datetime_format": {"enum": list(DATETIME_FORMATS)},
                "csv": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "patients": _csv_options(),
                        "history": _csv_options(),
                    },
                },
                "datasets": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["patients"],
                    "properties": {
                        "patients": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["columns"],
                            "properties": {
                                "columns": _columns(PATIENT_FIELDS, ["source_id"]),
                                "values": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "sex_at_birth": _value_map(SEX_VALUES)
                                    },
                                },
                            },
                        },
                        "history": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["columns"],
                            "properties": {
                                "columns": _columns(
                                    HISTORY_FIELDS, list(HISTORY_REQUIRED)
                                ),
                                "values": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {"kind": _value_map(HISTORY_KINDS)},
                                },
                            },
                        },
                    },
                },
            },
        },
        "profile": {
            "type": "object",
            "additionalProperties": False,
            "required": ["schema", "source_kind", "datasets", "attachments"],
            "properties": {
                "schema": {"const": PROFILE_NAME},
                "source_kind": {"enum": list(SOURCE_KINDS)},
                "datasets": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        name: {"$ref": "#/$defs/dataset_profile"}
                        for name in ("patients", "history")
                    },
                },
                "attachments": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["count", "clean", "rejected", "bytes"],
                    "properties": {
                        key: {"type": "integer", "minimum": 0}
                        for key in ("count", "clean", "rejected", "bytes")
                    },
                },
            },
        },
        "dataset_profile": {
            "type": "object",
            "additionalProperties": False,
            "required": ["row_count", "defective_rows", "columns"],
            "properties": {
                "row_count": {"type": "integer", "minimum": 0},
                "defective_rows": {"type": "integer", "minimum": 0},
                "csv": _csv_options(),
                "columns": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["header", "non_empty", "max_length", "kinds"],
                        "properties": {
                            "header": {"$ref": "#/$defs/column"},
                            "non_empty": {"type": "integer", "minimum": 0},
                            "max_length": {"type": "integer", "minimum": 0},
                            "kinds": {
                                "type": "array",
                                "uniqueItems": True,
                                "items": {
                                    "enum": [
                                        "empty",
                                        "integer",
                                        "decimal",
                                        "date",
                                        "datetime",
                                        "cpf_like",
                                        "text",
                                    ]
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


class _Validator(Protocol):
    def is_valid(self, instance: object) -> bool: ...


def _validator(definition: str) -> _Validator:
    library = import_module("jsonschema")
    schema = {"$ref": f"#/$defs/{definition}", "$defs": SCHEMA["$defs"]}
    validator: _Validator = library.Draft202012Validator(schema)
    return validator


_MAPPING_VALIDATOR: Final = _validator("mapping")
_PROFILE_VALIDATOR: Final = _validator("profile")


def document_digest(document: Mapping[str, object]) -> str:
    """Hash one canonical JSON document (RFC 8785) for approval binding."""
    return hashlib.sha256(rfc8785.dumps(cast("Any", dict(document)))).hexdigest()


def validate_mapping(document: object) -> dict[str, object]:
    """Refuse any mapping outside the closed contract, then its semantics."""
    if not isinstance(document, dict) or not _MAPPING_VALIDATOR.is_valid(document):
        raise ImportInputError(Code.INVALID_MAPPING)
    datasets = document["datasets"]
    patients = datasets["patients"]["columns"]
    if "legal_name" not in patients and "social_name" not in patients:
        raise ImportInputError(Code.INVALID_MAPPING)
    if "history" in datasets and document["source_kind"] not in ("zip", "api"):
        raise ImportInputError(Code.INVALID_MAPPING)
    for dataset in datasets.values():
        columns = list(dataset["columns"].values())
        if len(set(columns)) != len(columns):
            raise ImportInputError(Code.INVALID_MAPPING)
    return document


def validate_profile(document: object) -> bool:
    """Whether a profile report satisfies the committed contract."""
    return _PROFILE_VALIDATOR.is_valid(document)


def mapped_columns_present(
    document: Mapping[str, object], datasets: Mapping[str, Dataset]
) -> bool:
    """Whether every mapped column exists in the profiled source headers."""
    mapped = document["datasets"]
    if not isinstance(mapped, dict):
        return False
    for name, dataset in mapped.items():
        source = datasets.get(name)
        if source is None or not isinstance(dataset, dict):
            return False
        columns = dataset["columns"]
        if not isinstance(columns, dict) or not set(columns.values()) <= set(
            source.headers
        ):
            return False
    return True


# Advisory header vocabulary (pt-BR and English); never patient values.
_SYNONYMS: Final = {
    "patients": {
        "source_id": (
            "codigo",
            "cod",
            "id",
            "prontuario",
            "numero prontuario",
            "registro",
            "matricula",
            "source id",
            "patient id",
        ),
        "legal_name": (
            "nome",
            "nome completo",
            "paciente",
            "nome do paciente",
            "name",
            "full name",
            "legal name",
        ),
        "social_name": ("nome social", "social name"),
        "birth_date": (
            "nascimento",
            "data de nascimento",
            "data nascimento",
            "dt nascimento",
            "dn",
            "birth date",
            "date of birth",
            "dob",
        ),
        "cpf": ("cpf", "documento cpf"),
        "cns": ("cns", "cartao sus", "cartao nacional de saude"),
        "sex_at_birth": ("sexo", "sexo ao nascer", "sex", "sex at birth"),
        "registered_at": (
            "data cadastro",
            "data de cadastro",
            "cadastrado em",
            "registered at",
            "created at",
        ),
        "registered_by": ("cadastrado por", "registered by", "created by"),
    },
    "history": {
        "source_id": ("codigo", "id", "registro", "source id"),
        "patient_source_id": (
            "codigo paciente",
            "paciente",
            "prontuario",
            "patient id",
            "patient source id",
        ),
        "kind": ("tipo", "type", "kind"),
        "recorded_at": (
            "data",
            "data hora",
            "datahora",
            "data do atendimento",
            "recorded at",
            "date",
        ),
        "author": ("autor", "profissional", "medico", "author", "clinician"),
        "text": ("texto", "evolucao", "nota", "anotacao", "descricao", "text", "note"),
        "code": ("medida", "sinal vital", "codigo medida", "code"),
        "value": ("valor", "resultado", "value"),
        "unit": ("unidade", "unit"),
        "file": ("arquivo", "anexo", "file", "attachment"),
        "title": ("titulo", "nome do arquivo", "title"),
    },
}
_VALUE_HINTS: Final = {
    "sex_at_birth": {
        "F": "female",
        "M": "male",
        "Feminino": "female",
        "Masculino": "male",
        "Intersexo": "intersex",
        "Nao informado": "not_informed",
    },
    "kind": {
        "Evolucao": "note",
        "Nota": "note",
        "Sinal vital": "observation",
        "Medida": "observation",
        "Anexo": "attachment",
    },
}


def _normalized_header(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(c for c in folded if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", plain).split())


@dataclass(frozen=True, slots=True)
class MappingSuggestion:
    """An advisory mapping draft; it is never applied without a saved version."""

    document: dict[str, object]
    confidence: dict[str, str]
    method: str = "deterministic_header_vocabulary"
    advisory: bool = True


def suggest(
    *,
    source_kind: str,
    datasets: Mapping[str, Dataset],
    csv_options: Mapping[str, object],
) -> MappingSuggestion:
    """Match profiled headers against the fixed vocabulary, exact before partial."""
    document: dict[str, object] = {
        "schema": SCHEMA_NAME,
        "source_kind": source_kind,
        "date_format": "DD/MM/YYYY",
        "datetime_format": "DD/MM/YYYY HH:MM",
    }
    if csv_options:
        document["csv"] = dict(csv_options)
    mapped: dict[str, object] = {}
    confidence: dict[str, str] = {}
    for name, dataset in datasets.items():
        headers = {_normalized_header(header): header for header in dataset.headers}
        columns: dict[str, str] = {}
        used: set[str] = set()
        for field, synonyms in _SYNONYMS[name].items():
            exact = next(
                (
                    headers[s]
                    for s in synonyms
                    if s in headers and headers[s] not in used
                ),
                None,
            )
            partial = next(
                (
                    header
                    for key, header in headers.items()
                    if header not in used
                    and any(
                        len(s) >= MIN_PARTIAL_SYNONYM and s in key for s in synonyms
                    )
                ),
                None,
            )
            chosen = exact or partial
            if chosen is not None:
                columns[field] = chosen
                used.add(chosen)
                confidence[f"{name}.{field}"] = "high" if exact else "low"
        entry: dict[str, object] = {"columns": columns}
        hint = "sex_at_birth" if name == "patients" else "kind"
        if hint in columns:
            entry["values"] = {hint: dict(_VALUE_HINTS[hint])}
        mapped[name] = entry
    document["datasets"] = mapped
    return MappingSuggestion(document=document, confidence=confidence)
