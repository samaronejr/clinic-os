"""Exact gateway posture; model terms and global controls are owner-written."""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final = frozenset(
    {"ai_aiartifact", "ai_aiinvocation", "ai_aicircuit"}
)
NON_RLS_TABLES: Final = frozenset({"ai_modelconfiguration", "ai_gatewayswitch"})
RUNTIME_GRANTS: Final = {
    **{table: frozenset({"SELECT", "INSERT"}) for table in CUSTOM_RLS_TABLES},
    **{table: frozenset({"SELECT"}) for table in NON_RLS_TABLES},
}
COLUMN_GRANTS: Final = frozenset(
    (table, column, "UPDATE")
    for table, columns in {
        "ai_aiinvocation": (
            "operation_id",
            "status",
            "reason_code",
            "tokens_in",
            "tokens_out",
            "tokens_cached",
            "audio_seconds",
            "cost_micros",
            "cost_minor",
            "latency_ms",
            "settled_at",
        ),
    }.items()
    for column in columns
)
