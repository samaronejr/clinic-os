"""Natural-key inputs for the published pre-sequence inbox case script."""

from typing import Final

TIMESTAMP = "1790000000"
LATE = "1790000050"

# Step vocabulary: ("send",), ("dispatch", label), ("status", label, state[,
# stamp]), ("retry"|"retry_skew"|"retry_tie",), ("close",). Labels name
# attempts in creation order: a, b, c.
_A_FAILED = (("send",), ("dispatch", "a"), ("status", "a", "failed"))
CASE_STEPS: dict[str, tuple[str, tuple[tuple[str, ...], ...]]] = {
    # Single attempt: every outbox and provider outcome the base can record.
    "p_queued": ("whatsapp", (("send",),)),
    "p_sent": ("whatsapp", (("send",), ("dispatch", "a"))),
    "p_sent_receipt": (
        "whatsapp",
        (("send",), ("dispatch", "a"), ("status", "a", "sent")),
    ),
    "p_delivered": (
        "whatsapp",
        (("send",), ("dispatch", "a"), ("status", "a", "delivered")),
    ),
    "p_read": ("whatsapp", (("send",), ("dispatch", "a"), ("status", "a", "read"))),
    "p_failed": ("whatsapp", _A_FAILED),
    "p_failed_delivered": (
        "whatsapp",
        (*_A_FAILED, ("status", "a", "delivered", LATE)),
    ),
    "p_cancelled": ("whatsapp", (("send",), ("close",), ("dispatch", "a"))),
    # The original and one retry, in every outcome of the retry.
    "r_queued": ("whatsapp", (*_A_FAILED, ("retry",))),
    "r_sent": ("whatsapp", (*_A_FAILED, ("retry",), ("dispatch", "b"))),
    "r_failed": (
        "whatsapp",
        (*_A_FAILED, ("retry",), ("dispatch", "b"), ("status", "b", "failed")),
    ),
    "r_delivered": (
        "whatsapp",
        (*_A_FAILED, ("retry",), ("dispatch", "b"), ("status", "b", "delivered")),
    ),
    "r_cancelled": (
        "whatsapp",
        (*_A_FAILED, ("retry",), ("close",), ("dispatch", "b")),
    ),
    # Email/SMS receipts name no attempt; a sole attempt still has parity.
    "e_failed": ("email", _A_FAILED),
    "e_retry_sent": ("email", (*_A_FAILED, ("retry",), ("dispatch", "b"))),
    "e_retry_failed": (
        "email",
        (*_A_FAILED, ("retry",), ("dispatch", "b"), ("status", "b", "failed")),
    ),
    # Round-3 worker clocks: the retry's INSERT behind, or tied and lower.
    "skew": ("whatsapp", (*_A_FAILED, ("retry_skew",), ("dispatch", "b"))),
    "tie": ("whatsapp", (*_A_FAILED, ("retry_tie",), ("dispatch", "b"))),
    # Two retries: the base recorded no order between them.
    "ambiguous": (
        "whatsapp",
        (
            *_A_FAILED,
            ("retry",),
            ("dispatch", "b"),
            ("status", "b", "failed"),
            ("retry",),
        ),
    ),
    # Three attempts the provider never accepted (the thread was closed).
    "none_accepted": (
        "whatsapp",
        (
            ("send",),
            ("close",),
            ("dispatch", "a"),
            ("retry",),
            ("dispatch", "b"),
            ("retry",),
        ),
    ),
    # SMS: the same envelope receipts as email, which name no attempt.
    "s_sent": ("sms", (("send",), ("dispatch", "a"))),
    "s_delivered": (
        "sms",
        (("send",), ("dispatch", "a"), ("status", "a", "delivered")),
    ),
    "s_failed": ("sms", _A_FAILED),
    "e_delivered": (
        "email",
        (("send",), ("dispatch", "a"), ("status", "a", "delivered")),
    ),
    "s_retry_sent": ("sms", (*_A_FAILED, ("retry",), ("dispatch", "b"))),
    "s_retry_failed": (
        "sms",
        (*_A_FAILED, ("retry",), ("dispatch", "b"), ("status", "b", "failed")),
    ),
    # Outbox-level outcomes: rejected (terminal failed) and ambiguous (unknown).
    "o_rejected": ("whatsapp", (("send",), ("fault", "a", "rejected"))),
    "o_unknown": ("whatsapp", (("send",), ("fault", "a", "unknown"))),
    "o_rejected_retry_sent": (
        "whatsapp",
        (("send",), ("fault", "a", "rejected"), ("retry",), ("dispatch", "b")),
    ),
    "s_rejected": ("sms", (("send",), ("fault", "a", "rejected"))),
    "e_unknown": ("email", (("send",), ("fault", "a", "unknown"))),
    # Round 5: a later failure of a base-retried email/SMS message, and two
    # workers dispatching the next retry at once.
    **dict.fromkeys(
        ("e_stuck", "e_two_dispatch"),
        ("email", (*_A_FAILED, ("retry",), ("dispatch", "b"))),
    ),
    "s_stuck": ("sms", (*_A_FAILED, ("retry",), ("dispatch", "b"))),
    # Round 5 (b): B fails too, C (lowest UUID) is accepted, then A fails late.
    "r5_b": (
        "whatsapp",
        (
            *_A_FAILED,
            ("retry",),
            ("dispatch", "b"),
            ("status", "b", "failed"),
            ("retry_tie",),
            ("dispatch", "c"),
            ("status", "a", "failed", LATE),
        ),
    ),
    # Round 8: the base left a send in flight (ambiguous provider answer).
    "r8_legacy_inflight": ("whatsapp", (("send",), ("fault", "a", "unknown"))),
    # Round 7: a single queued attempt the candidate's worker sends.
    **dict.fromkeys(("r7_inflight", "r7_rejected"), ("whatsapp", (("send",),))),
    # Round 6: the base itself left two accepted attempts (B and C) open.
    **dict.fromkeys(
        ("r6_two_open", "r6_raw_sql"),
        (
            "whatsapp",
            (
                *_A_FAILED,
                ("retry",),
                ("dispatch", "b"),
                ("status", "a", "failed", LATE),
                ("retry",),
                ("dispatch", "c"),
            ),
        ),
    ),
    # Round 4: B accepted under a skewed clock, a late failure for A makes the
    # base itself show failed and queue C.
    **dict.fromkeys(
        ("accepted_then_queued", "queued_cancelled_first", "race", "r5_a", "crash"),
        (
            "whatsapp",
            (
                *_A_FAILED,
                ("retry_skew",),
                ("dispatch", "b"),
                ("status", "a", "failed", LATE),
                ("retry",),
            ),
        ),
    ),
}

