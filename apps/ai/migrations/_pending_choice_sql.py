"""Frozen, private original-scope decisions; none is callable by an app principal."""

# These names are permanent migration inputs. A live model/policy inventory
# test compares them to the marked models; it never imports this as its oracle.
RELATIONS = (
    "ai_actionproposal",
    "ai_agentrun",
    "ai_aiartifact",
    "ai_careproposal",
    "ai_extractioncandidate",
    "ai_extractionjob",
    "ai_frontdeskturn",
    "analytics_analystreview",
    "comms_suggestedreply",
    "scribe_aidraft",
    "scribe_draftactionproposal",
    "scribe_draftsectionsuggestion",
    "scribe_transcriptsegment",
    "scribe_transcriptversion",
)

SUBJECT = """
CREATE FUNCTION clinic_app.ai_pending_subject(
 org uuid, clinic uuid, enrolled uuid, visit uuid, pinned uuid,
 issued timestamptz, required boolean, scope_purpose text)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE patient uuid; current_text uuid;
BEGIN
 IF org IS DISTINCT FROM NULLIF(current_setting('app.current_tenant',true),'')::uuid
  OR clinic IS NULL OR enrolled IS NULL OR issued IS NULL THEN RETURN false; END IF;
 SELECT e.patient_id INTO patient FROM clinic_app.intake_patientclinicenrollment e
 WHERE (e.organization_id,e.clinic_id,e.id)=(org,clinic,enrolled);
 IF NOT FOUND THEN RETURN false; END IF;
 IF visit IS NOT NULL THEN
  IF NOT EXISTS(SELECT 1 FROM clinic_app.ehr_encounter v
      WHERE (v.organization_id,v.clinic_id,v.id,v.patient_id)=
            (org,clinic,visit,patient))
   OR EXISTS(SELECT 1 FROM clinic_app.consent_aiusedisclosure d
      WHERE (d.organization_id,d.clinic_id,d.encounter_id)=(org,clinic,visit)
       AND (d.refused OR NOT d.informed)) THEN RETURN false; END IF;
  IF required AND NOT EXISTS(SELECT 1 FROM clinic_app.consent_aiusedisclosure d
      WHERE (d.organization_id,d.clinic_id,d.encounter_id)=(org,clinic,visit)
       AND d.informed AND NOT d.refused) THEN RETURN false; END IF;
 END IF;
 SELECT t.id INTO current_text FROM clinic_app.consent_consenttext t
 WHERE (t.organization_id,t.clinic_id,t.purpose)=(org,clinic,scope_purpose)
 ORDER BY t.version DESC LIMIT 1;
 IF NOT FOUND THEN RETURN NOT required AND pinned IS NULL; END IF;
 RETURN EXISTS(SELECT 1 FROM clinic_app.consent_consentacceptance a
  WHERE (a.organization_id,a.clinic_id,a.enrollment_id,a.patient_id,a.text_id)=
        (org,clinic,enrolled,patient,current_text)
   AND (pinned IS NULL OR a.id=pinned) AND a.accepted_at<=issued
   AND NOT EXISTS(SELECT 1 FROM clinic_app.consent_consentrevocation r
     WHERE r.acceptance_id=a.id));
END $f$;
"""

