"""Transcript scope, consent fencing, complete coverage and append-only history."""

_sql = r"""
GRANT SELECT ON clinic_app.scribe_chunktranscription,
 clinic_app.scribe_transcriptversion,clinic_app.scribe_transcriptsegment
 TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.scribe_asr_allowed(
 requested_session uuid, acceptance uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(
  SELECT 1 FROM clinic_app.scribe_recordingsession s
  JOIN clinic_app.consent_consentacceptance a ON a.id=acceptance
  JOIN clinic_app.consent_consenttext t ON t.id=a.text_id
  WHERE s.id=requested_session AND NOT s.discarded
   AND clinic_app.scribe_assigned(s.id)
   AND clinic_app.scribe_consent(
    s.consent_id,s.clinic_id,s.enrollment_id,s.encounter_id)
   AND (a.organization_id,a.clinic_id,a.enrollment_id)
     =(s.organization_id,s.clinic_id,s.enrollment_id)
   AND t.purpose='ai_assistance'
   AND NOT EXISTS(SELECT 1 FROM clinic_app.consent_consentrevocation r
    WHERE r.acceptance_id=a.id)
   AND NOT EXISTS(SELECT 1 FROM clinic_app.consent_consenttext newer
    WHERE newer.clinic_id=t.clinic_id AND newer.purpose=t.purpose
     AND newer.version>t.version))
$f$;
CREATE FUNCTION clinic_app.scribe_transcript_coverage(requested_session uuid)
RETURNS jsonb LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT coalesce(jsonb_agg(jsonb_build_object(
  'epoch_id',ep.id,'track',track.key,'sequence',positions.sequence_number,
  'chunk_id',CASE WHEN c.state='accepted' THEN c.id ELSE NULL END)
  ORDER BY ep.number,track.key,positions.sequence_number),'[]'::jsonb)
 FROM clinic_app.scribe_captureepoch ep
 CROSS JOIN LATERAL jsonb_each(ep.high_water) track
 CROSS JOIN LATERAL generate_series(0,(track.value::text)::integer)
  AS positions(sequence_number)
 LEFT JOIN clinic_app.scribe_audiochunk c
  ON c.epoch_id=ep.id AND c.track=track.key AND c.seq=positions.sequence_number
 WHERE ep.session_id=requested_session
  AND clinic_app.scribe_assigned(requested_session)
$f$;
CREATE FUNCTION clinic_app.scribe_transcript_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s clinic_app.scribe_recordingsession; c clinic_app.scribe_audiochunk;
 v clinic_app.scribe_transcriptversion; prior clinic_app.scribe_transcriptversion;
 q clinic_app.scribe_chunktranscription; i clinic_app.ai_aiinvocation; item jsonb;
 coverage_limit integer:=67108864;
BEGIN
 IF TG_OP<>'INSERT' THEN
  RAISE EXCEPTION 'transcript history is retained' USING ERRCODE='23514';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
  'scribe-transcript:'||NEW.session_id,0));
 SELECT * INTO s FROM clinic_app.scribe_recordingsession WHERE id=NEW.session_id;
 IF s.id IS NULL OR s.discarded OR NEW.organization_id<>s.organization_id
  OR NOT clinic_app.scribe_assigned(s.id) THEN
  RAISE EXCEPTION 'transcript authority denied' USING ERRCODE='42501';
 END IF;
 IF TG_TABLE_NAME='scribe_chunktranscription' THEN
  SELECT * INTO c FROM clinic_app.scribe_audiochunk WHERE id=NEW.chunk_id;
  SELECT * INTO i FROM clinic_app.ai_aiinvocation WHERE id=NEW.invocation_id;
  IF c.id IS NULL OR i.id IS NULL OR c.session_id<>s.id
   OR c.organization_id<>s.organization_id OR c.state<>'accepted'
   OR octet_length(c.wrapped_key)=0
   OR NOT clinic_app.scribe_asr_allowed(s.id,NEW.consent_id)
   OR (i.organization_id,i.clinic_id,i.user_id,i.purpose)
     IS DISTINCT FROM (s.organization_id,s.clinic_id,s.physician_id,'scribe_asr'::text)
   OR i.status<>'reserved' OR i.operation_id IS NULL
   OR NOT EXISTS(SELECT 1 FROM clinic_app.ai_aiartifact a
    WHERE a.id=i.inputs_ref_id AND a.patient_enrollment_id=s.enrollment_id
     AND a.kind='input' AND a.method='transcribe')
  THEN RAISE EXCEPTION 'invalid transcription binding' USING ERRCODE='23514'; END IF;
  NEW.created_at:=statement_timestamp();
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='scribe_transcriptversion' THEN
  SELECT * INTO prior FROM clinic_app.scribe_transcriptversion
   WHERE session_id=s.id ORDER BY revision DESC LIMIT 1;
  IF NEW.revision<>coalesce(prior.revision,0)+1
   OR NEW.predecessor_id IS DISTINCT FROM prior.id
   OR NEW.created_by_id<>s.physician_id
   OR NEW.mutation NOT IN ('recognized','finalized','speaker_relabeled','corrected')
   OR (NEW.mutation IN ('speaker_relabeled','corrected') AND prior.state='final'
    AND NEW.coverage IS DISTINCT FROM prior.coverage)
   OR jsonb_typeof(NEW.coverage->'completed') IS DISTINCT FROM 'array'
   OR jsonb_typeof(NEW.coverage->'sources') IS DISTINCT FROM 'array'
   OR jsonb_array_length(NEW.coverage->'sources')>100000
   OR (NEW.mutation IN ('recognized','finalized')
    AND NEW.coverage->'recording_closed' IS DISTINCT FROM
     to_jsonb(s.state IN ('stopped','stopped_revoked')))
  THEN RAISE EXCEPTION 'invalid transcript successor' USING ERRCODE='23514'; END IF;
  IF NEW.mutation IN ('recognized','finalized')
   AND NEW.coverage->'slots' IS DISTINCT FROM
    clinic_app.scribe_transcript_coverage(s.id) THEN
   RAISE EXCEPTION 'transcript coverage changed'
    USING ERRCODE='23514',CONSTRAINT='scribe_transcript_coverage_changed';
  END IF;
  IF NEW.mutation='recognized' THEN coverage_limit:=66060288; END IF;
  IF octet_length(NEW.coverage::text)>1048576 OR
   coalesce((SELECT sum(octet_length(coverage::text))
    FROM clinic_app.scribe_transcriptversion WHERE session_id=s.id),0)
    +octet_length(NEW.coverage::text)>coverage_limit THEN
   RAISE EXCEPTION 'transcript capacity reached'
    USING ERRCODE='23514',CONSTRAINT='scribe_transcript_capacity';
  END IF;
  FOR item IN SELECT * FROM jsonb_array_elements(NEW.coverage->'completed') LOOP
   IF NOT EXISTS(
    SELECT 1 FROM clinic_app.scribe_chunktranscription r
    JOIN clinic_app.ai_aiinvocation invocation ON invocation.id=r.invocation_id
    WHERE r.session_id=s.id AND to_jsonb(r.chunk_id)=item
     AND invocation.status='succeeded')
   THEN RAISE EXCEPTION 'invalid transcript coverage' USING ERRCODE='23514'; END IF;
  END LOOP;
  IF NEW.mutation='recognized' THEN
   SELECT * INTO q FROM clinic_app.scribe_chunktranscription WHERE id=NEW.request_id;
   IF q.id IS NULL OR q.session_id<>s.id OR NEW.recognition_index IS NULL
    OR NEW.recognition_index NOT BETWEEN 0 AND 3
    OR NEW.state<>'partial' OR prior.state='final'
    OR NOT clinic_app.scribe_asr_allowed(s.id,q.consent_id)
    OR NOT EXISTS(SELECT 1 FROM clinic_app.ai_aiinvocation invocation
     WHERE invocation.id=q.invocation_id AND invocation.status='succeeded')
   THEN RAISE EXCEPTION 'invalid recognition successor' USING ERRCODE='23514'; END IF;
  ELSIF NEW.request_id IS NOT NULL OR NEW.recognition_index IS NOT NULL
   OR (NEW.mutation='finalized' AND (NEW.state<>'final' OR prior.state='final'))
   OR (NEW.mutation IN ('speaker_relabeled','corrected')
    AND (prior.id IS NULL OR NEW.state<>prior.state))
  THEN RAISE EXCEPTION 'invalid review successor' USING ERRCODE='23514'; END IF;
  IF NEW.state='final' THEN
   IF s.state NOT IN ('stopped','stopped_revoked') THEN
    RAISE EXCEPTION 'recording is open' USING ERRCODE='23514';
   END IF;
   IF NOT NEW.incomplete_acknowledged AND (
    jsonb_array_length(NEW.coverage->'slots')=0 OR EXISTS(
    SELECT 1 FROM jsonb_array_elements(NEW.coverage->'slots') slot
    WHERE slot->'chunk_id'='null'::jsonb
     OR NOT (NEW.coverage->'completed' @> jsonb_build_array(slot->'chunk_id'))))
   THEN RAISE EXCEPTION 'transcript coverage incomplete' USING ERRCODE='23514'; END IF;
  ELSIF NEW.incomplete_acknowledged THEN
   RAISE EXCEPTION 'invalid incomplete disposition' USING ERRCODE='23514';
  END IF;
  NEW.created_at:=statement_timestamp();
  NEW.write_transaction_id:=txid_current();
  RETURN NEW;
 END IF;
 SELECT * INTO v FROM clinic_app.scribe_transcriptversion WHERE id=NEW.version_id;
 SELECT * INTO c FROM clinic_app.scribe_audiochunk WHERE id=NEW.chunk_id;
 IF v.id IS NULL OR c.id IS NULL OR v.session_id<>s.id OR c.session_id<>s.id
  OR v.organization_id<>s.organization_id OR c.organization_id<>s.organization_id
  OR v.write_transaction_id<>txid_current()
  OR NOT (v.coverage->'sources' @> jsonb_build_array(NEW.source_id))
  OR NEW.start_ms>=NEW.end_ms OR NEW.end_ms>86400000
  OR NEW.speaker NOT IN ('clinician','patient','caregiver','unknown')
  OR NEW.confidence_milli>1000 OR octet_length(NEW.text) NOT BETWEEN 1 AND 262144
  OR octet_length(NEW.uncertainty) NOT BETWEEN 1 AND 262144
  OR NEW.language !~ '^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8}){0,3}$'
 THEN RAISE EXCEPTION 'invalid transcript segment' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END
$f$;
CREATE FUNCTION clinic_app.scribe_asr_consent_fence()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE org uuid; purpose text;
BEGIN
 IF TG_TABLE_NAME='consent_consenttext' THEN
  org:=NEW.organization_id; purpose:=NEW.purpose;
 ELSE
  SELECT a.organization_id,t.purpose INTO org,purpose
   FROM clinic_app.consent_consentacceptance a
   JOIN clinic_app.consent_consenttext t ON t.id=a.text_id
   WHERE a.id=NEW.acceptance_id;
 END IF;
 IF purpose IN ('ai_assistance','consultation_recording') THEN
  PERFORM pg_advisory_xact_lock(hashtextextended(
   'clinic-lock-v1:comms-subject:ai-organization:'||org,0));
 END IF;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.scribe_asr_allowed(uuid,uuid),
 clinic_app.scribe_transcript_coverage(uuid),clinic_app.scribe_transcript_guard(),
 clinic_app.scribe_asr_consent_fence() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.scribe_asr_allowed(uuid,uuid),
 clinic_app.scribe_transcript_coverage(uuid) TO clinic_app,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.scribe_transcript_guard(),
 clinic_app.scribe_asr_consent_fence() TO clinic_owner;
RESET ROLE;
"""

