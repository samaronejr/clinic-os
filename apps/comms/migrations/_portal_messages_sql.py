# ruff: noqa: S608 - migration-owned fragments only; no caller input reaches SQL
"""Patient side of the portal channel: session-bound reads, replies and audit.

A patient session (or a verified delegate's) holding the ``messages`` operation
reads the portal threads of its effective enrollment through these resolvers and
replies in a thread that is not closed. No staff or tenant GUC exists in a patient
request, so every read and write is a SECURITY DEFINER function that rebinds the
live session through ``current_patient_authority('messages')``; an unknown and a
foreign thread return the same denial. The patient reads its own side of the
thread and only the clinic messages the outbox accepted (latest operation
succeeded or delivered), never a queued, cancelled or failed one, an internal note
or a suggestion. The message guard gains one branch: an inbound portal row needs
that live session for the thread's enrollment and a thread that is not closed,
and refuses any staff actor; it opens no WhatsApp service window.
"""

from apps.comms.migrations._inbox_sql import SQL as INBOX_SQL

_GUARD_HEAD = "CREATE FUNCTION clinic_app.comms_message_guard_v1()"
_GUARD_START = INBOX_SQL.index(_GUARD_HEAD)
_GUARD_END = INBOX_SQL.index("END $f$;", _GUARD_START) + len("END $f$;")
_PRIOR_GUARD = INBOX_SQL[_GUARD_START:_GUARD_END]
_ANCHOR = " IF NEW.direction = 'in' THEN\n"
# The census decoder executes this module without str.count: split is the
# exactly-once check (tests/scheduling/clock_runsql.py).
if len(_PRIOR_GUARD.split(_ANCHOR)) - 1 != 1:  # pragma: no cover - import contract
    message = "inbound message guard branch moved"
    raise RuntimeError(message)
_PORTAL_BRANCH = """  IF c.channel = 'portal' THEN
   -- A portal reply comes only from the live patient session of the thread's
   -- enrollment that holds the messages operation, never from staff, and never
   -- into a closed thread. It opens no service window.
   IF actor IS NOT NULL OR c.state = 'closed' OR NOT EXISTS (
       SELECT 1 FROM clinic_app.current_patient_authority('messages') s
       WHERE s.organization_id = c.organization_id
         AND s.clinic_id = c.clinic_id AND s.enrollment_id = c.enrollment_id)
   THEN RAISE EXCEPTION 'portal reply requires the patient session'
        USING ERRCODE='42501';
   END IF;
   RETURN NEW;
  END IF;
"""
_REPLACE_HEAD = "CREATE OR REPLACE FUNCTION clinic_app.comms_message_guard_v1()"
GUARD_SQL = _PRIOR_GUARD.replace(_ANCHOR, _ANCHOR + _PORTAL_BRANCH, 1).replace(
    _GUARD_HEAD, _REPLACE_HEAD, 1
)
PRIOR_GUARD_SQL = _PRIOR_GUARD.replace(_GUARD_HEAD, _REPLACE_HEAD, 1)

# An outbound message is shown to the patient only once the outbox accepted it.
_LATEST_OPERATION = """
 LEFT JOIN LATERAL (SELECT o.status FROM clinic_app.comms_integrationoperation o
   WHERE o.organization_id=m.organization_id AND o.subject_type='comms.message'
     AND o.subject_id=m.id ORDER BY o.created_at DESC,o.id DESC LIMIT 1) op ON true"""
_VISIBLE = """(m.direction='in' OR (m.provider_state<>'failed'
   AND op.status IN ('succeeded','delivered')))"""

GRANTS_SQL = """
GRANT INSERT ON clinic_app.comms_message TO clinic_resolver;
GRANT UPDATE (state,needs_clinician,triage_category,revision,updated_at)
 ON clinic_app.comms_conversation TO clinic_resolver;
"""

