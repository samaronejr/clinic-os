"""Consent-specific FORCE RLS, immutable provenance and patient authority."""

_sql = """
GRANT SELECT ON clinic_app.consent_consenttext,
 clinic_app.consent_consentacceptance, clinic_app.consent_consentrevocation
 TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.consent_session()
RETURNS TABLE(session_id uuid, organization_id uuid, clinic_id uuid,
 patient_id uuid, enrollment_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT s.id,s.organization_id,s.clinic_id,s.patient_id,s.enrollment_id
 FROM clinic_app.intake_patientsession s
 JOIN clinic_app.intake_patientaccessgrant g ON g.id=s.grant_id
 WHERE s.id=NULLIF(current_setting('app.current_patient_session',true),'')::uuid
 AND NULLIF(current_setting('app.current_user_id',true),'') IS NULL
 AND s.revoked_at IS NULL AND g.revoked_at IS NULL
 AND s.expires_at > statement_timestamp()
 AND s.idle_expires_at > statement_timestamp()
 AND 'consent'=ANY(s.operations)
$f$;
CREATE FUNCTION clinic_app.consent_immutable()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 RAISE EXCEPTION 'consent history is immutable' USING ERRCODE='23514';
END
$f$;
CREATE FUNCTION clinic_app.consent_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE s RECORD; t RECORD; a RECORD; last_version integer;
BEGIN
 IF TG_TABLE_NAME='consent_consenttext' THEN
   IF NOT clinic_app.questionnaire_staff(NEW.clinic_id,ARRAY['owner','clinic_admin'])
      OR NEW.published_by_id IS DISTINCT FROM
        NULLIF(current_setting('app.current_user_id',true),'')::uuid
      OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
        WHERE c.id=NEW.clinic_id AND c.organization_id=NEW.organization_id)
      OR btrim(NEW.text)='' OR length(NEW.text)>20000
      OR NEW.digest <> encode(
        clinic_app.digest(convert_to(NEW.text,'UTF8'),'sha256'),'hex')
   THEN RAISE EXCEPTION 'invalid consent publication' USING ERRCODE='23514'; END IF;
   PERFORM pg_advisory_xact_lock(hashtextextended(
     'consent:'||NEW.clinic_id||':'||NEW.purpose,0));
   SELECT coalesce(max(version),0) INTO last_version
     FROM clinic_app.consent_consenttext
     WHERE clinic_id=NEW.clinic_id AND purpose=NEW.purpose;
   IF NEW.version <> last_version+1 THEN
     RAISE EXCEPTION 'stale consent publication' USING ERRCODE='23514'; END IF;
   NEW.created_at := statement_timestamp();
   RETURN NEW;
 END IF;
 SELECT * INTO s FROM clinic_app.consent_session();
 IF s.session_id IS NULL OR NEW.patient_session_id IS DISTINCT FROM s.session_id
 OR NEW.organization_id IS DISTINCT FROM s.organization_id
 OR NEW.clinic_id IS DISTINCT FROM s.clinic_id THEN
   RAISE EXCEPTION 'patient consent authority required' USING ERRCODE='42501'; END IF;
 IF TG_TABLE_NAME='consent_consentacceptance' THEN
   SELECT * INTO t FROM clinic_app.consent_consenttext WHERE id=NEW.text_id;
   IF t.id IS NULL OR t.clinic_id IS DISTINCT FROM s.clinic_id
      OR NEW.patient_id IS DISTINCT FROM s.patient_id
      OR NEW.enrollment_id IS DISTINCT FROM s.enrollment_id THEN
     RAISE EXCEPTION 'invalid consent binding' USING ERRCODE='23514'; END IF;
   PERFORM pg_advisory_xact_lock(hashtextextended(
     'consent:'||t.clinic_id||':'||t.purpose,0));
   IF EXISTS (SELECT 1 FROM clinic_app.consent_consenttext newer
     WHERE newer.clinic_id=t.clinic_id AND newer.purpose=t.purpose
       AND newer.version>t.version) THEN
     RAISE EXCEPTION 'stale consent text' USING ERRCODE='23514'; END IF;
   NEW.accepted_at := statement_timestamp();
 ELSE
   SELECT * INTO a FROM clinic_app.consent_consentacceptance WHERE id=NEW.acceptance_id;
   IF a.id IS NULL OR a.enrollment_id IS DISTINCT FROM s.enrollment_id
      OR a.patient_id IS DISTINCT FROM s.patient_id
      OR a.clinic_id IS DISTINCT FROM s.clinic_id THEN
     RAISE EXCEPTION 'invalid revocation binding' USING ERRCODE='23514'; END IF;
   NEW.revoked_at := statement_timestamp();
 END IF;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.consent_session(),
 clinic_app.consent_immutable(),clinic_app.consent_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.consent_session() TO clinic_app, clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.consent_immutable(),clinic_app.consent_guard()
 TO clinic_owner;
RESET ROLE;
"""

