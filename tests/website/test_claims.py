"""Claims registry: website templates and help guides carry no unregistered text.

``docs/web/claims.yaml`` has two lists. ``claims`` holds every statement of
fact, each with a tracked ``evidence_ref`` and the ``evidence_quote`` phrases
that file must contain. On pages, the element marked ``data-claim="<id>"``
directly wraps the claim's exact msgid; help-guide claims (``surface:
guides``) are guide strings. ``non_claims`` holds every other text, each with
a reason from ``NON_CLAIM_REASONS``.

What is enforced: the test checks the page templates and the help guides for
unregistered text. It reads every template under ``templates/website/``,
subfolders included, and every help-guide string, step instruction, screen
title and control label in ``apps/website/guides.py``. In the templates it
reads every msgid (trans or translate, either quote style), literal text node,
visible attribute (``VISIBLE_ATTRIBUTES``, every text-valued ARIA attribute
included) and blocktrans body, and fails on any text in neither list. It also
fails on a template form it cannot classify: an unconsumed i18n form; a
visible attribute that holds template output; a string literal in ``{{ }}``,
in blocktrans arguments or in a tag outside ``LITERAL_TAGS``; a tag outside
``ALLOWED_TAGS`` (``{% with %}``, ``{% firstof %}``); an include or extends
outside ``LOADABLE``; and a ``{{ }}`` output or variable ``{% trans %}`` whose
loop-resolved path is not pinned in ``OUTPUT_PATHS`` or ``GUIDE_TRANS_PATHS``.
A route test renders every route of the website urlconf, every root route a
website view serves and every route whose path lies in ``WEBSITE_PUBLIC``, the
one constant the tenancy middleware bypass is built from. It fails unless each
such route lies in that space and every template it renders is under
``templates/website/`` or listed in ``RENDERED_OUTSIDE_SCAN``, and it fails on
an allowlist entry that no route renders.

What is not: ``RENDERED_OUTSIDE_SCAN`` holds the text-free icon include and
the app-wide 404 page with its layouts, which are outside this scope. The
pinned guide paths are trusted to carry guide strings and the other pinned
outputs to be counters, a slug, step counts or the configured contact
address; the test reads no view code. Declared limits: the tests do not check
values the views compute at request time or the pt-BR translations, and the
route test renders each route once, anonymously, with no query string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final, Never

import pytest
import yaml
from apps.tenancy.middleware import WEBSITE_PUBLIC
from apps.website import urls as website_urls
from apps.website.guides import AUDIENCES, GUIDES
from django.http import HttpResponse
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver, path, reverse
from django.utils import translation

ROOT: Final = Path(__file__).resolve().parents[2]
REGISTRY: Final = ROOT / "docs" / "web" / "claims.yaml"
TEMPLATES: Final = ROOT / "templates" / "website"
# A claim element wraps exactly one msgid: <p data-claim="id">{% trans "..." %}
CLAIM_MARKUP: Final = re.compile(
    r'data-claim="(?P<id>[^"]+)"[^>]*>\s*\{% trans "(?P<msgid>[^"]+)" %\}'
)
ANY_CLAIM: Final = re.compile(r'data-claim="([^"]+)"')
# Local agent guides and evidence are never committed, so they cannot back a claim.
UNTRACKED_EVIDENCE: Final = ("AGENTS.md", ".omo/")
SURFACES: Final = frozenset({"pages", "guides"})
NON_CLAIM_REASONS: Final = {
    "heading": "names a page or section; asserts nothing about the product",
    "intro": "says what the page covers; its facts are registered claims",
    "navigation": "a link, button or landmark label",
    "label": "a status, field, category or control word",
    "instruction": "tells the reader what to do",
    "advice": "a recommendation to the reader",
    "illustration": "inside a figure captioned as fictional",
    "brand": "brand name, tagline or brand-use rule (PRODUCT.md, DESIGN.md)",
    "metadata": "page title or meta description",
    "state": "the page's own configured state, pinned by tests/website/test_pages.py",
}
# Attributes whose value is shown or announced as text: the HTML ones and
# every string-valued WAI-ARIA 1.3 property. ID references (aria-labelledby,
# aria-describedby) point at elements whose text is collected anyway.
VISIBLE_ATTRIBUTES: Final = frozenset(
    {
        "alt",
        "title",
        "placeholder",
        "value",
        "label",
        "aria-label",
        "aria-description",
        "aria-roledescription",
        "aria-valuetext",
        "aria-placeholder",
        "aria-braillelabel",
        "aria-brailleroledescription",
        "aria-keyshortcuts",
        "aria-colindextext",
        "aria-rowindextext",
    }
)
# The only template tags the website templates may use. Anything else, such as
# {% with %}, {% firstof %}, {% cycle %}, {% now %} or {% filter %}, fails.
ALLOWED_TAGS: Final = frozenset(
    {
        "load",
        "extends",
        "block",
        "endblock",
        "include",
        "url",
        "static",
        "if",
        "elif",
        "else",
        "endif",
        "for",
        "empty",
        "endfor",
        "trans",
        "translate",
        "blocktrans",
        "blocktranslate",
        "endblocktrans",
        "endblocktranslate",
        "plural",
    }
)
# Tags whose string literals are collected msgids or name a URL, a file or a
# compared value; a literal in any other tag or in {{ }} fails.
LITERAL_TAGS: Final = frozenset(
    {"trans", "translate", "url", "static", "extends", "include", "if", "elif"}
)
# The only templates a website template may extend or include, and why.
LOADABLE: Final = {
    "website/base.html": "parsed here with the other website templates",
    "includes/components/icon.html": "an aria-hidden SVG that renders no text",
}
# Templates a website route renders outside templates/website/, and why. The
# 404 page and its layouts are app-wide and outside the claims scope; the route
# test fails on an entry that no route renders.
RENDERED_OUTSIDE_SCAN: Final = {
    "includes/components/icon.html": "an aria-hidden SVG that renders no text",
    "404.html": "the app-wide not-found page; /pricing/ returns it until todo 74",
    "base_refusal.html": "the app-wide refusal layout that 404.html extends",
    "base.html": "the app-wide layout that base_refusal.html extends",
}
# Keyword arguments for every website route that takes URL parameters.
ROUTE_ARGUMENTS: Final = {"website:guide": [{"slug": guide.slug} for guide in GUIDES]}
# Every {{ }} output and blocktrans argument, by template, with {% for %}
# variables resolved to their iterable ("steps[].place"). The values come from
# the views: counters, a slug, the configured contact address, step counts and
# guide strings built from apps/website/guides.py.
OUTPUT_PATHS: Final = {
    "contact.html": frozenset({"contact_email"}),
    "help_guide.html": frozenset(
        {
            "forloop.counter",
            "guide.slug",
            "steps[].place",
            "steps[].sentence",
            "steps|length",
            "track_position",
            "track_length",
        }
    ),
    "help_index.html": frozenset(
        {"forloop.counter", "groups[].key", "groups[].guides[].steps|length"}
    ),
    "home.html": frozenset({"forloop.counter"}),
}
# {% trans <variable> %} is trusted only on these guide-registry paths.
GUIDE_TRANS_PATHS: Final = {
    "help_guide.html": frozenset(
        {
            "guide.title",
            "guide.summary",
            "audience",
            "guide.before[]",
            "steps[].result",
            "guide.notes[]",
            "previous_step.title",
            "next_step.title",
            "related[].title",
        }
    ),
    "help_index.html": frozenset(
        {
            "first_steps[].guide.title",
            "first_steps[].audience",
            "first_steps[].guide.summary",
            "groups[].label",
            "groups[].guides[].title",
        }
    ),
    "home.html": frozenset({"starter_guides[].title", "starter_guides[].summary"}),
}
COMMENT: Final = re.compile(r"\{% comment %\}.*?\{% endcomment %\}", re.DOTALL)
BLOCKTRANS: Final = re.compile(
    r"\{% blocktrans(?:late)?\b[^%]*%\}(?P<body>.*?)\{% endblocktrans(?:late)? %\}",
    re.DOTALL,
)
# {% trans %} or {% translate %}, either quote style, optional context and "as".
TRANS: Final = re.compile(
    r"\{% trans(?:late)? (?P<q>[\"'])(?P<msgid>.+?)(?P=q)"
    r"(?: context (?P<cq>[\"'])(?P<context>.+?)(?P=cq))?(?: as \w+)? %\}"
)
VARIABLE_TRANS: Final = re.compile(r"\{% trans(?:late)? (?P<expr>[a-z_][\w.]*) %\}")
# Anything i18n left after the three patterns above is a form they missed.
UNCONSUMED_I18N: Final = re.compile(
    r"\{%\s*(?:trans|translate|blocktrans|blocktranslate|endblocktrans"
    r"|endblocktranslate|plural)\b|\b_\(\s*[\"']"
)
# A visible attribute may hold literal text or a consumed msgid, never output
# the collector cannot see ({{ variable }} or another tag).
UNTRACKED_ATTRIBUTE: Final = re.compile(
    r"\b(?:" + "|".join(sorted(VISIBLE_ATTRIBUTES)) + r")\s*=\s*"
    r"(?P<q>[\"'])(?:(?!(?P=q)).)*?\{[{%]",
    re.DOTALL,
)
OTHER_TAG: Final = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)
TAG: Final = re.compile(
    r"\{%\s*(?P<tag>.*?)\s*%\}|\{\{\s*(?P<output>.*?)\s*\}\}", re.DOTALL
)
STRING: Final = re.compile(r"\"[^\"]*\"|'[^']*'")
CONTEXT_ARGUMENT: Final = re.compile(r"\bcontext\s+(?:\"[^\"]*\"|'[^']*')")
ARGUMENT: Final = re.compile(r"(\w+)=(\S+)")
ROOT_NAME: Final = re.compile(r"[a-z_]\w*")
PARSER_REFUSAL: Final = (
    r"unconsumed|untracked|literal|ALLOWED_TAGS|LOADABLE|GUIDE_TRANS_PATHS"
)
TOKEN: Final = re.compile(r"\x01(\d+)\x01")
VOID: Final = frozenset({"img", "meta", "link", "br", "input", "hr", "source"})


class ClaimsRegistryError(ValueError):
    """The registry is malformed or a claim lacks verifiable evidence."""


@dataclass(frozen=True, slots=True)
class Claim:
    id: str
    claim: str
    evidence_ref: str
    evidence_quote: tuple[str, ...]
    surface: str


@dataclass(frozen=True, slots=True)
class Shown:
    """One text the website renders and where it sits."""

    source: str
    kind: str  # msgid | literal | blocktrans | variable | guide
    text: str
    claim: str | None
    path: str


def _fail(message: str) -> Never:
    raise ClaimsRegistryError(message)


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _text(entry: dict[str, object], key: str, claim_id: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        _fail(f"claim {claim_id!r} has no {key}")
    return value


def _claim(entry: object, root: Path) -> Claim:
    if not isinstance(entry, dict):
        _fail("each claim must be a mapping")
    claim_id = entry.get("id")
    if not isinstance(claim_id, str) or not claim_id:
        _fail("each claim needs an id")
    claim = _text(entry, "claim", claim_id)
    reference = _text(entry, "evidence_ref", claim_id)
    candidate = Path(reference)
    if (
        candidate.is_absolute()
        or ".." in candidate.parts
        or reference.endswith(UNTRACKED_EVIDENCE[0])
        or reference.startswith(UNTRACKED_EVIDENCE[1])
    ):
        _fail(f"claim {claim_id!r} cites evidence outside the tracked tree")
    evidence = root / candidate
    if not evidence.is_file():
        _fail(f"claim {claim_id!r} cites missing evidence {reference}")
    quotes = entry.get("evidence_quote")
    if (
        not isinstance(quotes, list)
        or not quotes
        or not all(isinstance(q, str) and q.strip() for q in quotes)
    ):
        _fail(f"claim {claim_id!r} has no evidence_quote")
    body = _normalized(evidence.read_text(encoding="utf-8"))
    for quote in quotes:
        if _normalized(quote) not in body:
            _fail(f"claim {claim_id!r}: {reference} does not contain {quote!r}")
    surface = entry.get("surface", "pages")
    if surface not in SURFACES:
        _fail(f"claim {claim_id!r} has unknown surface {surface!r}")
    return Claim(claim_id, claim, reference, tuple(quotes), str(surface))


def load_claims(path: Path, root: Path = ROOT) -> dict[str, Claim]:
    """Parse and verify a claims registry; raise on any unsupported claim."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        _fail("registry must be a version 1 mapping")
    entries = data.get("claims")
    if not isinstance(entries, list) or not entries:
        _fail("registry lists no claims")
    claims: dict[str, Claim] = {}
    for entry in entries:
        claim = _claim(entry, root)
        if claim.id in claims:
            _fail(f"duplicate claim id {claim.id!r}")
        claims[claim.id] = claim
    return claims


