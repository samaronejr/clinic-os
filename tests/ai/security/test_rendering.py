"""Persisted hostile excerpts are inert text on both real chart response paths."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import TYPE_CHECKING

import pytest
from apps.ai.models import AIInvocation
from apps.ai.tasks import run_operation
from apps.ehr.history import HistoryChange, save_history
from apps.tenancy.db import tenant_context
from django.template import Context, Engine
from django.urls import reverse
from django.utils.html import escape

from ai.brief_support import seeded
from ai.security.corpus import CASES
from ehr.test_addenda import signed_in
from ehr.test_autosave import as_actor

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

XSS = (
    "<script>window.syntheticExfil49=true</script>",
    '<img src="https://exfil.synthetic.invalid/x" '
    'onerror="window.syntheticExfil49=true">',
    '<svg onload="window.syntheticExfil49=true"></svg>',
    '<a href="javascript:window.syntheticExfil49=true">Synthetic</a>',
    next(case.content for case in CASES if case.identifier == "portal-message-pt"),
)


class RenderedHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []
        self.script_text: list[str] = []
        self.in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, attrs))
        self.in_script = tag == "script"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.in_script = False

    def handle_data(self, data: str) -> None:
        if self.in_script:
            self.script_text.append(data)


def assert_safe_rendering(content: str, rendered: str) -> None:
    """Use the same payload-sensitive oracle for actual responses and controls."""
    assert content not in rendered
    assert str(escape(content)) in rendered
    parsed = RenderedHTML()
    parsed.feed(rendered)
    assert "syntheticExfil" not in "".join(parsed.script_text)
    assert not any(
        key.casefold().startswith("on")
        or (
            key in {"href", "src"}
            and value is not None
            and (
                value.casefold().startswith("javascript:")
                or "exfil.synthetic.invalid" in value.casefold()
            )
        )
        for _tag, attrs in parsed.tags
        for key, value in attrs
    )


@pytest.mark.parametrize("content", XSS)
def test_safe_filter_negative_control_is_rejected_by_the_response_oracle(
    content: str,
) -> None:
    # A deliberately unsafe test-only template must fail the actual HTTP oracle.
    template = Engine().from_string("<article>{{ content|safe }}</article>")
    unsafe = template.render(Context({"content": content}))
    with pytest.raises(AssertionError):
        assert_safe_rendering(content, unsafe)
    escaped = Engine().from_string("<article>{{ content }}</article>")
    assert_safe_rendering(content, escaped.render(Context({"content": content})))


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("content", XSS)
@pytest.mark.parametrize("enhanced", [False, True], ids=["native", "htmx"])
def test_xss_in_authorized_source_is_escaped_on_the_actual_http_surface(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    enhanced: bool,
) -> None:
    world = seeded(rbac_graph, monkeypatch)
    g = world.graph
    with as_actor(g, g.physician):
        save_history(
            clinic_id=g.clinic_a,
            encounter_id=world.chart.version.document.encounter_id,
            change=HistoryChange(
                "problem", 0, "documented", content, "active", "Synthetic"
            ),
        )
    url = reverse("ai:chart-assistant", args=(g.clinic_a,))
    body = {
        "enrollment_id": str(world.chart.enrollment),
        "command": "generate",
        "surface": "workspace",
    }
    with signed_in(g.physician) as client:
        queued = client.post(url, body, HTTP_HX_REQUEST="true" if enhanced else "false")
        assert queued.status_code == 200
        with tenant_context(g.physician, g.organization_a):
            invocation = AIInvocation.objects.get(
                user_id=g.physician, clinic_id=g.clinic_a
            )
            operation = invocation.operation_id
        assert operation is not None
        assert run_operation(operation_id=str(operation)) == "succeeded"
        response = client.post(
            url,
            {**body, "command": "display", "invocation_id": str(invocation.pk)},
            HTTP_HX_REQUEST="true" if enhanced else "false",
        )
    assert response.status_code == 200
    assert b'data-chart-state="ready"' in response.content
    assert_safe_rendering(content, response.content.decode())
    assert "no-store" in response["Cache-Control"]
