"""Independent exact fiscal resolver grants, signatures and execution posture."""

from typing import Final

TABLE_GRANTS: Final = {
    ("billing_fiscalconfiguration", "SELECT"),
    ("billing_fiscaldocument", "SELECT"),
    ("billing_fiscaltransition", "SELECT"),
    ("billing_fiscaltransition", "INSERT"),
}
RESULTS: Final = {
    ("billing_fiscal_access", "requested_clinic uuid"): "boolean",
    ("billing_fiscal_guard", ""): "trigger",
    ("billing_fiscal_snapshot", ""): "trigger",
}
POSTURE: Final = {
    "billing_fiscal_access": ("v", True, ["clinic_app", "clinic_resolver"]),
    "billing_fiscal_guard": ("v", False, ["clinic_resolver"]),
    "billing_fiscal_snapshot": ("v", False, ["clinic_resolver"]),
}
