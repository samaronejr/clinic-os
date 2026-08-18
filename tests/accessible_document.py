from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
from typing import Final

import pytest

LABELABLE: Final = frozenset({"input", "select", "textarea"})
UNLABELLED_INPUT_TYPES: Final = frozenset({"hidden", "submit", "button"})


class Document(HTMLParser):
    """Parse one rendered response into its accessibility-relevant shape."""

    def __init__(self, content: bytes) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.labels: list[dict[str, str | None]] = []
        self.feed(content.decode())

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if tag == "label":
            self.labels.append(attributes)

    def identifiers(self) -> list[str]:
        return [
            element_id
            for _tag, attributes in self.elements
            if (element_id := attributes.get("id")) is not None
        ]

    def attributes_for(self, element_id: str) -> dict[str, str | None]:
        for _tag, attributes in self.elements:
            if attributes.get("id") == element_id:
                return attributes
        pytest.fail(f"missing element #{element_id}")

    def tagged(self, tag: str) -> list[dict[str, str | None]]:
        return [attributes for name, attributes in self.elements if name == tag]

    def assert_unique_identifiers(self) -> None:
        duplicates = [
            value for value, count in Counter(self.identifiers()).items() if count > 1
        ]
        assert duplicates == []

    def assert_descriptions_resolve(self) -> None:
        known = set(self.identifiers())
        for _tag, attributes in self.elements:
            described_by = attributes.get("aria-describedby")
            if described_by is not None:
                assert set(described_by.split()) <= known
            labelled_by = attributes.get("aria-labelledby")
            if labelled_by is not None:
                assert set(labelled_by.split()) <= known

    def assert_every_control_is_labelled(self) -> None:
        label_targets = {
            target for label in self.labels if (target := label.get("for")) is not None
        }
        for tag, attributes in self.elements:
            if tag not in LABELABLE:
                continue
            if attributes.get("type") in UNLABELLED_INPUT_TYPES:
                continue
            element_id = attributes.get("id")
            assert element_id is not None
            assert element_id in label_targets

    def assert_every_form_is_post_with_csrf(self) -> None:
        forms = self.tagged("form")
        assert forms
        for form in forms:
            assert (form.get("method") or "").lower() == "post"
            action = form.get("action") or ""
            assert "?" not in action
        tokens = [
            attributes
            for attributes in self.tagged("input")
            if attributes.get("name") == "csrfmiddlewaretoken"
        ]
        assert len(tokens) == len(forms)
