"""Static copy contracts of the patient messages pages: catalog only, pt-BR complete."""

from __future__ import annotations

from typing import Final

import pytest

from comms.test_inbox_pages import (
    ROOT,
    TEMPLATES,
    _catalog,
    _plain_text,
    _python_msgids,
    _template_msgids,
)

PAGES: Final = ("patient_messages.html", "patient_thread.html")
PYTHON: Final = (
    ROOT / "apps" / "comms" / "portal_views.py",
    ROOT / "apps" / "intake" / "portal.py",
)


@pytest.mark.parametrize("page", PAGES)
def test_no_patient_messages_page_prints_text_outside_the_catalog(page: str) -> None:
    source = (TEMPLATES / page).read_text(encoding="utf-8")
    assert _plain_text(source) == []


def test_every_patient_messages_msgid_has_a_portuguese_translation() -> None:
    msgids: set[tuple[str | None, str]] = set()
    for page in PAGES:
        msgids |= _template_msgids((TEMPLATES / page).read_text(encoding="utf-8"))
    for path in PYTHON:
        msgids |= _python_msgids(path)
    catalog = _catalog()
    assert ("portal conversation state", "Closed") in msgids
    untranslated = sorted(
        (context or "", msgid)
        for context, msgid in msgids
        if not catalog.get((context, msgid)) or catalog[(context, msgid)] == msgid
    )
    assert untranslated == []
