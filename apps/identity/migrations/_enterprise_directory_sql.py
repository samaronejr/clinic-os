"""Fixed resolver-owned SCIM authority; directory grants never widen bundles."""

SQL = """
CREATE FUNCTION clinic_app.enterprise_org_admin(clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
 WHERE c.id=clinic AND c.organization_id=
 NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 AND clinic_app.identity_session_authenticator_verified(
 decode(NULLIF(current_setting('app.enterprise_factor',true),''),'hex'))
 AND NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
 WHERE c.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND NOT clinic_app.has_permission('staff.organization',c.id,NULL))
 AND NULLIF(current_setting('app.current_principal',true),'') IS NULL
 AND NULLIF(current_setting('app.current_patient_session',true),'') IS NULL
 AND NULLIF(current_setting('app.current_scim_token',true),'') IS NULL
$f$;

CREATE FUNCTION clinic_app.enterprise_directory_authorized(requested_connection uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.identity_ssoconnection c
 JOIN clinic_app.providers_providercapability p ON p.key=c.provider_key
 JOIN clinic_app.providers_capabilityversion v ON v.id=p.current_version_id
 WHERE c.id=requested_connection AND c.revoked_at IS NULL
 AND c.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND p.key='enterprise_identity' AND p.current_version_id=v.id
 AND (p.clinic_id=c.clinic_id OR (p.clinic_id IS NULL AND NOT EXISTS (
 SELECT 1 FROM clinic_app.providers_providercapability override
 WHERE override.key=p.key AND override.clinic_id=c.clinic_id)))
 AND v.environment='synthetic' AND v.provider='deterministic-oidc'
 AND v.state IN ('researched','selected_in_plan')
 AND (clinic_app.enterprise_org_admin(c.clinic_id) OR EXISTS (
 SELECT 1 FROM clinic_app.identity_scimtoken t WHERE t.connection_id=c.id
 AND t.secret_hash=decode(NULLIF(
 current_setting('app.current_scim_token',true),''),'hex')
 AND t.revoked_at IS NULL AND t.expires_at>statement_timestamp())))
$f$;

CREATE FUNCTION clinic_app.enterprise_scim_auth(requested_hash bytea)
RETURNS TABLE(connection_id uuid, organization_id uuid, clinic_id uuid, limited boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
DECLARE t clinic_app.identity_scimtoken; c clinic_app.identity_ssoconnection;
BEGIN
 IF octet_length(requested_hash)<>32 THEN RETURN; END IF;
 SELECT * INTO t FROM clinic_app.identity_scimtoken
 WHERE secret_hash=requested_hash AND revoked_at IS NULL
 AND expires_at>statement_timestamp() FOR UPDATE;
 IF NOT FOUND THEN RETURN; END IF;
 SELECT s.* INTO c FROM clinic_app.identity_ssoconnection s
 JOIN clinic_app.providers_providercapability p ON p.key=s.provider_key
 JOIN clinic_app.providers_capabilityversion v ON v.id=p.current_version_id
 WHERE s.id=t.connection_id AND s.revoked_at IS NULL
 AND p.key='enterprise_identity' AND p.current_version_id=v.id
 AND (p.clinic_id=s.clinic_id OR (p.clinic_id IS NULL AND NOT EXISTS (
 SELECT 1 FROM clinic_app.providers_providercapability override
 WHERE override.key=p.key AND override.clinic_id=s.clinic_id)))
 AND v.provider='deterministic-oidc' AND v.environment='synthetic'
 AND v.state IN ('researched','selected_in_plan');
 IF NOT FOUND THEN RETURN; END IF;
 IF t.window_start<=statement_timestamp()-interval '1 minute' THEN
 UPDATE clinic_app.identity_scimtoken SET window_start=statement_timestamp(),
 window_count=1 WHERE id=t.id;
 ELSE
 IF t.window_count>=100 THEN
 RETURN QUERY SELECT c.id,c.organization_id,c.clinic_id,true; RETURN;
 END IF;
 UPDATE clinic_app.identity_scimtoken SET window_count=window_count+1 WHERE id=t.id;
 END IF;
 RETURN QUERY SELECT c.id,c.organization_id,c.clinic_id,false;
END $f$;

CREATE FUNCTION clinic_app.enterprise_connection_scope(requested_clinic uuid)
RETURNS TABLE(connection_id uuid, organization_id uuid)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
 SELECT c.id,c.organization_id FROM clinic_app.identity_ssoconnection c
 JOIN clinic_app.providers_providercapability p ON p.key=c.provider_key
 JOIN clinic_app.providers_capabilityversion v ON v.id=p.current_version_id
 JOIN clinic_app.identity_clinic u ON u.organization_id=c.organization_id
 WHERE u.id=requested_clinic AND c.revoked_at IS NULL
 AND p.key='enterprise_identity' AND p.current_version_id=v.id
 AND (p.clinic_id=c.clinic_id OR (p.clinic_id IS NULL AND NOT EXISTS (
 SELECT 1 FROM clinic_app.providers_providercapability override
 WHERE override.key=p.key AND override.clinic_id=c.clinic_id)))
 AND v.provider='deterministic-oidc' AND v.environment='synthetic'
 AND v.state IN ('researched','selected_in_plan')
$f$;

CREATE FUNCTION clinic_app.enterprise_new_directory_user(requested_connection uuid)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
DECLARE result uuid:=gen_random_uuid();
BEGIN
 IF NOT clinic_app.enterprise_directory_authorized(requested_connection)
 THEN RETURN NULL; END IF;
 INSERT INTO clinic_app.identity_user(id,username,password,is_active,is_staff,
 is_superuser,first_name,last_name,email,date_joined)
 VALUES(result,'directory-'||result::text,'!',true,false,false,'','','',
 statement_timestamp());
 RETURN result;
END $f$;

CREATE FUNCTION clinic_app.enterprise_scim_audit_scope(requested_clinic uuid)
RETURNS TABLE(principal_id uuid, organization_id uuid)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
 SELECT t.id,t.organization_id FROM clinic_app.identity_scimtoken t
 JOIN clinic_app.enterprise_connection_scope(requested_clinic) scope
 ON scope.connection_id=t.connection_id AND scope.organization_id=t.organization_id
 WHERE t.secret_hash=decode(NULLIF(
 current_setting('app.current_scim_token',true),''),'hex')
 AND t.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND t.revoked_at IS NULL AND t.expires_at>statement_timestamp()
 AND NULLIF(current_setting('app.current_user_id',true),'') IS NULL
 AND NULLIF(current_setting('app.current_principal',true),'') IS NULL
 AND NULLIF(current_setting('app.current_patient_session',true),'') IS NULL
 AND NULLIF(current_setting('app.current_sso_connection',true),'') IS NULL
 AND NULLIF(current_setting('app.current_support_grant',true),'') IS NULL
$f$;

CREATE FUNCTION clinic_app.enterprise_sync_directory(requested_connection uuid)
RETURNS TABLE(binding_id uuid,scope_clinic uuid,verb text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
DECLARE c clinic_app.identity_ssoconnection; item record; role_id uuid;
 created_binding uuid; directory_managed boolean;
BEGIN
 IF NOT clinic_app.enterprise_directory_authorized(requested_connection)
 THEN RAISE EXCEPTION 'directory access denied' USING ERRCODE='42501'; END IF;
 SELECT * INTO c FROM clinic_app.identity_ssoconnection
 WHERE id=requested_connection FOR UPDATE;
 FOR item IN SELECT b.id,b.membership_id,b.clinic_id,b.manages_membership
 FROM clinic_app.identity_scimrolebinding b
 JOIN clinic_app.identity_scimresource u ON u.id=b.resource_id
 JOIN clinic_app.identity_userclinicrole r ON r.id=b.membership_id
 WHERE u.connection_id=c.id AND b.revoked_at IS NULL AND (
 r.revoked_at IS NOT NULL OR NOT EXISTS (
 SELECT 1 FROM clinic_app.identity_scimmembership member
 JOIN clinic_app.identity_scimresource g ON g.id=member.group_id
 JOIN clinic_app.identity_scimrolemapping mapping ON mapping.group_id=g.id
 WHERE member.member_id=u.id AND member.revoked_at IS NULL
 AND u.active AND u.deleted_at IS NULL AND g.active AND g.deleted_at IS NULL
 AND mapping.revoked_at IS NULL AND mapping.unit_id=r.clinic_id
 AND mapping.bundle=r.role
 AND NOT clinic_app.enterprise_unit_denied(r.clinic_id,r.user_id)))
 LOOP
 IF item.manages_membership THEN
 UPDATE clinic_app.identity_userclinicrole SET revoked_at=statement_timestamp()
 WHERE id=item.membership_id AND revoked_at IS NULL AND workflow_bundle_version=1;
 END IF;
 UPDATE clinic_app.identity_scimrolebinding SET revoked_at=statement_timestamp()
 WHERE id=item.id;
 RETURN QUERY SELECT item.id,item.clinic_id,'revoked'::text;
 END LOOP;
 FOR item IN SELECT DISTINCT u.id,u.user_id,mapping.unit_id,mapping.bundle
 FROM clinic_app.identity_scimresource u
 JOIN clinic_app.identity_scimmembership member ON member.member_id=u.id
 JOIN clinic_app.identity_scimresource g ON g.id=member.group_id
 JOIN clinic_app.identity_scimrolemapping mapping ON mapping.group_id=g.id
 WHERE u.connection_id=c.id AND u.kind='Users' AND u.active
 AND u.deleted_at IS NULL AND g.active AND g.deleted_at IS NULL
 AND member.revoked_at IS NULL AND mapping.revoked_at IS NULL
 AND NOT clinic_app.enterprise_unit_denied(mapping.unit_id,u.user_id)
 LOOP
 role_id:=NULL; directory_managed:=true;
 IF item.bundle IN ('owner','physician','receptionist','assistant') THEN
 INSERT INTO clinic_app.identity_userclinicrole
 (id,organization_id,clinic_id,user_id,role,workflow_bundle_version)
 VALUES(gen_random_uuid(),c.organization_id,item.unit_id,item.user_id,item.bundle,1)
 ON CONFLICT (organization_id,clinic_id,user_id,role)
 WHERE revoked_at IS NULL DO NOTHING RETURNING id INTO role_id;
 END IF;
 IF role_id IS NULL THEN
 SELECT r.id,(r.workflow_bundle_version=1 AND
 COALESCE(b.manages_membership,false)) INTO role_id,directory_managed
 FROM clinic_app.identity_userclinicrole r
 LEFT JOIN clinic_app.identity_scimrolebinding b ON b.membership_id=r.id
 AND b.revoked_at IS NULL
 WHERE r.organization_id=c.organization_id AND r.clinic_id=item.unit_id
 AND r.user_id=item.user_id AND r.role=item.bundle AND r.revoked_at IS NULL;
 END IF;
 IF role_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM clinic_app.identity_scimrolebinding b
 WHERE b.membership_id=role_id AND b.revoked_at IS NULL
 AND b.manages_membership=directory_managed) THEN
 created_binding:=gen_random_uuid();
 INSERT INTO clinic_app.identity_scimrolebinding
 (id,organization_id,clinic_id,resource_id,membership_id,manages_membership,created_at)
 VALUES(created_binding,c.organization_id,item.unit_id,item.id,role_id,
 directory_managed,statement_timestamp())
 RETURNING id INTO created_binding;
 RETURN QUERY SELECT created_binding,item.unit_id,'created'::text;
 END IF;
 END LOOP;
 FOR item IN SELECT user_id,active AND deleted_at IS NULL AS enabled
 FROM clinic_app.identity_scimresource WHERE connection_id=c.id AND kind='Users'
 LOOP
 UPDATE clinic_app.identity_user SET is_active=item.enabled WHERE id=item.user_id;
 IF NOT item.enabled THEN
 UPDATE clinic_app.identity_sessionrecord SET revoked_at=statement_timestamp()
 WHERE user_id=item.user_id AND revoked_at IS NULL;
 PERFORM clinic_app.identity_retire_authenticators(item.user_id);
 END IF;
 END LOOP;
END $f$;

CREATE FUNCTION clinic_app.enterprise_sso_principal(
 requested_connection uuid, subject_hash bytea)
RETURNS uuid LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
 SELECT r.user_id FROM clinic_app.identity_scimresource r
 JOIN clinic_app.identity_user u ON u.id=r.user_id AND u.is_active
 JOIN clinic_app.identity_ssoconnection c ON c.id=r.connection_id
 WHERE c.id=requested_connection AND c.revoked_at IS NULL AND r.kind='Users'
 AND r.active AND r.deleted_at IS NULL AND r.external_hash=subject_hash
 AND c.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND c.id=NULLIF(current_setting('app.current_sso_connection',true),'')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.enterprise_connection_scope(c.clinic_id) scope
 WHERE scope.connection_id=c.id)
$f$;

CREATE FUNCTION clinic_app.enterprise_sso_account(
 requested_connection uuid, subject_hash bytea)
RETURNS TABLE(user_id uuid, username text, encoded_password text, resource_id uuid)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
 SELECT u.id,u.username,u.password,r.id FROM clinic_app.identity_scimresource r
 JOIN clinic_app.identity_user u ON u.id=r.user_id AND u.is_active
 JOIN clinic_app.identity_ssoconnection c ON c.id=r.connection_id
 WHERE c.id=requested_connection AND c.revoked_at IS NULL AND r.kind='Users'
 AND r.active AND r.deleted_at IS NULL AND r.external_hash=subject_hash
 AND c.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND c.id=NULLIF(current_setting('app.current_sso_connection',true),'')::uuid
 AND u.id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.user_has_org(c.organization_id)
 AND EXISTS (SELECT 1 FROM clinic_app.enterprise_connection_scope(c.clinic_id) scope
 WHERE scope.connection_id=c.id)
 AND EXISTS (SELECT 1 FROM clinic_app.identity_scimrolebinding b
 WHERE b.resource_id=r.id AND b.revoked_at IS NULL)
$f$;
"""
