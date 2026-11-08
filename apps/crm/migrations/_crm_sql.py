"""CRM FORCE RLS, immutable provenance and purpose-bound send ordering."""

from apps.crm.rules import MARKETING_BODIES

TABLES = (
    "referralsource",
    "lead",
    "marketingpreference",
    "campaign",
    "campaignsend",
    "satisfactionsurvey",
    "publicbookingprofile",
)
_NEUTRAL = ",".join("'" + body.replace("'", "''") + "'" for body in MARKETING_BODIES)

SQL = """
GRANT SELECT ON clinic_app.crm_referralsource, clinic_app.crm_lead,
 clinic_app.crm_marketingpreference, clinic_app.crm_campaign,
 clinic_app.crm_campaignsend, clinic_app.crm_satisfactionsurvey,
 clinic_app.crm_publicbookingprofile TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.crm_manager(clinic uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.has_permission('demographics.read',clinic,NULL)
 AND EXISTS(SELECT 1 FROM clinic_app.identity_userclinicrole r
 JOIN clinic_app.identity_user u ON u.id=r.user_id AND u.is_active
 JOIN clinic_app.identity_clinic c ON c.id=r.clinic_id
 WHERE r.clinic_id=clinic AND r.role='clinic_manager'
 AND r.user_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND c.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
$f$;
CREATE FUNCTION clinic_app.crm_public_profile(requested_code text)
RETURNS TABLE(clinic_id uuid, name text, description text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT p.clinic_id,c.name::text,p.description::text
 FROM clinic_app.crm_publicbookingprofile p
 JOIN clinic_app.identity_clinic c ON c.id=p.clinic_id
 WHERE p.active AND p.code=requested_code
 AND length(requested_code) BETWEEN 1 AND 40
$f$;
CREATE FUNCTION clinic_app.crm_immutable()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 RAISE EXCEPTION 'immutable CRM receipt' USING ERRCODE='42501';
END $f$;
CREATE FUNCTION clinic_app.crm_contact_version(channel_name text)
RETURNS integer LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT c.verified_version FROM clinic_app.intake_patientcontact c
 JOIN clinic_app.consent_session() s ON s.patient_id=c.patient_id
 WHERE c.channel=channel_name AND c.verified_version=c.destination_version
 AND NOT clinic_app.patient_is_delegate()
$f$;
CREATE FUNCTION clinic_app.crm_neutral_message()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE purpose text;
BEGIN
 SELECT c.purpose INTO purpose FROM clinic_app.comms_conversation c
 WHERE c.id=NEW.conversation_id;
 IF NEW.direction='out' AND purpose='marketing' AND NOT EXISTS(
 SELECT 1 FROM clinic_app.comms_templateversion t WHERE t.id=NEW.template_id
 AND t.purpose='marketing' AND t.category='marketing'
 AND t.approval_status='approved' AND t.parameter_count=0
 AND t.body IN (__NEUTRAL__)) THEN
   RAISE EXCEPTION 'neutral marketing content required' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
CREATE FUNCTION clinic_app.crm_marketing_lock()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE c uuid; p text;
BEGIN
 IF TG_TABLE_NAME='consent_consenttext' THEN
   c:=NEW.clinic_id; p:=NEW.purpose;
 ELSIF TG_TABLE_NAME='consent_consentrevocation' THEN
   SELECT a.clinic_id,t.purpose INTO c,p
   FROM clinic_app.consent_consentacceptance a
   JOIN clinic_app.consent_consenttext t ON t.id=a.text_id
   WHERE a.id=NEW.acceptance_id;
 ELSIF TG_TABLE_NAME='comms_conversation' THEN
   c:=NEW.clinic_id; p:=NEW.purpose;
 ELSIF TG_TABLE_NAME='intake_patientcontact' THEN
   FOR c IN SELECT en.clinic_id FROM clinic_app.intake_patientclinicenrollment en
     WHERE en.patient_id=NEW.patient_id ORDER BY en.clinic_id LOOP
     PERFORM pg_advisory_xact_lock(hashtextextended(
      'clinic-lock-v1:comms-subject:crm.marketing:'||c::text,0));
   END LOOP;
   RETURN NEW;
 END IF;
 IF p='marketing' THEN
   PERFORM pg_advisory_xact_lock(hashtextextended(
    'clinic-lock-v1:comms-subject:crm.marketing:'||c::text,0));
 END IF;
 RETURN NEW;
END $f$;
CREATE FUNCTION clinic_app.crm_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s record; e record; t record; c record; latest integer;
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
   RAISE EXCEPTION 'CRM deletion forbidden' USING ERRCODE='42501';
 END IF;
 IF NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic cl
 WHERE cl.id=NEW.clinic_id AND cl.organization_id=NEW.organization_id) THEN
   RAISE EXCEPTION 'invalid CRM binding' USING ERRCODE='23514';
 END IF;
 IF TG_TABLE_NAME='crm_marketingpreference' THEN
   SELECT * INTO s FROM clinic_app.consent_session();
   IF s.session_id IS NULL OR clinic_app.patient_is_delegate()
   OR (NEW.organization_id,NEW.clinic_id,NEW.enrollment_id,NEW.patient_session_id)
    IS DISTINCT FROM (s.organization_id,s.clinic_id,s.enrollment_id,s.session_id)
   THEN RAISE EXCEPTION 'patient choice required' USING ERRCODE='42501'; END IF;
   PERFORM pg_advisory_xact_lock(hashtextextended(
    'clinic-lock-v1:comms-subject:crm.marketing:'||NEW.clinic_id::text,0));
   SELECT coalesce(max(revision),0) INTO latest
   FROM clinic_app.crm_marketingpreference
   WHERE enrollment_id=NEW.enrollment_id AND channel=NEW.channel;
   IF NEW.revision<>latest+1 THEN
     RAISE EXCEPTION 'stale marketing choice' USING ERRCODE='23514'; END IF;
   IF NEW.opted_in THEN
     SELECT * INTO c FROM clinic_app.intake_patientcontact
     WHERE patient_id=s.patient_id AND channel=NEW.channel;
     SELECT * INTO t FROM clinic_app.consent_consenttext
     WHERE clinic_id=s.clinic_id AND purpose='marketing' ORDER BY version DESC LIMIT 1;
     IF c.id IS NULL OR c.verified_version<>c.destination_version
     OR NEW.contact_version<>c.verified_version
     OR NOT EXISTS(SELECT 1 FROM clinic_app.consent_consentacceptance a
       WHERE a.enrollment_id=s.enrollment_id AND a.text_id=t.id
       AND NOT EXISTS(SELECT 1 FROM clinic_app.consent_consentrevocation r
         WHERE r.acceptance_id=a.id))
     THEN RAISE EXCEPTION 'current marketing consent required'
       USING ERRCODE='23514'; END IF;
   ELSIF NEW.contact_version<>0 THEN
     RAISE EXCEPTION 'invalid marketing choice' USING ERRCODE='23514';
   END IF;
   NEW.created_at:=statement_timestamp();
   RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='crm_satisfactionsurvey' THEN
   SELECT * INTO s FROM clinic_app.patient_booking_scope();
   SELECT * INTO e FROM clinic_app.scheduling_appointment WHERE id=NEW.appointment_id;
   IF s.session_id IS NULL OR clinic_app.patient_is_delegate()
   OR (NEW.organization_id,NEW.clinic_id,NEW.patient_session_id)
     IS DISTINCT FROM(s.organization_id,s.clinic_id,s.session_id)
   OR e.id IS NULL OR e.patient_id IS DISTINCT FROM s.patient_id
   OR e.clinic_id IS DISTINCT FROM s.clinic_id OR e.status<>'completed'
   THEN RAISE EXCEPTION 'completed own visit required' USING ERRCODE='42501'; END IF;
   NEW.created_at:=statement_timestamp();
   RETURN NEW;
 END IF;
 IF NOT clinic_app.crm_manager(NEW.clinic_id) THEN
   RAISE EXCEPTION 'CRM manager required' USING ERRCODE='42501'; END IF;
 IF TG_OP='UPDATE' THEN
   IF (to_jsonb(NEW)-ARRAY['state','enrollment_id','name','email','revision',
       'excluded_count','active','description'])
   IS DISTINCT FROM
      (to_jsonb(OLD)-ARRAY['state','enrollment_id','name','email','revision',
       'excluded_count','active','description'])
   THEN RAISE EXCEPTION 'immutable CRM binding' USING ERRCODE='23514'; END IF;
 END IF;
 IF TG_TABLE_NAME='crm_lead' THEN
   IF NEW.source_id IS NOT NULL AND NOT EXISTS(
     SELECT 1 FROM clinic_app.crm_referralsource r WHERE r.id=NEW.source_id
     AND r.clinic_id=NEW.clinic_id AND r.organization_id=NEW.organization_id)
   THEN RAISE EXCEPTION 'invalid lead source' USING ERRCODE='23514'; END IF;
   IF TG_OP='INSERT' AND (NEW.state<>'active' OR NEW.name IS NULL
     OR NEW.email IS NULL OR NEW.expires_at<=statement_timestamp()
     OR NEW.expires_at>statement_timestamp()+interval '91 days') THEN
     RAISE EXCEPTION 'invalid lead retention' USING ERRCODE='23514'; END IF;
   IF NEW.enrollment_id IS NOT NULL AND NOT EXISTS(
     SELECT 1 FROM clinic_app.intake_patientclinicenrollment en
     WHERE en.id=NEW.enrollment_id AND en.clinic_id=NEW.clinic_id
     AND en.organization_id=NEW.organization_id) THEN
     RAISE EXCEPTION 'invalid lead conversion' USING ERRCODE='23514'; END IF;
   IF TG_OP='UPDATE' AND NOT (
     OLD.state='active' AND NEW.state='converted'
       AND NEW.expires_at>statement_timestamp()
       AND NEW.name=OLD.name AND NEW.email=OLD.email
     OR OLD.state IN ('active','converted') AND NEW.state='redacted'
       AND NEW.expires_at<=statement_timestamp()) THEN
     RAISE EXCEPTION 'illegal lead transition' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='crm_campaign' THEN
   SELECT * INTO t FROM clinic_app.comms_templateversion WHERE id=NEW.template_id;
   IF t.id IS NULL OR t.clinic_id IS DISTINCT FROM NEW.clinic_id
   OR t.organization_id IS DISTINCT FROM NEW.organization_id
   OR t.purpose<>'marketing' OR t.category<>'marketing'
   OR t.approval_status<>'approved' OR t.parameter_count<>0
   OR t.body NOT IN (__NEUTRAL__)
   THEN RAISE EXCEPTION 'neutral marketing template required'
     USING ERRCODE='23514'; END IF;
   IF TG_OP='INSERT' AND (NEW.state<>'draft' OR NEW.revision<>1
   OR NEW.created_by_id IS DISTINCT FROM
     NULLIF(current_setting('app.current_user_id',true),'')::uuid) THEN
     RAISE EXCEPTION 'invalid campaign creation' USING ERRCODE='23514'; END IF;
   IF TG_OP='UPDATE' AND NOT (
      (OLD.state='draft' AND NEW.state='approved'
       OR OLD.state='approved' AND NEW.state='sending'
       OR OLD.state='sending' AND NEW.state='sent')
      AND NEW.revision=OLD.revision+1) THEN
     RAISE EXCEPTION 'illegal campaign transition' USING ERRCODE='23514'; END IF;
   IF TG_OP='UPDATE' AND NEW.state='sent' AND (
     NOT EXISTS(SELECT 1 FROM clinic_app.crm_campaignsend WHERE campaign_id=NEW.id)
     OR EXISTS(SELECT 1 FROM clinic_app.crm_campaignsend cs
       WHERE cs.campaign_id=NEW.id AND NOT EXISTS(
         SELECT 1 FROM clinic_app.comms_integrationoperation op
         WHERE op.subject_type='comms.message' AND op.subject_id=cs.message_id
         AND op.status IN ('succeeded','delivered')))) THEN
     RAISE EXCEPTION 'send receipts required' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='crm_campaignsend' THEN
   SELECT * INTO c FROM clinic_app.crm_campaign WHERE id=NEW.campaign_id;
   IF c.id IS NULL OR c.clinic_id IS DISTINCT FROM NEW.clinic_id
   OR c.state<>'sending' OR NOT EXISTS(
     SELECT 1 FROM clinic_app.comms_message m
     JOIN clinic_app.comms_conversation cv ON cv.id=m.conversation_id
     WHERE m.id=NEW.message_id AND cv.enrollment_id=NEW.enrollment_id
     AND cv.clinic_id=NEW.clinic_id AND cv.purpose='marketing'
     AND m.template_id=c.template_id AND m.direction='out')
   THEN RAISE EXCEPTION 'invalid campaign message' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='crm_publicbookingprofile' THEN
   IF NEW.code !~ '^[a-z0-9_-]{1,40}$'
   OR length(NEW.description) NOT BETWEEN 1 AND 400
   OR NEW.description ~ '[<>]' THEN
     RAISE EXCEPTION 'invalid public copy' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $f$;
"""
SQL = SQL.replace("__NEUTRAL__", _NEUTRAL)