RESOLVER_SQL = f"""
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.comms_patient_scope()
RETURNS TABLE(session_id uuid,organization_id uuid,clinic_id uuid,
 clinic_timezone text,acting_for_patient boolean)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT s.session_id,s.organization_id,s.clinic_id,k.timezone::text,
   s.delegate_grant_id IS NOT NULL
 FROM clinic_app.current_patient_authority('messages') s
 JOIN clinic_app.identity_clinic k ON k.id=s.clinic_id
   AND k.organization_id=s.organization_id
$f$;
CREATE FUNCTION clinic_app.comms_patient_conversations()
RETURNS TABLE(conversation_id uuid,purpose text,state text,
 created_at timestamptz,last_message_at timestamptz,message_count integer)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT c.id,c.purpose::text,c.state::text,c.created_at,v.last_at,
   v.total::integer
 FROM clinic_app.current_patient_authority('messages') s
 JOIN clinic_app.comms_conversation c ON c.organization_id=s.organization_id
   AND c.clinic_id=s.clinic_id AND c.enrollment_id=s.enrollment_id
   AND c.channel='portal'
 CROSS JOIN LATERAL (SELECT max(m.created_at) AS last_at,count(*) AS total
   FROM clinic_app.comms_message m{_LATEST_OPERATION}
   WHERE m.conversation_id=c.id AND {_VISIBLE}) v
 ORDER BY coalesce(v.last_at,c.created_at) DESC,c.id
 LIMIT 50
$f$;
CREATE FUNCTION clinic_app.comms_patient_messages(requested_conversation uuid,
 kek text)
RETURNS TABLE(message_id uuid,from_clinic boolean,body text,
 created_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT x.id,x.direction='out',convert_from(clinic_app.protected_decrypt(kek,
   'comms.message.body',x.body),'UTF8'),x.created_at
 FROM (SELECT m.id,m.direction,m.body,m.created_at
   FROM clinic_app.current_patient_authority('messages') s
   JOIN clinic_app.comms_conversation c ON c.id=requested_conversation
     AND c.organization_id=s.organization_id AND c.clinic_id=s.clinic_id
     AND c.enrollment_id=s.enrollment_id AND c.channel='portal'
   JOIN clinic_app.comms_message m ON m.conversation_id=c.id{_LATEST_OPERATION}
   WHERE {_VISIBLE}
   ORDER BY m.created_at DESC,m.id DESC LIMIT 200) x
 ORDER BY x.created_at,x.id
$f$;
CREATE FUNCTION clinic_app.comms_patient_reply(requested_clinic uuid,
 requested_conversation uuid,body_envelope bytea,reply_key uuid,kek text,
 triage text)
RETURNS TABLE(message_id uuid,conversation_id uuid,changed boolean,
 escalated boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s RECORD; c clinic_app.comms_conversation;
 prior clinic_app.comms_message; created uuid; event_key text; flagged boolean;
BEGIN
 IF reply_key IS NULL OR kek IS NULL OR body_envelope IS NULL
    OR octet_length(body_envelope) NOT BETWEEN 1 AND 65536
    OR triage IS NULL
    OR triage NOT IN ('urgent','clinical','administrative','unclassified') THEN
   RAISE EXCEPTION 'invalid portal reply' USING ERRCODE='22023';
 END IF;
 SELECT * INTO s FROM clinic_app.current_patient_authority('messages');
 IF s.session_id IS NULL OR s.clinic_id IS DISTINCT FROM requested_clinic THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501';
 END IF;
 SELECT * INTO c FROM clinic_app.comms_conversation x
 WHERE x.id=requested_conversation AND x.organization_id=s.organization_id
   AND x.clinic_id=s.clinic_id AND x.enrollment_id=s.enrollment_id
   AND x.channel='portal'
 FOR UPDATE;
 IF c.id IS NULL THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501';
 END IF;
 event_key:='portal:'||reply_key::text;
 SELECT * INTO prior FROM clinic_app.comms_message m
 WHERE m.conversation_id=c.id AND m.event_id=event_key AND m.direction='in';
 IF prior.id IS NOT NULL THEN
   -- A replay returns the original receipt even after the thread closed; the
   -- same key with other text is a conflict, decided on the plaintexts.
   IF clinic_app.protected_decrypt(kek,'comms.message.body',prior.body)
      IS DISTINCT FROM
      clinic_app.protected_decrypt(kek,'comms.message.body',body_envelope) THEN
     RAISE EXCEPTION 'portal reply conflict' USING ERRCODE='40001';
   END IF;
   RETURN QUERY SELECT prior.id,c.id,false,false;
   RETURN;
 END IF;
 IF c.state='closed' THEN
   RAISE EXCEPTION 'conversation is closed' USING ERRCODE='55000';
 END IF;
 -- Staff read this row through the field purpose: refuse any other envelope.
 PERFORM clinic_app.protected_decrypt(kek,'comms.message.body',body_envelope);
 created:=gen_random_uuid();
 INSERT INTO clinic_app.comms_message
 (id,organization_id,conversation_id,direction,body,template_id,sent_by_id,
  command_id,event_id,template_parameters,supported,provider_state,created_at)
 VALUES (created,c.organization_id,c.id,'in',body_envelope,NULL,NULL,NULL,
  event_key,NULL,true,'',statement_timestamp());
 flagged:=triage IN ('urgent','clinical');
 UPDATE clinic_app.comms_conversation x SET
   state=CASE WHEN x.state='pending' THEN 'open' ELSE x.state END,
   needs_clinician=x.needs_clinician OR flagged,
   triage_category=CASE WHEN
     (CASE triage WHEN 'urgent' THEN 3 WHEN 'clinical' THEN 2 ELSE 1 END)
     > (CASE x.triage_category WHEN 'urgent' THEN 3 WHEN 'clinical' THEN 2
        WHEN '' THEN 0 ELSE 1 END)
     THEN triage ELSE x.triage_category END,
   last_activity_at=statement_timestamp(),revision=x.revision+1,
   updated_at=statement_timestamp()
 WHERE x.id=c.id;
 RETURN QUERY SELECT created,c.id,true,flagged AND NOT c.needs_clinician;
END
$f$;
CREATE FUNCTION clinic_app.comms_patient_audit_scope(record_id uuid,
 event_name text)
RETURNS TABLE(session_id uuid,organization_id uuid,clinic_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT s.session_id,s.organization_id,s.clinic_id
 FROM clinic_app.current_patient_authority('messages') s
 WHERE (event_name='comms.portal.reply_received' AND EXISTS (
     SELECT 1 FROM clinic_app.comms_message m
     JOIN clinic_app.comms_conversation c ON c.id=m.conversation_id
     WHERE m.id=record_id AND m.direction='in' AND c.channel='portal'
       AND c.organization_id=s.organization_id AND c.clinic_id=s.clinic_id
       AND c.enrollment_id=s.enrollment_id))
  OR (event_name='comms.portal.escalated' AND EXISTS (
     SELECT 1 FROM clinic_app.comms_conversation c
     WHERE c.id=record_id AND c.channel='portal' AND c.needs_clinician
       AND c.organization_id=s.organization_id AND c.clinic_id=s.clinic_id
       AND c.enrollment_id=s.enrollment_id))
$f$;
REVOKE ALL ON FUNCTION clinic_app.comms_patient_scope(),
 clinic_app.comms_patient_conversations(),
 clinic_app.comms_patient_messages(uuid,text),
 clinic_app.comms_patient_reply(uuid,uuid,bytea,uuid,text,text),
 clinic_app.comms_patient_audit_scope(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_patient_scope(),
 clinic_app.comms_patient_conversations(),
 clinic_app.comms_patient_messages(uuid,text),
 clinic_app.comms_patient_reply(uuid,uuid,bytea,uuid,text,text) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.comms_patient_audit_scope(uuid,text)
 TO clinic_owner;
RESET ROLE;
"""

