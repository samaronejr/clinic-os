"""Idempotent catalog linkage; original encrypted registration facts never change."""

from psycopg import sql


def foreign_key(table: str, name: str, columns: str, target: str, keys: str) -> str:
    """Install a fixed, validated FK once without changing existing values."""
    return (
        sql.SQL("""
DO $constraint$
DECLARE source_forced boolean; target_forced boolean;
BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint
  WHERE conrelid={}::regclass AND conname={}) THEN
  SELECT relforcerowsecurity INTO source_forced FROM pg_catalog.pg_class
   WHERE oid={}::regclass;
  SELECT relforcerowsecurity INTO target_forced FROM pg_catalog.pg_class
   WHERE oid={}::regclass;
  ALTER TABLE {} NO FORCE ROW LEVEL SECURITY;
  ALTER TABLE {} NO FORCE ROW LEVEL SECURITY;
  ALTER TABLE {} ADD CONSTRAINT {}
   FOREIGN KEY ({}) REFERENCES {} ({})
   NOT DEFERRABLE;
  IF source_forced THEN ALTER TABLE {} FORCE ROW LEVEL SECURITY; END IF;
  IF target_forced THEN ALTER TABLE {} FORCE ROW LEVEL SECURITY; END IF;
 END IF;
END $constraint$;
""")
        .format(
            sql.Literal("clinic_app." + table),
            sql.Literal(name),
            sql.Literal("clinic_app." + table),
            sql.Literal("clinic_app." + target),
            sql.Identifier("clinic_app", table),
            sql.Identifier("clinic_app", target),
            sql.Identifier("clinic_app", table),
            sql.Identifier(name),
            sql.SQL(",").join(map(sql.Identifier, columns.split(","))),
            sql.Identifier("clinic_app", target),
            sql.SQL(",").join(map(sql.Identifier, keys.split(","))),
            sql.Identifier("clinic_app", table),
            sql.Identifier("clinic_app", target),
        )
        .as_string()
    )


INTAKE_SQL = "".join(
    (
        """
ALTER TABLE clinic_app.intake_insurancemembership
 ADD COLUMN IF NOT EXISTS payer_id uuid NULL;
CREATE UNIQUE INDEX IF NOT EXISTS insurance_payer_org_id
 ON clinic_app.insurance_payer(organization_id,id);
CREATE INDEX IF NOT EXISTS intake_membership_payer_id
 ON clinic_app.intake_insurancemembership(payer_id);
""",
        foreign_key(
            "intake_insurancemembership",
            "intake_membership_payer_fk",
            "payer_id",
            "insurance_payer",
            "id",
        ),
        foreign_key(
            "intake_insurancemembership",
            "intake_membership_payer_org_fk",
            "organization_id,payer_id",
            "insurance_payer",
            "organization_id,id",
        ),
        """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.intake_membership_payer_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE payer clinic_app.insurance_payer;
BEGIN
 IF TG_OP='INSERT' AND NEW.payer_id IS NULL THEN RETURN NEW; END IF;
 IF TG_OP='UPDATE' AND OLD.payer_id IS NULL AND NEW.payer_id IS NOT NULL
  AND (to_jsonb(NEW)-'payer_id')=(to_jsonb(OLD)-'payer_id') THEN
  SELECT * INTO payer FROM clinic_app.insurance_payer
   WHERE id=NEW.payer_id AND organization_id=NEW.organization_id;
  IF payer.id IS NOT NULL
   AND NEW.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
   AND clinic_app.has_permission('tiss.manage',payer.clinic_id,NULL)
   AND EXISTS(SELECT 1 FROM clinic_app.intake_patientclinicenrollment e
    WHERE e.organization_id=NEW.organization_id AND e.clinic_id=payer.clinic_id
     AND e.patient_id=NEW.patient_id) THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501';
 END IF;
 RAISE EXCEPTION 'patient identity history is append-only'
  USING ERRCODE='23514',CONSTRAINT='intake_identity_append_only_check';
END $f$;
REVOKE ALL ON FUNCTION clinic_app.intake_membership_payer_guard()
 FROM PUBLIC,clinic_app,clinic_agent,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.intake_membership_payer_guard()
 TO clinic_owner;
RESET ROLE;
DROP TRIGGER IF EXISTS intake_insurancemembership_append_only
 ON clinic_app.intake_insurancemembership;
CREATE TRIGGER intake_insurancemembership_append_only
 BEFORE INSERT OR UPDATE OR DELETE ON clinic_app.intake_insurancemembership
 FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_membership_payer_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.intake_membership_payer_guard()
 FROM clinic_owner;
RESET ROLE;

GRANT SELECT ON clinic_app.intake_insurancemembership TO clinic_resolver;
GRANT UPDATE(payer_id) ON clinic_app.intake_insurancemembership TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.insurance_link_registered_payer(
 clinic uuid,membership uuid,payer uuid,kek text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE registered clinic_app.intake_insurancemembership;
 operator clinic_app.insurance_payer; ans text; matches integer;
BEGIN
 IF NOT clinic_app.has_permission('tiss.manage',clinic,NULL) THEN
  RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501'; END IF;
 SELECT m.* INTO registered FROM clinic_app.intake_insurancemembership m
  JOIN clinic_app.intake_patientclinicenrollment e
   ON e.organization_id=m.organization_id AND e.patient_id=m.patient_id
  WHERE m.id=membership AND e.clinic_id=clinic
   AND m.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  FOR UPDATE OF m;
 SELECT * INTO operator FROM clinic_app.insurance_payer
  WHERE id=payer AND clinic_id=clinic
   AND organization_id=registered.organization_id;
 IF registered.id IS NULL OR operator.id IS NULL THEN
  RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501'; END IF;
 IF registered.payer_id IS NOT NULL THEN RETURN false; END IF;
 IF registered.ans_number IS NULL THEN RETURN false; END IF;
 ans:=convert_from(clinic_app.protected_decrypt(
  kek,'intake.insurancemembership.ans_number',registered.ans_number),'UTF8');
 SELECT count(*) INTO matches FROM clinic_app.insurance_payer p
  WHERE p.organization_id=registered.organization_id AND p.clinic_id=clinic
   AND p.ans_registry=ans;
 IF ans!~'^[0-9]{6}$' OR ans<>operator.ans_registry OR matches<>1 THEN
  RETURN false; END IF;
 UPDATE clinic_app.intake_insurancemembership SET payer_id=payer
  WHERE id=membership AND organization_id=registered.organization_id
   AND payer_id IS NULL;
 RETURN FOUND;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.insurance_link_registered_payer(uuid,uuid,uuid,text)
 FROM PUBLIC,clinic_agent,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.insurance_link_registered_payer(
 uuid,uuid,uuid,text) TO clinic_app;
RESET ROLE;
""",
    )
)
