"""TOTP enrollment locks the reused pending device FOR NO KEY UPDATE (S6, T1).

``identity_totp_enroll`` reused the user's pending device with ``FOR UPDATE``,
then the request's end-of-request touch updates its session row. A SCIM
deprovisioning of that user revokes the session row and inserts a retirement
row for every device; the deferred foreign key takes ``FOR KEY SHARE`` on the
pending device at commit, which only ``FOR UPDATE`` blocks.

``FOR NO KEY UPDATE`` still serializes the enrolment's own UPDATE of the
device (the ``clinic-totp:<user>`` advisory lock already serializes concurrent
enrolments). Derived from the text 0050 installed, as 0059 is; the reverse
replays that text exactly, and a populated enterprise install refuses it
before any change, like 0053-0059.
"""

from typing import Final

from ._delegate_access_sql import _POPULATED
from ._revocation_lock_sql import _definition, _insert
from ._totp_secret_sql import FUNCTIONS_SQL

_ENROLL: Final = _definition(FUNCTIONS_SQL, "identity_totp_enroll")
_DEVICE_LOCK: Final = "    ORDER BY device.id\n    LIMIT 1\n    FOR UPDATE;\n"

SQL: Final = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _insert(
        _ENROLL,
        _DEVICE_LOCK,
        _DEVICE_LOCK.replace("FOR UPDATE;", "FOR NO KEY UPDATE;"),
    )
    + "RESET ROLE;\n"
)
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
    + _ENROLL
    + "RESET ROLE;\n"
)
