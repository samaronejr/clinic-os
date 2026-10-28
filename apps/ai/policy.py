"""Closed organization policy and governed configuration selection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from config.settings.contracts import SYNTHETIC_DATA_MODE
from django.conf import settings
from django.db import connection

from apps.ai.adapters import provider_for
from apps.ai.contracts import (
    MAX_MICROS,
    PURPOSES,
    REGION_CLASSES,
    GatewayInputError,
    GatewayRefusedError,
    Price,
    Purpose,
)
from apps.ai.models import AICircuit, GatewaySwitch, ModelConfiguration
from apps.providers.services import current_version

if TYPE_CHECKING:
    from uuid import UUID

type JsonValue = (
    str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
)


class PolicyJSON(TypedDict):
    """The only values accepted in ClinicConfiguration.ai_policy."""

    enabled: bool
    purposes: list[str]
    monthly_budget_micros: int
    purpose_budgets: dict[str, int]


@dataclass(frozen=True, slots=True)
class OrganizationPolicy:
    """A closed policy, never an authorization or provider approval."""

    enabled: bool = False
    purposes: tuple[str, ...] = ()
    monthly_budget_micros: int = 0
    purpose_budgets: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        """Reject open vocabulary, duplicates, bool money, and missing caps."""
        caps = dict(self.purpose_budgets)
        if (
            type(self.enabled) is not bool
            or type(self.monthly_budget_micros) is not int
            or not 0 <= self.monthly_budget_micros <= MAX_MICROS
            or len(set(self.purposes)) != len(self.purposes)
            or not set(self.purposes) <= set(PURPOSES)
            or len(caps) != len(self.purpose_budgets)
            or not set(caps) <= set(PURPOSES)
            or not set(self.purposes) <= set(caps)
            or any(
                type(cap) is not int or not 0 <= cap <= MAX_MICROS
                for cap in caps.values()
            )
        ):
            raise GatewayInputError
        object.__setattr__(self, "purpose_budgets", tuple(sorted(self.purpose_budgets)))

    def as_json(self) -> PolicyJSON:
        """Return the persisted closed representation."""
        return {
            "enabled": self.enabled,
            "purposes": list(self.purposes),
            "monthly_budget_micros": self.monthly_budget_micros,
            "purpose_budgets": dict(self.purpose_budgets),
        }


def parse_policy(raw: JsonValue | PolicyJSON) -> OrganizationPolicy:
    """Parse the JSON boundary exactly once, refusing unknown keys and types."""
    if raw == {}:
        return OrganizationPolicy()
    if not isinstance(raw, dict) or set(raw) != {
        "enabled",
        "purposes",
        "monthly_budget_micros",
        "purpose_budgets",
    }:
        raise GatewayInputError
    enabled = raw["enabled"]
    purposes = raw["purposes"]
    total = raw["monthly_budget_micros"]
    caps = raw["purpose_budgets"]
    if (
        not isinstance(enabled, bool)
        or not isinstance(purposes, list)
        or not all(isinstance(p, str) for p in purposes)
        or not isinstance(total, int)
        or not isinstance(caps, dict)
    ):
        raise GatewayInputError
    names: list[str] = []
    for purpose in purposes:
        if not isinstance(purpose, str):
            raise GatewayInputError
        names.append(purpose)
    budgets: list[tuple[str, int]] = []
    for name, cap in caps.items():
        if not isinstance(cap, int):
            raise GatewayInputError
        budgets.append((name, cap))
    return OrganizationPolicy(enabled, tuple(names), total, tuple(sorted(budgets)))


def organization_policy() -> OrganizationPolicy:
    """Read the org-effective snapshot through its bounded resolver."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.ai_organization_policy()")
        row = cursor.fetchone()
    raw = row[0] if row and row[0] is not None else {}
    return parse_policy(json.loads(raw) if isinstance(raw, str) else raw)


def enabled_policy(purpose: Purpose) -> OrganizationPolicy:
    """Check both kill switches and explicit per-purpose opt-in on every call."""
    control = GatewaySwitch.objects.filter(pk=1, enabled=True).first()
    policy = organization_policy()
    if (
        settings.CLINIC_DATA_MODE != SYNTHETIC_DATA_MODE
        or control is None
        or not policy.enabled
        or purpose.value not in policy.purposes
    ):
        raise GatewayRefusedError(reason="disabled")
    return policy


def price_for(configuration: ModelConfiguration) -> Price:
    """Adapt approved integer terms without rounding."""
    return Price(
        configuration.input_micros,
        configuration.output_micros,
        configuration.cached_micros,
        configuration.audio_micros,
    )


def check_configuration(configuration: ModelConfiguration, clinic_id: UUID) -> None:
    """Refuse unapproved regions, retired terms, or unavailable capabilities."""
    control = GatewaySwitch.objects.filter(pk=1, enabled=True).first()
    capability = current_version(configuration.capability_key, clinic_id=clinic_id)
    if (
        configuration.state != "active"
        or not configuration.approval_ref
        or not configuration.retention_terms
        or not configuration.training_terms
        or control is None
        or not isinstance(control.approved_regions, list)
        or configuration.region not in control.approved_regions
        or REGION_CLASSES.get(configuration.region) != configuration.jurisdiction_class
        or capability is None
        or capability.state in {"degraded", "revoked"}
        or (
            capability.region
            and capability.region not in {"synthetic", configuration.region}
        )
    ):
        raise GatewayRefusedError(reason="configuration_unavailable")
    provider_for(configuration.provider)


def select_configuration(*, clinic_id: UUID, purpose: Purpose) -> ModelConfiguration:
    """Fallback only from an open primary to approved same-jurisdiction terms."""
    primary = ModelConfiguration.objects.filter(
        purpose=purpose.value, state="active", route="primary"
    ).first()
    if primary is None:
        raise GatewayRefusedError(reason="configuration_unavailable")
    check_configuration(primary, clinic_id)
    if not AICircuit.objects.filter(
        configuration=primary, closed_at__isnull=True
    ).exists():
        return primary
    for fallback in ModelConfiguration.objects.filter(
        purpose=purpose.value,
        state="active",
        route="fallback",
        jurisdiction_class=primary.jurisdiction_class,
    ).order_by("-version"):
        check_configuration(fallback, clinic_id)
        if not AICircuit.objects.filter(
            configuration=fallback, closed_at__isnull=True
        ).exists():
            return fallback
    raise GatewayRefusedError(reason="circuit_open")
