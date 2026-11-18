"""Serialize every per-user session revocation with key rotation (review S1).

``identity_session_rotate`` (0054) and the membership branch of
``identity_revoke_changed_sessions`` take
``pg_advisory_xact_lock(hashtextextended('clinic-lock-v1:user:' || user, 0))``.
Every other statement that revoked all of a user's sessions ran unlocked, so a
rotation committing during it registered a key the revocation never saw. Each
now takes the same key, for every affected user, before revoking:

- the ``identity_user`` branch (password and active-flag changes);
- the ``identity_rolegrant`` branch, every affected member in user_id order;
- a membership reassignment, the old and new user in uuid order;
- ``identity_retire_authenticators`` (re-entrant, so offboarding is unchanged),
  which also covers recovery-code use, authenticator reset and SCIM;
- SCIM directory sync, per user in user_id order, before its active-flag and
  session updates;
- ``identity_recovery_consume``, whose own key no other caller shared;
- ``identity_session_register``, closing the first-session window.

Each definition is sliced from the module that installed it, so the reverse
replays the original text exactly. ``identity_session_rotate_hash`` stays
installed (the SQL inventories pin every defined name) but loses clinic_app's
EXECUTE.
"""

from typing import Final

from ._delegate_access_sql import _POPULATED
from ._enterprise_directory_sql import SQL as DIRECTORY_SQL
from ._lifecycle_recovery_sql import RECOVERY_SQL
from ._lifecycle_sessions_sql import SESSION_SQL
from ._passkey_retirement_sql import RETIREMENT_SQL

_OPEN: Final = "$function$"


def _definition(sql: str, name: str, tag: str = _OPEN) -> str:
    """Return the installed ``clinic_app.<name>`` definition as a replacement."""
    for head in (
        f"CREATE OR REPLACE FUNCTION clinic_app.{name}(",
        f"CREATE FUNCTION clinic_app.{name}(",
    ):
        start = sql.find(head)
        if start >= 0:
            break
    else:
        msg = f"no definition of {name}"
        raise ValueError(msg)
    body = sql.index(tag, start) + len(tag)
    end = sql.index(tag + ";", body) + len(tag) + 1
    signature = sql[start:end].split(" FUNCTION ", 1)[1]
    return f"CREATE OR REPLACE FUNCTION {signature}\n"


def _insert(text: str, anchor: str, replacement: str) -> str:
    """Replace the one occurrence of ``anchor``; anything else is a stale slice."""
    if text.count(anchor) != 1:
        msg = f"lock anchor not unique: {anchor[:60]!r}"
        raise ValueError(msg)
    return text.replace(anchor, replacement)


def _lock(user: str, indent: str) -> str:
    return (
        f"{indent}PERFORM pg_advisory_xact_lock(hashtextextended(\n"
        f"{indent}    'clinic-lock-v1:user:' || {user}::text, 0\n"
        f"{indent}));\n"
    )


_REVOKE: Final = _definition(SESSION_SQL, "identity_revoke_changed_sessions")
_REGISTER: Final = _definition(SESSION_SQL, "identity_session_register")
_RETIRE: Final = _definition(RETIREMENT_SQL, "identity_retire_authenticators")
_CONSUME: Final = _definition(RECOVERY_SQL, "identity_recovery_consume")
# Owned by clinic_owner, unlike the resolver-owned four: replaced after RESET.
_SYNC: Final = _definition(DIRECTORY_SQL, "enterprise_sync_directory", "$f$")

_MEMBERSHIP_LOCK: Final = (
    "        PERFORM pg_advisory_xact_lock(hashtextextended(\n"
    "            'clinic-lock-v1:user:' || NEW.user_id::text, 0\n"
    "        ));\n"
)
_GRANT_MEMBERS: Final = """\
        -- Every member the grant change revokes, in user_id order.
        FOR locked_user IN
            SELECT DISTINCT membership.user_id
            FROM clinic_app.identity_userclinicrole AS membership
            WHERE membership.revoked_at IS NULL
              AND (
                  (membership.clinic_id = NEW.clinic_id
                   AND membership.role = NEW.role)
                  OR (
                      TG_OP = 'UPDATE' AND membership.clinic_id = OLD.clinic_id
                      AND membership.role = OLD.role
                  )
              )
            ORDER BY 1
        LOOP
"""
_REASSIGNMENT: Final = (
    "        -- A reassignment revokes both users' sessions: both, in uuid order.\n"
    "        IF TG_OP = 'UPDATE' AND OLD.user_id IS DISTINCT FROM NEW.user_id THEN\n"
    + _lock("LEAST(OLD.user_id, NEW.user_id)", "            ")
    + _lock("GREATEST(OLD.user_id, NEW.user_id)", "            ")
    + "        ELSE\n"
    + _lock("NEW.user_id", "            ")
    + "        END IF;\n"
)

_FORWARD: Final = (
    _insert(
        _insert(
            _insert(
                _insert(
                    _REVOKE,
                    "DECLARE\n    changed_user uuid;\n",
                    "DECLARE\n    changed_user uuid;\n    locked_user uuid;\n",
                ),
                "        changed_user := NEW.id;\n",
                "        changed_user := NEW.id;\n" + _lock("changed_user", "        "),
            ),
            "    ELSIF TG_TABLE_NAME = 'identity_rolegrant' THEN\n",
            "    ELSIF TG_TABLE_NAME = 'identity_rolegrant' THEN\n"
            + _GRANT_MEMBERS
            + _lock("locked_user", "            ")
            + "        END LOOP;\n",
        ),
        _MEMBERSHIP_LOCK,
        _REASSIGNMENT,
    ),
    _insert(
        _REGISTER,
        "    INSERT INTO clinic_app.identity_sessionrecord (",
        _lock("requested_user", "    ")
        + "    INSERT INTO clinic_app.identity_sessionrecord (",
    ),
    _insert(
        _RETIRE,
        _OPEN + "\nBEGIN\n",
        _OPEN + "\nBEGIN\n" + _lock("requested_user", "    "),
    ),
    _insert(
        _CONSUME,
        "    PERFORM pg_advisory_xact_lock("
        "hashtextextended(requested_user::text, 0));\n",
        _lock("requested_user", "    "),
    ),
)
_SYNC_FORWARD: Final = _insert(
    _SYNC,
    " FROM clinic_app.identity_scimresource WHERE connection_id=c.id"
    " AND kind='Users'\n LOOP\n",
    " FROM clinic_app.identity_scimresource WHERE connection_id=c.id"
    " AND kind='Users'\n ORDER BY user_id\n LOOP\n" + _lock("item.user_id", " "),
)

SQL: Final = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + "".join(_FORWARD)
    + """
REVOKE EXECUTE ON FUNCTION clinic_app.identity_session_rotate_hash(bytea)
    FROM clinic_app;
RESET ROLE;
"""
    + _SYNC_FORWARD
)

# Like 0053-0054: a populated enterprise install refuses before any change.
REVERSE_SQL: Final = (
    """
SET LOCAL ROLE clinic_resolver;
DO $populated$
BEGIN
 IF __POPULATED__ THEN
 RAISE EXCEPTION 'populated enterprise identity rolls back only by restore'
 USING ERRCODE='55000';
 END IF;
END $populated$;
GRANT EXECUTE ON FUNCTION clinic_app.identity_session_rotate_hash(bytea)
    TO clinic_app;
""".replace("__POPULATED__", _POPULATED)
    + f"{_REVOKE}{_REGISTER}{_RETIRE}{_CONSUME}"
    + "RESET ROLE;\n"
    + _SYNC
)
