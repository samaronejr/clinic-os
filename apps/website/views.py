"""Public Clinic Ops website and help center: GET-only, no session writes.

Every route here is listed in the tenancy bypass (``apps/tenancy/middleware.py``).
The pages run no tenant query and read no record, so every visitor gets the same
page. The one exception is the home route, which forwards a signed-in staff
session to its workspace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from django.conf import settings
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_safe

from apps.core.workspace import has_session_identity
from apps.website.guides import (
    AUDIENCES,
    FIRST_STEPS,
    Guide,
    first_steps,
    guide_by_slug,
    guides_for,
)

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse, HttpResponseBase


@require_safe
def home(request: HttpRequest) -> HttpResponseBase:
    """Greet visitors with the product site; send signed-in staff to work."""
    if has_session_identity(request):
        return redirect("workspace-home")
    return render(
        request,
        "website/home.html",
        {"page": "home", "starter_guides": first_steps()[:3]},
    )


@require_safe
def product(request: HttpRequest) -> HttpResponse:
    """Describe each module and whether it runs on a simulated provider."""
    return render(request, "website/product.html", {"page": "product"})


@require_safe
def security(request: HttpRequest) -> HttpResponse:
    """State the security posture, including what is not approved yet."""
    return render(request, "website/security.html", {"page": "security"})


@require_safe
def contact(request: HttpRequest) -> HttpResponse:
    """Offer the deploy-configured contact address, or say it is unpublished."""
    return render(
        request,
        "website/contact.html",
        {"page": "contact", "contact_email": settings.CLINIC_WEBSITE_CONTACT_EMAIL},
    )


@require_safe
def brand(request: HttpRequest) -> HttpResponse:
    """Show the brand board: the supplied mark, palette, type and voice."""
    return render(request, "website/brand.html", {"page": "brand"})


@require_safe
def pricing(_request: HttpRequest) -> NoReturn:
    """Reserve the pricing slot: a 404 that no page links to.

    Todo 74 replaces this body once the owner approves the packaging, and
    records that approval in ``docs/web/claims.yaml`` (``pricing``).
    """
    raise Http404


@require_safe
def help_index(request: HttpRequest) -> HttpResponse:
    """List the onboarding path, then every guide grouped by audience."""
    groups = [
        {"key": key, "label": label, "guides": guides_for(key)}
        for key, label in AUDIENCES.items()
    ]
    path = [
        {"guide": guide, "audience": AUDIENCES[guide.audience]}
        for guide in first_steps()
    ]
    return render(
        request,
        "website/help_index.html",
        {"page": "help", "first_steps": path, "groups": groups},
    )


@require_safe
def help_guide(request: HttpRequest, slug: str) -> HttpResponse:
    """Render one guide step by step with the screens' real labels."""
    guide = guide_by_slug(slug)
    if guide is None:
        raise Http404
    return render(request, "website/help_guide.html", _guide_context(guide))


def _guide_context(guide: Guide) -> dict[str, object]:
    steps = [
        {
            "place": step.screen.title.text,
            "sentence": step.sentence(),
            "result": step.result,
        }
        for step in guide.steps
    ]
    context: dict[str, object] = {
        "page": "help",
        "guide": guide,
        "audience": AUDIENCES[guide.audience],
        "steps": steps,
        "related": [g for g in guides_for(guide.audience) if g.slug != guide.slug],
    }
    if guide.slug in FIRST_STEPS:
        track = first_steps()
        position = FIRST_STEPS.index(guide.slug)
        context["track_position"] = position + 1
        context["track_length"] = len(track)
        context["previous_step"] = track[position - 1] if position > 0 else None
        context["next_step"] = (
            track[position + 1] if position + 1 < len(track) else None
        )
    return context
