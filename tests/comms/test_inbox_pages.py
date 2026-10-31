"""Static contracts of the inbox pages that need no database: copy and catalog."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

import pytest
from django.utils.translation import gettext, override

ROOT: Final = Path(__file__).resolve().parents[2]
TEMPLATES: Final = ROOT / "templates" / "comms"
PAGES: Final = ("inbox.html", "conversation.html")
PYTHON: Final = (
    ROOT / "apps" / "comms" / "inbox_views.py",
    ROOT / "apps" / "comms" / "inbox_queries.py",
)
CATALOG: Final = ROOT / "locale" / "pt_BR" / "LC_MESSAGES" / "django.po"
# Brand and proper names stay as they are in every language.
BRAND: Final = frozenset({"Clinic", "OS"})
SAME_IN_PORTUGUESE: Final = frozenset({"WhatsApp", "SMS", "Portal", "Marketing"})
TRANS_TAG: Final = re.compile(
    r"\{% trans (?:\"((?:[^\"\\]|\\.)*)\"|'([^']*)')(?: context \"([^\"]*)\")? %\}"
)
BLOCK_TAG: Final = re.compile(
    r"\{% blocktrans[^%]*%\}(.*?)\{% endblocktrans %\}", re.DOTALL
)
GETTEXT_CALLS: Final = {"_", "gettext", "gettext_lazy"}
PGETTEXT_CALLS: Final = {"pgettext", "pgettext_lazy"}


def _plain_text(source: str) -> list[str]:
    """Words a page would print without going through the catalog."""
    source = BLOCK_TAG.sub("", source)
    source = re.sub(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", "", source, flags=re.DOTALL)
    attributes = re.findall(r'(?:aria-label|title|placeholder|alt)="([^"]*)"', source)
    text = re.sub(r"<[^>]*>", " ", source)
    words = re.findall(r"[^\W\d_]{2,}", " ".join([text, *attributes]))
    return [word for word in words if word not in BRAND]


def _template_msgids(source: str) -> set[tuple[str | None, str]]:
    found: set[tuple[str | None, str]] = set()
    for match in TRANS_TAG.finditer(source):
        text = (match[1] or match[2]).replace('\\"', '"')
        found.add((match[3] or None, text))
    for block in BLOCK_TAG.finditer(source):
        body = re.sub(r"\{\{\s*(\w+)\s*\}\}", r"%(\1)s", block[1].strip())
        found.add((None, body))
    return found


def _python_msgids(path: Path) -> set[tuple[str | None, str]]:
    found: set[tuple[str | None, str]] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        strings = [
            arg.value
            for arg in node.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        ]
        if node.func.id in GETTEXT_CALLS and len(strings) == 1:
            found.add((None, strings[0]))
        elif node.func.id in PGETTEXT_CALLS and len(strings) == 2:
            found.add((strings[0], strings[1]))
    return found


def _catalog() -> dict[tuple[str | None, str], str]:
    text = CATALOG.read_text()
    entries = re.finditer(
        r'^(?:msgctxt "((?:[^"\\]|\\.)*)"\n)?msgid "((?:[^"\\]|\\.)*)"\n'
        r'((?:"(?:[^"\\]|\\.)*"\n)*)msgstr "((?:[^"\\]|\\.)*)"',
        text,
        re.MULTILINE,
    )
    return {
        (entry[1], entry[2].replace('\\"', '"')): entry[4].replace('\\"', '"')
        for entry in entries
    }


@pytest.mark.parametrize("page", PAGES)
def test_each_inbox_page_carries_the_synthetic_disclosure(page: str) -> None:
    source = (TEMPLATES / page).read_text(encoding="utf-8")
    assert "data-synthetic-disclosure" in source


@pytest.mark.parametrize("page", PAGES)
def test_no_inbox_page_prints_text_outside_the_catalog(page: str) -> None:
    source = (TEMPLATES / page).read_text(encoding="utf-8")
    assert _plain_text(source) == []


def test_every_inbox_msgid_has_a_portuguese_translation() -> None:
    msgids: set[tuple[str | None, str]] = set()
    for page in PAGES:
        msgids |= _template_msgids((TEMPLATES / page).read_text(encoding="utf-8"))
    for path in PYTHON:
        msgids |= _python_msgids(path)
    catalog = _catalog()
    assert len(msgids) > 100
    untranslated = sorted(
        (context or "", msgid)
        for context, msgid in msgids
        if not catalog.get((context, msgid))
        or (catalog[(context, msgid)] == msgid and msgid not in SAME_IN_PORTUGUESE)
    )
    assert untranslated == []


def test_the_disclosure_is_translated_to_portuguese() -> None:
    disclosure = (
        "Synthetic integration: messages are simulated and never reach a real "
        "provider. Sent, delivered and read states here come from simulated "
        "acknowledgements, not from real delivery."
    )
    with override("pt-br"):
        assert gettext(disclosure) != disclosure