for table in ("consenttext", "consentacceptance", "consentrevocation"):
    _sql += f"""
ALTER TABLE clinic_app.consent_{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.consent_{table} FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.consent_{table} FROM PUBLIC,clinic_app;
GRANT SELECT,INSERT ON clinic_app.consent_{table} TO clinic_app;
CREATE POLICY setup_tenant ON clinic_app.consent_{table} TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE TRIGGER consent_immutable BEFORE UPDATE OR DELETE ON clinic_app.consent_{table}
 FOR EACH ROW EXECUTE FUNCTION clinic_app.consent_immutable();
CREATE TRIGGER consent_binding BEFORE INSERT ON clinic_app.consent_{table}
 FOR EACH ROW EXECUTE FUNCTION clinic_app.consent_guard();
"""

_sql += """
CREATE POLICY consent_text_read ON clinic_app.consent_consenttext
 FOR SELECT TO clinic_app
 USING (clinic_app.questionnaire_staff(clinic_id,
   ARRAY['owner','clinic_admin','receptionist','physician'])
 OR clinic_id=(SELECT s.clinic_id FROM clinic_app.consent_session() s));
CREATE POLICY consent_text_insert ON clinic_app.consent_consenttext
 FOR INSERT TO clinic_app
 WITH CHECK (clinic_app.questionnaire_staff(clinic_id,ARRAY['owner','clinic_admin']));
CREATE POLICY consent_acceptance_read ON clinic_app.consent_consentacceptance
 FOR SELECT TO clinic_app
 USING (clinic_app.questionnaire_staff(clinic_id,
   ARRAY['owner','clinic_admin','receptionist','physician'])
 OR enrollment_id=(SELECT s.enrollment_id FROM clinic_app.consent_session() s));
CREATE POLICY consent_acceptance_insert ON clinic_app.consent_consentacceptance
 FOR INSERT TO clinic_app
 WITH CHECK (enrollment_id=
   (SELECT s.enrollment_id FROM clinic_app.consent_session() s));
CREATE POLICY consent_revocation_read ON clinic_app.consent_consentrevocation
 FOR SELECT TO clinic_app
 USING (clinic_app.questionnaire_staff(clinic_id,
   ARRAY['owner','clinic_admin','receptionist','physician'])
 OR EXISTS (SELECT 1 FROM clinic_app.consent_consentacceptance a
 WHERE a.id=acceptance_id
 AND a.enrollment_id=(SELECT s.enrollment_id FROM clinic_app.consent_session() s)));
CREATE POLICY consent_revocation_insert ON clinic_app.consent_consentrevocation
 FOR INSERT TO clinic_app
 WITH CHECK (EXISTS (SELECT 1 FROM clinic_app.consent_consentacceptance a
 WHERE a.id=acceptance_id
 AND a.enrollment_id=(SELECT s.enrollment_id FROM clinic_app.consent_session() s)));
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.consent_immutable(),clinic_app.consent_guard()
 FROM clinic_owner;
RESET ROLE;
"""

