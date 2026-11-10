"""Translate closed financial vocabularies; render cents without floating point."""

from typing import Final

from django import template
from django.utils.translation import gettext_lazy as _

register = template.Library()

ACCOUNT_LABELS: Final = {
    "bank": _("Bank"),
    "petty_cash": _("Petty cash"),
    "psp_clearing": _("PSP clearing"),
    "payout_in_transit": _("Payouts in transit"),
    "patient_ar": _("Patient receivables"),
    "insurer_ar": _("Insurer receivables"),
    "patient_advances": _("Patient advances"),
    "refunds_payable": _("Refunds payable"),
    "unidentified_receipts": _("Unidentified receipts"),
    "insurer_overpayments": _("Insurer overpayments"),
    "practitioner_payables": _("Practitioner payables"),
    "taxes_payable": _("Taxes payable"),
    "service_revenue": _("Service revenue"),
    "contra_revenue": _("Contra-revenue"),
    "discounts": _("Discounts"),
    "payer_adjustments": _("Payer adjustments"),
    "psp_fees": _("PSP fees"),
    "practitioner_compensation": _("Practitioner compensation"),
    "bad_debt": _("Bad debt"),
    "suspense": _("Suspense"),
    "opening_balances": _("Opening balances"),
}
STATE_LABELS: Final = {
    "proposed": _("Proposed"),
    "posted": _("Posted"),
    "observed": _("Observed"),
    "verified": _("Verified"),
    "quarantined": _("Quarantined"),
    "requested": _("Requested"),
    "approved": _("Approved"),
    "sending": _("Sending"),
    "confirmed": _("Confirmed"),
    "rejected": _("Rejected"),
    "unknown": _("Unknown outcome"),
    "open": _("Open"),
    "closed": _("Closed"),
}


@register.filter
def minor_brl(value: int) -> str:
    """Format integer-centavo projections; this is not an input parser."""
    whole, cents = divmod(abs(value), 100)
    grouped = f"{whole:,}".replace(",", ".")
    return f"R$ {'-' if value < 0 else ''}{grouped},{cents:02d}"


@register.filter
def money_state(value: str) -> str:
    """Render only the database's closed state vocabulary."""
    return str(STATE_LABELS[value])


@register.filter
def account_label(value: str) -> str:
    """Use display msgids, never a raw internal account code."""
    return str(ACCOUNT_LABELS[value])
