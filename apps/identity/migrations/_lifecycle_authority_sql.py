"""Give lifecycle writers narrow owner authority, never runtime table grants."""

from typing import Final

from ._lifecycle_invitations_sql import INVITATION_SQL
from ._lifecycle_offboarding_sql import OFFBOARDING_SQL


def _invitation_function(*, original: str, replacement: str) -> str:
    """Reuse the token-bound body with a separate owner-owned entrypoint."""
    body = INVITATION_SQL.split("CREATE FUNCTION clinic_app." + original + "(", 1)[
        1
    ].split("$function$;", 1)[0]
    return (
        "CREATE FUNCTION clinic_app.__name__(".replace("__name__", replacement)
        + body
        + "$function$;\n"
    )


OWNER_WRITERS: Final = (
    _invitation_function(
        original="identity_invitation_user",
        replacement="identity_invitation_account",
    )
    + _invitation_function(
        original="identity_invitation_begin",
        replacement="identity_invitation_password",
    )
    + _invitation_function(
        original="identity_invitation_finish",
        replacement="identity_invitation_accept",
    )
    + "CREATE FUNCTION clinic_app.identity_offboarding_revoke("
    + OFFBOARDING_SQL.split(
        "CREATE FUNCTION clinic_app.identity_offboarding_execute(", 1
    )[1].split("$function$;", 1)[0]
    + "$function$;\n"
)

CLAIM_REPAIR: Final = (
    "CREATE OR REPLACE FUNCTION clinic_app.identity_invitation_claim("
    + INVITATION_SQL.split("CREATE FUNCTION clinic_app.identity_invitation_claim(", 1)[
        1
    ]
    .split("$function$;", 1)[0]
    .replace("FOR UPDATE OF invitation, account;", "FOR UPDATE OF invitation;")
    .replace(
        "account.password, invitation.device_id",
        "account.password::text, invitation.device_id",
    )
    + "$function$;\n"
)

ACCEPT_GUARD: Final = (
    "CREATE OR REPLACE FUNCTION clinic_app.identity_invitation_guard()"
    + INVITATION_SQL.split("CREATE FUNCTION clinic_app.identity_invitation_guard()", 1)[
        1
    ]
    .split("$function$;", 1)[0]
    .replace("current_user <> 'clinic_resolver'", "current_user <> 'clinic_owner'")
    + "$function$;\n"
)

FORWARD_SQL: Final = (
    """
SET LOCAL ROLE clinic_resolver;
GRANT EXECUTE ON FUNCTION clinic_app.has_permission(text, uuid, uuid)
    TO clinic_owner;
REVOKE EXECUTE ON FUNCTION clinic_app.identity_invitation_user(uuid, uuid),
    clinic_app.identity_invitation_begin(bytea, bytea, text, text),
    clinic_app.identity_invitation_finish(bytea, text),
    clinic_app.identity_offboarding_execute(uuid, uuid)
    FROM clinic_app;
"""
    + CLAIM_REPAIR
    + """
RESET ROLE;
"""
    + OWNER_WRITERS
    + ACCEPT_GUARD
    + """
REVOKE ALL ON FUNCTION clinic_app.identity_invitation_account(uuid, uuid),
    clinic_app.identity_invitation_password(bytea, bytea, text, text),
    clinic_app.identity_invitation_accept(bytea, text),
    clinic_app.identity_offboarding_revoke(uuid, uuid)
    FROM PUBLIC, clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.identity_invitation_account(uuid, uuid),
    clinic_app.identity_invitation_password(bytea, bytea, text, text),
    clinic_app.identity_invitation_accept(bytea, text),
    clinic_app.identity_offboarding_revoke(uuid, uuid)
    TO clinic_app;
"""
)
