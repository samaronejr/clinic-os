"""No-store bundle administration and source-visible deterministic calculations."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, NotRequired, TypedDict

from django.core.exceptions import ValidationError
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.ehr.calculators import calculate
from apps.ehr.calculators.catalog import DEFINITIONS
from apps.ehr.calculators.contracts import Calculation, CalculatorDefinition, Refusal
from apps.ehr.pack_catalog import PACKS, PackDefinition
from apps.ehr.pack_forms import calculator_form
from apps.ehr.pack_models import SpecialtyPack
from apps.ehr.packs import (
    ADMIN_ROLES,
    READ_ROLES,
    install_pack,
    installed_packs,
    lock_catalog,
    reorder_packs,
    uninstall_pack,
)
from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.identity.current_context import (
    CurrentActorError,
    require_current_actor_clinic_roles,
)
from apps.identity.otp import privileged_totp_required

if TYPE_CHECKING:
    from uuid import UUID

    from django.forms import Form
    from django.http import HttpRequest, HttpResponse, QueryDict

POST_ACTIONS = frozenset({"install", "uninstall", "calculate", "move_up", "move_down"})


@dataclass(frozen=True, slots=True)
class CalculatorCard:
    """One source-bound native calculator with its transient submission."""

    definition: CalculatorDefinition
    title: str
    form: Form
    available: bool
    result: Calculation | None


@dataclass(frozen=True, slots=True)
class PackCard:
    """One catalogue version, its retained installation, and available controls."""

    definition: PackDefinition
    row: SpecialtyPack | None
    active: bool
    retired: bool
    can_move_up: bool
    can_move_down: bool
    calculators: tuple[CalculatorCard, ...]


class PackContext(TypedDict):
    """Only the native template's explicitly consumed values cross this boundary."""

    clinic_id: UUID
    cards: list[PackCard]
    can_install: bool
    outcome: str
    invalid: bool
    error: NotRequired[str]


def continuation(clinic_id: UUID) -> str:
    """Keep patient selectors and calculator values out of continuations."""
    return reverse("ehr:specialty-packs", kwargs={"clinic_id": clinic_id})


def _calculator_card(
    definition: CalculatorDefinition, data: QueryDict | None, *, active: bool
) -> CalculatorCard:
    """Interpret one transient submission after its installed-bundle check."""
    form = calculator_form(definition, data)
    result: Calculation | None = None
    if data is not None:
        if not active or data.get("version") != str(definition.version):
            raise ClinicalAccessDeniedError
        if form.is_valid():
            computed = calculate(
                key=definition.key,
                version=definition.version,
                inputs={name: str(value) for name, value in form.cleaned_data.items()},
            )
            if isinstance(computed, Refusal):
                form.add_error(
                    None,
                    _("Unavailable: license pending")
                    if computed.code == "license_pending"
                    else _(
                        "Enter valid values with a decimal point and explicit units."
                    ),
                )
            else:
                result = computed
    return CalculatorCard(
        definition,
        _(definition.title),
        form,
        definition.license_status != "pending",
        result,
    )


def _command(request: HttpRequest, clinic_id: UUID) -> str:
    """Make one serialized native administration decision."""
    key = request.POST.get("pack_key", "")
    action = request.POST.get("action", "")
    if action in POST_ACTIONS and request.POST.get("version") != "1":
        raise ValidationError(_("Choose a published specialty pack version."))
    match action:
        case "install":
            install_pack(clinic_id=clinic_id, pack_key=key, version=1)
            return _("Specialty pack installed.")
        case "uninstall":
            uninstall_pack(clinic_id=clinic_id, pack_key=key, version=1)
            return _("Pack uninstalled. Existing records are preserved.")
        case "move_up" | "move_down":
            lock_catalog(clinic_id=clinic_id)
            keys = [row.key for row in installed_packs(clinic_id=clinic_id)]
            if key not in keys:
                raise ClinicalAccessDeniedError
            index = keys.index(key)
            target = index + (-1 if action == "move_up" else 1)
            if target < 0 or target >= len(keys):
                reason = "pack_order_changed"
                raise ClinicalConflictError(reason)
            keys[index], keys[target] = keys[target], keys[index]
            reorder_packs(clinic_id=clinic_id, pack_keys=tuple(keys))
            return _("Pack order saved.")
        case _:
            raise ClinicalAccessDeniedError


def _context(
    request: HttpRequest, clinic_id: UUID, *, read_only: bool = False
) -> PackContext:
    require_current_actor_clinic_roles(clinic_id, READ_ROLES)
    try:
        require_current_actor_clinic_roles(clinic_id, ADMIN_ROLES)
        can_install = True
    except CurrentActorError:
        can_install = False
    action = (
        request.POST.get("action", "")
        if request.method == "POST" and not read_only
        else ""
    )
    key = request.POST.get("pack_key", "")
    if action and key not in PACKS:
        raise ClinicalAccessDeniedError
    outcome = _command(request, clinic_id) if action and action != "calculate" else ""
    rows = {row.key: row for row in SpecialtyPack.objects.filter(clinic_id=clinic_id)}
    active_keys = [
        row.key
        for row in sorted(rows.values(), key=lambda row: (row.position, row.key))
        if row.uninstalled_at is None
    ]
    definitions = [
        *[PACKS[key] for key in active_keys],
        *[definition for key, definition in PACKS.items() if key not in active_keys],
    ]
    cards: list[PackCard] = []
    for definition in definitions:
        pack_key = definition.key
        row = rows.get(pack_key)
        active = row is not None and row.uninstalled_at is None
        calculators: list[CalculatorCard] = []
        for calculator_key in definition.calculators:
            calculator = DEFINITIONS[calculator_key]
            submitted = (
                action == "calculate"
                and key == pack_key
                and request.POST.get("calculator_key") == calculator_key
            )
            calculators.append(
                _calculator_card(
                    calculator,
                    request.POST if submitted else None,
                    active=active,
                )
            )
        cards.append(
            PackCard(
                definition,
                row,
                active,
                row is not None and row.uninstalled_at is not None,
                active and pack_key != active_keys[0],
                active and pack_key != active_keys[-1],
                tuple(calculators),
            )
        )
    if (
        action == "calculate"
        and request.POST.get("calculator_key") not in PACKS[key].calculators
    ):
        raise ClinicalAccessDeniedError
    return {
        "clinic_id": clinic_id,
        "cards": cards,
        "can_install": can_install,
        "outcome": outcome,
        "invalid": any(c.form.errors for card in cards for c in card.calculators),
    }


@never_cache
@privileged_totp_required(continuation)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def specialty_packs(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    """Serve the same native surface with or without HTMX."""
    try:
        context = _context(request, clinic_id)
    except (CurrentActorError, ClinicalAccessDeniedError, ValueError):
        return render(request, "403.html", status=403)
    except (ValidationError, ClinicalConflictError):
        # Nothing may be written by a refused command; service savepoints own it.
        context = _context(request, clinic_id, read_only=True)
        context["error"] = _("The pack changed. Reload and review its current version.")
        return render(request, "ehr/specialty_packs.html", context, status=409)
    status = HTTPStatus.BAD_REQUEST if context["invalid"] else HTTPStatus.OK
    return render(request, "ehr/specialty_packs.html", context, status=status)