SQL += """
CREATE FUNCTION clinic_app.crm_patient_audit_scope(record_id uuid, event_name text)
RETURNS TABLE(session_id uuid, organization_id uuid, clinic_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT p.patient_session_id,p.organization_id,p.clinic_id
 FROM clinic_app.crm_marketingpreference p
 JOIN clinic_app.consent_session() s ON s.session_id=p.patient_session_id
 WHERE p.id=record_id AND event_name='crm.preference.recorded'
 UNION ALL
 SELECT p.patient_session_id,p.organization_id,p.clinic_id
 FROM clinic_app.crm_satisfactionsurvey p
 JOIN clinic_app.patient_booking_scope() s ON s.session_id=p.patient_session_id
 WHERE p.id=record_id AND event_name='crm.survey.recorded'
$f$;
REVOKE ALL ON FUNCTION clinic_app.crm_manager(uuid),
 clinic_app.crm_public_profile(text),clinic_app.crm_immutable(),
 clinic_app.crm_contact_version(text),
 clinic_app.crm_neutral_message(),
 clinic_app.crm_marketing_lock(),clinic_app.crm_guard(),
 clinic_app.crm_patient_audit_scope(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.crm_manager(uuid),
 clinic_app.crm_public_profile(text),clinic_app.crm_contact_version(text)
 TO clinic_app,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.crm_patient_audit_scope(uuid,text) TO clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.crm_immutable(),clinic_app.crm_marketing_lock(),
 clinic_app.crm_guard(),clinic_app.crm_neutral_message() TO clinic_owner;
RESET ROLE;
CREATE FUNCTION clinic_app.crm_patient_audit(record_id uuid,event_name text,
 occurred_at_utc timestamptz,content_hash bytea)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s record; previous_hash bytea; inserted_seq bigint;
BEGIN
 SELECT * INTO s FROM clinic_app.crm_patient_audit_scope(record_id,event_name);
 IF s.session_id IS NULL THEN
   RAISE EXCEPTION 'patient CRM receipt required' USING ERRCODE='42501'; END IF;
 IF current_setting('transaction_isolation')<>'read committed'
 OR content_hash IS NULL OR octet_length(content_hash)<>32
 OR occurred_at_utc IS NULL
 OR occurred_at_utc NOT BETWEEN clock_timestamp()-interval '5 minutes'
   AND clock_timestamp()+interval '5 minutes' THEN
   RAISE EXCEPTION 'invalid audit input' USING ERRCODE='22023'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
  ('clinic-audit:'||s.organization_id::text) COLLATE pg_catalog."C",0));
 SELECT a.curr_hash INTO previous_hash FROM clinic_app.audit_event a
 WHERE a.organization_id=s.organization_id ORDER BY a.seq DESC LIMIT 1;
 previous_hash:=coalesce(previous_hash,decode(repeat('00',32),'hex'));
 INSERT INTO clinic_app.audit_event(organization_id,actor_user_id,event_type,
 component_id,component_ip,affected_record_type,affected_record_id,
 occurred_at_utc,payload,prev_hash,curr_hash)
 VALUES(s.organization_id,s.session_id,event_name,'clinic-os-web',NULL,
 CASE event_name WHEN 'crm.preference.recorded' THEN 'crm.preference'
 ELSE 'crm.survey' END,record_id::text,occurred_at_utc,
 jsonb_build_object('clinic_id',s.clinic_id::text,'object_verb','recorded'),
 previous_hash,clinic_app.digest(content_hash||previous_hash,'sha256'))
 RETURNING seq INTO inserted_seq;
 RETURN inserted_seq;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.crm_patient_audit(uuid,text,timestamptz,bytea)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.crm_patient_audit(uuid,text,timestamptz,bytea)
 TO clinic_app;
"""

