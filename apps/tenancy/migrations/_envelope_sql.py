"""Tenant-scoped pgcrypto envelope encryption and wrapped DEK lifecycle.

The contract is the task-6 protected-data record: AES-256 payloads through
pgcrypto's reviewed OpenPGP implementation (``pgp_sym_encrypt_bytea`` with
``cipher-algo=aes256``, MDC integrity on, compression off), tenant-bound data
encryption keys wrapped by a KEK that arrives per call from the approved
secret boundary, and authenticated context binding organization, purpose and
key version inside the integrity-protected frame. No custom cipher, IV or
composition is introduced; every failure raises and leaves nothing written.

``tenancy_tenantdatakey`` persists only wrapped key bytes, versions and
status. FORCE RLS plus the tenant policy bind even privileged definer
functions to ``app.current_tenant``; the runtime role holds no table grants
and can reach key material only through ``tenant_encrypt``/``tenant_decrypt``.
Key administration (issue, rewrap, reencrypt, status) is owner-only.
"""

from typing import Final

INSTALL_SQL: Final = """
ALTER TABLE clinic_app.tenancy_tenantdatakey ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.tenancy_tenantdatakey FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON clinic_app.tenancy_tenantdatakey
    AS PERMISSIVE FOR ALL TO PUBLIC
    USING (organization_id = NULLIF(
        pg_catalog.current_setting('app.current_tenant', true), ''
    )::pg_catalog.uuid)
    WITH CHECK (organization_id = NULLIF(
        pg_catalog.current_setting('app.current_tenant', true), ''
    )::pg_catalog.uuid);
REVOKE ALL ON clinic_app.tenancy_tenantdatakey
    FROM PUBLIC, clinic_app, clinic_resolver;

CREATE FUNCTION clinic_app.tenant_dek_unwrap(
    kek pg_catalog.text,
    requested_version pg_catalog.int4
)
RETURNS pg_catalog.bytea
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
DECLARE
    tenant_setting pg_catalog.text;
    context_organization_id pg_catalog.uuid;
    wrapped pg_catalog.bytea;
BEGIN
    tenant_setting := NULLIF(
        pg_catalog.current_setting('app.current_tenant', true), ''
    );
    IF tenant_setting IS NULL OR NOT pg_catalog.pg_input_is_valid(
        tenant_setting, 'uuid'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'valid app.current_tenant is required';
    END IF;
    context_organization_id := tenant_setting::pg_catalog.uuid;
    IF context_organization_id =
        '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid
    THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'system organization is not a tenant context';
    END IF;
    IF kek IS NULL OR NOT kek ~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'wrapping key must be 32 bytes hex-encoded';
    END IF;
    SELECT key_row.wrapped_key INTO wrapped
      FROM clinic_app.tenancy_tenantdatakey AS key_row
     WHERE key_row.organization_id = context_organization_id
       AND key_row.key_version = requested_version;
    IF wrapped IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'TEN01',
            MESSAGE = 'tenant data key version is unavailable';
    END IF;
    RETURN clinic_app.pgp_sym_decrypt_bytea(wrapped, kek);
END
$function$;

CREATE FUNCTION clinic_app.tenant_dek_issue(kek pg_catalog.text)
RETURNS pg_catalog.int4
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
DECLARE
    tenant_setting pg_catalog.text;
    context_organization_id pg_catalog.uuid;
    next_version pg_catalog.int4;
BEGIN
    tenant_setting := NULLIF(
        pg_catalog.current_setting('app.current_tenant', true), ''
    );
    IF tenant_setting IS NULL OR NOT pg_catalog.pg_input_is_valid(
        tenant_setting, 'uuid'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'valid app.current_tenant is required';
    END IF;
    context_organization_id := tenant_setting::pg_catalog.uuid;
    IF context_organization_id =
        '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid
    THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'system organization is not a tenant context';
    END IF;
    IF kek IS NULL OR NOT kek ~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'wrapping key must be 32 bytes hex-encoded';
    END IF;
    SELECT key_row.key_version + 1 INTO next_version
      FROM clinic_app.tenancy_tenantdatakey AS key_row
     WHERE key_row.organization_id = context_organization_id
     ORDER BY key_row.key_version DESC
     LIMIT 1
     FOR UPDATE OF key_row;
    IF next_version IS NULL THEN
        next_version := 1;
    END IF;
    UPDATE clinic_app.tenancy_tenantdatakey
       SET status = 'retired', retired_at = pg_catalog.statement_timestamp()
     WHERE organization_id = context_organization_id
       AND status = 'active';
    INSERT INTO clinic_app.tenancy_tenantdatakey (
        id, organization_id, key_version, wrapped_key, status,
        created_at, retired_at
    ) VALUES (
        pg_catalog.gen_random_uuid(),
        context_organization_id,
        next_version,
        clinic_app.pgp_sym_encrypt_bytea(
            clinic_app.gen_random_bytes(32),
            kek,
            'cipher-algo=aes256, compress-algo=0'
        ),
        'active',
        pg_catalog.statement_timestamp(),
        NULL
    );
    RETURN next_version;
END
$function$;

CREATE FUNCTION clinic_app.tenant_encrypt(
    kek pg_catalog.text,
    purpose pg_catalog.text,
    plaintext pg_catalog.bytea
)
RETURNS pg_catalog.bytea
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
DECLARE
    tenant_setting pg_catalog.text;
    context_organization_id pg_catalog.uuid;
    active_version pg_catalog.int4;
    dek pg_catalog.bytea;
    frame pg_catalog.bytea;
BEGIN
    tenant_setting := NULLIF(
        pg_catalog.current_setting('app.current_tenant', true), ''
    );
    IF tenant_setting IS NULL OR NOT pg_catalog.pg_input_is_valid(
        tenant_setting, 'uuid'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'valid app.current_tenant is required';
    END IF;
    context_organization_id := tenant_setting::pg_catalog.uuid;
    IF context_organization_id =
        '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid
    THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'system organization is not a tenant context';
    END IF;
    IF purpose IS NULL
        OR pg_catalog.char_length(purpose) NOT BETWEEN 1 AND 128
        OR purpose ~ '[[:cntrl:]]'
    THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'envelope purpose must be 1-128 printable characters';
    END IF;
    IF plaintext IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'envelope plaintext is required';
    END IF;
    SELECT key_row.key_version INTO active_version
      FROM clinic_app.tenancy_tenantdatakey AS key_row
     WHERE key_row.organization_id = context_organization_id
       AND key_row.status = 'active';
    IF active_version IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'TEN01',
            MESSAGE = 'tenant data key version is unavailable';
    END IF;
    dek := clinic_app.tenant_dek_unwrap(kek, active_version);
    frame := pg_catalog.decode('01', 'hex')
        || pg_catalog.decode(
            pg_catalog.replace(context_organization_id::pg_catalog.text, '-', ''),
            'hex'
        )
        || pg_catalog.convert_to(purpose, 'UTF8')
        || pg_catalog.decode('00', 'hex')
        || plaintext;
    RETURN pg_catalog.decode('01', 'hex')
        || pg_catalog.decode(
            pg_catalog.lpad(pg_catalog.to_hex(active_version), 8, '0'), 'hex'
        )
        || clinic_app.pgp_sym_encrypt_bytea(
            frame,
            pg_catalog.encode(dek, 'hex'),
            'cipher-algo=aes256, compress-algo=0'
        );
END
$function$;

CREATE FUNCTION clinic_app.tenant_decrypt(
    kek pg_catalog.text,
    expected_purpose pg_catalog.text,
    envelope pg_catalog.bytea
)
RETURNS pg_catalog.bytea
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
DECLARE
    tenant_setting pg_catalog.text;
    context_organization_id pg_catalog.uuid;
    envelope_version pg_catalog.int4;
    dek pg_catalog.bytea;
    frame pg_catalog.bytea;
    separator pg_catalog.int4;
    stored_purpose pg_catalog.text;
BEGIN
    tenant_setting := NULLIF(
        pg_catalog.current_setting('app.current_tenant', true), ''
    );
    IF tenant_setting IS NULL OR NOT pg_catalog.pg_input_is_valid(
        tenant_setting, 'uuid'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'valid app.current_tenant is required';
    END IF;
    context_organization_id := tenant_setting::pg_catalog.uuid;
    IF context_organization_id =
        '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid
    THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'system organization is not a tenant context';
    END IF;
    IF envelope IS NULL OR pg_catalog.octet_length(envelope) < 6
        OR pg_catalog.substring(envelope, 1, 1)
            <> pg_catalog.decode('01', 'hex')
    THEN
        RAISE EXCEPTION USING ERRCODE = 'TEN02',
            MESSAGE = 'envelope format is invalid';
    END IF;
    envelope_version := (
        'x' || pg_catalog.encode(
            pg_catalog.substring(envelope, 2, 4), 'hex'
        )
    )::pg_catalog.bit(32)::pg_catalog.int4;
    dek := clinic_app.tenant_dek_unwrap(kek, envelope_version);
    frame := clinic_app.pgp_sym_decrypt_bytea(
        pg_catalog.substring(envelope, 6),
        pg_catalog.encode(dek, 'hex')
    );
    IF pg_catalog.octet_length(frame) < 19
        OR pg_catalog.substring(frame, 1, 1)
            <> pg_catalog.decode('01', 'hex')
        OR pg_catalog.substring(frame, 2, 16)
            <> pg_catalog.decode(
                pg_catalog.replace(
                    context_organization_id::pg_catalog.text, '-', ''
                ),
                'hex'
            )
    THEN
        RAISE EXCEPTION USING ERRCODE = 'TEN03',
            MESSAGE = 'envelope tenant context does not match';
    END IF;
    separator := position(
        pg_catalog.decode('00', 'hex') in pg_catalog.substring(frame, 18)
    );
    IF separator < 1 THEN
        RAISE EXCEPTION USING ERRCODE = 'TEN02',
            MESSAGE = 'envelope format is invalid';
    END IF;
    stored_purpose := pg_catalog.convert_from(
        pg_catalog.substring(frame, 18, separator - 1), 'UTF8'
    );
    IF expected_purpose IS NULL OR stored_purpose <> expected_purpose THEN
        RAISE EXCEPTION USING ERRCODE = 'TEN03',
            MESSAGE = 'envelope purpose does not match';
    END IF;
    RETURN pg_catalog.substring(frame, 18 + separator);
END
$function$;

CREATE FUNCTION clinic_app.tenant_reencrypt(
    kek pg_catalog.text,
    purpose pg_catalog.text,
    envelope pg_catalog.bytea
)
RETURNS pg_catalog.bytea
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
BEGIN
    RETURN clinic_app.tenant_encrypt(
        kek,
        purpose,
        clinic_app.tenant_decrypt(kek, purpose, envelope)
    );
END
$function$;

CREATE FUNCTION clinic_app.tenant_dek_rewrap(
    old_kek pg_catalog.text,
    new_kek pg_catalog.text
)
RETURNS pg_catalog.int4
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
DECLARE
    tenant_setting pg_catalog.text;
    context_organization_id pg_catalog.uuid;
    key_row pg_catalog.record;
    rewrapped pg_catalog.int4;
BEGIN
    tenant_setting := NULLIF(
        pg_catalog.current_setting('app.current_tenant', true), ''
    );
    IF tenant_setting IS NULL OR NOT pg_catalog.pg_input_is_valid(
        tenant_setting, 'uuid'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'valid app.current_tenant is required';
    END IF;
    context_organization_id := tenant_setting::pg_catalog.uuid;
    IF context_organization_id =
        '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid
    THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'system organization is not a tenant context';
    END IF;
    IF old_kek IS NULL OR NOT old_kek ~ '^[0-9a-f]{64}$'
        OR new_kek IS NULL OR NOT new_kek ~ '^[0-9a-f]{64}$'
    THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'wrapping keys must be 32 bytes hex-encoded';
    END IF;
    rewrapped := 0;
    FOR key_row IN
        SELECT key_version, wrapped_key
          FROM clinic_app.tenancy_tenantdatakey
         WHERE organization_id = context_organization_id
         ORDER BY key_version
         FOR UPDATE
    LOOP
        UPDATE clinic_app.tenancy_tenantdatakey
           SET wrapped_key = clinic_app.pgp_sym_encrypt_bytea(
               clinic_app.pgp_sym_decrypt_bytea(key_row.wrapped_key, old_kek),
               new_kek,
               'cipher-algo=aes256, compress-algo=0'
           )
         WHERE organization_id = context_organization_id
           AND key_version = key_row.key_version;
        rewrapped := rewrapped + 1;
    END LOOP;
    RETURN rewrapped;
END
$function$;

CREATE FUNCTION clinic_app.tenant_key_status()
RETURNS TABLE(
    key_version pg_catalog.int4,
    status pg_catalog.text,
    created_at pg_catalog.timestamptz,
    retired_at pg_catalog.timestamptz
)
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
    SELECT key_row.key_version,
           key_row.status,
           key_row.created_at,
           key_row.retired_at
      FROM clinic_app.tenancy_tenantdatakey AS key_row
     WHERE key_row.organization_id = NULLIF(
         pg_catalog.current_setting('app.current_tenant', true), ''
     )::pg_catalog.uuid
     ORDER BY key_row.key_version
$function$;

ALTER FUNCTION clinic_app.tenant_dek_unwrap(pg_catalog.text, pg_catalog.int4)
    OWNER TO clinic_owner;
ALTER FUNCTION clinic_app.tenant_dek_issue(pg_catalog.text)
    OWNER TO clinic_owner;
ALTER FUNCTION clinic_app.tenant_encrypt(
    pg_catalog.text, pg_catalog.text, pg_catalog.bytea
) OWNER TO clinic_owner;
ALTER FUNCTION clinic_app.tenant_decrypt(
    pg_catalog.text, pg_catalog.text, pg_catalog.bytea
) OWNER TO clinic_owner;
ALTER FUNCTION clinic_app.tenant_reencrypt(
    pg_catalog.text, pg_catalog.text, pg_catalog.bytea
) OWNER TO clinic_owner;
ALTER FUNCTION clinic_app.tenant_dek_rewrap(pg_catalog.text, pg_catalog.text)
    OWNER TO clinic_owner;
ALTER FUNCTION clinic_app.tenant_key_status() OWNER TO clinic_owner;

REVOKE ALL ON FUNCTION clinic_app.tenant_dek_unwrap(
    pg_catalog.text, pg_catalog.int4
) FROM PUBLIC, clinic_app, clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.tenant_dek_issue(pg_catalog.text)
    FROM PUBLIC, clinic_app, clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.tenant_encrypt(
    pg_catalog.text, pg_catalog.text, pg_catalog.bytea
) FROM PUBLIC, clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.tenant_decrypt(
    pg_catalog.text, pg_catalog.text, pg_catalog.bytea
) FROM PUBLIC, clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.tenant_reencrypt(
    pg_catalog.text, pg_catalog.text, pg_catalog.bytea
) FROM PUBLIC, clinic_app, clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.tenant_dek_rewrap(
    pg_catalog.text, pg_catalog.text
) FROM PUBLIC, clinic_app, clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.tenant_key_status()
    FROM PUBLIC, clinic_app, clinic_resolver;

GRANT EXECUTE ON FUNCTION clinic_app.tenant_encrypt(
    pg_catalog.text, pg_catalog.text, pg_catalog.bytea
) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.tenant_decrypt(
    pg_catalog.text, pg_catalog.text, pg_catalog.bytea
) TO clinic_app;
-- The recovery superuser verifies restored key material through the same
-- closed decrypt boundary during rehearsals; it never receives encrypt,
-- issue, rewrap or unwrap authority.
GRANT EXECUTE ON FUNCTION clinic_app.tenant_decrypt(
    pg_catalog.text, pg_catalog.text, pg_catalog.bytea
) TO clinic_super;
"""

REVERSE_SQL: Final = """
DROP FUNCTION IF EXISTS clinic_app.tenant_key_status();
DROP FUNCTION IF EXISTS clinic_app.tenant_dek_rewrap(
    pg_catalog.text, pg_catalog.text
);
DROP FUNCTION IF EXISTS clinic_app.tenant_reencrypt(
    pg_catalog.text, pg_catalog.text, pg_catalog.bytea
);
DROP FUNCTION IF EXISTS clinic_app.tenant_decrypt(
    pg_catalog.text, pg_catalog.text, pg_catalog.bytea
);
DROP FUNCTION IF EXISTS clinic_app.tenant_encrypt(
    pg_catalog.text, pg_catalog.text, pg_catalog.bytea
);
DROP FUNCTION IF EXISTS clinic_app.tenant_dek_issue(pg_catalog.text);
DROP FUNCTION IF EXISTS clinic_app.tenant_dek_unwrap(
    pg_catalog.text, pg_catalog.int4
);
DROP POLICY IF EXISTS tenant_isolation ON clinic_app.tenancy_tenantdatakey;
ALTER TABLE clinic_app.tenancy_tenantdatakey DISABLE ROW LEVEL SECURITY;
"""
