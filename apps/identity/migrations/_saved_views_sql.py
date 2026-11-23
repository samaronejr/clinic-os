"""Frozen SQL for the saved views migration."""

from typing import Final

from apps.tenancy.migrations_support import revoke_default_dml

_OWNER: Final = """
        organization_id = NULLIF(
            pg_catalog.current_setting('app.current_tenant', true), ''
        )::pg_catalog.uuid
        AND user_id = NULLIF(
            pg_catalog.current_setting('app.current_user_id', true), ''
        )::pg_catalog.uuid
        AND EXISTS (
            SELECT 1 FROM clinic_app.identity_userclinicrole AS role_row
             WHERE role_row.user_id = identity_savedview.user_id
               AND role_row.clinic_id = identity_savedview.clinic_id
               AND role_row.organization_id = identity_savedview.organization_id
        )
"""

FORWARD_SQL: Final = (
    """
ALTER TABLE clinic_app.identity_savedview OWNER TO clinic_owner;
ALTER TABLE clinic_app.identity_savedview ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_savedview FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_savedview
    ADD CONSTRAINT identity_savedview_clinic_fk
    FOREIGN KEY (organization_id, clinic_id)
    REFERENCES clinic_app.identity_clinic (organization_id, id);

CREATE POLICY savedview_owner_only
    ON clinic_app.identity_savedview
    AS PERMISSIVE
    FOR ALL
    TO clinic_app
    USING ("""
    + _OWNER
    + """    )
    WITH CHECK ("""
    + _OWNER
    + """    );
"""
    + revoke_default_dml("identity_savedview", {"SELECT", "INSERT"})
    + """
REVOKE ALL PRIVILEGES ON TABLE clinic_app.identity_savedview
    FROM PUBLIC, clinic_resolver;
GRANT UPDATE (archived_at) ON TABLE clinic_app.identity_savedview TO clinic_app;

CREATE FUNCTION clinic_app.identity_savedview_transition()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'saved view is immutable';
    END IF;
    IF OLD.archived_at IS NOT NULL OR NEW.archived_at IS NULL
       OR (to_jsonb(NEW) - 'archived_at') IS DISTINCT FROM
          (to_jsonb(OLD) - 'archived_at') THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'invalid saved view transition';
    END IF;
    RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION clinic_app.identity_savedview_transition() FROM PUBLIC;
CREATE TRIGGER identity_savedview_transition
BEFORE UPDATE OR DELETE ON clinic_app.identity_savedview
FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_savedview_transition();
"""
)

REVERSE_SQL: Final = """
DROP TRIGGER IF EXISTS identity_savedview_transition ON clinic_app.identity_savedview;
DROP FUNCTION IF EXISTS clinic_app.identity_savedview_transition();
DROP POLICY IF EXISTS savedview_owner_only ON clinic_app.identity_savedview;
ALTER TABLE clinic_app.identity_savedview
    DROP CONSTRAINT IF EXISTS identity_savedview_clinic_fk;
"""