def load_non_claims(path: Path) -> dict[str, str]:
    """Return text -> reason for every declared non-claim."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data.get("non_claims") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        _fail("registry lists no non_claims")
    non_claims: dict[str, str] = {}
    for entry in entries:
        text = entry.get("text") if isinstance(entry, dict) else None
        reason = entry.get("reason") if isinstance(entry, dict) else None
        if not isinstance(text, str) or not text.strip():
            _fail("each non_claim needs a text")
        if reason not in NON_CLAIM_REASONS:
            _fail(f"non_claim {text!r} has no reason from NON_CLAIM_REASONS")
        if text in non_claims:
            _fail(f"duplicate non_claim {text!r}")
        non_claims[text] = str(reason)
    return non_claims


class _Collector(HTMLParser):
    def __init__(self, source: str, tokens: list[Shown]) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.tokens = tokens
        self.stack: list[tuple[str, str, str | None]] = []
        self.shown: list[Shown] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        for name, value in attrs:
            if value and (name in VISIBLE_ATTRIBUTES or TOKEN.search(value)):
                self._collect(value, None)
        if tag not in VOID:
            classes = (values.get("class") or "").split()
            label = f"{tag}.{classes[0]}" if classes else tag
            self.stack.append((tag, label, values.get("data-claim")))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag("img", attrs)

    def handle_endtag(self, tag: str) -> None:
        while self.stack:
            if self.stack.pop()[0] == tag:
                break

    def handle_data(self, data: str) -> None:
        self._collect(data, self.stack[-1][2] if self.stack else None)

    def _collect(self, data: str, claim: str | None) -> None:
        path = "/".join(label for _tag, label, _claim in self.stack)
        for part in re.split(r"(\x01\d+\x01)", data):
            token = TOKEN.fullmatch(part)
            if token:
                found = self.tokens[int(token.group(1))]
                self.shown.append(
                    Shown(found.source, found.kind, found.text, claim, path)
                )
                continue
            literal = _normalized(part)
            # Punctuation between translated pieces (" · ", ":") says nothing.
            if re.search(r"\w", literal):
                self.shown.append(Shown(self.source, "literal", literal, claim, path))


def _resolve(expression: str, scopes: list[dict[str, str | None]]) -> str | None:
    """Rewrite a loop variable as its iterable; None is a blocktrans argument."""
    root = ROOT_NAME.match(expression)
    name = root.group(0) if root else ""
    for scope in reversed(scopes):
        if name in scope:
            source = scope[name]
            return None if source is None else source + expression[len(name) :]
    return expression


def _check_output(
    name: str, expression: str, scopes: list[dict[str, str | None]]
) -> None:
    """A {{ }} output or blocktrans argument is a pinned path with no literal."""
    if STRING.search(expression):
        _fail(f"{name}: string literal in output {expression!r}")
    path = _resolve(expression, scopes)
    if path is not None and path not in OUTPUT_PATHS.get(name, frozenset()):
        _fail(f"{name}: untracked output {path!r} is not in OUTPUT_PATHS")


def _check_tag(
    name: str, tag: str, rest: str, scopes: list[dict[str, str | None]]
) -> None:
    """A tag is allowed, loads only LOADABLE templates and keeps loop scopes."""
    if tag not in ALLOWED_TAGS:
        _fail(f"{name}: tag {tag!r} is not in ALLOWED_TAGS")
    if tag in {"blocktrans", "blocktranslate"}:
        if STRING.search(CONTEXT_ARGUMENT.sub("", rest)):
            _fail(f"{name}: string literal in {tag} arguments")
        arguments = dict(ARGUMENT.findall(rest))
        for expression in arguments.values():
            _check_output(name, expression, scopes)
        placeholders: dict[str, str | None] = dict.fromkeys(arguments)
        scopes.append(placeholders)
    elif tag in {"endblocktrans", "endblocktranslate", "endfor"}:
        scopes.pop()
    elif tag == "for":
        names, _, iterable = rest.partition(" in ")
        source = _resolve(iterable.strip(), scopes) or iterable.strip()
        scopes.append(
            {variable.strip(): f"{source}[]" for variable in names.split(",")}
        )
    elif tag not in LITERAL_TAGS and STRING.search(rest):
        _fail(f"{name}: string literal in {tag!r}")
    if tag in {"include", "extends"}:
        target = STRING.match(rest)
        if target is None or target.group(0)[1:-1] not in LOADABLE:
            _fail(f"{name}: {tag} {rest!r} is not in LOADABLE")


def _check_template(name: str, source: str) -> None:
    """Fail on any template output the collector cannot classify."""
    scopes: list[dict[str, str | None]] = []
    for match in TAG.finditer(source):
        if match["output"] is not None:
            _check_output(name, match["output"], scopes)
            continue
        tag, _, rest = match["tag"].partition(" ")
        variable = VARIABLE_TRANS.fullmatch(match.group(0))
        if variable:
            path = _resolve(variable["expr"], scopes)
            if path not in GUIDE_TRANS_PATHS.get(name, frozenset()):
                _fail(f"{name}: trans of {path!r} is not in GUIDE_TRANS_PATHS")
        _check_tag(name, tag, rest.strip(), scopes)


def _template_shown(template: Path, name: str) -> list[Shown]:
    """Every text one template renders, after the structural checks."""
    source = COMMENT.sub("", template.read_text(encoding="utf-8"))
    _check_template(name, source)
    tokens: list[Shown] = []

    def token(kind: str, text: str) -> str:
        tokens.append(Shown(name, kind, text, None, ""))
        return f"\x01{len(tokens) - 1}\x01"

    def block(match: re.Match[str]) -> str:
        body = re.sub(r"\{\{\s*(\w+)\s*\}\}", r"%(\1)s", match["body"])
        return token("blocktrans", _normalized(body))

    source = BLOCKTRANS.sub(block, source)
    source = TRANS.sub(lambda m: token("msgid", m["msgid"]), source)
    source = VARIABLE_TRANS.sub(lambda m: token("variable", m["expr"]), source)
    leftover = UNCONSUMED_I18N.search(source)
    if leftover:
        _fail(f"{name}: unconsumed i18n form {leftover.group(0)!r}")
    if UNTRACKED_ATTRIBUTE.search(source):
        _fail(f"{name}: visible attribute renders untracked output")
    collector = _Collector(name, tokens)
    collector.feed(OTHER_TAG.sub("", source))
    collector.close()
    return collector.shown


def scanned_templates(directory: Path = TEMPLATES) -> list[Path]:
    """Every template under the website tree, subfolders included."""
    return sorted(directory.rglob("*.html"))


def template_texts(directory: Path = TEMPLATES) -> list[Shown]:
    """Every text the website templates render, with its claim wrapper."""
    return [
        shown
        for template in scanned_templates(directory)
        for shown in _template_shown(
            template, template.relative_to(directory).as_posix()
        )
    ]


def _public(route: str) -> bool:
    """Whether a URL route lies in the website's public space."""
    path = "/" + route
    return path in WEBSITE_PUBLIC.paths or path.startswith(WEBSITE_PUBLIC.prefixes)