AUDIT_SQL = """
CREATE FUNCTION clinic_app.comms_patient_audit(record_id uuid,event_name text,
 occurred_at_utc timestamptz,content_hash bytea)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s RECORD; previous_hash bytea; inserted_seq bigint;
BEGIN
 SELECT * INTO s FROM clinic_app.comms_patient_audit_scope(record_id,event_name);
 IF s.session_id IS NULL THEN
   RAISE EXCEPTION 'patient access denied' USING ERRCODE='42501';
 END IF;
 IF current_setting('transaction_isolation')<>'read committed'
    OR content_hash IS NULL OR octet_length(content_hash)<>32
    OR occurred_at_utc IS NULL
    OR occurred_at_utc NOT BETWEEN clock_timestamp()-interval '5 minutes'
      AND clock_timestamp()+interval '5 minutes' THEN
   RAISE EXCEPTION 'invalid audit input' USING ERRCODE='22023';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
   ('clinic-audit:'||s.organization_id::text) COLLATE pg_catalog."C",0));
 SELECT a.curr_hash INTO previous_hash FROM clinic_app.audit_event a
 WHERE a.organization_id=s.organization_id ORDER BY a.seq DESC LIMIT 1;
 previous_hash:=coalesce(previous_hash,decode(repeat('00',32),'hex'));
 INSERT INTO clinic_app.audit_event
 (organization_id,actor_user_id,event_type,component_id,component_ip,
  affected_record_type,affected_record_id,occurred_at_utc,payload,prev_hash,
  curr_hash)
 VALUES (s.organization_id,s.session_id,event_name,'clinic-os-web',NULL,
  CASE event_name WHEN 'comms.portal.escalated' THEN 'comms.conversation'
   ELSE 'comms.message' END,
  record_id::text,occurred_at_utc,
  jsonb_build_object('clinic_id',s.clinic_id::text,'object_verb',
   CASE event_name WHEN 'comms.portal.escalated' THEN 'escalated'
    ELSE 'received' END),
  previous_hash,clinic_app.digest(content_hash||previous_hash,'sha256'))
 RETURNING seq INTO inserted_seq;
 RETURN inserted_seq;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.comms_patient_audit(uuid,text,timestamptz,bytea)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
 clinic_app.comms_patient_audit(uuid,text,timestamptz,bytea) TO clinic_app;
"""

SQL = GRANTS_SQL + GUARD_SQL + "\n" + RESOLVER_SQL + AUDIT_SQL

REVERSE_SQL = (
    """
DROP FUNCTION clinic_app.comms_patient_audit(uuid,text,timestamptz,bytea);
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.comms_patient_audit_scope(uuid,text);
DROP FUNCTION clinic_app.comms_patient_reply(uuid,uuid,bytea,uuid,text,text);
DROP FUNCTION clinic_app.comms_patient_messages(uuid,text);
DROP FUNCTION clinic_app.comms_patient_conversations();
DROP FUNCTION clinic_app.comms_patient_scope();
RESET ROLE;
"""
    + PRIOR_GUARD_SQL
    + """
REVOKE UPDATE (state,needs_clinician,triage_category,revision,updated_at)
 ON clinic_app.comms_conversation FROM clinic_resolver;
REVOKE INSERT ON clinic_app.comms_message FROM clinic_resolver;
"""
)
