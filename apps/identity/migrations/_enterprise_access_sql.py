"""Emergency reads exclude restricted notes; support reads no clinical rows."""

SQL = """
CREATE FUNCTION clinic_app.enterprise_manager(clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
 SELECT (clinic_app.has_permission('staff.clinic',clinic,NULL)
 OR clinic_app.has_permission('staff.organization',clinic,NULL))
 AND clinic_app.identity_session_authenticator_verified(
 decode(NULLIF(current_setting('app.enterprise_factor',true),''),'hex'))
$f$;

CREATE FUNCTION clinic_app.enterprise_break_glass_usable(
 requested_grant uuid, clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.identity_breakglassgrant g
 WHERE g.id=requested_grant AND g.clinic_id=clinic AND
 g.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND g.clinician_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND g.revoked_at IS NULL AND g.reviewed_at IS NULL
 AND g.created_at<=statement_timestamp() AND g.expires_at>statement_timestamp()
 AND (clinic_app.has_permission('break_glass.request',clinic,NULL)
 OR clinic_app.has_permission('break_glass.request_scoped',clinic,NULL)))
$f$;

CREATE FUNCTION clinic_app.enterprise_break_glass_versions(requested_grant uuid)
RETURNS SETOF clinic_app.ehr_clinicaldocumentversion
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
 SELECT version.* FROM clinic_app.identity_breakglassgrant g
 JOIN clinic_app.intake_patientclinicenrollment e ON e.id=g.enrollment_id
 JOIN clinic_app.ehr_encounter visit ON visit.patient_id=e.patient_id
 AND visit.clinic_id=e.clinic_id AND visit.organization_id=e.organization_id
 JOIN clinic_app.ehr_clinicaldocument document ON document.encounter_id=visit.id
 JOIN clinic_app.ehr_clinicaldocumentversion version ON version.document_id=document.id
 WHERE g.id=requested_grant
 AND clinic_app.enterprise_break_glass_usable(g.id,g.clinic_id)
 AND clinic_app.has_permission('break_glass.request',g.clinic_id,NULL)
 AND (g.encounter_id IS NULL OR g.encounter_id=visit.id)
 AND version.state IN ('finalized','superseded')
 AND NOT EXISTS (SELECT 1 FROM clinic_app.ehr_restrictednote restricted
 WHERE restricted.document_id=document.id)
 ORDER BY version.created_at,version.id
$f$;

CREATE FUNCTION clinic_app.enterprise_support_scope(
 requested_grant uuid, requested_clinic uuid, requested_session bytea)
RETURNS TABLE(organization_id uuid, user_id uuid, expires_at timestamptz)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
DECLARE target record; previous_actor text; previous_tenant text; valid boolean;
BEGIN
 SELECT g.organization_id,g.support_user_id,g.expires_at,
 credential.organization_id AS authentication_tenant,
 session.verification_passkey_id
 INTO target
 FROM clinic_app.identity_supportaccessgrant g
 JOIN clinic_app.identity_user u ON u.id=g.support_user_id AND u.is_active
 JOIN clinic_app.identity_sessionrecord session ON session.user_id=u.id
 LEFT JOIN clinic_app.identity_passkey credential
 ON credential.id=session.verification_passkey_id
 WHERE g.id=requested_grant AND g.clinic_id=requested_clinic
 AND g.revoked_at IS NULL AND g.reviewed_at IS NULL
 AND g.created_at<=statement_timestamp() AND g.expires_at>statement_timestamp()
 AND session.session_key_hash=requested_session AND session.revoked_at IS NULL
 AND session.expires_at>statement_timestamp()
 AND (session.verification_passkey_id IS NULL OR EXISTS (
 SELECT 1 FROM clinic_app.identity_organization organization
 WHERE organization.id=g.organization_id AND organization.passkey_step_up_enabled));
 IF NOT FOUND THEN RETURN; END IF;
 previous_actor:=coalesce(current_setting('app.current_user_id',true),'');
 previous_tenant:=coalesce(current_setting('app.current_tenant',true),'');
 PERFORM set_config('app.current_user_id',target.support_user_id::text,true);
 PERFORM set_config('app.current_tenant',
 coalesce(target.authentication_tenant::text,''),true);
 valid:=clinic_app.identity_session_authenticator_verified(requested_session);
 PERFORM set_config('app.current_user_id',previous_actor,true);
 PERFORM set_config('app.current_tenant',previous_tenant,true);
 IF valid THEN
 RETURN QUERY SELECT target.organization_id,target.support_user_id,target.expires_at;
 END IF;
END
$f$;

CREATE FUNCTION clinic_app.enterprise_break_glass_observations(requested_grant uuid)
RETURNS SETOF clinic_app.ehr_observation
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
 SELECT observation.* FROM clinic_app.identity_breakglassgrant g
 JOIN clinic_app.intake_patientclinicenrollment e ON e.id=g.enrollment_id
 JOIN clinic_app.ehr_observation observation ON observation.patient_id=e.patient_id
 AND observation.clinic_id=e.clinic_id AND observation.organization_id=e.organization_id
 WHERE g.id=requested_grant AND g.encounter_id IS NULL
 AND clinic_app.enterprise_break_glass_usable(g.id,g.clinic_id)
 ORDER BY observation.effective_at,observation.id
$f$;

CREATE FUNCTION clinic_app.enterprise_patient_access_log(
 clinic uuid, page_number integer)
RETURNS TABLE(notice_id uuid,occurred_at timestamptz,clinician_label text,
 expires_at timestamptz,reviewed boolean,clinic_timezone text)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
 SELECT notice.id,notice.created_at,clinician.username::text,g.expires_at,
  g.reviewed_at IS NOT NULL,unit.timezone::text
 FROM clinic_app.current_patient_authority('enrollment_view') scope
 JOIN clinic_app.identity_breakglassgrant g ON g.enrollment_id=scope.enrollment_id
 AND g.organization_id=scope.organization_id AND g.clinic_id=scope.clinic_id
 JOIN clinic_app.identity_governancenotice notice ON notice.break_glass_id=g.id
 AND notice.audience='patient'
 JOIN clinic_app.identity_user clinician ON clinician.id=g.clinician_id
  JOIN clinic_app.identity_clinic unit ON unit.id=scope.clinic_id
 WHERE scope.clinic_id=clinic AND scope.delegate_grant_id IS NULL
 AND page_number BETWEEN 0 AND 10000
 ORDER BY notice.created_at DESC,notice.id
 LIMIT 100 OFFSET (page_number*100)
$f$;

CREATE FUNCTION clinic_app.enterprise_unit_member_revoke(clinic uuid, target_user uuid)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
DECLARE changed integer;
BEGIN
 IF NOT clinic_app.enterprise_org_admin(clinic) THEN
 RAISE EXCEPTION 'unit policy denied' USING ERRCODE='42501'; END IF;
 UPDATE clinic_app.identity_userclinicrole SET revoked_at=statement_timestamp()
 WHERE clinic_id=clinic AND user_id=target_user AND revoked_at IS NULL
 AND organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 GET DIAGNOSTICS changed=ROW_COUNT;
 RETURN changed;
END $f$;

CREATE FUNCTION clinic_app.enterprise_template_propagate(source uuid, unit uuid)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
DECLARE template clinic_app.ehr_specialtytemplate; next_id uuid:=gen_random_uuid();
 next_version integer;
BEGIN
 SELECT * INTO template FROM clinic_app.ehr_specialtytemplate WHERE id=source;
 IF NOT FOUND OR NOT clinic_app.enterprise_org_admin(template.clinic_id)
 OR NOT clinic_app.enterprise_org_admin(unit) OR unit=template.clinic_id
 OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
 WHERE c.id=unit AND c.organization_id=template.organization_id) THEN
 RAISE EXCEPTION 'unit template denied' USING ERRCODE='42501'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
 'enterprise-template:'||unit::text||':'||template.key,0));
 SELECT coalesce(max(version),0)+1 INTO next_version
 FROM clinic_app.ehr_specialtytemplate WHERE clinic_id=unit AND key=template.key;
 INSERT INTO clinic_app.ehr_specialtytemplate
 (id,organization_id,clinic_id,key,version,title,prompts,created_at)
 VALUES(next_id,template.organization_id,unit,template.key,next_version,
 template.title,template.prompts,statement_timestamp());
 RETURN next_id;
END $f$;
"""
