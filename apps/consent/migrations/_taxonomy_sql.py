"""Consent taxonomy SQL: notices, refusals, participant and AI-use records.

The same ``consent_guard`` trigger gains branches for the four new tables;
``consent_audit_scope`` and ``consent_audit`` learn the ``consent.refused``
patient-actor event. ``CREATE OR REPLACE`` keeps owner, volatility and ACLs;
the reverse SQL restores the exact prior function bodies.
"""

_sql = """
GRANT SELECT ON clinic_app.consent_noticeversion,
 clinic_app.consent_refusalrecord TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.consent_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE s RECORD; t RECORD; a RECORD; e RECORD; last_version integer;
BEGIN
 IF TG_TABLE_NAME='consent_consenttext' THEN
   IF NOT clinic_app.questionnaire_staff(NEW.clinic_id,ARRAY['owner','clinic_admin'])
      OR NEW.published_by_id IS DISTINCT FROM
        NULLIF(current_setting('app.current_user_id',true),'')::uuid
      OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
        WHERE c.id=NEW.clinic_id AND c.organization_id=NEW.organization_id)
      OR NEW.text IS NULL OR pg_catalog.octet_length(NEW.text)>40000
      OR NEW.digest !~ '^[0-9a-f]{64}$'
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
 IF TG_TABLE_NAME='consent_noticeversion' THEN
   IF NOT clinic_app.questionnaire_staff(NEW.clinic_id,ARRAY['owner','clinic_admin'])
      OR NEW.published_by_id IS DISTINCT FROM
        NULLIF(current_setting('app.current_user_id',true),'')::uuid
      OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
        WHERE c.id=NEW.clinic_id AND c.organization_id=NEW.organization_id)
      OR NEW.text IS NULL OR pg_catalog.octet_length(NEW.text)>40000
      OR NEW.digest !~ '^[0-9a-f]{64}$'
   THEN RAISE EXCEPTION 'invalid notice publication' USING ERRCODE='23514'; END IF;
   PERFORM pg_advisory_xact_lock(hashtextextended(
     'consent:'||NEW.clinic_id||':notice:'||NEW.topic,0));
   SELECT coalesce(max(version),0) INTO last_version
     FROM clinic_app.consent_noticeversion
     WHERE clinic_id=NEW.clinic_id AND topic=NEW.topic;
   IF NEW.version <> last_version+1 THEN
     RAISE EXCEPTION 'stale notice publication' USING ERRCODE='23514'; END IF;
   NEW.created_at := statement_timestamp();
   RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='consent_participantacknowledgment' THEN
   SELECT * INTO e FROM clinic_app.ehr_encounter WHERE id=NEW.session_id;
   IF e.id IS NULL OR e.clinic_id IS DISTINCT FROM NEW.clinic_id
      OR e.organization_id IS DISTINCT FROM NEW.organization_id
      OR NOT clinic_app.questionnaire_staff(NEW.clinic_id,ARRAY['physician'])
      OR NEW.acknowledged_by_clinician_id IS DISTINCT FROM
        NULLIF(current_setting('app.current_user_id',true),'')::uuid
   THEN RAISE EXCEPTION 'invalid participant acknowledgment'
     USING ERRCODE='23514'; END IF;
   NEW.acknowledged_at := statement_timestamp();
   RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='consent_aiusedisclosure' THEN
   SELECT * INTO e FROM clinic_app.ehr_encounter WHERE id=NEW.encounter_id;
   IF e.id IS NULL OR e.clinic_id IS DISTINCT FROM NEW.clinic_id
      OR e.organization_id IS DISTINCT FROM NEW.organization_id
      OR e.patient_id IS DISTINCT FROM NEW.patient_id
      OR NOT clinic_app.questionnaire_staff(NEW.clinic_id,ARRAY['physician'])
      OR NEW.recorded_by_id IS DISTINCT FROM
        NULLIF(current_setting('app.current_user_id',true),'')::uuid
      OR (NEW.refused AND NOT NEW.informed)
   THEN RAISE EXCEPTION 'invalid AI-use disclosure' USING ERRCODE='23514'; END IF;
   NEW.recorded_at := statement_timestamp();
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
 ELSIF TG_TABLE_NAME='consent_refusalrecord' THEN
   SELECT * INTO t FROM clinic_app.consent_consenttext WHERE id=NEW.text_id;
   IF t.id IS NULL OR t.clinic_id IS DISTINCT FROM s.clinic_id
      OR NEW.patient_id IS DISTINCT FROM s.patient_id
      OR NEW.enrollment_id IS DISTINCT FROM s.enrollment_id THEN
     RAISE EXCEPTION 'invalid refusal binding' USING ERRCODE='23514'; END IF;
   PERFORM pg_advisory_xact_lock(hashtextextended(
     'consent:'||t.clinic_id||':'||t.purpose,0));
   IF EXISTS (SELECT 1 FROM clinic_app.consent_consenttext newer
     WHERE newer.clinic_id=t.clinic_id AND newer.purpose=t.purpose
       AND newer.version>t.version) THEN
     RAISE EXCEPTION 'stale consent text' USING ERRCODE='23514'; END IF;
   IF EXISTS (SELECT 1 FROM clinic_app.consent_consentacceptance acc
     WHERE acc.enrollment_id=NEW.enrollment_id AND acc.text_id=NEW.text_id
     AND NOT EXISTS (SELECT 1 FROM clinic_app.consent_consentrevocation r
       WHERE r.acceptance_id=acc.id)) THEN
     RAISE EXCEPTION 'active consent cannot be refused' USING ERRCODE='23514';
   END IF;
   NEW.refused_at := statement_timestamp();
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
CREATE OR REPLACE FUNCTION clinic_app.consent_audit_scope(
 record_id uuid, event_name text)
RETURNS TABLE(session_id uuid, organization_id uuid, clinic_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT s.session_id,s.organization_id,s.clinic_id FROM clinic_app.consent_session() s
 WHERE (event_name='consent.accepted' AND EXISTS (
 SELECT 1 FROM clinic_app.consent_consentacceptance a WHERE a.id=record_id
 AND a.patient_session_id=s.session_id AND a.enrollment_id=s.enrollment_id))
 OR (event_name='consent.refused' AND EXISTS (
 SELECT 1 FROM clinic_app.consent_refusalrecord r WHERE r.id=record_id
 AND r.patient_session_id=s.session_id AND r.enrollment_id=s.enrollment_id))
 OR (event_name='consent.revoked' AND EXISTS (
 SELECT 1 FROM clinic_app.consent_consentrevocation r
 JOIN clinic_app.consent_consentacceptance a ON a.id=r.acceptance_id
 WHERE r.id=record_id AND r.patient_session_id=s.session_id
 AND a.enrollment_id=s.enrollment_id))
$f$;
RESET ROLE;
CREATE OR REPLACE FUNCTION clinic_app.consent_audit(record_id uuid,
 event_name text, occurred_at_utc timestamptz, content_hash bytea)
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
 WHEN event_name='consent.refused' THEN 'consent.refusal'
 ELSE 'consent.revocation' END,
 record_id::text,occurred_at_utc,
 jsonb_build_object('clinic_id',s.clinic_id::text,'object_verb',
 CASE WHEN event_name='consent.accepted' THEN 'accepted'
 WHEN event_name='consent.refused' THEN 'refused' ELSE 'revoked' END),
 previous_hash,clinic_app.digest(content_hash||previous_hash,'sha256'))
 RETURNING seq INTO inserted_seq;
 RETURN inserted_seq;
END
$f$;
"""

