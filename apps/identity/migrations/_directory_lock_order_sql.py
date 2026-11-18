"""SCIM deprovisioning and retirement lock rows before any session key (S3).

0056 put one order on each user: ``identity_user`` row, authenticator rows,
membership rows, session key, session rows. Two paths still broke it:

- ``enterprise_sync_directory`` revoked memberships in its first loop, whose
  trigger takes the user's session key, and only locked that user's row and
  passkeys later, in its per-user loop. Group removal plus ``active=false`` in
  one sync is the ordinary deprovisioning shape. A concurrent passkey sign-in or
  password change for that user could deadlock, and PostgreSQL may then abort
  the whole sync.
- ``identity_retire_authenticators`` inserted its retirement rows, which a
  concurrent retirer waits for, before locking passkey and recovery rows, while
  offboarding and recovery-code use lock those rows first.

The sync now locks, right after its connection row and before any membership
change, every directory user of the connection in user_id order: the
``identity_user`` row, then ``identity_lock_authenticators``. Retirement calls the
same helper before inserting retirement rows. Both are derived from 0056's text,
and the reverse replays it exactly.
"""

from typing import Final

from ._delegate_access_sql import _POPULATED
from ._revocation_lock_sql import _insert
from ._session_lock_order_sql import _OWNER_FORWARD, _RESOLVER_FORWARD

_RETIRE: Final = _RESOLVER_FORWARD[3]
_SYNC: Final = _OWNER_FORWARD[1]

_CONNECTION_LOCK: Final = " WHERE id=requested_connection FOR UPDATE;\n"
_DIRECTORY_USERS: Final = (
    " -- Each directory user's row, then authenticators, in user_id order,\n"
    " -- before any membership change takes that user's session key.\n"
    " FOR item IN SELECT DISTINCT u.user_id\n"
    " FROM clinic_app.identity_scimresource u\n"
    " WHERE u.connection_id=c.id AND u.kind='Users' AND u.user_id IS NOT NULL\n"
    " ORDER BY 1\n"
    " LOOP\n"
    " PERFORM 1 FROM clinic_app.identity_user WHERE id=item.user_id FOR UPDATE;\n"
    " PERFORM clinic_app.identity_lock_authenticators(item.user_id);\n"
    " END LOOP;\n"
)
_DIRECTORY_USERS_FIRST: Final = _CONNECTION_LOCK + _DIRECTORY_USERS
_RETIRE_HEAD: Final = "$function$\nBEGIN\n"
_RETIRE_ROWS_FIRST: Final = (
    _RETIRE_HEAD
    + "    -- Passkey and recovery rows before the retirement rows, as offboarding\n"
    + "    -- and recovery-code use lock them.\n"
    + "    PERFORM clinic_app.identity_lock_authenticators(requested_user);\n"
)

SQL: Final = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _insert(_RETIRE, _RETIRE_HEAD, _RETIRE_ROWS_FIRST)
    + "RESET ROLE;\n"
    + _insert(_SYNC, _CONNECTION_LOCK, _DIRECTORY_USERS_FIRST)
)

# Like 0053-0056: a populated enterprise install refuses before any change.
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
    + _RETIRE
    + "RESET ROLE;\n"
    + _SYNC
)