CHOICE = """
CREATE FUNCTION clinic_app.ai_pending_choice(relation_name text, requested uuid)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE
 org uuid:=NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 clinic uuid; enrolled uuid; visit uuid; receipt uuid; recording uuid;
 source uuid; draft uuid; raw_input uuid; invocation uuid; job uuid; turn uuid;
 session_key uuid; chunk_key uuid; issued timestamptz; accepted boolean;
 feature text; operation_kind text; must_accept boolean:=false;
 input clinic_app.ai_aiartifact;
BEGIN
 IF org IS NULL OR requested IS NULL THEN RETURN false; END IF;
 CASE relation_name
 WHEN 'ai_aiartifact' THEN
  SELECT * INTO input FROM clinic_app.ai_aiartifact a
  WHERE a.organization_id=org AND a.id=requested;
  IF NOT FOUND THEN RETURN false; END IF;
  IF input.kind='input' THEN raw_input:=input.id;
  ELSE invocation:=input.invocation_id; END IF;
 WHEN 'ai_agentrun','ai_actionproposal' THEN
  SELECT r.invocation_id,p.capability INTO invocation,feature
  FROM clinic_app.ai_agentrun r
  JOIN clinic_app.ai_agentpolicyversion p ON p.id=r.policy_id
  WHERE r.organization_id=org AND r.id=CASE WHEN relation_name='ai_agentrun'
    THEN requested ELSE (SELECT a.run_id FROM clinic_app.ai_actionproposal a
         WHERE a.organization_id=org AND a.id=requested) END;
  IF NOT FOUND THEN RETURN false; END IF;
  -- Non-model workflow proposals have their own source/effect checks and
  -- do not acquire patient material merely by using the agent state machine.
  IF invocation IS NULL THEN
   RETURN feature NOT IN ('care_coordination','revenue_cycle');
  END IF;
 WHEN 'ai_careproposal' THEN
  SELECT c.state IN ('accepted','approved','rejected'),c.clinic_id,c.enrollment_id,
         p.encounter_id,c.created_at
  INTO accepted,clinic,enrolled,visit,issued FROM clinic_app.ai_careproposal c
  JOIN clinic_app.workflows_careplan p ON (p.organization_id,p.clinic_id,p.id)=
     (c.organization_id,c.clinic_id,c.plan_id) AND p.enrollment_id=c.enrollment_id
  WHERE c.organization_id=org AND c.id=requested;
  IF NOT FOUND THEN RETURN false; END IF;
  IF accepted THEN RETURN true; END IF;
 WHEN 'ai_extractionjob' THEN job:=requested;
 WHEN 'ai_extractioncandidate' THEN
  SELECT c.status IN ('confirmed','rejected'),c.job_id INTO accepted,job
  FROM clinic_app.ai_extractioncandidate c
  WHERE c.organization_id=org AND c.id=requested;
  IF NOT FOUND THEN RETURN false; END IF;
  IF accepted THEN RETURN true; END IF;
 WHEN 'scribe_aidraft' THEN draft:=requested;
 WHEN 'scribe_draftsectionsuggestion' THEN
  SELECT s.draft_id INTO draft FROM clinic_app.scribe_draftsectionsuggestion s
  WHERE s.organization_id=org AND s.id=requested;
 WHEN 'scribe_draftactionproposal' THEN
  SELECT s.draft_id INTO draft FROM clinic_app.scribe_draftactionproposal s
  WHERE s.organization_id=org AND s.id=requested;
 WHEN 'scribe_transcriptsegment' THEN
  SELECT s.session_id,s.chunk_id,s.text_reviewed OR v.state='final'
  INTO session_key,chunk_key,accepted
  FROM clinic_app.scribe_transcriptsegment s
  JOIN clinic_app.scribe_transcriptversion v ON v.id=s.version_id
  WHERE s.organization_id=org AND s.id=requested;
  IF NOT FOUND THEN RETURN false; END IF;
  IF accepted THEN RETURN true; END IF;
 WHEN 'scribe_transcriptversion' THEN
  SELECT v.session_id,v.state='final' OR v.mutation IN ('corrected','speaker_relabeled')
  INTO session_key,accepted FROM clinic_app.scribe_transcriptversion v
  WHERE v.organization_id=org AND v.id=requested;
  IF NOT FOUND THEN RETURN false; END IF;
  IF accepted THEN RETURN true; END IF;
 WHEN 'ai_frontdeskturn' THEN
  SELECT c.clinic_id,c.enrollment_id,t.created_at,t.reply_id IS NOT NULL
  INTO clinic,enrolled,issued,accepted FROM clinic_app.ai_frontdeskturn t
  JOIN clinic_app.ai_frontdeskthread f ON f.id=t.thread_id
  JOIN clinic_app.comms_conversation c ON c.id=f.conversation_id
  WHERE t.organization_id=org AND t.id=requested;
  IF NOT FOUND THEN RETURN false; END IF;
  IF accepted OR enrolled IS NULL THEN RETURN true; END IF;
 WHEN 'comms_suggestedreply' THEN
  SELECT c.clinic_id,c.enrollment_id,s.created_at,s.state<>'proposed'
  INTO clinic,enrolled,issued,accepted FROM clinic_app.comms_suggestedreply s
  JOIN clinic_app.comms_conversation c ON c.id=s.conversation_id
  WHERE s.organization_id=org AND s.id=requested;
  IF NOT FOUND THEN RETURN false; END IF;
  IF accepted OR enrolled IS NULL THEN RETURN true; END IF;
 WHEN 'analytics_analystreview' THEN
  -- A human decision about an aggregate descriptor. No patient source.
  RETURN EXISTS(SELECT 1 FROM clinic_app.analytics_analystreview a
                WHERE a.organization_id=org AND a.id=requested);
 ELSE RETURN false;
 END CASE;
 IF invocation IS NOT NULL THEN
  SELECT i.inputs_ref_id INTO raw_input FROM clinic_app.ai_aiinvocation i
  WHERE i.organization_id=org AND i.id=invocation;
  IF NOT FOUND THEN RETURN false; END IF;
 END IF;
 IF raw_input IS NOT NULL THEN
  SELECT * INTO input FROM clinic_app.ai_aiartifact a
  WHERE a.organization_id=org AND a.id=raw_input AND a.kind='input';
  IF NOT FOUND THEN RETURN false; END IF;
  clinic:=input.clinic_id; enrolled:=input.patient_enrollment_id;
  issued:=input.created_at;
  IF invocation IS NULL THEN
   SELECT i.id INTO invocation FROM clinic_app.ai_aiinvocation i
    WHERE i.organization_id=org AND i.inputs_ref_id=raw_input LIMIT 1;
  END IF;
  CASE input.purpose
  WHEN 'brief','decision_support' THEN
   visit:=input.source_ref; receipt:=input.source_acceptance; must_accept:=true;
   IF visit IS NULL OR receipt IS NULL THEN RETURN false; END IF;
  WHEN 'revenue_cycle' THEN
   SELECT g.encounter_id,m.enrollment_id INTO visit,enrolled
    FROM clinic_app.insurance_guide g JOIN clinic_app.insurance_insurancemembership m
    ON (m.organization_id,m.clinic_id,m.id)=
       (g.organization_id,g.clinic_id,g.membership_id)
    WHERE g.organization_id=org AND g.clinic_id=clinic AND g.id=input.source_ref;
   IF NOT FOUND THEN RETURN false; END IF;
  WHEN 'care_coordination' THEN
   SELECT p.encounter_id INTO visit FROM clinic_app.workflows_careplan p
    WHERE (p.organization_id,p.clinic_id,p.enrollment_id,p.id)=
          (org,clinic,enrolled,input.source_ref);
   IF NOT FOUND THEN RETURN false; END IF;
  WHEN 'extraction' THEN
   SELECT j.id INTO job FROM clinic_app.ai_extractionjob j
    WHERE j.organization_id=org AND j.invocation_id=invocation;
   IF job IS NULL AND input.kind='input' AND relation_name='ai_aiartifact'
      AND requested=raw_input THEN
    -- Input is the supplied document, not an inferred candidate; the job
    -- and source relation are installed after gateway admission in one txn.
    RETURN clinic_app.ai_pending_subject(org,clinic,enrolled,NULL,NULL,
                                        issued,false,'ai_assistance');
   END IF;
   IF job IS NULL THEN RETURN false; END IF;
  WHEN 'scribe_asr' THEN
   SELECT c.session_id,c.chunk_id,c.consent_id INTO session_key,chunk_key,receipt
    FROM clinic_app.scribe_chunktranscription c
    WHERE c.organization_id=org AND c.invocation_id=invocation;
   IF NOT FOUND THEN
    RETURN relation_name='ai_aiartifact' AND requested=raw_input;
   END IF;
  WHEN 'scribe_draft' THEN
   SELECT d.id INTO draft FROM clinic_app.scribe_aidraft d
    WHERE d.organization_id=org AND d.invocation_id=invocation;
   IF NOT FOUND THEN
    RETURN relation_name='ai_aiartifact' AND requested=raw_input;
   END IF;
  WHEN 'front_desk' THEN IF enrolled IS NULL THEN RETURN true; END IF;
  WHEN 'analyst' THEN RETURN enrolled IS NULL;
  ELSE RETURN false;
  END CASE;
 END IF;
 IF job IS NOT NULL THEN
  SELECT j.clinic_id,j.patient_enrollment_id,j.created_at,a.encounter_id,
    j.attachment_id IS NULL OR a.encounter_id IS NOT NULL
  INTO clinic,enrolled,issued,visit,accepted
  FROM clinic_app.ai_extractionjob j LEFT JOIN clinic_app.ehr_clinicalattachment a
    ON (a.organization_id,a.clinic_id,a.id)=
       (j.organization_id,j.clinic_id,j.attachment_id)
  WHERE j.organization_id=org AND j.id=job;
  IF NOT FOUND OR NOT accepted THEN RETURN false; END IF;
 END IF;
 IF draft IS NOT NULL THEN
  SELECT d.clinic_id,s.enrollment_id,d.encounter_id,d.consent_id,d.created_at
  INTO clinic,enrolled,visit,receipt,issued FROM clinic_app.scribe_aidraft d
   JOIN clinic_app.scribe_transcriptversion v ON v.id=d.transcript_id
   JOIN clinic_app.scribe_recordingsession s ON s.id=v.session_id
   WHERE d.organization_id=org AND d.id=draft;
  IF NOT FOUND THEN RETURN false; END IF;
  must_accept:=true;
 END IF;
 IF session_key IS NOT NULL THEN
  SELECT s.clinic_id,s.enrollment_id,s.encounter_id,s.consent_id,s.created_at
   INTO clinic,enrolled,visit,recording,issued FROM clinic_app.scribe_recordingsession s
   WHERE s.organization_id=org AND s.id=session_key;
  IF NOT FOUND OR NOT clinic_app.ai_pending_subject(org,clinic,enrolled,visit,recording,
                                 issued,true,'consultation_recording') THEN
   RETURN false;
  END IF;
  IF chunk_key IS NOT NULL THEN
   SELECT c.consent_id,c.created_at INTO receipt,issued
    FROM clinic_app.scribe_chunktranscription c
    WHERE c.organization_id=org AND c.session_id=session_key AND c.chunk_id=chunk_key;
   IF NOT FOUND THEN RETURN false; END IF;
  ELSE
   SELECT v.created_at INTO issued FROM clinic_app.scribe_transcriptversion v
    WHERE v.organization_id=org AND v.id=requested;
  END IF;
  must_accept:=true;
 END IF;
 RETURN clinic_app.ai_pending_subject(org,clinic,enrolled,visit,receipt,
                                    issued,must_accept,'ai_assistance');
END $f$;
"""
