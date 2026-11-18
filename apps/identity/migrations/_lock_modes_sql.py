"""One global lock order with the right modes (review S4).

Per user, every path locks in this order, each step optional:

1. user gate ``clinic-lock-v1:user:<id>`` (advisory);
2. ``identity_user`` row, ``FOR NO KEY UPDATE`` (as a password or active-flag
   UPDATE takes it);
3. authenticator rows (passkey, recovery batch), ``FOR NO KEY UPDATE``;
4. membership rows (UPDATE; the membership trigger's gate is then re-entrant);
5. session key ``clinic-lock-v1:sessions:<id>`` (advisory);
6. session rows (UPDATE/INSERT).

A session-row INSERT's foreign keys take ``FOR KEY SHARE`` on rows 2 and 3.
``FOR UPDATE`` conflicts with that, so a sign-in already holding the session
key could not insert while another transaction held the user or passkey row
and waited for that key. ``FOR NO KEY UPDATE`` does not conflict with
``FOR KEY SHARE`` and still serializes with password and active-flag changes.
Across users, gates are taken in user_id order before any row.

Changed here:
- ``enterprise_sync_directory``: the up-front pass covers only the users the
  run will change (its own revocation, creation and active-flag predicates,
  sliced from the installed loops). It takes all their gates in user_id order,
  then each user's row (``FOR NO KEY UPDATE``) and authenticators, then their
  session keys in user_id order (the role-grant branch takes several users'
  keys in that order). The active flag is updated only when it differs, and
  the disable branch's row lock becomes ``FOR NO KEY UPDATE``.
- ``identity_offboarding_execute``, ``identity_offboarding_revoke``,
  ``identity_lock_authenticators``: ``FOR UPDATE`` becomes ``FOR NO KEY UPDATE``.
- ``enterprise_unit_member_revoke``, ``enterprise_unit_policy_apply``: take the
  target user's gate before revoking memberships.

Each definition is derived from the text that installed it; the reverse
replays that text exactly.
"""

from typing import Final

from ._delegate_access_sql import _POPULATED
from ._directory_lock_order_sql import (
    _CONNECTION_LOCK,
    _DIRECTORY_USERS,
    _DIRECTORY_USERS_FIRST,
)
from ._directory_lock_order_sql import _SYNC as _SYNC_0056
from ._enterprise_access_sql import SQL as ACCESS_SQL
from ._enterprise_unit_policy_sql import SQL as UNIT_POLICY_SQL
from ._revocation_lock_sql import _definition, _insert
from ._session_lock_order_sql import (
    _OWNER_FORWARD,
    _RESOLVER_FORWARD,
    LOCK_AUTHENTICATORS,
    _session_lock,
)


def _gate(user: str, indent: str) -> str:
    return (
        f"{indent}PERFORM pg_advisory_xact_lock(hashtextextended(\n"
        f"{indent}    'clinic-lock-v1:user:' || {user}::text, 0));\n"
    )


def _query(text: str, select: str) -> str:
    """Return the loop query that starts with ``select``, up to its ``LOOP``."""
    start = text.index(select)
    return text[start : text.index("\n LOOP\n", start)]


# Installed text, replaced from and restored to.
_SYNC: Final = _insert(_SYNC_0056, _CONNECTION_LOCK, _DIRECTORY_USERS_FIRST)
_LOCK: Final = _definition(LOCK_AUTHENTICATORS, "identity_lock_authenticators")
_EXECUTE: Final = _RESOLVER_FORWARD[5]
_OFFBOARD_REVOKE: Final = _OWNER_FORWARD[0]
_MEMBER_REVOKE: Final = _definition(ACCESS_SQL, "enterprise_unit_member_revoke", "$f$")
_POLICY_APPLY: Final = _definition(
    UNIT_POLICY_SQL, "enterprise_unit_policy_apply", "$f$"
)