def website_routes(
    patterns: list[URLPattern | URLResolver] | None = None,
    namespace: str = "",
    prefix: str = "",
    *,
    inside: bool = False,
) -> list[tuple[str, str, URLPattern]]:
    """(name, route, pattern) for every website urlconf route, every root route a
    website view serves and every route whose path lies in WEBSITE_PUBLIC."""
    routes: list[tuple[str, str, URLPattern]] = []
    for entry in get_resolver().url_patterns if patterns is None else patterns:
        route = prefix + str(entry.pattern).removeprefix("^").removesuffix("$")
        if isinstance(entry, URLResolver):
            space = f"{namespace}{entry.namespace}:" if entry.namespace else namespace
            module = getattr(entry.urlconf_module, "__name__", "")
            nested = inside or module == website_urls.__name__
            routes += website_routes(entry.url_patterns, space, route, inside=nested)
        elif (
            inside
            or entry.callback.__module__.startswith("apps.website")
            or _public(route)
        ):
            name = f"{namespace}{entry.name}" if entry.name else f"/{route}"
            routes.append((name, route, entry))
    return routes


def _route_urls(name: str, route: str, pattern: URLPattern) -> list[str] | None:
    """The URLs to request for one route; None when its parameters are unknown."""
    if not pattern.pattern.regex.groupindex:
        return ["/" + route]
    if name not in ROUTE_ARGUMENTS:
        return None
    return [reverse(name, kwargs=kwargs) for kwargs in ROUTE_ARGUMENTS[name]]


