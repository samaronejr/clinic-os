"""One lock order for session rotation and revocation (review S2).

0054/0055 serialized rotation with revocation on ``clinic-lock-v1:user:``, the
project's practitioner and user gate. That gate is taken first, before row work
(``apps/scheduling/locks.py``, offboarding), while the revocation triggers take
it after their row. That inversion can deadlock: a password change holds the
``identity_user`` row and waits for the gate, while offboarding holds the gate and
waits for the row. Passkey rows invert the same way against
``identity_retire_authenticators``.

Rotation, first-session registration and every session revocation now share a
dedicated key, ``clinic-lock-v1:sessions:<user>``, so S1 stays closed. Each takes
it last, immediately before touching session rows, and nothing is locked after
it but session rows. One order per user:

    identity_user row -> authenticator rows (passkey, recovery batch)
    -> membership rows -> session key -> session rows

- ``identity_retire_authenticators`` updates its authenticator rows first, then
  takes the session key before revoking sessions.
- ``identity_lock_authenticators`` (new, resolver-owned, so row security cannot
  hide another tenant's passkeys) locks a user's live passkey and recovery-batch
  rows in id order. Offboarding calls it after locking the user row and before
  revoking memberships, whose trigger takes the session key; recovery-code use
  calls it before its session key.
- SCIM's disable branch locks the user row, retires the authenticators (rows,
  then key), and then updates the active flag and revokes under the key.
- The membership branch keeps its original ``NEW.user_id`` gate, then takes the
  session keys of the old and new user in uuid order.

Each definition is derived from the text that installed it, so the reverse
replays that text exactly.
"""

from typing import Final

from ._atomic_rotation_sql import SQL as ROTATION_SQL
from ._delegate_access_sql import _POPULATED
from ._lifecycle_authority_sql import OWNER_WRITERS
from ._lifecycle_offboarding_sql import OFFBOARDING_SQL
from ._revocation_lock_sql import (
    _FORWARD,
    _MEMBERSHIP_LOCK,
    _REASSIGNMENT,
    _SYNC_FORWARD,
    _definition,
    _insert,
    _lock,
)


def _session_lock(user: str, indent: str) -> str:
    return (
        f"{indent}PERFORM pg_advisory_xact_lock(hashtextextended(\n"
        f"{indent}    'clinic-lock-v1:sessions:' || {user}::text, 0\n"
        f"{indent}));\n"
    )


_GATE: Final = "'clinic-lock-v1:user:'"
_SESSIONS: Final = "'clinic-lock-v1:sessions:'"

# The installed text each function is replaced from (and restored to).
_ROTATE: Final = _definition(ROTATION_SQL, "identity_session_rotate")
# Indexed, not unpacked: the clock census decoder folds only name targets.
_REVOKE: Final = _FORWARD[0]
_REGISTER: Final = _FORWARD[1]
_RETIRE: Final = _FORWARD[2]
_CONSUME: Final = _FORWARD[3]
_SYNC: Final = _SYNC_FORWARD
_EXECUTE: Final = _definition(OFFBOARDING_SQL, "identity_offboarding_execute")
_OFFBOARD_REVOKE: Final = _definition(OWNER_WRITERS, "identity_offboarding_revoke")

LOCK_AUTHENTICATORS: Final = """
CREATE FUNCTION clinic_app.identity_lock_authenticators(requested_user uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM 1 FROM clinic_app.identity_passkey
    WHERE user_id = requested_user AND removed_at IS NULL
    ORDER BY id FOR UPDATE;
    PERFORM 1 FROM clinic_app.identity_recoverybatch
    WHERE user_id = requested_user AND revoked_at IS NULL
    ORDER BY id FOR UPDATE;
END
$function$;
REVOKE ALL ON FUNCTION clinic_app.identity_lock_authenticators(uuid)
    FROM PUBLIC, clinic_app, clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.identity_lock_authenticators(uuid)
    TO clinic_owner;
"""

_AUTHENTICATORS_FIRST: Final = (
    "    IF NOT account_active THEN\n        RETURN true;\n    END IF;\n"
)
_PRELOCK: Final = (
    _AUTHENTICATORS_FIRST
    + "    -- Authenticator rows before the memberships whose trigger takes the\n"
    + "    -- session key (one order: user row, authenticators, key, sessions).\n"
    + "    PERFORM clinic_app.identity_lock_authenticators(requested_user);\n"
)

