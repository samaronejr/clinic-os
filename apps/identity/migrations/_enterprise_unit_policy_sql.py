"""An explicit unit denial cannot be undone by SCIM or invitation replay."""

SQL = """
CREATE FUNCTION clinic_app.enterprise_unit_denied(clinic uuid, target_user uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
 SELECT coalesce((SELECT policy.access_denied
 FROM clinic_app.identity_unitgovernancechange policy
 WHERE policy.kind='unit_membership' AND policy.clinic_id=clinic
 AND policy.target_user_id=target_user
 ORDER BY policy.policy_version DESC LIMIT 1),false)
$f$;

CREATE FUNCTION clinic_app.enterprise_unit_membership_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
BEGIN
 IF NEW.revoked_at IS NULL AND
 clinic_app.enterprise_unit_denied(NEW.clinic_id,NEW.user_id) THEN
 RAISE EXCEPTION 'unit access denied' USING ERRCODE='42501'; END IF;
 RETURN NEW;
END $f$;
CREATE TRIGGER enterprise_unit_membership
 BEFORE INSERT OR UPDATE ON clinic_app.identity_userclinicrole
 FOR EACH ROW EXECUTE FUNCTION clinic_app.enterprise_unit_membership_guard();

CREATE FUNCTION clinic_app.enterprise_unit_policy_apply()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
BEGIN
 IF NEW.kind='unit_membership' AND NEW.access_denied THEN
 IF NOT clinic_app.enterprise_org_admin(NEW.clinic_id) THEN
 RAISE EXCEPTION 'unit access denied' USING ERRCODE='42501'; END IF;
 UPDATE clinic_app.identity_userclinicrole SET revoked_at=statement_timestamp()
 WHERE clinic_id=NEW.clinic_id AND user_id=NEW.target_user_id
 AND organization_id=NEW.organization_id AND revoked_at IS NULL;
 END IF;
 RETURN NULL;
END $f$;
CREATE TRIGGER enterprise_unit_policy
 AFTER INSERT ON clinic_app.identity_unitgovernancechange
 FOR EACH ROW EXECUTE FUNCTION clinic_app.enterprise_unit_policy_apply();
"""
