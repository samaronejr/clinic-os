"""Resolver-owned TOTP secret store and in-database RFC 6238 verification.

The runtime role must not be able to forge a step-up at the SQL level. Before
this boundary clinic_app could read the device secret (``otp_totp_totpdevice.key``)
and write ``last_t``/``last_used_at`` directly, so it could compute a code
offline or stamp a fresh use with no code and make ``identity_session_verify``
trust it. Here the secret moves into ``identity_totpsecret`` (resolver-owned, no
runtime or owner grant) and the only writer of the device's verification state
is ``identity_totp_verify``, a ``clinic_resolver`` SECURITY DEFINER that computes
the RFC 6238 code with pgcrypto against the hidden secret, enforces ``last_t``
monotonicity (replay) and django_otp's throttling, and is reproduced exactly so
real authenticators keep working. A device-table guard trigger refuses every
other writer's change to the verification columns. The session stamp
(``identity_session_verify``) now succeeds only inside the transaction in which
this verifier accepted a real code for the device, and consumes that
acceptance, so a borrowed fresh use can never verify another session.

pgcrypto (``hmac``/``gen_random_bytes``) lives in the ``clinic_app`` schema and
is schema-qualified, so the definers keep the hardened ``pg_catalog, pg_temp``
search_path that the resolver catalog pins for identity functions. The runtime
role holds no CREATE on ``clinic_app``, so the qualified names cannot be shadowed.
"""

from typing import Final

from apps.identity.migrations._passkey_retirement_sql import RETIREMENT_SQL

# Resolver-owned secret store plus the resolver privileges the definers need on
# the device table. The data move runs last, as clinic_resolver: the device
# table is FORCE RLS and clinic_owner has no policy that shows every device, so
# only the BYPASSRLS resolver sees all of them. The device guard does not exist
# yet when the move runs.
STORE_TABLE_SQL: Final = """
CREATE TABLE clinic_app.identity_totpsecret (
    device_id integer PRIMARY KEY
        REFERENCES clinic_app.otp_totp_totpdevice (id) ON DELETE CASCADE,
    secret bytea NOT NULL,
    -- The transaction in which identity_totp_verify last accepted a genuine
    -- code for this device; identity_session_verify stamps a session only
    -- inside that same transaction and consumes it (one code, one session).
    verified_xact xid8
);
REVOKE ALL ON clinic_app.identity_totpsecret FROM PUBLIC, clinic_app, clinic_agent;
ALTER TABLE clinic_app.identity_totpsecret ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_totpsecret FORCE ROW LEVEL SECURITY;
-- Ownership moves to the resolver so there is no runtime or ambient owner read
-- path (membership in clinic_resolver is INHERIT FALSE, SET TRUE only).
ALTER TABLE clinic_app.identity_totpsecret OWNER TO clinic_resolver;
-- Granted after the handoff, as the new owner, so the entries persist: the
-- owner-owned staff-invitation writer seeds a secret during provisioning
-- (INSERT, allowed through FORCE RLS by the owner policy below), and the ORM
-- test teardown empties the table alongside the device table it references
-- (TRUNCATE). The owner never gets SELECT, so it can never read a secret back.
SET LOCAL ROLE clinic_resolver;
GRANT INSERT, TRUNCATE ON clinic_app.identity_totpsecret TO clinic_owner;
CREATE POLICY identity_totpsecret_owner_insert
    ON clinic_app.identity_totpsecret AS PERMISSIVE FOR INSERT TO clinic_owner
    WITH CHECK (true);
RESET ROLE;
-- The resolver definers verify and enrol against the device table; swap the
-- narrow lifecycle column SELECT (0023) for the exact table privileges they use.
REVOKE SELECT (id, user_id, confirmed, last_t, last_used_at)
    ON clinic_app.otp_totp_totpdevice FROM clinic_resolver;
GRANT SELECT, INSERT, UPDATE ON clinic_app.otp_totp_totpdevice TO clinic_resolver;
GRANT USAGE ON SEQUENCE clinic_app.otp_totp_totpdevice_id_seq TO clinic_resolver;
-- Move every existing seed. Fail closed first on a seed that cannot move intact
-- (django-otp keys are hex), so clearing key never discards a usable secret.
SET LOCAL ROLE clinic_resolver;
DO $check$
BEGIN
    IF EXISTS (
        SELECT 1 FROM clinic_app.otp_totp_totpdevice
        WHERE key <> '' AND key !~ '^([0-9A-Fa-f]{2})+$'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'a TOTP device key is not hex and cannot move intact';
    END IF;
END
$check$;
INSERT INTO clinic_app.identity_totpsecret (device_id, secret)
SELECT id, decode(lower(key), 'hex')
FROM clinic_app.otp_totp_totpdevice
WHERE key <> '';
UPDATE clinic_app.otp_totp_totpdevice SET key = '' WHERE key <> '';
RESET ROLE;
"""

