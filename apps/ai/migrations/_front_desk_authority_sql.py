"""Corrective task 54 authority: offer receipts, human ownership and contacts.

The deployed definitions are the starting point, never edited in place.
Pending rows retain their provenance; a handoff's permanent execution cutoff
excludes everything already received, including after staff resolve the handoff.
"""

from apps.ai.migrations._front_desk_sql import SQL as PRIOR
from apps.intake.migrations._delegate_session_sql import SQL as PATIENT_PRIOR


def _function(source: str, name: str) -> str:
    head = f"CREATE FUNCTION clinic_app.{name}("
    if head not in source:
        head = f"CREATE OR REPLACE FUNCTION clinic_app.{name}("
    start = source.index(head)
    end = source.index("$f$;", start) + len("$f$;")
    return source[start:end].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


_ELIGIBLE = """h.state<>'handoff_pending'
  AND (h.automation_after IS NULL OR u.created_at>h.automation_after)"""
_CONTACT = """EXISTS (
 SELECT 1 FROM clinic_app.intake_patientcontact pc
 JOIN clinic_app.intake_patientclinicenrollment e ON e.patient_id=pc.patient_id
  AND e.organization_id=pc.organization_id
 WHERE pc.id=h.verified_contact_id AND pc.organization_id=h.organization_id
  AND pc.channel=c.channel AND e.id=c.enrollment_id AND e.clinic_id=c.clinic_id
  AND h.verified_enrollment_id=e.id
  AND pc.destination_version=h.verified_contact_version
  AND pc.verified_version=pc.destination_version AND pc.verified_at IS NOT NULL)"""
_PUBLISHED = """EXISTS (
 SELECT 1 FROM clinic_app.ai_frontdeskturn offered
 JOIN clinic_app.comms_message published ON published.id=offered.reply_id
 WHERE offered.id=h.offered_turn_id AND offered.thread_id=h.id
  AND offered.state='done' AND offered.outcome='offered'
  AND published.provider_state='sent' AND published.created_at<m.created_at)"""

