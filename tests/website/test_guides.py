"""Help guides stay bound to the real screens they describe.

Each step names a URL, the template that renders the control, the screen title
and the control's exact source text. If a route, template, title or label
changes, the guide fails here rather than describing a button that is gone.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from apps.website.guides import AUDIENCES, FIRST_STEPS, GUIDES, Label, Step
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.urls.converters import (
    IntConverter,
    SlugConverter,
    StringConverter,
    UUIDConverter,
)
from django.utils import translation
from django.utils.html import escape

if TYPE_CHECKING:
    from apps.website.guides import Guide

ROOT: Final = Path(__file__).resolve().parents[2]
TEMPLATES: Final = ROOT / "templates"
OK: Final = 200
STEPS: Final = [
    pytest.param(guide, index, step, id=f"{guide.slug}-{index + 1}")
    for guide in GUIDES
    for index, step in enumerate(guide.steps)
]


def _source(template: str) -> str:
    path = TEMPLATES / template
    assert path.is_file(), template
    return path.read_text(encoding="utf-8")


def _renders(label: Label, template: str) -> bool:
    source = _source(template)
    if label.translated:
        # {% trans "msgid" %}, optionally bound to a variable a component renders.
        tag = rf'\{{% trans "{re.escape(label.source)}"(?: as \w+)? %\}}'
        return re.search(tag, source) is not None
    return re.search(rf">\s*{re.escape(label.source)}\s*<", source) is not None


def _sample(converter: object) -> object:
    if isinstance(converter, UUIDConverter):
        return uuid4()
    if isinstance(converter, IntConverter):
        return 1
    if isinstance(converter, (SlugConverter, StringConverter)):
        return "synthetic"
    pytest.fail(f"uninspected converter {type(converter).__name__}")


def _pattern(url_name: str) -> URLPattern:
    namespace, name = url_name.split(":")
    resolver = get_resolver()
    for entry in resolver.url_patterns:
        if isinstance(entry, URLResolver) and entry.namespace == namespace:
            for pattern in entry.url_patterns:
                if isinstance(pattern, URLPattern) and pattern.name == name:
                    return pattern
    pytest.fail(f"no route named {url_name}")


@pytest.mark.parametrize(("guide", "index", "step"), STEPS)
def test_each_step_points_at_a_real_screen_and_control(
    guide: Guide, index: int, step: Step
) -> None:
    pattern = _pattern(step.screen.url_name)
    kwargs = {key: _sample(value) for key, value in pattern.pattern.converters.items()}

    assert reverse(step.screen.url_name, kwargs=kwargs)
    assert _renders(step.screen.title, step.screen.template), step.screen.title
    assert _renders(step.control, step.template), (guide.slug, index, step.control)


@pytest.mark.parametrize(("guide", "index", "step"), STEPS)
def test_each_instruction_names_its_control_once_in_both_languages(
    guide: Guide, index: int, step: Step
) -> None:
    assert step.text.count("{control}") == 1, (guide.slug, index)
    with translation.override("pt-br"):
        translated = translation.gettext(step.text)
        sentence = str(step.sentence())
        label = step.control.text

    assert translated != step.text
    assert translated.count("{control}") == 1
    assert f'<strong class="help-control">{escape(label)}</strong>' in sentence


def _guide_strings(guide: Guide) -> list[str]:
    strings = [guide.title, guide.summary, *guide.before, *guide.notes]
    for step in guide.steps:
        strings.append(step.text)
        if step.result:
            strings.append(step.result)
    return strings


def test_every_guide_string_reads_in_pt_br() -> None:
    strings = [text for guide in GUIDES for text in _guide_strings(guide)]
    strings += list(AUDIENCES.values())
    with translation.override("pt-br"):
        untranslated = [text for text in strings if translation.gettext(text) == text]

    assert untranslated == []


def test_registry_shape_and_onboarding_path() -> None:
    slugs = [guide.slug for guide in GUIDES]

    assert len(slugs) == len(set(slugs))
    assert {guide.audience for guide in GUIDES} <= set(AUDIENCES)
    assert len(FIRST_STEPS) == len(set(FIRST_STEPS))
    assert set(FIRST_STEPS) <= set(slugs)
    assert FIRST_STEPS[0] == "invite-your-team"


@pytest.mark.parametrize("guide", GUIDES, ids=[guide.slug for guide in GUIDES])
def test_guide_page_shows_every_step_with_the_screens_labels(guide: Guide) -> None:
    response = Client().get(reverse("website:guide", args=[guide.slug]))

    assert response.status_code == OK
    body = response.content.decode()
    assert body.count("data-step>") == len(guide.steps)
    with translation.override("pt-br"):
        for step in guide.steps:
            assert escape(step.control.text) in body
            assert escape(step.screen.title.text) in body
