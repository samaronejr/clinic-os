"""Exact gateway posture; model terms and global controls are owner-written."""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
AGENT_TABLES: Final = frozenset(
    {
        "ai_agentpolicyversion",
        "ai_agentrun",
        "ai_actionproposal",
        "ai_approval",
        "ai_actionexecution",
        "ai_actionreceipt",
        "ai_compensation",
    }
)
CUSTOM_RLS_TABLES: Final = (
    frozenset({"ai_aiartifact", "ai_aiinvocation", "ai_aicircuit"}) | AGENT_TABLES
)
NON_RLS_TABLES: Final = frozenset({"ai_modelconfiguration", "ai_gatewayswitch"})
RUNTIME_GRANTS: Final = {
    **{table: frozenset({"SELECT", "INSERT"}) for table in CUSTOM_RLS_TABLES},
    **{table: frozenset({"SELECT"}) for table in NON_RLS_TABLES},
    **{
        table: frozenset({"SELECT", "INSERT", "UPDATE"})
        for table in (
            "ai_agentrun",
            "ai_actionproposal",
            "ai_actionexecution",
            "ai_compensation",
        )
    },
}
AGENT_GRANTS: Final = {
    **{table: frozenset({"SELECT"}) for table in AGENT_TABLES},
    **{
        table: frozenset({"SELECT", "UPDATE"})
        for table in ("ai_agentrun", "ai_actionproposal")
    },
    **{
        table: frozenset({"SELECT", "INSERT", "UPDATE"})
        for table in ("ai_actionexecution", "ai_compensation")
    },
    "ai_actionreceipt": frozenset({"SELECT", "INSERT"}),
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
