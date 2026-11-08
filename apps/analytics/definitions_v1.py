"""Frozen metric definitions v1; migrations retain these exact semantics."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from django.utils.translation import gettext_noop


@dataclass(frozen=True, slots=True)
class Metric:
    """A formula is data over two approved aggregates, never executable SQL."""

    key: str
    label: str
    numerator: str
    denominator: str
    source: str
    unit: Literal["percent", "seconds", "minor", "count"]
    time_basis: Literal["window", "as_of"] = "window"
    available: bool = True

    def specification(self) -> dict[str, object]:
        """Publish every governance field in metric-definition-v1."""
        ratio = self.unit in {"percent", "seconds"}
        totals_only = self.time_basis == "as_of" or self.key in {
            "utilization",
            "activation",
            "retained_usage",
        }
        return {
            "schema": "metric-definition-v1",
            "metric_id": self.key,
            "version": 1,
            "grain": "clinic",
            "formula": {
                "op": "ratio" if ratio else "sum",
                "numerator": "numerator",
                "denominator": "denominator" if ratio else "one",
                "scale": 100 if self.unit == "percent" else 1,
            },
            "numerator": self.numerator,
            "denominator": self.denominator,
            "unit": self.unit,
            "time_basis": self.time_basis,
            "window": {"bounds": "[start,end)", "maximum_days": 366},
            "timezone": "clinic",
            "filters": [] if totals_only else ["weekdays"],
            "dimensions": [] if totals_only else ["day"],
            "late_data": "explicit refresh rebuilds; previous snapshots are retained",
            "freshness_seconds": 900,
            "authorization": {
                "all": ["charge.read"],
                "any": ["configuration.clinic", "configuration.organization"],
                "scope": "exact clinic; requester; current authorization fingerprint",
            },
            "quality": {
                "missing_source": "partial",
                "zero_denominator": "null",
                "unresolved": "partial",
                "forecast": "unvalidated; unavailable",
            },
            "provenance": self.source,
            "available": self.available,
            "benchmarking": "disabled; BLOCKED-ON-EG-2",
        }


_METRICS = (
    Metric(
        "utilization",
        gettext_noop("Utilization"),
        "Booked, arrived, in-progress, completed and no-show practitioner seconds",
        "Unretired practitioner availability seconds; both clipped to the window",
        "scheduling.Appointment[start_at,end_at); AvailabilityBlock[start_at,end_at)",
        "percent",
    ),
    Metric(
        "no_show",
        gettext_noop("Resolved no-show rate"),
        "Appointments resolved as no_show",
        "Appointments resolved as completed or no_show; unresolved visits are partial",
        "scheduling.Appointment.start_at",
        "percent",
    ),
    Metric(
        "waiting_time",
        gettext_noop("Waiting time"),
        "Seconds from queue arrival to first call",
        "Called queue entries; uncalled arrivals remain partial",
        "scheduling.QueueEntry.created_at -> called_at",
        "seconds",
    ),
    Metric(
        "task_turnaround",
        gettext_noop("Task turnaround"),
        "Seconds from task creation to its terminal done update",
        "Completed tasks; unfinished tasks remain partial",
        "workflows.Task.created_at -> updated_at(done)",
        "seconds",
    ),
    Metric(
        "receivables",
        gettext_noop("Receivables"),
        "Posted patient_ar and insurer_ar debits less credits through the cutoff",
        "One; cumulative BRL centavos, including opening balances",
        "billing.JournalBook -> JournalEntry.posted_at -> JournalLine -> Account",
        "minor",
        "as_of",
    ),
    Metric(
        "verified_collections",
        gettext_noop("Verified collections"),
        "Posted collection asset debits; unverified observations are excluded",
        "One; BRL centavos before fees and refunds",
        "billing.JournalEntry(collection).posted_at -> JournalLine -> Account",
        "minor",
    ),
    Metric(
        "net_collections",
        gettext_noop("Net collections"),
        "Posted collections less confirmed refund and chargeback asset credits",
        "One; BRL centavos before PSP fees",
        "billing.JournalEntry(collection,refund_confirmed).posted_at -> JournalLine",
        "minor",
    ),
    Metric(
        "denials",
        gettext_noop("Denials"),
        "Adjudicated denial postings; generic payer adjustments are not denials",
        "One; no reviewed denial posting classification exists on this base",
        "Journal-only source required; insurance denial posting contract unavailable",
        "minor",
        available=False,
    ),
    Metric(
        "payout_liabilities",
        gettext_noop("Payout liabilities"),
        "Posted practitioner_payables and payout_in_transit credits less debits",
        "One; cumulative BRL centavos, including retained unknown outcomes",
        "billing.JournalEntry.posted_at -> JournalLine -> Account",
        "minor",
        "as_of",
    ),
    Metric(
        "result_ack_latency",
        gettext_noop("Result acknowledgment latency"),
        "Seconds from matched result version arrival to its acknowledgment",
        "Acknowledged matched versions; unacknowledged versions remain partial",
        "orders.ResultVersion.created_at -> ResultAcknowledgment.created_at",
        "seconds",
    ),
    Metric(
        "follow_up_overdue",
        gettext_noop("Overdue follow-ups"),
        "Nonterminal follow_up tasks with a deadline before refresh time",
        "One; tasks whose due date lies in the window",
        "workflows.Task.due_at",
        "count",
    ),
    Metric(
        "support_load",
        gettext_noop("Support load"),
        "Open customer support cases created in the window",
        "One; no support-case source is installed on this base",
        "Unavailable support-case source; operational tasks are not support cases",
        "count",
        available=False,
    ),
    Metric(
        "ai_correction_time",
        gettext_noop("AI correction elapsed time"),
        "Elapsed seconds from suggestion creation to a modified review",
        "Modified suggestions; includes queue time, not active editing time",
        "scribe.ReviewDecision.created_at; "
        "duration from DraftSectionSuggestion.created_at",
        "seconds",
    ),
    Metric(
        "activation",
        gettext_noop("Staff activation"),
        "Current exact-clinic staff with an operational audit event in the window",
        "Current active exact-clinic staff; excludes reporting events",
        "identity.UserClinicRole; audit.AuditEvent metadata only",
        "percent",
    ),
    Metric(
        "retained_usage",
        gettext_noop("Retained paid usage"),
        "Paid staff active in both adjacent equal windows",
        "Paid staff active in the preceding window; subscription evidence required",
        "Subscription and metered usage source unavailable; "
        "never inferred from the clinic journal",
        "percent",
        available=False,
    ),
)
METRICS: Final = MappingProxyType({metric.key: metric for metric in _METRICS})
