"""Every template control that starts AI work renders only under its ai_enabled guard.

No AI affordance may be offered while the global switch or the organization
opt-in refuses its purpose. Each form that posts to an AI endpoint is
classified here. A command that admits new inference must sit inside an
``{% if %}`` on the variable bound by ``{% ai_enabled "<purpose>" as name %}``.
A reviewed command only displays, decides, saves or retires existing output.
Anything unclassified fails, so a new AI control cannot land unguarded.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from bisect import bisect_right
from pathlib import Path
from typing import Final

import pytest
from apps.ai.contracts import Purpose
from apps.ai.policy import enabled_policy
from apps.scribe import transcript_views

TEMPLATES: Final = Path(__file__).resolve().parents[2] / "templates"

# Endpoint: the purpose its guard must name, and the commands admitting inference.
STARTS: Final = {
    "ai:chart-assistant": ("brief", frozenset({"generate", "ask"})),
    "ai:decision-support": ("decision_support", frozenset({"generate"})),
    "ai:extraction": ("extraction", frozenset({"start"})),
    "ai:revenue-cycle": ("revenue_cycle", frozenset({"request"})),
    "ai:care-coordination": ("care_coordination", frozenset({"request"})),
    "analytics:reports": ("analyst", frozenset({"ask"})),
    "scribe:review": ("scribe_draft", frozenset({"generate", "regenerate_"})),
    "scribe:transcript": ("scribe_asr", frozenset({"queue"})),
}
# Commands of the same endpoints that never admit inference.
REVIEWED: Final = {
    "ai:chart-assistant": frozenset({"display"}),
    "ai:decision-support": frozenset({"accepted", "display", "modified", "rejected"}),
    "ai:extraction": frozenset({"confirmed", "refresh", "rejected"}),
    "ai:revenue-cycle": frozenset({"approve", "propose", "refresh", "reject"}),
    "ai:care-coordination": frozenset(
        {"approve", "decide", "open", "preview", "refresh"}
    ),
    "analytics:reports": frozenset(
        {"accepted", "cached", "drill", "load", "proposal", "refresh", "rejected"}
        | {"save"}
    ),
    "scribe:review": frozenset({"open", "refresh", "save"}),
    "scribe:transcript": frozenset(
        {"correct", "finalize", "open", "refresh", "relabel"}
    ),
}
# Governance pages configure, evaluate or audit AI and must stay reachable to
# turn it on; none of their commands admits inference. Capture
# (``scribe:control``) records consented audio only; ASR is the transcript queue.
GOVERNANCE: Final = frozenset(
    {
        "ai:agent-settings",
        "ai:agents",
        "ai:evaluation",
        "ai:front-desk-handoffs",
        "ai:front-desk-knowledge",
        "ai:settings",
    }
)
# A link to an assistant page is itself an offer of AI work.
ASSISTANT_LINKS: Final = {"ai:revenue-cycle": "revenue_cycle"}
# A view-computed context flag that guards a template exactly like an ai_enabled
# variable: the view derives it through enabled_policy for this purpose, so a
# DB-free render never queries the switches (see the test below).
VIEW_GUARDS: Final = {"scribe/transcript.html": {"available": "scribe_asr"}}
# AI pages whose forms post back to the page itself.
PAGES: Final = {
    "ai/agent_settings.html": "ai:agent-settings",
    "ai/automations.html": "ai:agents",
    "ai/care_coordination.html": "ai:care-coordination",
    "ai/evaluation.html": "ai:evaluation",
    "ai/extraction.html": "ai:extraction",
    "ai/front_desk_handoffs.html": "ai:front-desk-handoffs",
    "ai/front_desk_knowledge.html": "ai:front-desk-knowledge",
    "ai/revenue_cycle.html": "ai:revenue-cycle",
    "ai/settings.html": "ai:settings",
}

COMMENT: Final = re.compile(
    r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}|\{#.*?#\}", re.DOTALL
)
TAG: Final = re.compile(r"\{%-?\s*(?P<body>.*?)\s*-?%\}", re.DOTALL)
GUARD: Final = re.compile(r'ai_enabled\s+"(?P<purpose>\w+)"\s+as\s+(?P<name>\w+)')
FORM: Final = re.compile(r"<form\b(?P<open>[^>]*)>(?P<body>.*?)</form>", re.DOTALL)
URL: Final = re.compile(r"\{%\s*url\s+'(?P<name>[\w:-]+)'")
CONTROL: Final = re.compile(r"<(?:button|input)\b[^>]*>", re.DOTALL)
COMMAND: Final = re.compile(r'\bname="(?:action|command)"')
VALUE: Final = re.compile(r'\bvalue="(?P<value>[^"]*)"')
LINK: Final = re.compile(
    r"<a\b[^>]*\bhref=\"\{%\s*url\s+'(?P<name>[\w:-]+)'", re.DOTALL
)


def _ai(endpoint: str) -> bool:
    return endpoint.startswith("ai:") or endpoint in STARTS


class Scan:
    """One template's branch conditions and guard bindings, by offset."""

    def __init__(self, text: str, preset: dict[str, str] | None = None) -> None:
        self.text = COMMENT.sub(lambda match: " " * len(match.group()), text)
        self.offsets: list[int] = []
        self.states: list[tuple[tuple[str | None, ...], dict[str, str]]] = []
        stack: list[str | None] = []
        bound: dict[str, str] = dict(preset or {})
        for tag in TAG.finditer(self.text):
            body = tag["body"]
            if body.startswith("if "):
                stack.append(body[3:])
            elif body.startswith("elif "):
                stack[-1] = body[5:]
            elif body == "else":
                stack[-1] = None
            elif body == "endif":
                stack.pop()
            elif guard := GUARD.fullmatch(body):
                bound = {**bound, guard["name"]: guard["purpose"]}
            self.offsets.append(tag.end())
            self.states.append((tuple(stack), bound))

    def guarded(self, offset: int, purpose: str) -> bool:
        """Whether an enclosing branch requires a guard bound to ``purpose``."""
        index = bisect_right(self.offsets, offset) - 1
        if index < 0:
            return False
        stack, bound = self.states[index]
        for condition in stack:
            if condition is None or " or " in f" {condition} ":
                continue
            names = {term.strip() for term in condition.split(" and ")}
            if any(bound.get(name) == purpose for name in names):
                return True
        return False