_enqueue = (
    _function(PRIOR, "ai_front_desk_enqueue")
    .replace(
        "WHERE h.conversation_id=c.id AND h.state<>'handoff_pending';",
        "WHERE h.conversation_id=c.id AND h.state<>'handoff_pending' FOR UPDATE;",
    )
    .replace("policy_id,created_at)", "policy_id,created_at,received_offer_id)")
    .replace(
        "'','',policy,statement_timestamp()",
        "'','',policy,statement_timestamp(),CASE WHEN "
        f"h.offered_until>statement_timestamp() AND {_PUBLISHED} "
        "THEN h.offered_turn_id END",
    )
)
_pending = _function(PRIOR, "ai_front_desk_pending").replace(
    "WHERE u.state='pending'", f"WHERE {_ELIGIBLE} AND u.state='pending'"
)
_turn = (
    _function(PRIOR, "ai_front_desk_turn")
    .replace(
        "tools text[],budget_left integer)",
        "tools text[],budget_left integer,offer_received boolean)",
    )
    .replace(
        "(h.verified_until IS NOT NULL AND h.verified_until>statement_timestamp())",
        "(h.verified_until IS NOT NULL AND h.verified_until>statement_timestamp()"
        f" AND {_CONTACT})",
    )
    .replace(
        "WHEN h.verified_until>statement_timestamp() THEN h.session_id END",
        f"WHEN h.verified_until>statement_timestamp() AND {_CONTACT} "
        "THEN h.session_id END",
    )
    .replace(
        "::integer\n FROM clinic_app.ai_frontdeskturn u",
        "::integer,\n"
        "  (u.received_offer_id=h.offered_turn_id AND "
        f"h.offered_until>statement_timestamp() AND {_PUBLISHED})\n"
        " FROM clinic_app.ai_frontdeskturn u",
    )
    .replace(
        "WHERE u.id=requested_turn AND u.state='pending'",
        f"WHERE {_ELIGIBLE} AND u.id=requested_turn AND u.state='pending'",
    )
)
_articles = _function(PRIOR, "ai_front_desk_articles").replace(
    "WHERE u.id=requested_turn AND u.state='pending'",
    f"WHERE {_ELIGIBLE} AND u.id=requested_turn AND u.state='pending'",
)
_verify = (
    _function(PRIOR, "ai_front_desk_verify")
    .replace(
        "grant_id uuid; minted uuid; matched boolean;",
        "grant_id uuid; minted uuid; matched boolean;"
        " contact clinic_app.intake_patientcontact;",
    )
    .replace(
        "IF u.id IS NULL OR h.id IS NULL OR c.channel='portal'",
        f"IF u.id IS NULL OR h.id IS NULL OR NOT ({_ELIGIBLE}) OR c.channel='portal'",
    )
    .replace(
        "matched:=e.id IS NOT NULL",
        """PERFORM pg_advisory_xact_lock(hashtextextended(
  'clinic-lock-v1:comms-subject:intake.patient_channel_preference:'
   ||e.patient_id::text||':'||c.channel,0));
 SELECT * INTO contact FROM clinic_app.intake_patientcontact pc
  WHERE pc.organization_id=c.organization_id AND pc.patient_id=e.patient_id
   AND pc.channel=c.channel AND pc.verified_version=pc.destination_version
   AND pc.verified_at IS NOT NULL
   AND clinic_app.protected_decrypt(kek,'intake.patientcontact.destination',
    pc.destination)=clinic_app.protected_decrypt(kek,
    'comms.conversation.contact_address',c.contact_address);
 matched:=contact.id IS NOT NULL AND e.id IS NOT NULL""",
    )
    .replace(
        "UPDATE clinic_app.ai_frontdeskthread SET verified_enrollment_id=e.id,",
        "UPDATE clinic_app.ai_frontdeskthread SET verified_enrollment_id=e.id,\n"
        "  verified_contact_id=contact.id,"
        "verified_contact_version=contact.destination_version,",
    )
)
_state = (
    _function(PRIOR, "ai_front_desk_state")
    .replace(
        "IF u.id IS NULL OR h.id IS NULL OR NOT (",
        f"IF u.id IS NULL OR h.id IS NULL OR NOT ({_ELIGIBLE}) OR NOT (",
    )
    .replace(
        "(h.verified_until IS NULL OR h.verified_until<=statement_timestamp()))",
        "(h.verified_until IS NULL OR h.verified_until<=statement_timestamp()"
        f" OR NOT ({_CONTACT})))",
    )
    .replace(
        "WHEN change='clear' THEN NULL",
        "WHEN change IN ('clear','handoff') THEN NULL",
    )
    .replace(
        "state=CASE WHEN change='handoff'",
        """offered_turn_id=CASE WHEN change='offer' THEN u.id
   WHEN change IN ('clear','handoff') THEN NULL ELSE offered_turn_id END,
  automation_after=CASE WHEN change='handoff' THEN statement_timestamp()
   ELSE automation_after END,
  handoff_turn_id=CASE WHEN change='handoff' THEN u.id ELSE handoff_turn_id END,
  state=CASE WHEN change='handoff'""",
    )
)
_finish = (
    _function(PRIOR, "ai_front_desk_finish")
    .replace(
        "WHERE x.id=u.thread_id;",
        "WHERE x.id=u.thread_id FOR UPDATE;",
    )
    .replace(
        "IF u.id IS NULL OR h.id IS NULL OR NOT (",
        f"""IF u.id IS NULL OR h.id IS NULL OR NOT (
   ({_ELIGIBLE}) OR
   (h.state='handoff_pending' AND
    h.handoff_turn_id=u.id
    AND turn_outcome IN ('handoff','not_verified') AND turn_tool=''))
  OR NOT (""",
    )
    .replace(
        "OR (turn_tool<>'' AND NOT EXISTS",
        f"""OR (c.channel<>'portal'
   AND turn_intent IN ('appointments','booking','reschedule','accept','decline')
   AND turn_outcome IN ('answered','offered','booked','waitlist')
   AND NOT (h.verified_until>statement_timestamp() AND ({_CONTACT})))
  OR (turn_tool='scheduling.book_approved' AND NOT (
   u.received_offer_id IS NOT NULL
   AND u.received_offer_id=h.offered_turn_id
   AND h.offered_until>statement_timestamp()
   AND (c.channel='portal' OR ({_CONTACT}))))
  OR (turn_tool<>'' AND NOT EXISTS""",
    )
    .replace(
        "PERFORM set_config('app.ai_front_desk_bridge','',true);",
        """IF turn_intent='decline' OR (turn_intent='accept'
   AND u.received_offer_id IS NOT NULL AND u.received_offer_id=h.offered_turn_id)
 THEN
  PERFORM set_config('app.ai_front_desk_bridge',h.id::text,true);
  UPDATE clinic_app.ai_frontdeskthread SET offered_appointment_id=NULL,
   offered_practitioner_id=NULL,offered_start=NULL,offered_until=NULL,
   offered_turn_id=NULL,revision=revision+1,updated_at=statement_timestamp()
   WHERE id=h.id;
 END IF;
 PERFORM set_config('app.ai_front_desk_bridge','',true);""",
    )
)
_authority = _function(PATIENT_PRIOR, "patient_authority").replace(
    "AND (requested_operation IS NULL OR requested_operation=ANY(scope.operations))",
    """AND (requested_operation IS NULL OR requested_operation=ANY(scope.operations))
 AND (NOT EXISTS (SELECT 1 FROM clinic_app.intake_patientaccessgrant channel_grant
   WHERE channel_grant.id=s.grant_id
    AND channel_grant.issued_by_label='front-desk channel')
  OR EXISTS (
   SELECT 1 FROM clinic_app.ai_frontdeskthread h
   JOIN clinic_app.comms_conversation c ON c.id=h.conversation_id
   WHERE h.session_id=s.id AND h.organization_id=s.organization_id
    AND h.clinic_id=s.clinic_id AND h.verified_enrollment_id=e.id
    AND h.state<>'handoff_pending' AND h.verified_until>statement_timestamp()
    AND __CONTACT__))""".replace("__CONTACT__", _CONTACT),
)
_touch = _function(PATIENT_PRIOR, "touch_patient_session").replace(
    "BEGIN\n",
    """BEGIN
 -- Share the existing destination-mutation key; no contact UPDATE grant.
 PERFORM 1 FROM clinic_app.ai_frontdeskthread h
 WHERE h.session_id=requested_session FOR SHARE;
 PERFORM pg_advisory_xact_lock(hashtextextended(
  'clinic-lock-v1:comms-subject:intake.patient_channel_preference:'
   ||pc.patient_id::text||':'||pc.channel,0))
 FROM clinic_app.ai_frontdeskthread h
 JOIN clinic_app.intake_patientcontact pc ON pc.id=h.verified_contact_id
 WHERE h.session_id=requested_session;
""",
    1,
)