def unscanned(rendered: list[Any], directory: Path = TEMPLATES) -> list[str]:
    """Rendered templates outside the scanned tree and off RENDERED_OUTSIDE_SCAN."""
    problems: list[str] = []
    for template in rendered:
        origin = getattr(template, "origin", None)
        source = Path(getattr(origin, "name", "") or "")
        label = getattr(origin, "template_name", None) or str(source)
        resolved = source.resolve() if source.is_absolute() else None
        inside = resolved is not None and resolved.is_relative_to(directory.resolve())
        allowed = (
            resolved is not None
            and label in RENDERED_OUTSIDE_SCAN
            and resolved == (ROOT / "templates" / label).resolve()
        )
        if not (inside or allowed):
            problems.append(label)
    return problems


def guide_texts() -> list[Shown]:
    """Every help-guide string a guide or the help index renders."""
    shown = [
        Shown("guides.py", "guide", label, None, "guides")
        for label in AUDIENCES.values()
    ]
    for guide in GUIDES:
        texts = [guide.title, guide.summary, *guide.before, *guide.notes]
        texts += [step.result for step in guide.steps if step.result]
        shown.extend(Shown(guide.slug, "guide", text, None, "guides") for text in texts)
        for step in guide.steps:
            # The instruction itself; a fact belongs in the step's result.
            shown.append(Shown(guide.slug, "step", step.text, None, "guides"))
            labels = (step.control.source, step.screen.title.source)
            shown.extend(
                Shown(guide.slug, "label", text, None, "guides") for text in labels
            )
    return shown


