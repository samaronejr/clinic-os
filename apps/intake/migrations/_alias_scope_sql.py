"""Same-organization alias projection and persisted human-session authority."""

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.intake_merge_authorized(
    requested_clinic uuid, requested_session uuid
)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    actor_text text := current_setting('app.current_user_id',true);
    tenant_text text := current_setting('app.current_tenant',true);
    session_text text := current_setting('app.current_merge_session',true);
BEGIN
    IF actor_text IS NULL OR tenant_text IS NULL
       OR actor_text !~*
           '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR tenant_text !~*
           '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
       OR session_text IS NULL OR session_text !~ '^[0-9a-f]{64}$'
       OR NULLIF(current_setting('app.current_principal',true),'') IS NOT NULL
       OR NULLIF(current_setting('app.current_patient_session',true),'') IS NOT NULL
    THEN RETURN false; END IF;
    RETURN EXISTS (
        SELECT 1 FROM clinic_app.identity_sessionrecord s
        JOIN clinic_app.identity_userclinicrole r ON r.user_id=s.user_id
        JOIN clinic_app.identity_clinic c ON c.id=r.clinic_id
        WHERE s.id=requested_session AND s.user_id=actor_text::uuid
          AND encode(s.session_key_hash,'hex')=session_text
          AND r.clinic_id=requested_clinic AND r.organization_id=tenant_text::uuid
          AND c.organization_id=r.organization_id
          AND r.role IN ('clinic_manager','physician','clinic_admin','owner')
          AND clinic_app.has_permission('demographics.read',requested_clinic,NULL)
          AND clinic_app.identity_session_verified(s.session_key_hash)
    );
END
$function$;
ALTER FUNCTION clinic_app.intake_merge_authorized(uuid,uuid)
    OWNER TO clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.intake_merge_authorized(uuid,uuid)
    FROM PUBLIC,clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.intake_merge_authorized(uuid,uuid)
    TO clinic_app,clinic_owner;

CREATE FUNCTION clinic_app.intake_merge_mutation_allowed(
    requested_clinic uuid, requested_patient uuid
)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM clinic_app.intake_mergedecision d
        WHERE d.id::text=current_setting('app.current_merge_decision',true)
          AND d.organization_id::text=current_setting('app.current_tenant',true)
          AND d.adjudicator_id::text=current_setting('app.current_user_id',true)
          AND d.decision='merge' AND d.created_at=transaction_timestamp()
          AND requested_patient IN (d.merged_patient_id,d.survivor_patient_id)
          AND clinic_app.intake_merge_authorized(requested_clinic,d.verified_session_id)
    )
$function$;
ALTER FUNCTION clinic_app.intake_merge_mutation_allowed(uuid,uuid)
    OWNER TO clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.intake_merge_mutation_allowed(uuid,uuid)
    FROM PUBLIC,clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.intake_merge_mutation_allowed(uuid,uuid)
    TO clinic_app,clinic_owner;

CREATE FUNCTION clinic_app.patient_alias_set(requested_patient uuid)
RETURNS TABLE(patient_id uuid, survivor_patient_id uuid, unified_at timestamptz)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    requested_org uuid;
    tenant_text text := current_setting('app.current_tenant',true);
    actor_text text := current_setting('app.current_user_id',true);
    patient_session_text text := current_setting('app.current_patient_session',true);
BEGIN
    IF NULLIF(current_setting('app.current_principal',true),'') IS NOT NULL
    THEN RETURN; END IF;
    IF NULLIF(tenant_text,'') IS NOT NULL THEN
        IF NULLIF(patient_session_text,'') IS NOT NULL OR actor_text IS NULL
           OR actor_text !~*
               '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR tenant_text !~*
               '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN RETURN; END IF;
        SELECT p.organization_id INTO requested_org
        FROM clinic_app.intake_patient p
        WHERE p.id=requested_patient AND p.organization_id=tenant_text::uuid
          AND EXISTS (
              SELECT 1 FROM clinic_app.intake_patientclinicenrollment e
              JOIN clinic_app.identity_userclinicrole r ON r.clinic_id=e.clinic_id
              JOIN clinic_app.identity_user u ON u.id=r.user_id AND u.is_active
              WHERE e.patient_id=p.id AND e.organization_id=p.organization_id
                AND r.organization_id=p.organization_id AND r.user_id=actor_text::uuid
          );
    ELSE
        IF patient_session_text IS NULL
           OR patient_session_text !~*
               '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           OR NULLIF(actor_text,'') IS NOT NULL
        THEN RETURN; END IF;
        SELECT s.organization_id INTO requested_org
        FROM clinic_app.current_patient_authority(NULL) s
        WHERE s.session_id=patient_session_text::uuid
          AND s.patient_id=requested_patient;
    END IF;
    IF requested_org IS NULL THEN RETURN; END IF;
    RETURN QUERY
    WITH RECURSIVE forward AS (
        SELECT requested_patient AS id
        UNION
        SELECT a.survivor_patient_id FROM clinic_app.intake_patientalias a
        JOIN forward f ON f.id=a.merged_patient_id
        WHERE a.active AND a.organization_id=requested_org
    ), root AS (
        SELECT f.id FROM forward f WHERE NOT EXISTS (
            SELECT 1 FROM clinic_app.intake_patientalias a
            WHERE a.active AND a.organization_id=requested_org
              AND a.merged_patient_id=f.id
        )
    ), members AS (
        SELECT r.id FROM root r
        UNION
        SELECT a.merged_patient_id FROM clinic_app.intake_patientalias a
        JOIN members m ON m.id=a.survivor_patient_id
        WHERE a.active AND a.organization_id=requested_org
    )
    SELECT m.id, r.id,
        (SELECT max(a.created_at) FROM clinic_app.intake_patientalias a
         WHERE a.active AND a.organization_id=requested_org
           AND a.merged_patient_id IN (SELECT lineage.id FROM members lineage))
    FROM members m CROSS JOIN root r ORDER BY m.id;
END
$function$;
ALTER FUNCTION clinic_app.patient_alias_set(uuid) OWNER TO clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.patient_alias_set(uuid) FROM PUBLIC,clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.patient_alias_set(uuid) TO clinic_app,clinic_owner;
RESET ROLE;
"""
