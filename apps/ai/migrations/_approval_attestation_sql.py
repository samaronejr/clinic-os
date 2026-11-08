"""Counted votes need SQL-verified step-up; approval and dispatch need quorum.

PostgreSQL cannot verify the HMAC attestation, whose key stays in the secret
store, so it verifies the fact behind it instead: the approver's own session
carries a persisted, resolver-written TOTP or passkey verification from the last
five minutes. Only ``agent_record_approval`` can insert a vote, and it records
that verification instant. ``agent_approval_current`` counts only such votes,
and the guard requires the declared quorum of current votes before a proposal
becomes approved or executing, before an execution is claimed and before its
send starts. Released bodies are edited at fixed anchors.
"""

from apps.ai.migrations._care_sql import _edit, _resolver
from apps.ai.migrations._revenue_bound_sql import _APPROVAL as PRIOR_APPROVAL
from apps.ai.migrations._revenue_bound_sql import _GUARD as PRIOR_GUARD

_RECORD_SIGNATURE = (
    "clinic_app.agent_record_approval(uuid,uuid,text,jsonb,uuid,jsonb,text,bytea)"
)

# The only vote writer. The caller's evidence facts must name this actor,
# clinic, digest and evidence id; the stored instant is the session's own.
_RECORD = r"""
CREATE FUNCTION clinic_app.agent_record_approval(requested uuid,approval_id uuid,
 approval_digest text,snapshot jsonb,evidence_ref uuid,evidence jsonb,
 attestation text,session_digest bytea)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE
 actor uuid:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 organization uuid; clinic uuid; verified timestamptz;
 facts jsonb:=evidence->'facts';
 instant numeric:=extract(epoch FROM statement_timestamp());
BEGIN
 SELECT a.organization_id,a.clinic_id INTO organization,clinic
 FROM clinic_app.ai_actionproposal a WHERE a.id=requested
 AND a.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 SELECT s.verified_at INTO verified FROM clinic_app.identity_sessionrecord s
 WHERE s.session_key_hash=session_digest AND s.user_id=actor
 AND (clinic_app.identity_session_verified(session_digest)
  OR clinic_app.identity_passkey_session_verified(session_digest,
   s.verification_passkey_id,300));
 IF organization IS NULL OR verified IS NULL
  OR (CASE WHEN clinic_app.ai_revenue_ancestry(clinic,'proposal',requested)
   THEN clinic_app.ai_revenue_row(clinic,'proposal',requested)
   ELSE clinic_app.agent_staff(clinic) END) IS NOT TRUE
  OR evidence IS DISTINCT FROM jsonb_build_object(
   'facts',facts,'signature',evidence->'signature')
  OR coalesce(evidence->>'signature','') !~ '^[0-9a-f]{64}$'
  OR facts IS DISTINCT FROM jsonb_build_object('id',evidence_ref::text,
   'clinic_id',clinic::text,'user_id',actor::text,'digest',approval_digest,
   'verified_at',facts->'verified_at','key_id','agent-attestation-v1')
  OR coalesce(facts->>'verified_at','') !~ '^[0-9]{1,12}$' THEN
  RETURN false;
 END IF;
 IF (facts->>'verified_at')::numeric NOT BETWEEN instant-300 AND instant THEN
  RETURN false;
 END IF;
 INSERT INTO clinic_app.ai_approval(id,created_at,organization_id,clinic_id,
  proposal_id,approver_id,digest,authority_snapshot,step_up_evidence_ref,
  step_up_evidence,key_id,attestation,step_up_verified_at)
 VALUES(approval_id,statement_timestamp(),organization,clinic,requested,actor,
  approval_digest,snapshot,evidence_ref,evidence,'agent-attestation-v1',
  attestation,verified);
 RETURN true;
END $f$;
"""
_RECORD_GRANTS = f"""
REVOKE ALL ON FUNCTION {_RECORD_SIGNATURE} FROM PUBLIC;
GRANT EXECUTE ON FUNCTION {_RECORD_SIGNATURE} TO clinic_app;
"""

# A vote counts only when the step-up instant it recorded precedes it closely.
_APPROVAL = _edit(
    PRIOR_APPROVAL,
    " WHERE a.id=approval_id AND a.proposal_id=requested\n",
    " WHERE a.id=approval_id AND a.proposal_id=requested\n"
    " AND a.step_up_verified_at IS NOT NULL\n"
    " AND a.step_up_verified_at<=a.created_at\n"
    " AND a.step_up_verified_at>a.created_at-interval '5 minutes'\n",
)