def unclassified(
    shown: list[Shown], claims: dict[str, Claim], non_claims: dict[str, str]
) -> list[tuple[str, str, str]]:
    """Texts that are neither a registered claim nor a declared non-claim."""
    claim_texts = {claim.claim for claim in claims.values()}
    problems = []
    for item in shown:
        # Variable trans is pinned by GUIDE_TRANS_PATHS at parse time, and the
        # strings it renders are collected by guide_texts.
        if item.kind == "variable":
            continue
        if item.claim is not None:
            if item.claim not in claims or claims[item.claim].claim != item.text:
                problems.append((item.source, item.text, "unregistered claim"))
        elif item.kind == "step":
            if non_claims.get(item.text) != "instruction":
                problems.append((item.source, item.text, "step is not an instruction"))
        elif item.kind == "guide" and item.text in claim_texts:
            continue
        elif item.text not in non_claims:
            problems.append((item.source, item.text, "neither claim nor non-claim"))
    return problems


def test_every_claim_cites_existing_evidence() -> None:
    for claim in load_claims(REGISTRY).values():
        assert (ROOT / claim.evidence_ref).is_file()


def test_every_visible_text_is_a_claim_or_a_justified_non_claim() -> None:
    claims = load_claims(REGISTRY)
    non_claims = load_non_claims(REGISTRY)
    shown = template_texts() + guide_texts()

    assert unclassified(shown, claims, non_claims) == []
    # A text is one or the other, never both, and no entry is stale.
    assert {claim.claim for claim in claims.values()} & set(non_claims) == set()
    texts = {item.text for item in shown}
    assert set(non_claims) - texts == set()
    assert {c.claim for c in claims.values()} - texts == set()