# CREATE TRIGGER needs EXECUTE on the resolver-owned trigger functions;
# grant it to the migration role and revoke it again at the end, mirroring
# the original policy migration.
_sql += """
SET LOCAL ROLE clinic_resolver;
GRANT EXECUTE ON FUNCTION clinic_app.consent_immutable(),
 clinic_app.consent_guard() TO clinic_owner;
RESET ROLE;
"""

for table in (
    "noticeversion",
    "refusalrecord",
    "participantacknowledgment",
    "aiusedisclosure",
):
    _sql += f"""
ALTER TABLE clinic_app.consent_{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.consent_{table} FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.consent_{table} FROM PUBLIC,clinic_app;
GRANT SELECT,INSERT ON clinic_app.consent_{table} TO clinic_app;
CREATE POLICY setup_tenant ON clinic_app.consent_{table} TO clinic_owner
 USING (organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK (organization_id=
   NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE TRIGGER consent_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.consent_{table}
 FOR EACH ROW EXECUTE FUNCTION clinic_app.consent_immutable();
CREATE TRIGGER consent_binding BEFORE INSERT ON clinic_app.consent_{table}
 FOR EACH ROW EXECUTE FUNCTION clinic_app.consent_guard();
"""

_sql += """
CREATE POLICY consent_notice_read ON clinic_app.consent_noticeversion
 FOR SELECT TO clinic_app
 USING (clinic_app.questionnaire_staff(clinic_id,
   ARRAY['owner','clinic_admin','receptionist','physician'])
 OR clinic_id=(SELECT s.clinic_id FROM clinic_app.consent_session() s));
CREATE POLICY consent_notice_insert ON clinic_app.consent_noticeversion
 FOR INSERT TO clinic_app
 WITH CHECK (clinic_app.questionnaire_staff(clinic_id,ARRAY['owner','clinic_admin']));
CREATE POLICY consent_refusal_read ON clinic_app.consent_refusalrecord
 FOR SELECT TO clinic_app
 USING (clinic_app.questionnaire_staff(clinic_id,
   ARRAY['owner','clinic_admin','receptionist','physician'])
 OR enrollment_id=(SELECT s.enrollment_id FROM clinic_app.consent_session() s));
CREATE POLICY consent_refusal_insert ON clinic_app.consent_refusalrecord
 FOR INSERT TO clinic_app
 WITH CHECK (enrollment_id=
   (SELECT s.enrollment_id FROM clinic_app.consent_session() s));
CREATE POLICY consent_participant_read ON clinic_app.consent_participantacknowledgment
 FOR SELECT TO clinic_app
 USING (clinic_app.questionnaire_staff(clinic_id,
   ARRAY['owner','clinic_admin','receptionist','physician']));
CREATE POLICY consent_participant_insert
 ON clinic_app.consent_participantacknowledgment
 FOR INSERT TO clinic_app
 WITH CHECK (clinic_app.questionnaire_staff(clinic_id,ARRAY['physician']));
CREATE POLICY consent_ai_disclosure_read ON clinic_app.consent_aiusedisclosure
 FOR SELECT TO clinic_app
 USING (clinic_app.questionnaire_staff(clinic_id,
   ARRAY['owner','clinic_admin','receptionist','physician'])
 OR EXISTS (SELECT 1 FROM clinic_app.consent_session() s
   WHERE consent_aiusedisclosure.patient_id=s.patient_id
   AND consent_aiusedisclosure.clinic_id=s.clinic_id));
CREATE POLICY consent_ai_disclosure_insert ON clinic_app.consent_aiusedisclosure
 FOR INSERT TO clinic_app
 WITH CHECK (clinic_app.questionnaire_staff(clinic_id,ARRAY['physician']));
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.consent_immutable(),
 clinic_app.consent_guard() FROM clinic_owner;
RESET ROLE;
"""

