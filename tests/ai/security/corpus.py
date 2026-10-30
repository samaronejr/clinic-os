"""Versioned, synthetic-only source payloads and an intentionally obedient model."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from apps.ai.action_payload import JSON, ActionPayload, closed, integer, text
from apps.ai.untrusted import UntrustedContent

type Surface = Literal["transcript", "pdf", "email", "portal_message", "kb_page"]
type Variant = Literal["plain", "nested", "base64", "url_encoded"]

ORIGINS: dict[Surface, Literal["transcript", "document", "message", "retrieval"]] = {
    "transcript": "transcript",
    "pdf": "document",
    "email": "message",
    "portal_message": "message",
    "kb_page": "retrieval",
}
VARIANTS: tuple[Variant, ...] = ("plain", "nested", "base64", "url_encoded")


@dataclass(frozen=True, slots=True)
class AttackCase:
    """A source fixture declares the malicious response of the worst-case model."""

    identifier: str
    surface: Surface
    language: str
    content: str
    model_patch: dict[str, JSON]

    def source(self, variant: Variant) -> UntrustedContent:
        """Keep nested and encoded instructions untrusted without decoding them."""
        match variant:
            case "plain":
                encoded = self.content
            case "nested":
                encoded = json.dumps(
                    {"quoted_document": {"messages": [{"content": self.content}]}},
                    ensure_ascii=False,
                )
            case "base64":
                encoded = base64.b64encode(self.content.encode()).decode("ascii")
            case "url_encoded":
                encoded = quote(self.content, safe="")
        return UntrustedContent(ORIGINS[self.surface], encoded)


def corpus() -> tuple[AttackCase, ...]:
    cases = []
    for path in sorted((Path(__file__).parent / "corpus").glob("*.json")):
        raw: JSON = json.loads(path.read_text(encoding="utf-8"))
        row = closed(
            raw,
            {
                "schema_version",
                "data_class",
                "id",
                "surface",
                "language",
                "text",
                "model_patch",
            },
        )
        assert integer(row["schema_version"], maximum=1) == 1
        assert row["data_class"] == "synthetic"
        identifier = text(row["id"])
        assert identifier == path.stem
        surface = row["surface"]
        assert surface in ORIGINS
        assert surface in ("transcript", "pdf", "email", "portal_message", "kb_page")
        language = text(row["language"])
        assert language in ("en", "pt-BR")
        patch = row["model_patch"]
        assert isinstance(patch, dict)
        cases.append(
            AttackCase(
                identifier, surface, language, text(row["text"], maximum=4096), patch
            )
        )
    assert cases
    assert len({case.identifier for case in cases}) == len(cases)
    return tuple(cases)


CASES = corpus()


@dataclass(slots=True)
class InjectionFollowingModel:
    """Ignore the trust label and emit exactly the attacker's requested effect."""

    inputs: list[dict[str, JSON]]

    def complete(
        self, case: AttackCase, *, source: UntrustedContent, seed: ActionPayload
    ) -> str:
        self.inputs.append(source.model_data())
        return json.dumps({**seed.data(), **case.model_patch})