FUNCTIONS_SQL: Final = r"""
-- The only writer of last_t, last_used_at, throttling, drift and confirmed.
-- Returns 1 = accepted, 0 = invalid code, -1 = throttled (verification not
-- allowed yet). Identity comes from the GUCs; the RFC 6238 code is checked
-- against the resolver-held secret, never a caller-chosen clock or secret. The
-- HOTP is django_otp.oath.hotp inline: int8send equals struct.pack('>Q').
CREATE FUNCTION clinic_app.identity_totp_verify(
    requested_device integer, requested_code text
)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    actor uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
    device clinic_app.otp_totp_totpdevice%ROWTYPE;
    secret bytea;
    now_epoch bigint := floor(extract(epoch FROM statement_timestamp()))::bigint;
    base_t bigint;
    min_t bigint;
    offset_step integer;
    matched_offset integer := NULL;
    code_value bigint;
    delay_seconds numeric;
    mac bytea;
    byte_offset integer;
    candidate_code bigint;
BEGIN
    -- An inactive account verifies only the device of its own pending staff
    -- invitation (confirmation precedes activation); every other inactive or
    -- foreign caller is refused before the device row is touched.
    IF actor IS NULL
       OR NULLIF(current_setting('app.current_principal', true), '') IS NOT NULL
       OR NULLIF(current_setting('app.current_patient_session', true), '') IS NOT NULL
       OR requested_code IS NULL OR requested_code !~ '^[0-9]{6,8}$'
       OR NOT (
           EXISTS (
               SELECT 1 FROM clinic_app.identity_user AS account
               WHERE account.id = actor AND account.is_active
           )
           OR EXISTS (
               SELECT 1 FROM clinic_app.identity_staffinvitation AS invitation
               WHERE invitation.pending_user_id = actor
                 AND invitation.device_id = requested_device
                 AND invitation.state = 'pending'
                 AND invitation.revoked_at IS NULL
                 AND invitation.expires_at > statement_timestamp()
           )
       )
    THEN
        RETURN 0;
    END IF;
    SELECT device_row.* INTO device
    FROM clinic_app.otp_totp_totpdevice AS device_row
    WHERE device_row.id = requested_device AND device_row.user_id = actor
      AND NOT EXISTS (
          SELECT 1 FROM clinic_app.identity_authenticatorretirement AS retirement
          WHERE retirement.device_id = device_row.id
      )
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN 0;
    END IF;
    -- django_otp ThrottlingMixin: factor * 2^(failures - 1) seconds of lockout.
    IF device.throttling_failure_count > 0
       AND device.throttling_failure_timestamp IS NOT NULL THEN
        delay_seconds := power(2, device.throttling_failure_count - 1);
        IF extract(
               epoch FROM statement_timestamp() - device.throttling_failure_timestamp
           ) < delay_seconds THEN
            RETURN -1;
        END IF;
    END IF;
    SELECT store.secret INTO secret
    FROM clinic_app.identity_totpsecret AS store
    WHERE store.device_id = device.id;
    code_value := requested_code::bigint;
    base_t := (now_epoch - device.t0) / device.step + device.drift;
    min_t := device.last_t + 1;
    IF secret IS NOT NULL THEN
        FOR offset_step IN (-device.tolerance)..device.tolerance LOOP
            IF (base_t + offset_step) >= min_t THEN
                mac := clinic_app.hmac(int8send(base_t + offset_step), secret, 'sha1');
                byte_offset := get_byte(mac, 19) & 15;
                candidate_code := (
                    ((get_byte(mac, byte_offset) & 127)::bigint << 24)
                    | (get_byte(mac, byte_offset + 1)::bigint << 16)
                    | (get_byte(mac, byte_offset + 2)::bigint << 8)
                    | get_byte(mac, byte_offset + 3)::bigint
                ) % power(10, device.digits)::bigint;
                IF candidate_code = code_value THEN
                    matched_offset := offset_step;
                    EXIT;
                END IF;
            END IF;
        END LOOP;
    END IF;
    IF matched_offset IS NULL THEN
        UPDATE clinic_app.otp_totp_totpdevice
        SET throttling_failure_timestamp = statement_timestamp(),
            throttling_failure_count = throttling_failure_count + 1
        WHERE id = device.id;
        RETURN 0;
    END IF;
    UPDATE clinic_app.otp_totp_totpdevice
    SET last_t = base_t + matched_offset,
        drift = device.drift + matched_offset,
        confirmed = true,
        throttling_failure_timestamp = NULL,
        throttling_failure_count = 0,
        last_used_at = statement_timestamp()
    WHERE id = device.id;
    UPDATE clinic_app.identity_totpsecret
    SET verified_xact = pg_current_xact_id()
    WHERE device_id = device.id;
    RETURN 1;
END
$function$;

-- Create a pending device plus its resolver-held secret and return the
-- provisioning secret (hex) exactly once, for the enrolling user's own QR.
-- Refused once a non-retired confirmed device exists, matching the web flow.
CREATE FUNCTION clinic_app.identity_totp_enroll()
RETURNS TABLE (device_id integer, provisioning_secret text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    actor uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
    pending integer;
    raw_secret bytea;
BEGIN
    IF actor IS NULL
       OR NULLIF(current_setting('app.current_principal', true), '') IS NOT NULL
       OR NULLIF(current_setting('app.current_patient_session', true), '') IS NOT NULL
       OR NOT EXISTS (
           SELECT 1 FROM clinic_app.identity_user AS account
           WHERE account.id = actor AND account.is_active
       )
    THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('clinic-totp:' || actor::text, 0)
    );
    IF EXISTS (
        SELECT 1 FROM clinic_app.otp_totp_totpdevice AS device
        WHERE device.user_id = actor AND device.confirmed
          AND NOT EXISTS (
              SELECT 1 FROM clinic_app.identity_authenticatorretirement AS retirement
              WHERE retirement.device_id = device.id
          )
    ) THEN
        RETURN;
    END IF;
    SELECT device.id INTO pending
    FROM clinic_app.otp_totp_totpdevice AS device
    WHERE device.user_id = actor AND NOT device.confirmed
      AND NOT EXISTS (
          SELECT 1 FROM clinic_app.identity_authenticatorretirement AS retirement
          WHERE retirement.device_id = device.id
      )
    ORDER BY device.id
    LIMIT 1
    FOR UPDATE;
    IF pending IS NULL THEN
        raw_secret := clinic_app.gen_random_bytes(20);
        INSERT INTO clinic_app.otp_totp_totpdevice (
            user_id, name, confirmed, key, step, t0, digits, tolerance, drift,
            last_t, throttling_failure_timestamp, throttling_failure_count,
            created_at, last_used_at
        ) VALUES (
            actor, 'Clinic OS authenticator', false, '', 30, 0, 6, 1, 0, -1,
            NULL, 0, statement_timestamp(), NULL
        ) RETURNING id INTO pending;
        INSERT INTO clinic_app.identity_totpsecret (device_id, secret)
        VALUES (pending, raw_secret);
    ELSE
        -- Reuse keeps the existing secret so a re-render shows the same QR the
        -- enrolling user already scanned; enrol is idempotent while pending.
        SELECT store.secret INTO raw_secret
        FROM clinic_app.identity_totpsecret AS store
        WHERE store.device_id = pending;
        IF raw_secret IS NULL THEN
            raw_secret := clinic_app.gen_random_bytes(20);
            INSERT INTO clinic_app.identity_totpsecret (device_id, secret)
            VALUES (pending, raw_secret);
        END IF;
    END IF;
    RETURN QUERY SELECT pending, encode(raw_secret, 'hex');
END
$function$;

-- A staff invitee resuming enrolment (token, username and password already
-- rechecked by the caller) sees the QR of its own unconfirmed invitation
-- device again, in the exact pending context the password writer binds; the
-- secret is never returned once the device is confirmed or retired.
CREATE FUNCTION clinic_app.identity_invitation_device_secret(requested_hash bytea)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT encode(store.secret, 'hex')
    FROM clinic_app.identity_staffinvitation AS invitation
    JOIN clinic_app.otp_totp_totpdevice AS device
        ON device.id = invitation.device_id
       AND device.user_id = invitation.pending_user_id
    JOIN clinic_app.identity_totpsecret AS store ON store.device_id = device.id
    WHERE invitation.token_hash = requested_hash
      AND invitation.state = 'pending'
      AND invitation.revoked_at IS NULL
      AND invitation.expires_at > statement_timestamp()
      AND invitation.password_set_at IS NOT NULL
      AND invitation.organization_id
          = NULLIF(current_setting('app.current_tenant', true), '')::uuid
      AND invitation.pending_user_id
          = NULLIF(current_setting('app.current_user_id', true), '')::uuid
      AND NULLIF(current_setting('app.current_principal', true), '') IS NULL
      AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
      AND NOT device.confirmed
      AND NOT EXISTS (
          SELECT 1 FROM clinic_app.identity_authenticatorretirement AS retirement
          WHERE retirement.device_id = device.id
      )
$function$;

-- Lock the ACL while clinic_owner still owns the functions (a non-owner cannot
-- revoke PUBLIC's default EXECUTE); the materialised grants persist through the
-- ownership handoff that follows.
REVOKE ALL ON FUNCTION clinic_app.identity_totp_verify(integer, text)
    FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_totp_enroll()
    FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_invitation_device_secret(bytea)
    FROM PUBLIC, clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.identity_totp_verify(integer, text)
    TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.identity_totp_enroll()
    TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.identity_invitation_device_secret(bytea)
    TO clinic_app;
ALTER FUNCTION clinic_app.identity_totp_verify(integer, text)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_totp_enroll()
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_invitation_device_secret(bytea)
    OWNER TO clinic_resolver;
"""

