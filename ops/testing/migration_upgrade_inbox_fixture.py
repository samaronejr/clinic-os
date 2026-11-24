"""Natural-key inputs for the published pre-sequence inbox case script."""

from typing import Final

# Order is part of the fixture: the script assigns addresses by case ordinal.
# Each entry declares the channel and number of enqueue commands.
CASES: Final = (
    ("p_queued", "whatsapp", 1),
    ("p_sent", "whatsapp", 1),
    ("p_sent_receipt", "whatsapp", 1),
    ("p_delivered", "whatsapp", 1),
    ("p_read", "whatsapp", 1),
    ("p_failed", "whatsapp", 1),
    ("p_failed_delivered", "whatsapp", 1),
    ("p_cancelled", "whatsapp", 1),
    ("r_queued", "whatsapp", 2),
    ("r_sent", "whatsapp", 2),
    ("r_failed", "whatsapp", 2),
    ("r_delivered", "whatsapp", 2),
    ("r_cancelled", "whatsapp", 2),
    ("e_failed", "email", 1),
    ("e_retry_sent", "email", 2),
    ("e_retry_failed", "email", 2),
    ("skew", "whatsapp", 2),
    ("tie", "whatsapp", 2),
    ("ambiguous", "whatsapp", 3),
    ("none_accepted", "whatsapp", 3),
    ("s_sent", "sms", 1),
    ("s_delivered", "sms", 1),
    ("s_failed", "sms", 1),
    ("e_delivered", "email", 1),
    ("s_retry_sent", "sms", 2),
    ("s_retry_failed", "sms", 2),
    ("o_rejected", "whatsapp", 1),
    ("o_unknown", "whatsapp", 1),
    ("o_rejected_retry_sent", "whatsapp", 2),
    ("s_rejected", "sms", 1),
    ("e_unknown", "email", 1),
    ("e_stuck", "email", 2),
    ("e_two_dispatch", "email", 2),
    ("s_stuck", "sms", 2),
    ("r5_b", "whatsapp", 3),
    ("r8_legacy_inflight", "whatsapp", 1),
    ("r7_inflight", "whatsapp", 1),
    ("r7_rejected", "whatsapp", 1),
    ("r6_two_open", "whatsapp", 3),
    ("r6_raw_sql", "whatsapp", 3),
    ("accepted_then_queued", "whatsapp", 3),
    ("queued_cancelled_first", "whatsapp", 3),
    ("race", "whatsapp", 3),
    ("r5_a", "whatsapp", 3),
    ("crash", "whatsapp", 3),
)
ACCOUNTS: Final = {
    "whatsapp": ("whatsapp-cloud-v1", "100000000000901"),
    "email": ("inbox-email-v1", "upgrade-caixa-1"),
    "sms": ("inbox-sms-v1", "upgrade-sms-1"),
}
INBOUND_BODY: Final = b"Oi"
OUTBOUND_BODY: Final = b"Mensagem sintetica"


def contact_address(channel: str, ordinal: int) -> bytes:
    """Resolve the seeded contact by its channel and ordinal, never by UUID."""
    if channel == "email":
        return f"paciente.upgrade.{ordinal}@example.invalid".encode("ascii")
    return f"+55119999{ordinal:05d}".encode("ascii")
