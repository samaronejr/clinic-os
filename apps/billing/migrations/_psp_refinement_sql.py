"""Frozen 0017: refine same-sequence uncertainty in retention and completion."""

from apps.billing.migrations._psp_observer_sql import GUARD, OBSERVER_FINISH


def _refine(definition: str, *, prior: str, current: str) -> str:
    """Retain the original definition except for the equal-version predicate."""
    old = f"({prior}.version={current}.version AND"
    if definition.count(old) != 1:
        msg = "unexpected frozen PSP version fence"
        raise ValueError(msg)
    return definition.replace(old, f"{old} {prior}.status<>'unknown' AND")


SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _refine(GUARD, prior="e", current="NEW")
    + _refine(
        OBSERVER_FINISH.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1),
        prior="n",
        current="e",
    )
    + "\nRESET ROLE;"
)
