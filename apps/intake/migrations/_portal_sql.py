"""Portal v2 reads: verified representation options and messaging channels."""

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.patient_delegate_options(kek text)
RETURNS TABLE(grant_id uuid,patient_name varchar,expires_at timestamptz,
 current boolean)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT d.id,convert_from(clinic_app.protected_decrypt(kek,
   CASE WHEN demographics.social_name IS NOT NULL
   THEN 'intake.patientdemographics.social_name' ELSE 'intake.patient.full_name' END,
   coalesce(demographics.social_name,p.full_name)),'UTF8')::varchar,
   d.expires_at,d.id IS NOT DISTINCT FROM s.delegate_grant_id
 FROM clinic_app.patient_identity(
   NULLIF(current_setting('app.current_patient_session',true),'')::uuid) s
 JOIN clinic_app.identity_patientdelegategrant d
   ON d.delegate_enrollment_id=s.enrollment_id AND d.clinic_id=s.clinic_id
   AND d.organization_id=s.organization_id AND d.state='active'
   AND d.expires_at>clinic_app.delegate_now()
   AND d.verified_by_id IS NOT NULL AND d.verified_at IS NOT NULL
   AND d.activated_at IS NOT NULL AND d.revoked_at IS NULL
   AND EXISTS (SELECT 1 FROM clinic_app.ehr_clinicalattachment evidence
     WHERE evidence.id=d.evidence_attachment_id AND evidence.state='available'
       AND evidence.organization_id=d.organization_id
       AND evidence.clinic_id=d.clinic_id)
 JOIN clinic_app.intake_patientclinicenrollment e
   ON e.id=d.represented_enrollment_id AND e.organization_id=d.organization_id
   AND e.clinic_id=d.clinic_id
 JOIN clinic_app.intake_patient p
   ON p.id=e.patient_id AND p.organization_id=e.organization_id
 LEFT JOIN LATERAL (SELECT x.social_name FROM clinic_app.intake_patientdemographics x
   WHERE x.patient_id=p.id AND x.organization_id=p.organization_id
   ORDER BY x.version DESC LIMIT 1) demographics ON true
 WHERE NULLIF(current_setting('app.current_user_id',true),'') IS NULL
 ORDER BY d.expires_at,d.id
$f$;
CREATE FUNCTION clinic_app.patient_channel_preferences()
RETURNS TABLE(purpose text,channel text,opted_in boolean,verified boolean)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT pref.purpose::text,pref.channel::text,pref.opted_in,
   EXISTS (SELECT 1 FROM clinic_app.intake_patientcontact c
     WHERE c.organization_id=s.organization_id AND c.patient_id=s.patient_id
       AND c.channel=pref.channel AND c.destination_version=c.verified_version)
 FROM clinic_app.current_patient_authority('enrollment_view') s
 JOIN clinic_app.intake_patientchannelpreference pref
   ON pref.organization_id=s.organization_id AND pref.clinic_id=s.clinic_id
   AND pref.patient_id=s.patient_id
 ORDER BY pref.purpose,pref.channel
$f$;
REVOKE ALL ON FUNCTION clinic_app.patient_delegate_options(text),
 clinic_app.patient_channel_preferences() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.patient_delegate_options(text),
 clinic_app.patient_channel_preferences() TO clinic_app;
RESET ROLE;
"""

REVERSE_SQL = """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.patient_channel_preferences();
DROP FUNCTION clinic_app.patient_delegate_options(text);
RESET ROLE;
"""
