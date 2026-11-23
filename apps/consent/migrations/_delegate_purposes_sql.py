"""Frozen SQL for the delegate purposes migration."""

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.consent_delegate_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE purpose text;
BEGIN
 SELECT t.purpose INTO purpose FROM clinic_app.consent_consenttext t
 WHERE t.id=NEW.text_id;
 IF NOT clinic_app.delegate_purpose_allowed(purpose) THEN
   RAISE EXCEPTION 'patient consent authority required' USING ERRCODE='42501';
 END IF;
 NEW.authority:=CASE WHEN clinic_app.patient_is_delegate()
   THEN 'delegate_explicit_action' ELSE 'patient_explicit_action' END;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.consent_delegate_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.consent_delegate_guard() TO clinic_owner;
RESET ROLE;
CREATE TRIGGER consent_delegate_binding BEFORE INSERT
 ON clinic_app.consent_consentacceptance FOR EACH ROW
 EXECUTE FUNCTION clinic_app.consent_delegate_guard();
CREATE TRIGGER consent_delegate_binding BEFORE INSERT
 ON clinic_app.consent_refusalrecord FOR EACH ROW
 EXECUTE FUNCTION clinic_app.consent_delegate_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.consent_delegate_guard() FROM clinic_owner;
RESET ROLE;
CREATE POLICY delegate_purpose ON clinic_app.consent_consenttext
 AS RESTRICTIVE TO clinic_app USING (
 NOT clinic_app.patient_is_delegate() OR clinic_app.delegate_purpose_allowed(purpose));
CREATE POLICY delegate_purpose ON clinic_app.consent_consentacceptance
 AS RESTRICTIVE TO clinic_app
 USING (NOT clinic_app.patient_is_delegate() OR clinic_app.delegate_purpose_allowed(
 (SELECT t.purpose FROM clinic_app.consent_consenttext t WHERE t.id=text_id)))
 WITH CHECK (NOT clinic_app.patient_is_delegate()
 OR clinic_app.delegate_purpose_allowed(
 (SELECT t.purpose FROM clinic_app.consent_consenttext t WHERE t.id=text_id)));
CREATE POLICY delegate_purpose ON clinic_app.consent_refusalrecord
 AS RESTRICTIVE TO clinic_app
 USING (NOT clinic_app.patient_is_delegate() OR clinic_app.delegate_purpose_allowed(
 (SELECT t.purpose FROM clinic_app.consent_consenttext t WHERE t.id=text_id)))
 WITH CHECK (NOT clinic_app.patient_is_delegate()
 OR clinic_app.delegate_purpose_allowed(
 (SELECT t.purpose FROM clinic_app.consent_consenttext t WHERE t.id=text_id)));
CREATE POLICY delegate_purpose ON clinic_app.consent_consentrevocation
 AS RESTRICTIVE TO clinic_app
 USING (NOT clinic_app.patient_is_delegate() OR EXISTS (
 SELECT 1 FROM clinic_app.consent_consentacceptance a
 JOIN clinic_app.consent_consenttext t ON t.id=a.text_id
 WHERE a.id=acceptance_id AND clinic_app.delegate_purpose_allowed(t.purpose)))
 WITH CHECK (NOT clinic_app.patient_is_delegate() OR EXISTS (
 SELECT 1 FROM clinic_app.consent_consentacceptance a
 JOIN clinic_app.consent_consenttext t ON t.id=a.text_id
 WHERE a.id=acceptance_id AND clinic_app.delegate_purpose_allowed(t.purpose)));
CREATE POLICY delegate_confidentiality ON clinic_app.consent_aiusedisclosure
 AS RESTRICTIVE TO clinic_app USING (NOT clinic_app.patient_is_delegate());
"""
