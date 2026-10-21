"""Exact physician-only capture policies and column-level transition grants."""

from typing import Final

RLS_TARGETS: Final[frozenset[tuple[str, str]]] = frozenset()
CUSTOM_RLS_TABLES: Final = frozenset(
    {
        "scribe_recordingsession",
        "scribe_captureepoch",
        "scribe_audiochunk",
        "scribe_recordingcommand",
    }
)
NON_RLS_TABLES: Final[frozenset[str]] = frozenset()
RUNTIME_GRANTS: Final = {
    table: frozenset({"SELECT", "INSERT"}) for table in CUSTOM_RLS_TABLES
}
COLUMN_GRANTS: Final = frozenset(
    (table, column, "UPDATE")
    for table, columns in {
        "scribe_recordingsession": (
            "state",
            "epoch",
            "revision",
            "discarded",
            "stopped_at",
            "token_digest",
        ),
        "scribe_captureepoch": ("ended_at", "reason", "high_water", "gaps"),
        "scribe_audiochunk": (
            "state",
            "operation_id",
            "encrypted_data",
            "wrapped_key",
        ),
    }.items()
    for column in columns
)