# The session stamp is bound to the verifier: it needs identity_totp_verify's
# acceptance of a genuine code for this device in this very transaction and
# consumes it, so a fresh device use by the real user can never verify a forged
# session registered on another connection, and one code verifies one session.
# The other predicates are unchanged from 0037; replaced as the resolver owner.
SESSION_VERIFY_SQL: Final = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.identity_session_verify(
    requested_hash bytea, requested_device integer
)
RETURNS boolean LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH verified AS (
        UPDATE clinic_app.identity_sessionrecord AS session
        SET verification_device_id = requested_device,
            verification_passkey_id = NULL,
            verified_at = statement_timestamp()
        FROM clinic_app.otp_totp_totpdevice AS device
        JOIN clinic_app.identity_totpsecret AS store
          ON store.device_id = device.id
        WHERE session.session_key_hash = requested_hash
          AND session.user_id = NULLIF(
              current_setting('app.current_user_id', true), ''
          )::uuid
          AND session.revoked_at IS NULL
          AND session.expires_at > statement_timestamp()
          AND device.id = requested_device AND device.user_id = session.user_id
          AND device.confirmed AND device.last_t >= 0
          AND device.last_used_at >= statement_timestamp() - interval '5 minutes'
          AND store.verified_xact = pg_current_xact_id()
          AND NOT EXISTS (
              SELECT 1 FROM clinic_app.identity_authenticatorretirement AS retirement
              WHERE retirement.device_id = device.id
          )
        RETURNING device.id AS device_id
    ), consumed AS (
        UPDATE clinic_app.identity_totpsecret AS store
        SET verified_xact = NULL
        FROM verified
        WHERE store.device_id = verified.device_id
        RETURNING store.device_id
    )
    SELECT EXISTS (SELECT 1 FROM consumed)