_REVOKED_SELECT: Final = "SELECT b.id,b.membership_id,b.clinic_id,b.manages_membership"
_CREATED_SELECT: Final = "SELECT DISTINCT u.id,u.user_id,mapping.unit_id,mapping.bundle"
_REVOKED_USERS: Final = _query(_SYNC, _REVOKED_SELECT).replace(
    _REVOKED_SELECT, "SELECT r.user_id"
)
_CREATED_USERS: Final = "".join(
    (
        _query(_SYNC, _CREATED_SELECT).replace(_CREATED_SELECT, "SELECT u.user_id"),
        "\n AND ((mapping.bundle IN ('owner','physician','receptionist','assistant')",
        "\n AND NOT EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole m",
        "\n WHERE m.organization_id=c.organization_id AND m.clinic_id=mapping.unit_id",
        "\n AND m.user_id=u.user_id AND m.role=mapping.bundle",
        " AND m.revoked_at IS NULL))",
        "\n OR EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole m",
        "\n WHERE m.organization_id=c.organization_id AND m.clinic_id=mapping.unit_id",
        "\n AND m.user_id=u.user_id AND m.role=mapping.bundle AND m.revoked_at IS NULL",
        "\n AND NOT EXISTS (SELECT 1 FROM clinic_app.identity_scimrolebinding b",
        "\n WHERE b.membership_id=m.id AND b.revoked_at IS NULL)))",
    )
)
_FLAGGED_USERS: Final = (
    "SELECT u.user_id FROM clinic_app.identity_scimresource u"
    "\n JOIN clinic_app.identity_user a ON a.id=u.user_id"
    "\n WHERE u.connection_id=c.id AND u.kind='Users' AND ("
    "\n NOT (u.active AND u.deleted_at IS NULL)"
    "\n OR a.is_active IS DISTINCT FROM (u.active AND u.deleted_at IS NULL))"
)
_CHANGED_USERS_FIRST: Final = "".join(
    (
        " -- Only the users this run changes: every gate in user_id order, then\n",
        " -- each user's row and authenticators, before any membership change.\n",
        " changed_users := ARRAY(SELECT DISTINCT changed.user_id FROM (\n ",
        _REVOKED_USERS,
        "\n UNION\n ",
        _CREATED_USERS,
        "\n UNION\n ",
        _FLAGGED_USERS,
        "\n ) AS changed WHERE changed.user_id IS NOT NULL ORDER BY 1);\n",
        " FOREACH changed_user IN ARRAY changed_users LOOP\n",
        _gate("changed_user", " "),
        " END LOOP;\n",
        " FOREACH changed_user IN ARRAY changed_users LOOP\n",
        " PERFORM 1 FROM clinic_app.identity_user WHERE id=changed_user\n",
        " FOR NO KEY UPDATE;\n",
        " PERFORM clinic_app.identity_lock_authenticators(changed_user);\n",
        " END LOOP;\n",
        " -- Their session keys in user_id order too: the run's membership\n",
        " -- triggers would take them in loop order.\n",
        " FOREACH changed_user IN ARRAY changed_users LOOP\n",
        _session_lock("changed_user", " "),
        " END LOOP;\n",
    )
)
_DECLARE: Final = " created_binding uuid; directory_managed boolean;\n"
_FLAG_UPDATE: Final = (
    " UPDATE clinic_app.identity_user SET is_active=item.enabled"
    " WHERE id=item.user_id;\n"
)
_DISABLE_ROW: Final = (
    " PERFORM 1 FROM clinic_app.identity_user WHERE id=item.user_id FOR UPDATE;\n"
)
_OFFBOARD_ROW: Final = "    WHERE id = requested_user FOR UPDATE;\n"

_SYNC_FORWARD: Final = _insert(
    _insert(
        _insert(
            _insert(
                _SYNC,
                _DECLARE,
                _DECLARE + " changed_users uuid[]; changed_user uuid;\n",
            ),
            _DIRECTORY_USERS,
            _CHANGED_USERS_FIRST,
        ),
        _FLAG_UPDATE,
        " UPDATE clinic_app.identity_user SET is_active=item.enabled"
        " WHERE id=item.user_id\n AND is_active IS DISTINCT FROM item.enabled;\n",
    ),
    _DISABLE_ROW,
    _DISABLE_ROW.replace("FOR UPDATE;", "FOR NO KEY UPDATE;"),
)
_LOCK_FORWARD: Final = _LOCK.replace(
    "ORDER BY id FOR UPDATE;", "ORDER BY id FOR NO KEY UPDATE;"
)
_GATED_UPDATE: Final = " UPDATE clinic_app.identity_userclinicrole SET revoked_at"

_RESOLVER_FORWARD_0058: Final = (
    _LOCK_FORWARD,
    _insert(
        _EXECUTE,
        _OFFBOARD_ROW,
        _OFFBOARD_ROW.replace("FOR UPDATE", "FOR NO KEY UPDATE"),
    ),
)
_OWNER_FORWARD_0058: Final = (
    _insert(
        _OFFBOARD_REVOKE,
        _OFFBOARD_ROW,
        _OFFBOARD_ROW.replace("FOR UPDATE", "FOR NO KEY UPDATE"),
    ),
    _SYNC_FORWARD,
    _insert(_MEMBER_REVOKE, _GATED_UPDATE, _gate("target_user", " ") + _GATED_UPDATE),
    _insert(
        _POLICY_APPLY, _GATED_UPDATE, _gate("NEW.target_user_id", " ") + _GATED_UPDATE
    ),
)

SQL: Final = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + "".join(_RESOLVER_FORWARD_0058)
    + "RESET ROLE;\n"
    + "".join(_OWNER_FORWARD_0058)
)

# Like 0053-0057: a populated enterprise install refuses before any change.
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
    + f"{_LOCK}{_EXECUTE}"
    + "RESET ROLE;\n"
    + f"{_OFFBOARD_REVOKE}{_SYNC}{_MEMBER_REVOKE}{_POLICY_APPLY}"
)