# Private to the guard's owner: the declared quorum of current, digest-bound
# votes, with no unverified or stale vote. Zero votes suffice only for a bounded
# policy executed by its machine principal.
_VOTES = r"""
CREATE FUNCTION clinic_app.agent_votes_current(requested uuid)
RETURNS boolean LANGUAGE sql VOLATILE
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT p.quorum IS NOT NULL
 AND (p.quorum>0 OR (v.autonomy_ceiling IN ('A2','A3') AND v.principal_id IS NOT NULL))
 AND (SELECT count(*) FROM clinic_app.ai_approval a WHERE a.proposal_id=p.id)
  >=p.quorum
 AND NOT EXISTS(SELECT 1 FROM clinic_app.ai_approval a WHERE a.proposal_id=p.id
  AND (a.digest IS DISTINCT FROM p.digest
   OR NOT clinic_app.agent_approval_current(p.id,a.id)))
 FROM clinic_app.ai_actionproposal p
 JOIN clinic_app.ai_agentrun r ON r.id=p.run_id
 JOIN clinic_app.ai_agentpolicyversion v ON v.id=r.policy_id
 WHERE p.id=requested
$f$;
REVOKE ALL ON FUNCTION clinic_app.agent_votes_current(uuid) FROM PUBLIC;
"""

_QUORUM_REFUSED = (
    "    RAISE EXCEPTION 'agent approval quorum refused' USING ERRCODE='23514';\n"
)
_TRANSITION = (
    "  IF TG_TABLE_NAME IN ('ai_actionproposal','ai_compensation','ai_actionexecution')"
    " THEN\n"
    "   IF OLD.state<>NEW.state\n"
    "    AND NOT clinic_app.agent_transition(OLD.state,NEW.state) THEN\n"
    "    RAISE EXCEPTION 'agent transition refused' USING ERRCODE='23514';\n"
    "   END IF;\n"
    "  END IF;\n"
)
_FENCE = (
    "    RAISE EXCEPTION 'agent fence refused' USING ERRCODE='23514';\n   END IF;\n"
)
_EXECUTION = (
    "   OR NEW.deadline<=NEW.intent_at OR NEW.send_started_at IS NOT NULL THEN\n"
    "   RAISE EXCEPTION 'agent execution refused' USING ERRCODE='23514';\n"
    "  END IF;\n"
)
_GUARD = PRIOR_GUARD
# Approval and execution need the declared quorum of current votes.
_GUARD = _edit(
    _GUARD,
    _TRANSITION,
    _TRANSITION + "  IF TG_TABLE_NAME='ai_actionproposal' THEN\n"
    "   IF OLD.state<>NEW.state AND NEW.state IN ('approved','executing')\n"
    "    AND clinic_app.agent_votes_current(NEW.id) IS NOT TRUE THEN\n"
    + _QUORUM_REFUSED
    + "   END IF;\n  END IF;\n",
)
# So does the send boundary, under the same retained mutation lock.
_GUARD = _edit(
    _GUARD,
    _FENCE,
    _FENCE + "   IF OLD.send_started_at IS NULL AND NEW.send_started_at IS NOT NULL\n"
    "    AND clinic_app.agent_votes_current(NEW.proposal_id) IS NOT TRUE THEN\n"
    + _QUORUM_REFUSED
    + "   END IF;\n",
)
# A new proposal declares the quorum its digest-bound payload requires.
_GUARD = _edit(
    _GUARD,
    "  IF NEW.state<>'proposal' OR NEW.digest !~ '^[0-9a-f]{64}$' THEN\n",
    "  IF NEW.state<>'proposal' OR NEW.digest !~ '^[0-9a-f]{64}$'\n"
    "   OR NEW.quorum IS NULL OR NEW.quorum>2 THEN\n",
)
# A vote carries the verified step-up instant its writer recorded.
_GUARD = _edit(
    _GUARD,
    "    AND NOT clinic_app.has_permission('automation.finance',NEW.clinic_id,NULL))\n"
    "   THEN\n",
    "    AND NOT clinic_app.has_permission('automation.finance',NEW.clinic_id,NULL))\n"
    "   OR NEW.step_up_verified_at IS NULL\n"
    "   OR NEW.step_up_verified_at>NEW.created_at\n"
    "   OR NEW.step_up_verified_at<=NEW.created_at-interval '5 minutes'\n"
    "   THEN\n",
)
# Claiming an execution needs the same current votes as the transition.
_GUARD = _edit(
    _GUARD,
    _EXECUTION,
    _EXECUTION + "  IF clinic_app.agent_votes_current(p.id) IS NOT TRUE THEN\n"
    "   RAISE EXCEPTION 'agent approval quorum refused' USING ERRCODE='23514';\n"
    "  END IF;\n",
)

SQL = (
    "GRANT INSERT ON clinic_app.ai_approval TO clinic_resolver;\n"
    "REVOKE INSERT ON clinic_app.ai_approval FROM clinic_app;\n"
    + _resolver((_APPROVAL, _RECORD, _RECORD_GRANTS))
    + _VOTES
    + _GUARD
    + "\n"
)
REVERSE_SQL = (
    PRIOR_GUARD
    + "\nDROP FUNCTION clinic_app.agent_votes_current(uuid);\n"
    + _resolver((PRIOR_APPROVAL, f"DROP FUNCTION {_RECORD_SIGNATURE};"))
    + "GRANT INSERT ON clinic_app.ai_approval TO clinic_app;\n"
    "REVOKE INSERT ON clinic_app.ai_approval FROM clinic_resolver;\n"
)
