"""Frozen v1 machine authority; login identity, never a caller-selected actor."""

SQL = """
ALTER TABLE clinic_app.identity_serviceprincipal
 ADD CONSTRAINT identity_principal_clinic_fk FOREIGN KEY (organization_id,clinic_id)
 REFERENCES clinic_app.identity_clinic(organization_id,id);
ALTER TABLE clinic_app.identity_serviceprincipalgrant
 ADD CONSTRAINT identity_principal_grant_org_fk
 FOREIGN KEY (organization_id,principal_id)
 REFERENCES clinic_app.identity_serviceprincipal(organization_id,id);

REVOKE ALL ON clinic_app.identity_serviceprincipal,
 clinic_app.identity_serviceprincipalgrant FROM PUBLIC, clinic_app, clinic_agent;
GRANT SELECT ON clinic_app.identity_serviceprincipal,
 clinic_app.identity_serviceprincipalgrant TO clinic_resolver;
ALTER TABLE clinic_app.identity_serviceprincipal ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_serviceprincipal FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_serviceprincipalgrant ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_serviceprincipalgrant FORCE ROW LEVEL SECURITY;
CREATE POLICY principal_provision ON clinic_app.identity_serviceprincipal
 TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=NULLIF(
 current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY principal_provision ON clinic_app.identity_serviceprincipalgrant
 TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=NULLIF(
 current_setting('app.current_tenant',true),'')::uuid);

CREATE FUNCTION clinic_app.identity_principal_immutable()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF TG_OP='DELETE' THEN
  RAISE EXCEPTION 'principal history is immutable' USING ERRCODE='23514';
 END IF;
 IF (to_jsonb(NEW)-'active') IS DISTINCT FROM (to_jsonb(OLD)-'active')
 OR NOT OLD.active OR NEW.active THEN
  RAISE EXCEPTION 'principal permits only revocation' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.identity_principal_immutable() FROM PUBLIC;
CREATE TRIGGER principal_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.identity_serviceprincipal FOR EACH ROW
 EXECUTE FUNCTION clinic_app.identity_principal_immutable();
CREATE TRIGGER principal_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.identity_serviceprincipalgrant FOR EACH ROW
 EXECUTE FUNCTION clinic_app.identity_principal_immutable();

SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.principal_scope(
 requested_principal uuid, requested_clinic uuid)
RETURNS uuid LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT p.organization_id FROM clinic_app.identity_serviceprincipal p
 WHERE p.id=requested_principal AND p.clinic_id=requested_clinic
 AND p.db_identity=session_user AND p.active AND p.grant_set_version=1
 AND pg_has_role(session_user,'clinic_agent','USAGE')
 AND current_setting('transaction_isolation')='read committed'
 AND NULLIF(current_setting('app.current_user_id',true),'') IS NULL
 AND NULLIF(current_setting('app.current_patient_session',true),'') IS NULL
 AND EXISTS (SELECT 1 FROM clinic_app.identity_serviceprincipalgrant g
   WHERE g.principal_id=p.id AND g.organization_id=p.organization_id
   AND g.active AND g.subject_scope='clinic' AND g.permission='appointment.read')
$f$;
REVOKE ALL ON FUNCTION clinic_app.principal_scope(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.principal_scope(uuid,uuid) TO clinic_agent;

CREATE FUNCTION clinic_app.principal_has(perm text, clinic uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE principal uuid; tenant uuid; registered_tenant uuid;
BEGIN
 BEGIN
  principal := NULLIF(current_setting('app.current_principal',true),'')::uuid;
  tenant := NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 EXCEPTION WHEN invalid_text_representation THEN RETURN false;
 END;
 registered_tenant := clinic_app.principal_scope(principal,clinic);
 IF tenant IS NULL OR registered_tenant IS NULL OR tenant<>registered_tenant
 THEN RETURN false; END IF;
 RETURN EXISTS (SELECT 1 FROM clinic_app.identity_serviceprincipalgrant g
  WHERE g.principal_id=principal AND g.organization_id=tenant AND g.active
  AND g.permission=perm AND g.subject_scope='clinic');
END $f$;
REVOKE ALL ON FUNCTION clinic_app.principal_has(text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.principal_has(text,uuid) TO clinic_agent;
RESET ROLE;

-- The existing permissive tenant policy must not bypass machine grant checks.
CREATE POLICY agent_grant ON clinic_app.scheduling_availabilityblock
 AS RESTRICTIVE FOR ALL TO clinic_agent
 USING (clinic_app.principal_has('appointment.read',clinic_id))
 WITH CHECK (clinic_app.principal_has('appointment.read',clinic_id));
GRANT SELECT ON clinic_app.scheduling_availabilityblock TO clinic_agent;
"""

REVERSE_SQL = """
REVOKE SELECT ON clinic_app.scheduling_availabilityblock FROM clinic_agent;
DROP POLICY agent_grant ON clinic_app.scheduling_availabilityblock;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.principal_has(text,uuid);
DROP FUNCTION clinic_app.principal_scope(uuid,uuid);
RESET ROLE;
DROP TRIGGER principal_immutable ON clinic_app.identity_serviceprincipalgrant;
DROP TRIGGER principal_immutable ON clinic_app.identity_serviceprincipal;
DROP FUNCTION clinic_app.identity_principal_immutable();
DROP POLICY principal_provision ON clinic_app.identity_serviceprincipalgrant;
DROP POLICY principal_provision ON clinic_app.identity_serviceprincipal;
ALTER TABLE clinic_app.identity_serviceprincipalgrant
 DROP CONSTRAINT identity_principal_grant_org_fk;
ALTER TABLE clinic_app.identity_serviceprincipal
 DROP CONSTRAINT identity_principal_clinic_fk;
REVOKE SELECT ON clinic_app.identity_serviceprincipalgrant,
 clinic_app.identity_serviceprincipal FROM clinic_resolver;
"""