$function$;
RESET ROLE;
"""

# The 0037 stamp, restored verbatim by the reverse.
_SESSION_VERIFY_START = RETIREMENT_SQL.index(
    "CREATE OR REPLACE FUNCTION clinic_app.identity_session_verify("
)
_SESSION_VERIFY_0037: Final = RETIREMENT_SQL[
    _SESSION_VERIFY_START : RETIREMENT_SQL.index("$function$;", _SESSION_VERIFY_START)
    + len("$function$;")
]

# A device-table guard: only the resolver definers may change the verification
# state; any other writer may at most insert a fresh pending device. Deleting a
# device stays allowed (it removes an authenticator, it cannot forge a use) and
# the foreign key cascades to the hidden secret. Installed after the data move
# so the clinic_owner backfill is exempt.
GUARD_SQL: Final = """
CREATE FUNCTION clinic_app.identity_totp_device_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF current_user = 'clinic_resolver' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.confirmed OR NEW.last_used_at IS NOT NULL OR NEW.last_t <> -1
           OR NEW.throttling_failure_count <> 0
           OR NEW.throttling_failure_timestamp IS NOT NULL OR NEW.drift <> 0
        THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'totp enrollment requires its verification boundary';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.key IS DISTINCT FROM OLD.key
       OR NEW.last_t IS DISTINCT FROM OLD.last_t
       OR NEW.last_used_at IS DISTINCT FROM OLD.last_used_at
       OR NEW.confirmed IS DISTINCT FROM OLD.confirmed
       OR NEW.drift IS DISTINCT FROM OLD.drift
       OR NEW.throttling_failure_count IS DISTINCT FROM OLD.throttling_failure_count
       OR NEW.throttling_failure_timestamp
           IS DISTINCT FROM OLD.throttling_failure_timestamp
       OR NEW.step IS DISTINCT FROM OLD.step
       OR NEW.t0 IS DISTINCT FROM OLD.t0
       OR NEW.digits IS DISTINCT FROM OLD.digits
       OR NEW.tolerance IS DISTINCT FROM OLD.tolerance
       OR NEW.user_id IS DISTINCT FROM OLD.user_id
    THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'totp verification state requires its verification boundary';
    END IF;
    RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION clinic_app.identity_totp_device_guard() FROM PUBLIC;