SQL = _sql

# Reverse restores the exact trigger and audit bodies installed by
# 0002_consent_policy + 0003_protected_fields and drops everything added here.
_reverse_sql = ""
for table, label in (
    ("noticeversion", "notice"),
    ("refusalrecord", "refusal"),
    ("participantacknowledgment", "participant"),
    ("aiusedisclosure", "ai_disclosure"),
):
    _reverse_sql += f"""
DROP TRIGGER consent_immutable ON clinic_app.consent_{table};
DROP TRIGGER consent_binding ON clinic_app.consent_{table};
DROP POLICY consent_{label}_read ON clinic_app.consent_{table};
DROP POLICY consent_{label}_insert ON clinic_app.consent_{table};
DROP POLICY setup_tenant ON clinic_app.consent_{table};
REVOKE ALL ON clinic_app.consent_{table} FROM clinic_app;
"""
_reverse_sql += """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.consent_guard()
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
      OR NEW.text IS NULL OR pg_catalog.octet_length(NEW.text)>40000
      OR NEW.digest !~ '^[0-9a-f]{64}$'
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
CREATE OR REPLACE FUNCTION clinic_app.consent_audit_scope(
 record_id uuid, event_name text)
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
RESET ROLE;
CREATE OR REPLACE FUNCTION clinic_app.consent_audit(record_id uuid,
 event_name text, occurred_at_utc timestamptz, content_hash bytea)
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
REVOKE SELECT ON clinic_app.consent_noticeversion,
 clinic_app.consent_refusalrecord FROM clinic_resolver;
"""
REVERSE_SQL = _reverse_sql
