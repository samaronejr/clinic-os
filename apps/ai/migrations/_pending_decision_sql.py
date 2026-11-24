"""Original-scope consent at a native decision, with no new SQL entry point.

Readers take a shared transaction lock without waiting. A concurrent choice
change wins or waits for an already-admitted decision to finish. The separate
visibility statement in the VOLATILE writer sees a choice committed before
admission. Never fold the lock and the check into a single SELECT snapshot.
"""

from apps.ai.migrations import (
    _approval_attestation_sql,
    _manual_pdf_unit_sql,
    _retained_effect_sql,
    _retained_proposal_sql,
    _rollout_evaluation_sql,
)
from apps.ai.migrations._care_sql import _edit, _resolver
from apps.ai.migrations._front_desk_authority_sql import _function
from apps.consent.migrations import _taxonomy_sql

_CHECK = """
 IF pg_catalog.pg_try_advisory_xact_lock_shared(pg_catalog.hashtextextended(
    'ai-pending-decision:'||__CLINIC__::text,0)) IS NOT TRUE THEN
  __DENY__
 END IF;
 IF clinic_app.enterprise_staff_context() IS NOT TRUE
 OR clinic_app.ai_pending_visible('__RELATION__',__ROW__) IS NOT TRUE THEN
  __DENY__
 END IF;
"""
_DENY = "RAISE EXCEPTION 'pending decision not available' USING ERRCODE='42501';"


def _check(clinic: str, relation: str, row: str, deny: str) -> str:
    return (
        _CHECK.replace("__CLINIC__", clinic)
        .replace("__RELATION__", relation)
        .replace("__ROW__", row)
        .replace("__DENY__", deny)
    )


_VOTE_PRIOR = _function(_approval_attestation_sql.SQL, "agent_record_approval")
_VOTE_ANCHOR = " INSERT INTO clinic_app.ai_approval("
_VOTE = _edit(
    _VOTE_PRIOR,
    _VOTE_ANCHOR,
    _check("clinic", "ai_actionproposal", "requested", "RETURN false;") + _VOTE_ANCHOR,
)

_AP_PRIOR = _function(_retained_proposal_sql.SQL, "ai_retained_proposal_guard")
_AP_ANCHOR = " SELECT * INTO r FROM clinic_app.ai_agentrun WHERE id=NEW.run_id;"
_AP = _edit(
    _AP_PRIOR,
    _AP_ANCHOR,
    " IF TG_OP='UPDATE' AND OLD.state IS DISTINCT FROM NEW.state\n"
    " AND OLD.state IN ('proposal','validated','approved')\n"
    " AND NEW.state IN ('approved','rejected','executing') THEN\n"
    + _check("OLD.clinic_id", "ai_actionproposal", "OLD.id", _DENY)
    + " END IF;\n"
    + _AP_ANCHOR,
)

_EXEC_PRIOR = _function(_rollout_evaluation_sql.SQL, "ai_rollout_guard")
_EXEC_ANCHOR = "  SELECT clinic_app.ai_rollout_agent_allowed(p.run_id) INTO valid"
_EXEC = _edit(
    _EXEC_PRIOR,
    _EXEC_ANCHOR,
    _check("NEW.clinic_id", "ai_actionproposal", "NEW.proposal_id", _DENY)
    + _EXEC_ANCHOR,
)

_CARE_PRIOR = _function(_retained_effect_sql.SQL, "ai_care_guard")
_CARE_ANCHOR = " NEW.decided_at:=statement_timestamp();"
_CARE = _edit(
    _CARE_PRIOR,
    _CARE_ANCHOR,
    " IF NEW.state IN ('accepted','approved','rejected') THEN\n"
    + _check("OLD.clinic_id", "ai_careproposal", "OLD.id", _DENY)
    + " END IF;\n"
    + _CARE_ANCHOR,
)

_REVIEW_PRIOR = _function(_manual_pdf_unit_sql.SQL, "ai_review_extraction")
_EXTRACT_ANCHOR = " IF decision='confirmed' THEN"
_REVIEW = _edit(
    _REVIEW_PRIOR,
    _EXTRACT_ANCHOR,
    _check("j.clinic_id", "ai_extractioncandidate", "c.id", _DENY) + _EXTRACT_ANCHOR,
)

_DRAFT_PRIOR = _function(_retained_effect_sql.SQL, "scribe_draft_guard")
_DRAFT_ANCHOR = "   IF NEW.action<>'rejected'"
_DRAFT = _edit(
    _DRAFT_PRIOR,
    "   IF NEW.action<>'rejected'\n   AND NOT clinic_app.ai_effect_current",
    _check("e.clinic_id", "scribe_draftsectionsuggestion", "NEW.suggestion_id", _DENY)
    + _DRAFT_ANCHOR
    + "\n   AND NOT clinic_app.ai_effect_current",
)

_CHOICE_PRIOR = _function(_taxonomy_sql.SQL, "consent_guard")
_LOCK = (
    "   PERFORM pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended("
    "'ai-pending-decision:'||NEW.clinic_id::text,0));\n"
)
_PURPOSES = "('ai_assistance','consultation_recording')"
_TEXT_LOCK = (
    "   PERFORM pg_advisory_xact_lock(hashtextextended(\n"
    "     'consent:'||NEW.clinic_id||':'||NEW.purpose,0));"
)
_TERMS_LOCK = (
    "   PERFORM pg_advisory_xact_lock(hashtextextended(\n"
    "     'consent:'||t.clinic_id||':'||t.purpose,0));"
)
_CHOICE_TEXT = _edit(
    _CHOICE_PRIOR,
    _TEXT_LOCK,
    "   IF NEW.purpose IN "
    + _PURPOSES
    + " THEN\n"
    + _LOCK
    + "   END IF;\n"
    + _TEXT_LOCK,
)
_CHOICE_TERMS = _edit(
    _CHOICE_TEXT,
    _TERMS_LOCK,
    "   IF t.purpose IN "
    + _PURPOSES
    + " THEN\n"
    + _LOCK
    + "   END IF;\n"
    + _TERMS_LOCK,
    2,
)
_DISCLOSURE_OK = (
    "   THEN RAISE EXCEPTION 'invalid AI-use disclosure' USING ERRCODE='23514';"
    " END IF;\n"
)
_CHOICE_VISIT = _edit(_CHOICE_TERMS, _DISCLOSURE_OK, _DISCLOSURE_OK + _LOCK)
_REVOKE_AT = "   NEW.revoked_at := statement_timestamp();"
_REVOKE_LOCK = """   IF EXISTS(SELECT 1 FROM clinic_app.consent_consenttext AS terms
    WHERE terms.id=a.text_id
      AND terms.purpose IN ('ai_assistance','consultation_recording')) THEN
__LOCK__   END IF;
"""
_CHOICE = _edit(
    _CHOICE_VISIT, _REVOKE_AT, _REVOKE_LOCK.replace("__LOCK__", _LOCK) + _REVOKE_AT
)

# CREATE OR REPLACE preserves the resolver owners, function identities, grants,
# and trigger attachments. No model-state or table/column privilege changes.
SQL = _resolver((_CHOICE, _VOTE, _AP, _EXEC, _CARE, _REVIEW, _DRAFT))
REVERSE_SQL = _resolver(
    (
        _CHOICE_PRIOR,
        _VOTE_PRIOR,
        _AP_PRIOR,
        _EXEC_PRIOR,
        _CARE_PRIOR,
        _REVIEW_PRIOR,
        _DRAFT_PRIOR,
    )
)