CREATE TRIGGER identity_totp_device_boundary
    BEFORE INSERT OR UPDATE ON clinic_app.otp_totp_totpdevice
    FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_totp_device_guard();
"""


# Staff-invitation enrolment already generated the device secret in Python and
# passed it as ``requested_device_key``; store it in the hidden secret table and
# clear ``key`` so an invitation-enrolled device is as unforgeable as a
# self-enrolled one. Both the resolver entrypoint (identity_invitation_begin,
# resolver-owned) and the owner-owned web writer (identity_invitation_password)
# share one body; signature and result are unchanged. identity_invitation_begin
# must be replaced as its clinic_resolver owner; identity_invitation_password
# stays clinic_owner-owned and writes the secret through its INSERT grant.
def _invitation_writer(name: str, *, store_secret: bool) -> str:
    """Render one staff-invitation password/device writer body."""
    key_literal = "''" if store_secret else "requested_device_key"
    secret_insert = (
        "    INSERT INTO clinic_app.identity_totpsecret(device_id, secret)\n"
        "    VALUES (created_device, decode(requested_device_key, 'hex'));\n"
        if store_secret
        else ""
    )
    return f"""
CREATE OR REPLACE FUNCTION clinic_app.{name}(
    requested_hash bytea, requested_username bytea,
    requested_password text, requested_device_key text
)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    invitation clinic_app.identity_staffinvitation%ROWTYPE;
    created_device integer;