def test_page_claims_are_marked_and_guide_claims_live_in_guides() -> None:
    claims = load_claims(REGISTRY)
    marked = {item.claim for item in template_texts() if item.claim}
    guides = {item.text for item in guide_texts()}

    assert marked == {c.id for c in claims.values() if c.surface == "pages"}
    for claim in claims.values():
        if claim.surface == "guides":
            assert claim.claim in guides, claim.id
    for template in scanned_templates():
        source = template.read_text(encoding="utf-8")
        # Every marker directly wraps one msgid, so nothing escapes the check.
        assert len(CLAIM_MARKUP.findall(source)) == len(ANY_CLAIM.findall(source))


def test_every_claim_reads_in_pt_br() -> None:
    claims = load_claims(REGISTRY)

    with translation.override("pt-br"):
        untranslated = [
            claim.id
            for claim in claims.values()
            if translation.gettext(claim.claim) == claim.claim
        ]
    assert untranslated == []


def test_pricing_stays_hidden_until_the_owner_approves_packaging() -> None:
    data = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))

    assert data["pricing"] == {"status": "hidden", "owner_approval_ref": None}


def test_an_unregistered_factual_sentence_fails(tmp_path: Path) -> None:
    page = tmp_path / "page.html"
    page.write_text(
        '<main><p>{% trans "Clinic Ops is certified for live data." %}</p>'
        '<p data-claim="x">{% trans "Unregistered." %}</p>'
        "<p>Ninety-nine clinics use it.</p></main>",
        encoding="utf-8",
    )

    problems = unclassified(template_texts(tmp_path), {}, {})

    assert [problem[1] for problem in problems] == [
        "Clinic Ops is certified for live data.",
        "Unregistered.",
        "Ninety-nine clinics use it.",
    ]


@pytest.mark.parametrize(
    "markup",
    [
        '<p>{% translate "Clinic Ops is certified." %}</p>',
        "<p>{% trans 'Clinic Ops is certified.' %}</p>",
        "<p>{% translate 'Clinic Ops is certified.' context 'x' %}</p>",
        "<p>{% blocktranslate %}Clinic Ops is certified.{% endblocktranslate %}</p>",
        '<p aria-description="Clinic Ops is certified."></p>',
        '<input placeholder="Clinic Ops is certified.">',
        '<input value="Clinic Ops is certified.">',
    ],
)
def test_every_i18n_form_and_visible_attribute_is_collected(
    tmp_path: Path, markup: str
) -> None:
    (tmp_path / "page.html").write_text(markup, encoding="utf-8")

    problems = unclassified(template_texts(tmp_path), {}, {})

    assert [problem[1] for problem in problems] == ["Clinic Ops is certified."]


