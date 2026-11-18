"""TOTP verification locks its device FOR NO KEY UPDATE (review S5, H1).

``identity_totp_verify`` locked the device row ``FOR UPDATE``; the verify view
then rotates the session under the session key. A retirement holding that key
inserts ``identity_authenticatorretirement(device_id)``, whose foreign key takes
``FOR KEY SHARE`` on the device, which only ``FOR UPDATE`` blocks: the S4 cycle,
with SCIM deprovisioning or offboarding as the possible victim.

``FOR NO KEY UPDATE`` still serializes concurrent verifications of one device
(replay protection) and the verifier's own UPDATE of it. Derived from the text
0050 installed; the reverse replays that text exactly, and like 0053-0058 a
populated enterprise install refuses it before any change.
"""

from typing import Final

from ._delegate_access_sql import _POPULATED
from ._revocation_lock_sql import _definition, _insert
from ._totp_secret_sql import FUNCTIONS_SQL

_VERIFY: Final = _definition(FUNCTIONS_SQL, "identity_totp_verify")
_DEVICE_LOCK: Final = "\n      )\n    FOR UPDATE;\n"

SQL: Final = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _insert(
        _VERIFY,
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
    + _VERIFY
    + "RESET ROLE;\n"
)