for table in TABLES:
    SQL += f"""
ALTER TABLE clinic_app.crm_{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.crm_{table} FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.crm_{table} FROM PUBLIC,clinic_app;
GRANT SELECT,INSERT ON clinic_app.crm_{table} TO clinic_app;
CREATE POLICY setup_tenant ON clinic_app.crm_{table} TO clinic_owner
 USING(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK(organization_id=
  NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY crm_staff_read ON clinic_app.crm_{table} FOR SELECT TO clinic_app
 USING(clinic_app.crm_manager(clinic_id));
CREATE TRIGGER crm_binding BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.crm_{table} FOR EACH ROW EXECUTE FUNCTION clinic_app.crm_guard();
CREATE TRIGGER crm_no_truncate BEFORE TRUNCATE ON clinic_app.crm_{table}
 FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.crm_immutable();
"""
    if table not in {"marketingpreference", "satisfactionsurvey"}:
        SQL += f"""
CREATE POLICY crm_staff_insert ON clinic_app.crm_{table} FOR INSERT TO clinic_app
 WITH CHECK(clinic_app.crm_manager(clinic_id));
"""
    if table in {
        "referralsource",
        "campaignsend",
        "marketingpreference",
        "satisfactionsurvey",
    }:
        SQL += f"""
CREATE TRIGGER crm_no_update BEFORE UPDATE ON clinic_app.crm_{table}
 FOR EACH ROW EXECUTE FUNCTION clinic_app.crm_immutable();
"""