for _table in ("chunktranscription", "transcriptversion", "transcriptsegment"):
    _sql += f"""
ALTER TABLE clinic_app.scribe_{_table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.scribe_{_table} FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.scribe_{_table} FROM PUBLIC,clinic_app;
GRANT SELECT,INSERT ON clinic_app.scribe_{_table} TO clinic_app;
CREATE POLICY setup_tenant ON clinic_app.scribe_{_table} TO clinic_owner
 USING(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
 WITH CHECK(
  organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY transcript_read ON clinic_app.scribe_{_table} FOR SELECT TO clinic_app
 USING(clinic_app.scribe_assigned(session_id));
CREATE POLICY transcript_insert ON clinic_app.scribe_{_table} FOR INSERT TO clinic_app
 WITH CHECK(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
  AND clinic_app.scribe_assigned(session_id));
CREATE TRIGGER transcript_binding BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.scribe_{_table} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.scribe_transcript_guard();
CREATE TRIGGER transcript_no_truncate BEFORE TRUNCATE ON clinic_app.scribe_{_table}
 FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.scribe_transcript_guard();
"""

_sql += """
CREATE TRIGGER scribe_asr_publication_fence
 BEFORE INSERT ON clinic_app.consent_consenttext
 FOR EACH ROW EXECUTE FUNCTION clinic_app.scribe_asr_consent_fence();
CREATE TRIGGER scribe_asr_revocation_fence
 BEFORE INSERT ON clinic_app.consent_consentrevocation
 FOR EACH ROW EXECUTE FUNCTION clinic_app.scribe_asr_consent_fence();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.scribe_transcript_guard(),
 clinic_app.scribe_asr_consent_fence() FROM clinic_owner;
RESET ROLE;
"""

SQL = "BEGIN;\n" + _sql + "\nCOMMIT;"