def _commands(form: re.Match[str]) -> list[tuple[int, str]]:
    """Each submitted action/command value with its offset in the template."""
    found = []
    for control in CONTROL.finditer(form["body"]):
        value = VALUE.search(control.group())
        if COMMAND.search(control.group()) and value is not None:
            offset = form.start("body") + control.start()
            found.append((offset, value["value"].split("{{", 1)[0]))
    return found


def _form_problems(
    name: str, template: Scan, form: re.Match[str], starts: set[str]
) -> list[str]:
    target = URL.search(form["open"])
    endpoint = target["name"] if target else PAGES.get(name)
    if endpoint is None:
        unmapped = name.startswith("ai/")
        return [f"{name}: AI page form posts to an unmapped page"] if unmapped else []
    if not _ai(endpoint) or endpoint in GOVERNANCE:
        return []
    problems = []
    for offset, command in _commands(form):
        where = f"{name}: {endpoint} {command!r}"
        if endpoint in STARTS and command in STARTS[endpoint][1]:
            starts.add(endpoint)
            if not template.guarded(offset, STARTS[endpoint][0]):
                problems.append(f"{where} renders without its ai_enabled guard")
        elif command not in REVIEWED.get(endpoint, frozenset()):
            problems.append(f"{where} is an unclassified AI command")
    return problems


def scan(name: str, text: str) -> tuple[list[str], set[str]]:
    """Return unguarded or unclassified controls, and the AI starts found."""
    template = Scan(text, VIEW_GUARDS.get(name))
    problems: list[str] = []
    starts: set[str] = set()
    for form in FORM.finditer(template.text):
        problems += _form_problems(name, template, form, starts)
    for link in LINK.finditer(template.text):
        endpoint = link["name"]
        if endpoint in ASSISTANT_LINKS:
            starts.add(f"link {endpoint}")
            if not template.guarded(link.start(), ASSISTANT_LINKS[endpoint]):
                problems.append(f"{name}: link {endpoint} without its guard")
        elif _ai(endpoint) and endpoint not in GOVERNANCE:
            problems.append(f"{name}: unclassified AI link {endpoint}")
    return problems, starts