for table, columns in {
    "lead": "state,enrollment_id,name,email",
    "campaign": "state,revision,excluded_count",
    "publicbookingprofile": "active,description",
}.items():
    SQL += f"""
GRANT UPDATE({columns}) ON clinic_app.crm_{table} TO clinic_app;
CREATE POLICY crm_staff_update ON clinic_app.crm_{table} FOR UPDATE TO clinic_app
 USING(clinic_app.crm_manager(clinic_id))
 WITH CHECK(clinic_app.crm_manager(clinic_id));
"""

SQL += """
CREATE POLICY crm_patient_read ON clinic_app.crm_marketingpreference
 FOR SELECT TO clinic_app
 USING(enrollment_id=(SELECT s.enrollment_id FROM clinic_app.consent_session() s));
CREATE POLICY crm_patient_insert ON clinic_app.crm_marketingpreference
 FOR INSERT TO clinic_app WITH CHECK(
 enrollment_id=(SELECT s.enrollment_id FROM clinic_app.consent_session() s));
CREATE POLICY crm_patient_read ON clinic_app.crm_satisfactionsurvey
 FOR SELECT TO clinic_app USING(EXISTS(
 SELECT 1 FROM clinic_app.scheduling_appointment a
 JOIN clinic_app.patient_booking_scope() s ON s.patient_id=a.patient_id
 WHERE a.id=appointment_id AND a.clinic_id=s.clinic_id));
CREATE POLICY crm_patient_insert ON clinic_app.crm_satisfactionsurvey
 FOR INSERT TO clinic_app WITH CHECK(
 clinic_id=(SELECT s.clinic_id FROM clinic_app.patient_booking_scope() s));
CREATE TRIGGER crm_marketing_boundary BEFORE INSERT
 ON clinic_app.consent_consenttext FOR EACH ROW
 EXECUTE FUNCTION clinic_app.crm_marketing_lock();
CREATE TRIGGER crm_marketing_boundary BEFORE UPDATE
 ON clinic_app.comms_conversation FOR EACH ROW
 EXECUTE FUNCTION clinic_app.crm_marketing_lock();
CREATE TRIGGER crm_neutral_marketing BEFORE INSERT ON clinic_app.comms_message
 FOR EACH ROW EXECUTE FUNCTION clinic_app.crm_neutral_message();
CREATE TRIGGER crm_marketing_boundary BEFORE UPDATE
 ON clinic_app.intake_patientcontact FOR EACH ROW
 EXECUTE FUNCTION clinic_app.crm_marketing_lock();
CREATE TRIGGER crm_marketing_boundary BEFORE INSERT
 ON clinic_app.consent_consentrevocation FOR EACH ROW
 EXECUTE FUNCTION clinic_app.crm_marketing_lock();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.crm_immutable(),clinic_app.crm_marketing_lock(),
 clinic_app.crm_guard(),clinic_app.crm_neutral_message() FROM clinic_owner;
RESET ROLE;
"""

