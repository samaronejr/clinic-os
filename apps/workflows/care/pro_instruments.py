"""Which published instruments may be assigned, and what each declares as urgent.

The calculators own arithmetic and licence status; nothing here invents items or
thresholds. A response is always routed to a human. ``concerning`` only raises
that human task's priority, using the one item an instrument publishes as a
safety item (PHQ-9 item 9). Absence of a response carries no meaning at all.
"""

from collections.abc import Callable, Mapping
from typing import Final

from django.utils.translation import gettext_noop

from apps.ehr.calculators import gad7, phq9
from apps.ehr.calculators.catalog import DEFINITIONS
from apps.ehr.calculators.contracts import Calculation, Refusal
from apps.workflows.care.vocab import PRO_INSTRUMENTS

AVAILABLE_LICENSES: Final = frozenset({"publisher_free_use"})
COMPUTE: Final[Mapping[str, Callable[[Mapping[str, str]], Calculation | Refusal]]] = {
    "phq9": phq9.compute,
    "gad7": gad7.compute,
}
# Item identifiers whose non-zero answer asks for prompt human attention. Content
# owner sign-off is an external gate (EG-3); an empty entry means no escalation.
CONCERN_ITEMS: Final[Mapping[str, str]] = {"phq9": "item_9"}
NO_RESPONSE_LABEL: Final = gettext_noop("No response")


def available_keys() -> frozenset[str]:
    """Return instruments that are licensed for use and implemented here."""
    return frozenset(
        key
        for key in PRO_INSTRUMENTS
        if key in DEFINITIONS
        and DEFINITIONS[key].license_status in AVAILABLE_LICENSES
        and key in COMPUTE
    )


def version(key: str) -> int:
    """Return the pinned calculator version of an available instrument."""
    return DEFINITIONS[key].version


def concerning(key: str, answers: Mapping[str, str]) -> bool:
    """Return whether the instrument's own published safety item is non-zero."""
    item = CONCERN_ITEMS.get(key)
    return item is not None and answers.get(item, "0") not in {"", "0"}