@pytest.mark.parametrize(
    "markup",
    [
        '<p>{% trans "Clinic Ops is certified." noop %}</p>',
        "<p>{% trans greeting|upper %}</p>",
        '<p>{{ _("Clinic Ops is certified.") }}</p>',
        '<p aria-description="{{ fact }}"></p>',
        "<img alt='{% firstof fact %}'>",
    ],
)
def test_an_unconsumed_i18n_form_or_attribute_fails_the_parser(
    tmp_path: Path, markup: str
) -> None:
    (tmp_path / "page.html").write_text(markup, encoding="utf-8")

    with pytest.raises(ClaimsRegistryError, match=PARSER_REFUSAL):
        template_texts(tmp_path)


@pytest.mark.parametrize(
    "markup",
    [
        '{% with fact="Clinic Ops is certified." %}<p>{{ fact }}</p>{% endwith %}',
        "{% with fact=headline %}<p>{{ fact }}</p>{% endwith %}",
        '<p>{{ "Clinic Ops is certified." }}</p>',
        "<p>{{ headline|default:'Clinic Ops is certified.' }}</p>",
        '<p>{% firstof "Clinic Ops is certified." %}</p>',
        "<p>{% firstof headline %}</p>",
        '{% include "includes/marketing/banner.html" %}',
        "{% include banner_template %}",
        '{% extends "base.html" %}',
        "<p>{{ headline }}</p>",
        "{% for item in modules %}<p>{% trans item %}</p>{% endfor %}",
        "<p>{% trans guide.title %}</p>",
        '{% blocktrans with fact="Clinic Ops is certified." %}'
        "{{ fact }}{% endblocktrans %}",
        "{% blocktrans with fact=headline %}{{ fact }}{% endblocktrans %}",
        "{% blocktrans %}{{ headline }}{% endblocktrans %}",
        '<p>{% trans "Clinic Ops is certified." as fact %}{{ fact }}</p>',
        '<input value="{{ headline }}">',
    ],
)
def test_a_template_form_the_parser_cannot_classify_fails(
    tmp_path: Path, markup: str
) -> None:
    (tmp_path / "page.html").write_text(markup, encoding="utf-8")

    with pytest.raises(ClaimsRegistryError, match=PARSER_REFUSAL):
        template_texts(tmp_path)


def test_allowed_includes_render_no_text() -> None:
    for target in LOADABLE:
        if not target.startswith("website/"):
            assert _template_shown(ROOT / "templates" / target, target) == []


def test_every_template_a_website_route_renders_is_scanned_or_allowlisted() -> None:
    routes = website_routes()
    names = {name for name, _route, _pattern in routes}
    # The walk reaches the whole website include and the root page.
    assert {f"website:{p.name}" for p in website_urls.urlpatterns} <= names
    assert "index" in names
    client = Client()
    problems: list[tuple[str, str]] = []
    rendered_outside: set[str] = set()
    for name, route, pattern in routes:
        if not _public(route):
            problems.append((name, f"/{route} is outside WEBSITE_PUBLIC"))
        urls = _route_urls(name, route, pattern)
        if urls is None:
            problems.append((name, "takes parameters missing from ROUTE_ARGUMENTS"))
            continue
        for url in urls:
            response = client.get(url)
            if not response.templates:
                problems.append((name, f"{response.status_code} renders no template"))
            problems += [(name, label) for label in unscanned(response.templates)]
            rendered_outside |= {
                str(template.origin.template_name)
                for template in response.templates
                if not Path(template.origin.name).is_relative_to(TEMPLATES)
            }

    assert problems == []
    # Every allowlist entry is rendered by some website route; none is stale.
    assert set(RENDERED_OUTSIDE_SCAN) <= rendered_outside


def test_a_root_route_in_the_public_space_is_walked_whatever_its_view() -> None:
    patterns: list[URLPattern | URLResolver] = [
        path("help/extra/faq/", lambda _request: HttpResponse(), name="faq"),
        path("brand/", lambda _request: HttpResponse()),
        path("workspace/other/", lambda _request: HttpResponse(), name="other"),
    ]

    walked = [(name, route) for name, route, _pattern in website_routes(patterns)]

    assert walked == [("faq", "help/extra/faq/"), ("/brand/", "brand/")]