_REASSIGNMENT_SESSIONS: Final = (
    _MEMBERSHIP_LOCK
    + "        -- A reassignment revokes both users' sessions: both, in uuid order.\n"
    + "        IF TG_OP = 'UPDATE' AND OLD.user_id IS DISTINCT FROM NEW.user_id THEN\n"
    + _session_lock("LEAST(OLD.user_id, NEW.user_id)", "            ")
    + _session_lock("GREATEST(OLD.user_id, NEW.user_id)", "            ")
    + "        ELSE\n"
    + _session_lock("NEW.user_id", "            ")
    + "        END IF;\n"
)

_SYNC_DISABLE: Final = "".join(
    (
        " LOOP\n",
        _lock("item.user_id", " "),
        " UPDATE clinic_app.identity_user SET is_active=item.enabled",
        " WHERE id=item.user_id;\n",
        " IF NOT item.enabled THEN\n",
        " UPDATE clinic_app.identity_sessionrecord",
        " SET revoked_at=statement_timestamp()\n",
        " WHERE user_id=item.user_id AND revoked_at IS NULL;\n",
        " PERFORM clinic_app.identity_retire_authenticators(item.user_id);\n",
        " END IF;\n",
    )
)
_SYNC_ORDERED: Final = "".join(
    (
        " LOOP\n",
        " -- User row, then authenticators and the session key (in retire).\n",
        " IF NOT item.enabled THEN\n",
        " PERFORM 1 FROM clinic_app.identity_user WHERE id=item.user_id FOR UPDATE;\n",
        " PERFORM clinic_app.identity_retire_authenticators(item.user_id);\n",
        " END IF;\n",
        " UPDATE clinic_app.identity_user SET is_active=item.enabled",
        " WHERE id=item.user_id;\n",
        " IF NOT item.enabled THEN\n",
        _session_lock("item.user_id", " "),
        " UPDATE clinic_app.identity_sessionrecord",
        " SET revoked_at=statement_timestamp()\n",
        " WHERE user_id=item.user_id AND revoked_at IS NULL;\n",
        " END IF;\n",
    )
)

_RESOLVER_FORWARD: Final = (
    _insert(_ROTATE, _GATE, _SESSIONS),
    _insert(_REGISTER, _GATE, _SESSIONS),
    _insert(
        _insert(
            _insert(
                _REVOKE,
                _lock("changed_user", "        "),
                _session_lock("changed_user", "        "),
            ),
            _lock("locked_user", "            "),
            _session_lock("locked_user", "            "),
        ),
        _REASSIGNMENT,
        _REASSIGNMENT_SESSIONS,
    ),
    _insert(
        _insert(_RETIRE, _lock("requested_user", "    "), ""),
        "    UPDATE clinic_app.identity_sessionrecord\n",
        _session_lock("requested_user", "    ")
        + "    UPDATE clinic_app.identity_sessionrecord\n",
    ),
    _insert(
        _insert(_CONSUME, _lock("requested_user", "    "), ""),
        "    FOR UPDATE OF code, batch;\n",
        "    FOR UPDATE OF code, batch;\n"
        "    PERFORM clinic_app.identity_lock_authenticators(requested_user);\n"
        + _session_lock("requested_user", "    "),
    ),
    _insert(_EXECUTE, _AUTHENTICATORS_FIRST, _PRELOCK),
)
_OWNER_FORWARD: Final = (
    _insert(_OFFBOARD_REVOKE, _AUTHENTICATORS_FIRST, _PRELOCK),
    _insert(_SYNC, _SYNC_DISABLE, _SYNC_ORDERED),
)

SQL: Final = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + LOCK_AUTHENTICATORS
    + "".join(_RESOLVER_FORWARD)
    + "RESET ROLE;\n"
    + "".join(_OWNER_FORWARD)
)

# Like 0053-0055: a populated enterprise install refuses before any change.
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
""".replace("__POPULATED__", _POPULATED)
    + f"{_ROTATE}{_REGISTER}{_REVOKE}{_RETIRE}{_CONSUME}{_EXECUTE}"
    + "RESET ROLE;\n"
    + f"{_OFFBOARD_REVOKE}{_SYNC}"
    + "SET LOCAL ROLE clinic_resolver;\n"
    + "DROP FUNCTION clinic_app.identity_lock_authenticators(uuid);\n"
    + "RESET ROLE;\n"
)
