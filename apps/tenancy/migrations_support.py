"""Migration helpers for trimming bootstrap default privileges.

``ops/db/bootstrap.sql`` grants ``clinic_app`` ``SELECT, INSERT, UPDATE,
DELETE`` on every new table in the ``clinic_app`` schema via
``ALTER DEFAULT PRIVILEGES``. The first migration of each tenant table must
revoke that default surface down to the grants the table actually needs.
"""

from collections.abc import Iterable
from typing import Final

from apps.tenancy.posture import APP_SCHEMA, RUNTIME_ROLE

DEFAULT_DML_PRIVILEGES: Final[tuple[str, ...]] = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
)
KEEP_OUTSIDE_DEFAULTS_MESSAGE: Final = "keep privileges outside the bootstrap defaults"


def revoke_default_dml(table: str, keep: Iterable[str]) -> str:
    """Build REVOKE SQL for the default DML privileges not in ``keep``.

    ``keep`` names the privileges the table intentionally retains; every
    other privilege granted by the bootstrap defaults is revoked from the
    runtime role.
    """
    kept = frozenset(keep)
    unknown = kept - frozenset(DEFAULT_DML_PRIVILEGES)
    if unknown:
        raise ValueError(KEEP_OUTSIDE_DEFAULTS_MESSAGE)
    revoked = [
        privilege for privilege in DEFAULT_DML_PRIVILEGES if privilege not in kept
    ]
    if not revoked:
        return f"-- {table}: runtime role keeps the default DML grants\n"
    privileges = ", ".join(revoked)
    return f"REVOKE {privileges} ON TABLE {APP_SCHEMA}.{table} FROM {RUNTIME_ROLE};\n"