def test_a_template_outside_the_scan_or_off_the_allowlist_fails() -> None:
    def rendered(name: str, template_name: str | None) -> SimpleNamespace:
        return SimpleNamespace(
            origin=SimpleNamespace(name=name, template_name=template_name)
        )

    shared = ROOT / "templates"
    scanned_or_allowed = [
        rendered(str(TEMPLATES / "pages" / "faq.html"), "website/pages/faq.html"),
        rendered(str(shared / "404.html"), "404.html"),
    ]
    outside = [
        rendered(str(shared / "marketing" / "faq.html"), "marketing/faq.html"),
        rendered(str(ROOT / "apps" / "website" / "templates" / "404.html"), "404.html"),
        rendered("<unknown source>", None),
    ]

    assert unscanned(scanned_or_allowed) == []
    assert unscanned(outside) == ["marketing/faq.html", "404.html", "<unknown source>"]


def test_templates_in_subfolders_are_scanned(tmp_path: Path) -> None:
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "faq.html").write_text(
        '<p>{% trans "Clinic Ops is certified." %}</p>', encoding="utf-8"
    )

    problems = unclassified(template_texts(tmp_path), {}, {})

    assert problems == [
        ("pages/faq.html", "Clinic Ops is certified.", "neither claim nor non-claim")
    ]


@pytest.mark.parametrize("attribute", sorted(VISIBLE_ATTRIBUTES))
def test_every_visible_attribute_is_collected(tmp_path: Path, attribute: str) -> None:
    (tmp_path / "page.html").write_text(
        f'<p {attribute}="Clinic Ops is certified."></p>', encoding="utf-8"
    )

    problems = unclassified(template_texts(tmp_path), {}, {})

    assert [problem[1] for problem in problems] == ["Clinic Ops is certified."]


def test_a_step_text_must_be_a_declared_instruction() -> None:
    step = Shown("guide", "step", "Press {control}; it is certified.", None, "")

    assert unclassified([step], {}, {}) != []
    assert unclassified([step], {}, {step.text: "advice"}) != []
    assert unclassified([step], {}, {step.text: "instruction"}) == []


def _write(tmp_path: Path, claim: dict[str, object]) -> Path:
    evidence = tmp_path / "evidence.md"
    evidence.write_text("The runtime role is NOBYPASSRLS.\n", encoding="utf-8")
    registry = tmp_path / "claims.yaml"
    registry.write_text(
        yaml.safe_dump({"version": 1, "claims": [claim]}), encoding="utf-8"
    )
    return registry


VALID: Final[dict[str, object]] = {
    "id": "runtime-role",
    "claim": "The runtime role cannot bypass row-level security.",
    "evidence_ref": "evidence.md",
    "evidence_quote": ["NOBYPASSRLS"],
}


def test_a_well_supported_claim_loads(tmp_path: Path) -> None:
    claims = load_claims(_write(tmp_path, VALID), root=tmp_path)

    assert claims["runtime-role"].evidence_ref == "evidence.md"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"evidence_ref": None}, "has no evidence_ref"),
        ({"evidence_ref": ""}, "has no evidence_ref"),
        ({"evidence_ref": "missing.md"}, "cites missing evidence"),
        ({"evidence_ref": "../evidence.md"}, "outside the tracked tree"),
        ({"evidence_ref": "apps/AGENTS.md"}, "outside the tracked tree"),
        ({"evidence_ref": ".omo/evidence.md"}, "outside the tracked tree"),
        ({"evidence_quote": []}, "has no evidence_quote"),
        ({"evidence_quote": ["superuser"]}, "does not contain"),
        ({"claim": ""}, "has no claim"),
        ({"surface": "footer"}, "unknown surface"),
    ],
)
def test_a_claim_without_verifiable_evidence_fails(
    tmp_path: Path, change: dict[str, object], message: str
) -> None:
    claim = {
        key: value for key, value in {**VALID, **change}.items() if value is not None
    }

    with pytest.raises(ClaimsRegistryError, match=message):
        load_claims(_write(tmp_path, claim), root=tmp_path)


def test_a_non_claim_needs_a_known_reason(tmp_path: Path) -> None:
    registry = tmp_path / "claims.yaml"
    registry.write_text(
        yaml.safe_dump({"non_claims": [{"text": "Help", "reason": "obvious"}]}),
        encoding="utf-8",
    )

    with pytest.raises(ClaimsRegistryError, match="no reason"):
        load_non_claims(registry)


def test_duplicate_claim_ids_fail(tmp_path: Path) -> None:
    registry = _write(tmp_path, VALID)
    registry.write_text(
        yaml.safe_dump({"version": 1, "claims": [VALID, VALID]}), encoding="utf-8"
    )

    with pytest.raises(ClaimsRegistryError, match="duplicate claim id"):
        load_claims(registry, root=tmp_path)
