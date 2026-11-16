"""The owner's global AI switch stops every front-desk bridge (AI-04).

The deployed ai.0008 definitions are the starting point, never edited in place.
Each bridge that selects the clinic's enabled front_desk policy now also
requires the enabled global gateway switch. clinic_resolver already reads that
table, so no grant, table or signature changes; reverse restores ai.0008.
"""

from apps.ai.migrations._front_desk_authority_sql import SQL as PRIOR

_ANCHOR = "AND pv.enabled AND pv.autonomy_ceiling='A2'"
_SWITCHED = (
    "AND pv.enabled AND pv.autonomy_ceiling='A2'\n"
    "    AND coalesce((SELECT gs.enabled FROM clinic_app.ai_gatewayswitch gs\n"
    "     WHERE gs.id=1),false)"
)
BRIDGES = (
    "ai_front_desk_enqueue",
    "ai_front_desk_turn",
    "ai_front_desk_verify",
    "ai_front_desk_state",
    "ai_front_desk_finish",
)


def _function(name: str) -> str:
    """Return one deployed ai.0008 definition; plain index and slice only."""
    head = f"CREATE OR REPLACE FUNCTION clinic_app.{name}("
    start = PRIOR.index(head)
    end = PRIOR.index("$f$;", start) + len("$f$;")
    return PRIOR[start:end]


def _switched(name: str) -> str:
    text = _function(name)
    if len(text.split(_ANCHOR)) - 1 != 1:  # pragma: no cover - import-time contract
        message = f"front-desk policy anchor moved in {name}"
        raise RuntimeError(message)
    return text.replace(_ANCHOR, _SWITCHED)


SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + "\n".join(_switched(name) for name in BRIDGES)
    + "\nRESET ROLE;\n"
)
REVERSE_SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + "\n".join(_function(name) for name in BRIDGES)
    + "\nRESET ROLE;\n"
)
