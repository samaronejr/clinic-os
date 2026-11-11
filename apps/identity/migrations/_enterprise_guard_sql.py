"""Database scope, immutable history and authority checks for every new row."""

SQL = """
CREATE FUNCTION clinic_app.enterprise_governance_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
DECLARE actor uuid:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 connection_id uuid; immutable_old jsonb; immutable_new jsonb;
 mutable_columns text[];
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
 RAISE EXCEPTION 'governance history is immutable' USING ERRCODE='23514'; END IF;
 IF TG_TABLE_NAME='identity_scimtoken' AND TG_OP='UPDATE' AND
 (to_jsonb(NEW)-ARRAY['window_start','window_count']) IS NOT DISTINCT FROM
 (to_jsonb(OLD)-ARRAY['window_start','window_count']) THEN
 -- Runtime has no UPDATE grant on these columns. The resolver's pre-scope
 -- authentication limiter must be able to count failed requests too.
 RETURN NEW;
 END IF;
 IF NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
 WHERE c.id=NEW.clinic_id AND c.organization_id=NEW.organization_id)
 OR NEW.organization_id IS DISTINCT FROM
 NULLIF(current_setting('app.current_tenant',true),'')::uuid THEN
 RAISE EXCEPTION 'governance access denied' USING ERRCODE='42501'; END IF;
 IF TG_OP='UPDATE' THEN
 mutable_columns:=CASE TG_TABLE_NAME
 WHEN 'identity_scimresource' THEN
 ARRAY['attributes','search_hash','active','revision','updated_at','deleted_at']
 WHEN 'identity_breakglassgrant' THEN
 ARRAY['revoked_at','reviewed_at','reviewed_by_id','review_task_id']
 WHEN 'identity_supportaccessgrant' THEN ARRAY['revoked_at','reviewed_at']
 WHEN 'identity_scimtoken' THEN ARRAY['revoked_at','window_start','window_count']
 ELSE ARRAY['revoked_at'] END;
 immutable_new:=to_jsonb(NEW)-mutable_columns;
 immutable_old:=to_jsonb(OLD)-mutable_columns;
 IF immutable_new IS DISTINCT FROM immutable_old THEN
 RAISE EXCEPTION 'governance identity is immutable' USING ERRCODE='23514'; END IF;
 IF TG_TABLE_NAME='identity_scimresource' THEN
 IF OLD.deleted_at IS NOT NULL OR NEW.revision<>OLD.revision+1 THEN
 RAISE EXCEPTION 'directory revision conflict' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='identity_scimtoken' THEN
 IF ROW(NEW.window_start,NEW.window_count) IS DISTINCT FROM
 ROW(OLD.window_start,OLD.window_count) THEN
 -- Only the authenticated, resolver-owned limiter can write these columns.
 RETURN NEW;
 END IF;
 IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL THEN
 RAISE EXCEPTION 'revocation is irreversible' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME IN ('identity_breakglassgrant','identity_supportaccessgrant')
 THEN
 IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
 OR OLD.reviewed_at IS NOT NULL AND NEW.reviewed_at IS DISTINCT FROM OLD.reviewed_at
 THEN RAISE EXCEPTION 'terminal grant is immutable' USING ERRCODE='23514'; END IF;
 IF TG_TABLE_NAME='identity_breakglassgrant' THEN
 IF OLD.review_task_id IS NOT NULL
 AND NEW.review_task_id IS DISTINCT FROM OLD.review_task_id THEN
 RAISE EXCEPTION 'mandatory review is immutable' USING ERRCODE='23514'; END IF;
 END IF;
 ELSE
 IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL THEN
 RAISE EXCEPTION 'revocation is irreversible' USING ERRCODE='23514'; END IF;
 END IF;
 END IF;
 IF TG_TABLE_NAME IN ('identity_ssoconnection','identity_scimtoken',
 'identity_scimrolemapping','identity_unitgovernancechange') THEN
 IF NOT clinic_app.enterprise_org_admin(NEW.clinic_id) THEN
 RAISE EXCEPTION 'organization governance denied' USING ERRCODE='42501'; END IF;
 END IF;
 IF TG_TABLE_NAME='identity_unitgovernancechange' THEN
 IF TG_OP<>'INSERT' OR NEW.created_by_id IS DISTINCT FROM actor THEN
 RAISE EXCEPTION 'unit policy history is immutable' USING ERRCODE='23514'; END IF;
 IF NEW.kind='unit_membership' THEN
 PERFORM pg_advisory_xact_lock(hashtextextended(
 'unit-policy:'||NEW.clinic_id::text||':'||NEW.target_user_id::text,0));
 SELECT coalesce(max(policy_version),0)+1 INTO NEW.policy_version
 FROM clinic_app.identity_unitgovernancechange
 WHERE clinic_id=NEW.clinic_id AND target_user_id=NEW.target_user_id;
 END IF;
 END IF;
 IF TG_TABLE_NAME='identity_ssoconnection' THEN
 IF (TG_OP='INSERT' AND NEW.created_by_id IS DISTINCT FROM actor)
 OR NEW.provider_key<>'enterprise_identity'
 OR (TG_OP='INSERT' AND NOT EXISTS (
 SELECT 1 FROM clinic_app.providers_capabilityversion v
 JOIN clinic_app.providers_providercapability p ON p.id=v.capability_id
 WHERE p.key=NEW.provider_key
 AND p.current_version_id=v.id AND (p.clinic_id IS NULL OR p.clinic_id=NEW.clinic_id)
 AND v.provider='deterministic-oidc' AND v.environment='synthetic'
 AND v.state IN ('researched','selected_in_plan'))) THEN
 RAISE EXCEPTION 'identity provider unavailable' USING ERRCODE='42501'; END IF;
 ELSIF TG_TABLE_NAME='identity_scimtoken' THEN
 IF octet_length(NEW.secret_hash)<>32 OR (TG_OP='INSERT' AND
 (NEW.expires_at<=statement_timestamp()
 OR NEW.expires_at>statement_timestamp()+interval '90 days'))
 OR (TG_OP='INSERT' AND NEW.issued_by_id IS DISTINCT FROM actor) THEN
 RAISE EXCEPTION 'directory token denied' USING ERRCODE='23514'; END IF;
 connection_id:=NEW.connection_id;
 ELSIF TG_TABLE_NAME='identity_scimresource' THEN
 connection_id:=NEW.connection_id;
 IF TG_OP='INSERT' AND NEW.kind='Users' AND NOT EXISTS (
 SELECT 1 FROM clinic_app.identity_user u WHERE u.id=NEW.user_id
 AND u.username='directory-'||u.id::text AND u.password='!'
 AND NOT EXISTS (SELECT 1 FROM clinic_app.identity_scimresource r
 WHERE r.user_id=u.id)) THEN
 RAISE EXCEPTION 'directory account binding denied' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='identity_scimrolemapping' THEN
 SELECT r.connection_id INTO connection_id FROM clinic_app.identity_scimresource r
 WHERE r.id=NEW.group_id AND r.kind='Groups' AND r.clinic_id=NEW.clinic_id
 AND r.organization_id=NEW.organization_id AND r.deleted_at IS NULL;
 IF (TG_OP='INSERT' AND NEW.approved_by_id IS DISTINCT FROM actor) OR NOT EXISTS (
 SELECT 1 FROM clinic_app.identity_clinic c WHERE c.id=NEW.unit_id
 AND c.organization_id=NEW.organization_id) OR NOT NEW.bundle=ANY(
 ARRAY['owner','org_admin','clinic_admin','clinic_manager','physician','nurse',
 'allied_professional','finance','scheduler','receptionist']) THEN
 RAISE EXCEPTION 'directory mapping denied' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='identity_scimmembership' THEN
 SELECT g.connection_id INTO connection_id FROM clinic_app.identity_scimresource g
 JOIN clinic_app.identity_scimresource u ON u.id=NEW.member_id
 WHERE g.id=NEW.group_id AND g.kind='Groups' AND u.kind='Users'
 AND u.connection_id=g.connection_id AND g.clinic_id=NEW.clinic_id
 AND u.organization_id=NEW.organization_id AND g.organization_id=NEW.organization_id
 AND g.deleted_at IS NULL AND u.deleted_at IS NULL;
 ELSIF TG_TABLE_NAME='identity_scimrolebinding' THEN
 -- Resolver/owner-only writes; ties to the actual canonical membership.
 IF NOT EXISTS (SELECT 1 FROM clinic_app.identity_scimresource u
 JOIN clinic_app.identity_userclinicrole r ON r.id=NEW.membership_id
 WHERE u.id=NEW.resource_id AND r.user_id=u.user_id
 AND r.organization_id=NEW.organization_id AND r.clinic_id=NEW.clinic_id)
 THEN RAISE EXCEPTION 'directory binding denied' USING ERRCODE='23514'; END IF;
 RETURN NEW;
 ELSIF TG_TABLE_NAME='identity_breakglassgrant' THEN
 IF TG_OP='UPDATE' AND NEW.reviewed_at IS DISTINCT FROM OLD.reviewed_at
 AND (NOT clinic_app.enterprise_manager(NEW.clinic_id)
 OR NEW.reviewed_by_id IS DISTINCT FROM actor OR NOT EXISTS (
 SELECT 1 FROM clinic_app.workflows_task task WHERE task.id=NEW.review_task_id
 AND task.state='done')) THEN
 RAISE EXCEPTION 'manager post event review required' USING ERRCODE='42501'; END IF;
 IF NOT (clinic_app.has_permission('break_glass.request',NEW.clinic_id,NULL)
 OR clinic_app.has_permission('break_glass.request_scoped',NEW.clinic_id,NULL))
 OR (TG_OP='INSERT' AND (NEW.clinician_id IS DISTINCT FROM actor
 OR NOT clinic_app.identity_session_authenticator_verified(
 decode(NULLIF(current_setting('app.enterprise_factor',true),''),'hex'))))
 OR NOT EXISTS (SELECT 1 FROM clinic_app.intake_patientclinicenrollment e
 WHERE e.id=NEW.enrollment_id AND e.organization_id=NEW.organization_id
 AND e.clinic_id=NEW.clinic_id AND (NEW.encounter_id IS NULL OR EXISTS (
 SELECT 1 FROM clinic_app.ehr_encounter visit WHERE visit.id=NEW.encounter_id
 AND visit.patient_id=e.patient_id AND visit.clinic_id=e.clinic_id
 AND visit.organization_id=e.organization_id))) THEN
 -- Managers may only end/review a grant, never borrow its clinical scope.
 IF TG_OP='INSERT' OR NOT clinic_app.enterprise_manager(NEW.clinic_id) THEN
 RAISE EXCEPTION 'emergency access denied' USING ERRCODE='42501'; END IF;
 END IF;
 ELSIF TG_TABLE_NAME='identity_supportaccessgrant' THEN
 IF NOT clinic_app.enterprise_org_admin(NEW.clinic_id)
 OR (TG_OP='INSERT' AND NEW.approved_by_id IS DISTINCT FROM actor)
 OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_user u
 WHERE u.id=NEW.support_user_id AND u.is_active) THEN
 RAISE EXCEPTION 'support approval denied' USING ERRCODE='42501'; END IF;
 ELSIF TG_TABLE_NAME='identity_governancenotice' THEN
 IF TG_OP<>'INSERT' OR NOT EXISTS (
 SELECT 1 FROM clinic_app.identity_breakglassgrant g
 WHERE g.id=NEW.break_glass_id AND g.clinic_id=NEW.clinic_id
 AND g.organization_id=NEW.organization_id AND g.clinician_id=actor) THEN
 RAISE EXCEPTION 'governance notice denied' USING ERRCODE='42501'; END IF;
 END IF;
 IF TG_TABLE_NAME IN ('identity_scimtoken','identity_scimresource',
 'identity_scimmembership','identity_scimrolemapping') AND
 (connection_id IS NULL OR NOT clinic_app.enterprise_directory_authorized(connection_id)
 OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_ssoconnection c
 WHERE c.id=connection_id AND c.organization_id=NEW.organization_id
 AND c.clinic_id=NEW.clinic_id)) THEN
 RAISE EXCEPTION 'directory access denied' USING ERRCODE='42501'; END IF;
 RETURN NEW;
END $f$;

CREATE FUNCTION clinic_app.enterprise_break_glass_complete()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM clinic_app.identity_breakglassgrant g
 JOIN clinic_app.workflows_task t ON t.id=g.review_task_id
 WHERE g.id=NEW.id AND t.kind='review' AND t.idempotency_key=g.id
 AND t.clinic_id=g.clinic_id AND t.organization_id=g.organization_id
 AND t.subject_ref=jsonb_build_object('kind','clinic','id',g.clinic_id::text)
 AND t.due_at=g.expires_at+interval '24 hours' AND t.priority='high'
 AND (SELECT count(*) FROM clinic_app.identity_governancenotice n
 WHERE n.break_glass_id=g.id)=2) THEN
 RAISE EXCEPTION 'emergency notification and review required' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END $f$;
CREATE CONSTRAINT TRIGGER enterprise_grant_complete
AFTER INSERT ON clinic_app.identity_breakglassgrant
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
EXECUTE FUNCTION clinic_app.enterprise_break_glass_complete();
"""
