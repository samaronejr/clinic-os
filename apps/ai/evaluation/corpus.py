"""Code-versioned engineering fixtures; real-world corpora cannot enter this loader."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from typing import Final, TypedDict, cast

from apps.ai.evaluation.protocol import EvaluationInputError

CAPABILITIES: Final = tuple(f"AI-{number:02}" for number in range(1, 9))
ADVERSARIAL_TAGS: Final = frozenset(
    {
        "accents",
        "noise",
        "caregiver_child",
        "interruptions",
        "conflicting_records",
        "mic_failure",
        "decimal_commas",
        "ambiguous_abbreviations",
        "wrong_patient_attachments",
        "stale_permissions",
        "malicious_content",
        "negation",
    }
)
ROOT: Final = Path(__file__).parent
MINIMUM_ENGINEERING_ENCOUNTERS: Final = 4
MAX_AUDIO_BYTES: Final = 24000
MAX_AUDIO_REGISTRY_BYTES: Final = 262144


class Case(TypedDict):
    """A synthetic encounter cluster, not a patient record selector."""

    id: str
    encounter: str
    specialty: str
    device: str
    accent: str
    adversarial: str
    script: str
    audio_fixture: str


class Manifest(TypedDict):
    """The version and lawful origin are mandatory machine-consumed fields."""

    schema_version: int
    version: str
    data_class: str
    lawful_provenance: str
    capabilities: list[str]
    cases: list[Case]


class AudioFixture(TypedDict):
    """Small, hashed offline speech bytes; no runtime synthesis is possible."""

    voice: str
    transform: str
    script_sha256: str
    audio_sha256: str
    encoding: str
    audio_base64: str


@dataclass(frozen=True, slots=True)
class Corpus:
    """Hash exact bytes, including scenario metadata and fixture identifiers."""

    manifest: Manifest
    sha256: str

    @property
    def cases(self) -> tuple[Case, ...]:
        """Do not expose a mutable alias of the parsed manifest."""
        return tuple(case.copy() for case in self.manifest["cases"])


def parse_manifest(content: bytes) -> Corpus:
    """Reject schema drift, duplicate cases and missing adversarial coverage."""
    try:
        raw = json.loads(content)
        schema = json.loads((ROOT / "corpus.schema.json").read_text())
        import_module("jsonschema").Draft202012Validator(schema).validate(raw)
    except (ValueError, import_module("jsonschema").ValidationError):
        raise EvaluationInputError from None
    manifest = cast("Manifest", raw)
    cases = manifest["cases"]
    if (
        manifest["data_class"] != "synthetic"
        or manifest["capabilities"] != list(CAPABILITIES)
        or len({case["id"] for case in cases}) != len(cases)
        or {case["adversarial"] for case in cases} != ADVERSARIAL_TAGS
        or len({case["encounter"] for case in cases}) < MINIMUM_ENGINEERING_ENCOUNTERS
    ):
        raise EvaluationInputError
    return Corpus(manifest, sha256(content).hexdigest())


def load_corpus(version: str) -> Corpus:
    """Use a closed registry to prevent traversal and unapproved corpus imports."""
    if version != "engineering-v1":
        raise EvaluationInputError
    content = (ROOT / "corpora" / "engineering-v1.json").read_bytes()
    parsed = parse_manifest(content)
    audio_content, fixtures = _audio_registry()
    if set(fixtures) != {case["id"] for case in parsed.cases}:
        raise EvaluationInputError
    for case in parsed.cases:
        _validated_audio(case, fixtures[case["audio_fixture"]])
    return Corpus(parsed.manifest, sha256(content + b"\0" + audio_content).hexdigest())


def _audio_registry() -> tuple[bytes, dict[str, AudioFixture]]:
    content = (ROOT / "corpora" / "audio-v1.json").read_bytes()
    if len(content) > MAX_AUDIO_REGISTRY_BYTES:
        raise EvaluationInputError
    raw = json.loads(content)
    if (
        not isinstance(raw, dict)
        or set(raw) != {"schema_version", "provenance", "fixtures"}
        or raw["schema_version"] != 1
        or not isinstance(raw["provenance"], str)
        or not raw["provenance"]
        or not isinstance(raw["fixtures"], dict)
    ):
        raise EvaluationInputError
    return content, cast("dict[str, AudioFixture]", raw["fixtures"])


def _validated_audio(case: Case, fixture: AudioFixture) -> bytes:
    if (
        set(fixture)
        != {
            "voice",
            "transform",
            "script_sha256",
            "audio_sha256",
            "encoding",
            "audio_base64",
        }
        or any(not isinstance(value, str) for value in fixture.values())
        or fixture["voice"] != case["accent"].removeprefix("flite-")
        or fixture["transform"] != case["adversarial"]
        or fixture["script_sha256"] != sha256(case["script"].encode()).hexdigest()
        or fixture["encoding"] != "ogg-opus"
    ):
        raise EvaluationInputError
    try:
        audio = base64.b64decode(fixture["audio_base64"], validate=True)
    except ValueError:
        raise EvaluationInputError from None
    if (
        not audio.startswith(b"OggS")
        or not 0 < len(audio) <= MAX_AUDIO_BYTES
        or sha256(audio).hexdigest() != fixture["audio_sha256"]
    ):
        raise EvaluationInputError
    return audio


def case_audio(case: Case) -> bytes:
    """Read static fixture bytes; tests and CI never need Flite or FFmpeg."""
    _, fixtures = _audio_registry()
    if case["audio_fixture"] not in fixtures:
        raise EvaluationInputError
    return _validated_audio(case, fixtures[case["audio_fixture"]])