CASES: Final = tuple(
    (
        name,
        channel,
        sum(step[0] in {"send", "retry", "retry_skew", "retry_tie"} for step in steps),
    )
    for name, (channel, steps) in CASE_STEPS.items()
)
ACCOUNTS: Final = {
    "whatsapp": ("whatsapp-cloud-v1", "100000000000901"),
    "email": ("inbox-email-v1", "upgrade-caixa-1"),
    "sms": ("inbox-sms-v1", "upgrade-sms-1"),
}
INBOUND_BODY: Final = b"Oi"
OUTBOUND_BODY: Final = b"Mensagem sintetica"
STATUS_RECEIPTS: Final = {
    channel: sum(
        step[0] == "status"
        for declared_channel, steps in CASE_STEPS.values()
        if declared_channel == channel
        for step in steps
    )
    for channel in ACCOUNTS
}
PROVIDER_METER_CASES: Final = frozenset(
    case
    for case, (channel, steps) in CASE_STEPS.items()
    if channel == "whatsapp"
    and any(step[0] == "status" and step[2] in {"sent", "delivered"} for step in steps)
)
METERED_DELIVERY_CASES: Final = frozenset(
    case
    for case, (_, steps) in CASE_STEPS.items()
    if any(
        step[0] == "status" and step[2] in {"sent", "delivered", "read"}
        for step in steps
    )
)

# Visible states after the declared steps. In particular a cancelled attempt
# is shown as failed, and an unknown provider outcome remains queued.
PARITY_STATES: Final = {
    "p_queued": "queued",
    "p_sent": "sent",
    "p_sent_receipt": "sent",
    "p_delivered": "delivered",
    "p_read": "read",
    "p_failed": "failed",
    "p_failed_delivered": "delivered",
    "p_cancelled": "failed",
    "r_queued": "queued",
    "r_sent": "sent",
    "r_failed": "failed",
    "r_delivered": "delivered",
    "r_cancelled": "failed",
    "e_failed": "failed",
    "e_retry_sent": "sent",
    "e_delivered": "delivered",
    "e_unknown": "queued",
    "s_sent": "sent",
    "s_delivered": "delivered",
    "s_failed": "failed",
    "s_retry_sent": "sent",
    "s_rejected": "failed",
    "o_rejected": "failed",
    "o_unknown": "queued",
    "o_rejected_retry_sent": "sent",
}


def contact_address(channel: str, ordinal: int) -> bytes:
    """Resolve the seeded contact by its channel and ordinal, never by UUID."""
    if channel == "email":
        return f"paciente.upgrade.{ordinal}@example.invalid".encode("ascii")
    return f"+55119999{ordinal:05d}".encode("ascii")