REVERSE = """
SET LOCAL ROLE clinic_resolver;
DO $f$
BEGIN
 IF EXISTS(SELECT 1 FROM clinic_app.crm_lead)
 OR EXISTS(SELECT 1 FROM clinic_app.crm_campaign)
 OR EXISTS(SELECT 1 FROM clinic_app.crm_marketingpreference)
 OR EXISTS(SELECT 1 FROM clinic_app.crm_satisfactionsurvey)
 OR EXISTS(SELECT 1 FROM clinic_app.crm_publicbookingprofile)
 OR EXISTS(SELECT 1 FROM clinic_app.crm_referralsource)
 THEN RAISE EXCEPTION 'populated CRM rollback requires restore'; END IF;
END $f$;
RESET ROLE;
DROP TRIGGER crm_marketing_boundary ON clinic_app.consent_consenttext;
DROP TRIGGER crm_marketing_boundary ON clinic_app.consent_consentrevocation;
DROP TRIGGER crm_marketing_boundary ON clinic_app.comms_conversation;
DROP TRIGGER crm_marketing_boundary ON clinic_app.intake_patientcontact;
DROP TRIGGER crm_neutral_marketing ON clinic_app.comms_message;
DROP FUNCTION clinic_app.crm_patient_audit(uuid,text,timestamptz,bytea);
"""
for table in reversed(TABLES):
    REVERSE += f"""
DROP TRIGGER crm_binding ON clinic_app.crm_{table};
DROP TRIGGER crm_no_truncate ON clinic_app.crm_{table};
"""
    if table in {
        "referralsource",
        "campaignsend",
        "marketingpreference",
        "satisfactionsurvey",
    }:
        REVERSE += f"DROP TRIGGER crm_no_update ON clinic_app.crm_{table};\n"
    REVERSE += f"DROP POLICY crm_staff_read ON clinic_app.crm_{table};\n"
    REVERSE += f"DROP POLICY setup_tenant ON clinic_app.crm_{table};\n"
    if table not in {"marketingpreference", "satisfactionsurvey"}:
        REVERSE += f"DROP POLICY crm_staff_insert ON clinic_app.crm_{table};\n"
    if table in {"lead", "campaign", "publicbookingprofile"}:
        REVERSE += f"DROP POLICY crm_staff_update ON clinic_app.crm_{table};\n"
    if table in {"marketingpreference", "satisfactionsurvey"}:
        REVERSE += f"""
DROP POLICY crm_patient_read ON clinic_app.crm_{table};
DROP POLICY crm_patient_insert ON clinic_app.crm_{table};
"""
REVERSE += """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.crm_patient_audit_scope(uuid,text);
DROP FUNCTION clinic_app.crm_guard();
DROP FUNCTION clinic_app.crm_marketing_lock();
DROP FUNCTION clinic_app.crm_immutable();
DROP FUNCTION clinic_app.crm_public_profile(text);
DROP FUNCTION clinic_app.crm_contact_version(text);
DROP FUNCTION clinic_app.crm_neutral_message();
DROP FUNCTION clinic_app.crm_manager(uuid);
RESET ROLE;
"""
