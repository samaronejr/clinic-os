"""Render the real extraction template without staff sessions, servers, or DB I/O."""

from __future__ import annotations

import base64
import json
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from apps.ai.extraction_contracts import parse_proposals
from apps.ai.extraction_documents import (
    document_input,
    render_page,
    synthetic_document_extraction,
)
from django.template import Context, Engine
from PIL import Image

from ai.test_extraction import pdf_fixture

ROOT = Path(__file__).resolve().parents[2]
CLINIC = UUID("10000000-0000-0000-0000-000000000045")
JOB = UUID("20000000-0000-0000-0000-000000000045")
CANDIDATE = UUID("30000000-0000-0000-0000-000000000045")
VERSION = UUID("40000000-0000-0000-0000-000000000045")


def render_fixture(*, status: str = "candidate", raw_text: str | None = None) -> str:
    document = pdf_fixture()
    proposed = json.loads(
        synthetic_document_extraction(
            document_input(data=document, content_type="application/pdf")
        )
    )["candidates"][0]
    if raw_text:
        proposed["raw_text"] = raw_text
    if status == "ambiguous":
        proposed["unit"] = ""
    source = parse_proposals(json.dumps({"candidates": [proposed]}), rotations=(0,))[0]
    engine = Engine(
        dirs=[str(ROOT / "templates")],
        loaders=[
            (
                "django.template.loaders.locmem.Loader",
                {
                    "base.html": (
                        '<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">'
                        '<meta name="viewport" content="width=device-width">'
                        "<title>{% block title %}{% endblock %}</title>"
                        "{% block extra_head %}{% endblock %}</head>"
                        "<body><main>{% block content %}{% endblock %}"
                        "</main></body></html>"
                    )
                },
            ),
            "django.template.loaders.filesystem.Loader",
        ],
        libraries={
            "i18n": "django.templatetags.i18n",
            "static": "django.templatetags.static",
        },
    )
    raster = render_page(data=document, content_type="application/pdf", page=1)
    with Image.open(BytesIO(raster)) as image:
        width, height = image.size
    left, top, right, bottom = source.region.bbox
    return engine.get_template("ai/extraction.html").render(
        Context(
            {
                "clinic_id": CLINIC,
                "csrf_token": "synthetic-csrf",
                "job": SimpleNamespace(id=JOB, result_version_id=VERSION),
                "rows": [
                    {
                        "record": SimpleNamespace(
                            id=CANDIDATE, position=0, status=status, reviewed_at=None
                        ),
                        "candidate": source,
                        "on_page": True,
                        "x": round(left / 10000 * width),
                        "y": round(top / 10000 * height),
                        "width": round((right - left) / 10000 * width),
                        "height": round((bottom - top) / 10000 * height),
                    }
                ],
                "page": 1,
                "pages": (1,),
                "zoom": 100,
                "zooms": (100, 200, 400),
                "page_image": "data:image/png;base64,"
                + base64.b64encode(raster).decode(),
                "image_width": width,
                "image_height": height,
            }
        )
    )


class _Surface(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


def test_native_review_posts_selectors_and_binds_each_region() -> None:
    html = render_fixture()
    surface = _Surface(html)
    forms = [attrs for tag, attrs in surface.elements if tag == "form"]
    assert forms
    assert all(attrs["method"] == "post" for attrs in forms)
    inputs = [attrs for tag, attrs in surface.elements if tag == "input"]
    assert any(attrs.get("name") == "candidate_id" for attrs in inputs)
    assert any(attrs.get("name") == "csrfmiddlewaretoken" for attrs in inputs)
    assert all(
        str(CANDIDATE) not in (attrs.get("href") or "")
        for _tag, attrs in surface.elements
    )
    assert any(
        attrs.get("href") == "#candidate-0"
        for tag, attrs in surface.elements
        if tag == "a"
    )
    assert any(tag == "rect" for tag, _attrs in surface.elements)
    assert not any("style" in attrs for _tag, attrs in surface.elements)


def test_document_zoom_has_a_native_control_and_keeps_geometry_in_one_viewbox() -> None:
    surface = _Surface(render_fixture())
    assert any(
        tag == "select" and attrs.get("name") == "zoom"
        for tag, attrs in surface.elements
    )
    assert any(attrs.get("data-zoom") == "100" for _tag, attrs in surface.elements)
    assert len([tag for tag, _attrs in surface.elements if tag == "svg"]) == 1


def test_ambiguous_and_terminal_candidates_have_no_confirmation_action() -> None:
    for status in ("ambiguous", "confirmed", "rejected"):
        surface = _Surface(render_fixture(status=status))
        confirmation = [
            attrs
            for tag, attrs in surface.elements
            if tag == "button" and attrs.get("value") == "confirmed"
        ]
        assert not confirmation or "disabled" in confirmation[0]


def test_source_markup_is_escaped_and_never_put_into_data_attributes() -> None:
    sentinel = "<script>synthetic()</script> 123,40 mg/dL [70,0 - 99,0]"
    html = render_fixture(raw_text=sentinel)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    surface = _Surface(html)
    assert all(
        "synthetic()" not in (value or "")
        for _tag, attrs in surface.elements
        for name, value in attrs.items()
        if name.startswith("data-")
    )