BEGIN
    SELECT * INTO invitation
    FROM clinic_app.identity_staffinvitation
    WHERE token_hash = requested_hash
      AND state = 'pending' AND revoked_at IS NULL
      AND expires_at > statement_timestamp()
      AND organization_id = NULLIF(
          current_setting('app.current_tenant', true), ''
      )::uuid
      AND pending_user_id = NULLIF(
          current_setting('app.current_user_id', true), ''
      )::uuid
    FOR UPDATE;
    IF NOT FOUND OR invitation.password_set_at IS NOT NULL
       OR requested_username IS NULL OR requested_password IS NULL
       OR requested_password = '!' OR requested_device_key IS NULL
       OR requested_device_key !~ '^[a-f0-9]{{40}}$'
    THEN
        RETURN NULL;
    END IF;
    UPDATE clinic_app.identity_user
    SET password = requested_password
    WHERE id = invitation.pending_user_id AND NOT is_active;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    INSERT INTO clinic_app.otp_totp_totpdevice(
        user_id, name, confirmed, key, step, t0, digits, tolerance,
        drift, last_t, throttling_failure_timestamp, throttling_failure_count,
        created_at, last_used_at
    ) VALUES (
        invitation.pending_user_id, 'Clinic OS authenticator', false,
        {key_literal}, 30, 0, 6, 1, 0, -1, NULL, 0,
        statement_timestamp(), NULL
    ) RETURNING id INTO created_device;
{secret_insert}    UPDATE clinic_app.identity_staffinvitation
    SET desired_username = requested_username,
        password_set_at = statement_timestamp(),
        device_id = created_device
    WHERE id = invitation.id;
    RETURN created_device;
END
$function$;
"""  # noqa: S608 - DDL built from migration literals, not caller input


INVITATION_SECRET_SQL: Final = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _invitation_writer("identity_invitation_begin", store_secret=True)
    + "RESET ROLE;\n"
    + _invitation_writer("identity_invitation_password", store_secret=True)
)

# Reversible without loss: before the store and the verifier are dropped, the
# resolver writes every hidden seed back into ``key`` (lowercase hex, exactly
# what django-otp generates), which is the pre-0048 posture, so a rollback to
# code that verifies in Python never strands a user without an authenticator.
REVERSE_SQL: Final = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _invitation_writer("identity_invitation_begin", store_secret=False)
    + "RESET ROLE;\n"
    + _invitation_writer("identity_invitation_password", store_secret=False)
    + """
DROP TRIGGER IF EXISTS identity_totp_device_boundary
    ON clinic_app.otp_totp_totpdevice;
DROP FUNCTION IF EXISTS clinic_app.identity_totp_device_guard();
SET LOCAL ROLE clinic_resolver;
UPDATE clinic_app.otp_totp_totpdevice AS device
SET key = encode(store.secret, 'hex')
FROM clinic_app.identity_totpsecret AS store
WHERE store.device_id = device.id;
"""
    + _SESSION_VERIFY_0037
    + """
DROP FUNCTION IF EXISTS clinic_app.identity_invitation_device_secret(bytea);
DROP FUNCTION IF EXISTS clinic_app.identity_totp_enroll();
DROP FUNCTION IF EXISTS clinic_app.identity_totp_verify(integer, text);
DROP TABLE IF EXISTS clinic_app.identity_totpsecret;
RESET ROLE;
REVOKE USAGE ON SEQUENCE clinic_app.otp_totp_totpdevice_id_seq FROM clinic_resolver;
REVOKE SELECT, INSERT, UPDATE
    ON clinic_app.otp_totp_totpdevice FROM clinic_resolver;
GRANT SELECT (id, user_id, confirmed, last_t, last_used_at)
    ON clinic_app.otp_totp_totpdevice TO clinic_resolver;
"""
)