# Resolver validates the receipt before the owner-owned audit writer appends it.
_sql += """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.consent_audit_scope(record_id uuid, event_name text)
RETURNS TABLE(session_id uuid, organization_id uuid, clinic_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT s.session_id,s.organization_id,s.clinic_id FROM clinic_app.consent_session() s
 WHERE (event_name='consent.accepted' AND EXISTS (
 SELECT 1 FROM clinic_app.consent_consentacceptance a WHERE a.id=record_id
 AND a.patient_session_id=s.session_id AND a.enrollment_id=s.enrollment_id))
 OR (event_name='consent.revoked' AND EXISTS (
 SELECT 1 FROM clinic_app.consent_consentrevocation r
 JOIN clinic_app.consent_consentacceptance a ON a.id=r.acceptance_id
 WHERE r.id=record_id AND r.patient_session_id=s.session_id
 AND a.enrollment_id=s.enrollment_id))
$f$;
REVOKE ALL ON FUNCTION clinic_app.consent_audit_scope(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.consent_audit_scope(uuid,text) TO clinic_owner;
RESET ROLE;
CREATE FUNCTION clinic_app.consent_audit(record_id uuid, event_name text,
 occurred_at_utc timestamptz, content_hash bytea)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE s RECORD; previous_hash bytea; inserted_seq bigint;
BEGIN
 SELECT * INTO s FROM clinic_app.consent_audit_scope(record_id,event_name);
 IF s.session_id IS NULL THEN
 RAISE EXCEPTION 'consent receipt required' USING ERRCODE='42501'; END IF;
 IF current_setting('transaction_isolation') <> 'read committed'
 OR content_hash IS NULL OR octet_length(content_hash) <> 32
 OR occurred_at_utc IS NULL
 OR occurred_at_utc NOT BETWEEN clock_timestamp()-interval '5 minutes'
 AND clock_timestamp()+interval '5 minutes' THEN
 RAISE EXCEPTION 'invalid audit input' USING ERRCODE='22023'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
 ('clinic-audit:'||s.organization_id::text) COLLATE pg_catalog."C",0));
 SELECT a.curr_hash INTO previous_hash FROM clinic_app.audit_event a
 WHERE a.organization_id=s.organization_id ORDER BY a.seq DESC LIMIT 1;
 previous_hash:=coalesce(previous_hash,decode(repeat('00',32),'hex'));
 INSERT INTO clinic_app.audit_event (organization_id,actor_user_id,event_type,
 component_id,component_ip,affected_record_type,affected_record_id,
 occurred_at_utc,payload,prev_hash,curr_hash)
 VALUES (s.organization_id,s.session_id,event_name,'clinic-os-web',NULL,
 CASE WHEN event_name='consent.accepted' THEN 'consent.acceptance'
 ELSE 'consent.revocation' END,
 record_id::text,occurred_at_utc,
 jsonb_build_object('clinic_id',s.clinic_id::text,'object_verb',
 CASE WHEN event_name='consent.accepted' THEN 'accepted' ELSE 'revoked' END),
 previous_hash,clinic_app.digest(content_hash||previous_hash,'sha256'))
 RETURNING seq INTO inserted_seq;
 RETURN inserted_seq;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.consent_audit(uuid,text,timestamptz,bytea)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.consent_audit(uuid,text,timestamptz,bytea)
 TO clinic_app;
"""

SQL = _sql

_reverse_sql = """
DROP FUNCTION clinic_app.consent_audit(uuid,text,timestamptz,bytea);
"""
for table in ("consenttext", "consentacceptance", "consentrevocation"):
    _reverse_sql += f"""
DROP TRIGGER consent_immutable ON clinic_app.consent_{table};
DROP TRIGGER consent_binding ON clinic_app.consent_{table};
"""
for table, label in (
    ("consenttext", "text"),
    ("consentacceptance", "acceptance"),
    ("consentrevocation", "revocation"),
):
    for action in ("read", "insert"):
        _reverse_sql += (
            f"DROP POLICY consent_{label}_{action} ON clinic_app.consent_{table};\n"
        )
_reverse_sql += """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.consent_audit_scope(uuid,text);
DROP FUNCTION clinic_app.consent_guard();
DROP FUNCTION clinic_app.consent_immutable();
DROP FUNCTION clinic_app.consent_session();
RESET ROLE;
"""
REVERSE_SQL = _reverse_sql