_DEFINITIONS = (
    _enqueue,
    _pending,
    _turn,
    _articles,
    _verify,
    _state,
    _finish,
    _authority,
    _touch,
)
SQL = (
    """
ALTER TABLE clinic_app.ai_frontdeskthread
 ADD CONSTRAINT ai_frontdesk_contact_scope_fk
 FOREIGN KEY (organization_id,verified_contact_id)
 REFERENCES clinic_app.intake_patientcontact(organization_id,id)
 DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE clinic_app.ai_frontdeskthread
 ADD CONSTRAINT ai_frontdesk_offer_scope_fk
 FOREIGN KEY (organization_id,clinic_id,offered_turn_id)
 REFERENCES clinic_app.ai_frontdeskturn(organization_id,clinic_id,id)
 DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE clinic_app.ai_frontdeskthread
 ADD CONSTRAINT ai_frontdesk_handoff_scope_fk
 FOREIGN KEY (organization_id,clinic_id,handoff_turn_id)
 REFERENCES clinic_app.ai_frontdeskturn(organization_id,clinic_id,id)
 DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE clinic_app.ai_frontdeskturn
 ADD CONSTRAINT ai_turn_received_offer_scope_fk
 FOREIGN KEY (organization_id,clinic_id,received_offer_id)
 REFERENCES clinic_app.ai_frontdeskturn(organization_id,clinic_id,id)
 DEFERRABLE INITIALLY DEFERRED;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.ai_front_desk_turn(uuid,text);
"""
    + "\n".join(_DEFINITIONS)
    + """
REVOKE ALL ON FUNCTION clinic_app.ai_front_desk_turn(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_front_desk_turn(uuid,text) TO clinic_app;
RESET ROLE;
"""
)

REVERSE_SQL = (
    """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.ai_front_desk_turn(uuid,text);
"""
    + "\n".join(
        _function(PRIOR, name)
        for name in (
            "ai_front_desk_enqueue",
            "ai_front_desk_pending",
            "ai_front_desk_turn",
            "ai_front_desk_articles",
            "ai_front_desk_verify",
            "ai_front_desk_state",
            "ai_front_desk_finish",
        )
    )
    + "\n"
    + _function(PATIENT_PRIOR, "patient_authority")
    + "\n"
    + _function(PATIENT_PRIOR, "touch_patient_session")
    + """
REVOKE ALL ON FUNCTION clinic_app.ai_front_desk_turn(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_front_desk_turn(uuid,text) TO clinic_app;
RESET ROLE;
ALTER TABLE clinic_app.ai_frontdeskthread
 DROP CONSTRAINT ai_frontdesk_contact_scope_fk,
 DROP CONSTRAINT ai_frontdesk_offer_scope_fk,
 DROP CONSTRAINT ai_frontdesk_handoff_scope_fk;
ALTER TABLE clinic_app.ai_frontdeskturn
 DROP CONSTRAINT ai_turn_received_offer_scope_fk;
"""
)
