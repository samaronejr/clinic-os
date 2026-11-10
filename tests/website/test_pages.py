"""The public website: GET-only, cookie-free, script-free, pt-BR, honest."""

from __future__ import annotations

import gettext as stdlib_gettext
import re
from pathlib import Path
from typing import Final

import pytest
from apps.website.guides import GUIDES
from config.settings.contracts import resolve_website_contact_email
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, override_settings
from django.urls import reverse
from django.utils.translation import gettext

OK: Final = 200
NOT_FOUND: Final = 404
METHOD_NOT_ALLOWED: Final = 405
PAGES: Final = (
    "/",
    "/product/",
    "/security/",
    "/contact/",
    "/brand/",
    "/help/",
    *(f"/help/{guide.slug}/" for guide in GUIDES),
)
PRIVATE_DIRECTIVES: Final = ("no-store", "private")
ROOT: Final = Path(__file__).resolve().parents[2]
TEMPLATES: Final = ROOT / "templates" / "website"
CATALOG: Final = ROOT / "locale" / "pt_BR" / "LC_MESSAGES" / "django.mo"
TRANS: Final = re.compile(r'\{% trans "([^"]+)"(?: context "([^"]+)")? %\}')
MISSING: Final = "\x00missing"


class _Missing(stdlib_gettext.NullTranslations):
    """Fallback that marks a msgid absent from the compiled catalog."""

    def gettext(self, message: str) -> str:
        return MISSING

    def pgettext(self, context: str, message: str) -> str:
        return MISSING


def test_every_website_msgid_is_in_the_pt_br_catalog() -> None:
    # Present even when pt-BR spells it the same way ("Agenda"): look the
    # msgid up in the committed .mo instead of comparing translations.
    with CATALOG.open("rb") as handle:
        catalog = stdlib_gettext.GNUTranslations(handle)
    catalog.add_fallback(_Missing())
    missing = [
        (template.name, msgid)
        for template in sorted(TEMPLATES.glob("*.html"))
        for msgid, context in TRANS.findall(template.read_text(encoding="utf-8"))
        if (catalog.pgettext(context, msgid) if context else catalog.gettext(msgid))
        == MISSING
    ]

    assert missing == []


@pytest.mark.parametrize("path", PAGES)
def test_public_page_renders_for_anonymous_visitors_without_side_effects(
    path: str,
) -> None:
    response = Client().get(path)

    assert response.status_code == OK
    body = response.content.decode()
    # Static content: no scripts, no forms, no cookies, no records in links.
    assert "<script" not in body
    assert "<form" not in body
    assert response.cookies == {}
    assert "?" not in "".join(re.findall(r'href="([^"]*)"', body))
    assert 'lang="pt-BR"' in body
    assert "css/clinic-os-website.css" in body
    cache_control = response.headers["Cache-Control"]
    assert all(directive in cache_control for directive in PRIVATE_DIRECTIVES)


@pytest.mark.parametrize("path", PAGES)
def test_public_page_is_get_only(path: str) -> None:
    assert Client().post(path).status_code == METHOD_NOT_ALLOWED


def test_home_leads_with_the_brand_promise_in_pt_br() -> None:
    body = Client().get("/").content.decode()

    title = gettext("The operating system for modern clinics.")
    assert title in body
    assert 'href="/auth/login/"' in body
    assert 'href="/contact/"' in body


def test_pricing_slot_is_hidden_and_unlinked() -> None:
    assert Client().get("/pricing/").status_code == NOT_FOUND
    for path in PAGES:
        body = Client().get(path).content.decode()
        assert reverse("website:pricing") not in body, path


def test_unknown_guide_is_not_found() -> None:
    assert Client().get("/help/not-a-guide/").status_code == NOT_FOUND


@override_settings(CLINIC_WEBSITE_CONTACT_EMAIL="equipe@clinic-ops.invalid")
def test_contact_page_offers_the_configured_address() -> None:
    body = Client().get("/contact/").content.decode()

    assert 'href="mailto:equipe@clinic-ops.invalid"' in body
    assert "data-contact-unpublished" not in body


@override_settings(CLINIC_WEBSITE_CONTACT_EMAIL="")
def test_contact_page_says_when_no_address_is_published() -> None:
    body = Client().get("/contact/").content.decode()

    assert "mailto:" not in body
    assert "data-contact-unpublished" in body


def test_contact_address_contract_fails_closed() -> None:
    assert resolve_website_contact_email({}) == ""
    assert (
        resolve_website_contact_email(
            {"CLINIC_WEBSITE_CONTACT_EMAIL": " equipe@clinic-ops.invalid "}
        )
        == "equipe@clinic-ops.invalid"
    )
    with pytest.raises(ImproperlyConfigured, match="one email address"):
        resolve_website_contact_email({"CLINIC_WEBSITE_CONTACT_EMAIL": "a, b"})