def test_every_ai_start_control_renders_under_its_guard() -> None:
    problems: list[str] = []
    found: set[str] = set()
    for path in sorted(TEMPLATES.rglob("*.html")):
        name = path.relative_to(TEMPLATES).as_posix()
        issues, starts = scan(name, path.read_text(encoding="utf-8"))
        problems += issues
        found |= starts
    assert problems == []
    # The scan reached every known AI start, so an empty result is meaningful.
    assert found == {*STARTS, *(f"link {endpoint}" for endpoint in ASSISTANT_LINKS)}


GUARDED_REQUEST = (
    '{% ai_enabled "care_coordination" as on %}{% if staff and on %}'
    '<form method="post" action="{% url \'ai:care-coordination\' clinic_id %}">'
    '<button name="action" value="request">x</button></form>{% endif %}'
)
REQUEST = (
    '<form method="post" action="{% url \'ai:care-coordination\' clinic_id %}">'
    '<button name="action" value="request">x</button></form>'
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (GUARDED_REQUEST, []),
        (REQUEST, ["ai:care-coordination 'request' renders without"]),
        (
            '{% ai_enabled "care_coordination" as on %}{% if on %}{% else %}'
            + REQUEST
            + "{% endif %}",
            ["ai:care-coordination 'request' renders without"],
        ),
        (
            '{% ai_enabled "brief" as on %}{% if on %}' + REQUEST + "{% endif %}",
            ["ai:care-coordination 'request' renders without"],
        ),
        (
            '{% ai_enabled "care_coordination" as on %}{% if on or staff %}'
            + REQUEST
            + "{% endif %}",
            ["ai:care-coordination 'request' renders without"],
        ),
        (
            '{% comment %}{% ai_enabled "care_coordination" as on %}'
            "{% if on %}{% endcomment %}" + REQUEST,
            ["ai:care-coordination 'request' renders without"],
        ),
        (
            REQUEST.replace('value="request"', 'value="summarize"'),
            ["ai:care-coordination 'summarize' is an unclassified"],
        ),
        (
            "<a href=\"{% url 'ai:revenue-cycle' clinic_id %}\">x</a>",
            ["link ai:revenue-cycle without its guard"],
        ),
    ],
)
def test_the_scan_flags_unguarded_and_unclassified_controls(
    text: str, expected: list[str]
) -> None:
    problems, _starts = scan("probe.html", text)
    assert len(problems) == len(expected), problems
    for problem, fragment in zip(problems, expected, strict=True):
        assert fragment in problem


def _calls_policy(function: object, purpose: str) -> bool:
    """Whether the function body itself calls enabled_policy(Purpose.<purpose>)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))  # type: ignore[arg-type]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        callee = node.func
        attr = getattr(callee, "attr", "")
        name = callee.id if isinstance(callee, ast.Name) else attr
        target = node.args[0]
        if (
            name == "enabled_policy"
            and isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "Purpose"
            and target.attr == purpose
        ):
            return True
    return False


def _assigns_from(function: object, key: str, provider: str) -> bool:
    """Whether the view stores context[key] from a direct call to provider."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))  # type: ignore[arg-type]
    return any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Subscript)
            and isinstance(target.slice, ast.Constant)
            and target.slice.value == key
            for target in node.targets
        )
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == provider
        for node in ast.walk(tree)
    )


def test_view_guards_are_derived_from_the_purpose_policy() -> None:
    # Each registered flag is stored from its provider, which calls the policy.
    assert VIEW_GUARDS == {"scribe/transcript.html": {"available": "scribe_asr"}}
    assert _assigns_from(transcript_views.transcript_review, "available", "_available")
    assert _calls_policy(transcript_views._available, "SCRIBE_ASR")


def _comment_only(recording: object) -> bool:
    # enabled_policy(Purpose.SCRIBE_ASR)  # noqa: ERA001 - the reviewed fake
    return recording is not None


def _wrong_purpose(recording: object) -> bool:
    enabled_policy(Purpose.BRIEF)
    return recording is not None


def _policy_text(recording: object) -> bool:
    return str(recording) != "enabled_policy(Purpose.SCRIBE_ASR)"


@pytest.mark.parametrize("fake", [_comment_only, _wrong_purpose, _policy_text])
def test_provenance_rejects_providers_that_do_not_call_the_purpose_policy(
    fake: object,
) -> None:
    assert not _calls_policy(fake, "SCRIBE_ASR")
