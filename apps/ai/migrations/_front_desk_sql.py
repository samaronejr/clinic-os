# ruff: noqa: S608 - migration-owned fragments only; no caller input reaches SQL
"""Front-desk agent (todo 54): exact posture, guards and the agent bridges.

Three tables hold no patient text: approved administrative answers, the agent's
state per conversation and the provenance of each turn. The agent never borrows
a staff identity. A turn exists only because the authenticated inbound principal
of the conversation's provider account enqueued it for a stored inbound message;
every later agent function rechecks that same principal context, the clinic's
enabled front_desk A2 policy, the tool allowlist and the daily action budget.
Appointment reads and changes never happen here: they run through the patient
booking services under the challenge's session or the portal's live session.
"""

from typing import Final

from apps.comms.migrations._portal_messages_sql import _VISIBLE as PORTAL_VISIBLE
from apps.comms.migrations._portal_messages_sql import GUARD_SQL as PORTAL_GUARD
from apps.comms.migrations._portal_messages_sql import RESOLVER_SQL as PORTAL_READERS

_SCOPE = "organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid"
_ACTOR = "NULLIF(current_setting('app.current_user_id',true),'')::uuid"
_TENANT = """coalesce(NULLIF(current_setting('app.current_tenant',true),'')::uuid,
 (SELECT s.organization_id
  FROM clinic_app.current_patient_authority('messages') s))"""
TABLES: Final = ("ai_knowledgearticle", "ai_frontdeskthread", "ai_frontdeskturn")
_ALL = ",".join(f"clinic_app.{table}" for table in TABLES)
DAILY_WINDOW: Final = "interval '1 day'"
OFFER_MINUTES: Final = 10
SESSION_MINUTES: Final = 30

# The message guard gains one branch, anchored on the deployed portal text: an
# outbound row without a sender is the agent's reply, admitted only while the
# finish bridge names exactly this message for its own claimed turn.
_GUARD_HEAD = "CREATE OR REPLACE FUNCTION clinic_app.comms_message_guard_v1()"
_ANCHOR = " IF actor IS NULL OR NEW.sent_by_id IS DISTINCT FROM actor THEN\n"
if len(PORTAL_GUARD.split(_ANCHOR)) - 1 != 1:  # pragma: no cover - import contract
    message = "outbound message guard branch moved"
    raise RuntimeError(message)
_AGENT_BRANCH = """ IF actor IS NULL AND NEW.sent_by_id IS NULL
    AND current_setting('app.ai_front_desk_reply',true)=NEW.id::text THEN
  IF c.state='closed' OR NOT EXISTS (
     SELECT 1 FROM clinic_app.ai_frontdeskturn u
     JOIN clinic_app.ai_frontdeskthread h ON h.id=u.thread_id
     WHERE u.reply_id=NEW.id AND u.state='finishing'
       AND h.conversation_id=c.id AND u.organization_id=c.organization_id)
  THEN RAISE EXCEPTION 'agent reply refused' USING ERRCODE='42501';
  END IF;
  RETURN NEW;
 END IF;
"""
GUARD_SQL: Final = PORTAL_GUARD.replace(_ANCHOR, _AGENT_BRANCH + _ANCHOR, 1)
PRIOR_GUARD_SQL: Final = PORTAL_GUARD

# The thread's owner: its authenticated ingest principal or the portal's current
# enrollment-bound patient session. Neither path installs a staff actor.
_PORTAL_OWNER = """(c.channel='portal' AND EXISTS (
 SELECT 1 FROM clinic_app.current_patient_authority('messages') s
 WHERE s.organization_id=c.organization_id AND s.clinic_id=c.clinic_id
  AND s.enrollment_id=c.enrollment_id))"""
_OWNER = (
    """((clinic_app.comms_ingest_account(c.clinic_id,c.channel::text)
 IS NOT DISTINCT FROM c.provider_account_id AND c.provider_account_id IS NOT NULL)
 OR """
    + _PORTAL_OWNER
    + ")"
)
# The clinic's latest front_desk policy, enabled at A2, whose service principal
# still holds agent.execute, under the organization's AI opt-in for the
# front_desk purpose (the rollback switch: either flag off stops every turn).
_POLICY = """(SELECT pv.id FROM clinic_app.ai_agentpolicyversion pv
  WHERE pv.organization_id=c.organization_id AND pv.clinic_id=c.clinic_id
   AND pv.capability='front_desk' AND pv.version=(SELECT max(pw.version)
    FROM clinic_app.ai_agentpolicyversion pw WHERE pw.clinic_id=c.clinic_id
    AND pw.capability='front_desk')
   AND pv.enabled AND pv.autonomy_ceiling='A2' AND pv.principal_id IS NOT NULL
   AND EXISTS (SELECT 1 FROM clinic_app.identity_serviceprincipal sp
    JOIN clinic_app.identity_serviceprincipalgrant g ON g.principal_id=sp.id
     AND g.organization_id=sp.organization_id
    WHERE sp.id=pv.principal_id AND sp.organization_id=pv.organization_id
     AND sp.clinic_id=pv.clinic_id AND sp.active AND sp.grant_set_version=2
     AND g.active AND g.subject_scope='clinic' AND g.permission='agent.execute'
     AND g.grant_set_version=2)
   AND ((SELECT k.ai_policy FROM clinic_app.identity_clinicconfiguration k
    WHERE k.organization_id=c.organization_id
    ORDER BY k.created_at DESC,k.id DESC LIMIT 1)->'enabled')='true'::jsonb
   AND ((SELECT k.ai_policy FROM clinic_app.identity_clinicconfiguration k
    WHERE k.organization_id=c.organization_id
    ORDER BY k.created_at DESC,k.id DESC LIMIT 1)->'purposes') ? 'front_desk')"""

SQL = (
    """
REVOKE ALL ON __ALL__ FROM PUBLIC, clinic_app, clinic_agent;
GRANT SELECT,INSERT ON clinic_app.ai_knowledgearticle TO clinic_app;
GRANT UPDATE (state,approved_by_id,approved_at,retired_at,revision)
 ON clinic_app.ai_knowledgearticle TO clinic_app;
GRANT SELECT ON clinic_app.ai_frontdeskthread,clinic_app.ai_frontdeskturn
 TO clinic_app;
GRANT UPDATE (state,handoff_reason,resolved_by_id,revision,updated_at)
 ON clinic_app.ai_frontdeskthread TO clinic_app;
GRANT SELECT ON clinic_app.ai_knowledgearticle TO clinic_resolver;
GRANT SELECT,INSERT,UPDATE ON clinic_app.ai_frontdeskthread,
 clinic_app.ai_frontdeskturn TO clinic_resolver;
-- The challenge mints a booking-only channel grant and session. Reading the
-- policy, conversation, messages, patient and enrollment and inserting the
-- session (and the reply message) were already the resolver's privileges.
GRANT INSERT ON clinic_app.intake_patientaccessgrant TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ai_front_desk_enqueue(requested_message uuid)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE c clinic_app.comms_conversation; m clinic_app.comms_message;
 policy uuid; thread uuid; turn uuid;
BEGIN
 SELECT * INTO m FROM clinic_app.comms_message x WHERE x.id=requested_message
  AND x.direction='in' AND x.created_at>=transaction_timestamp();
 SELECT * INTO c FROM clinic_app.comms_conversation z WHERE z.id=m.conversation_id;
 IF m.id IS NULL OR c.id IS NULL OR NOT (__OWNER__) OR c.purpose='marketing' THEN
  RETURN NULL;
 END IF;
 policy:=__POLICY__;
 IF policy IS NULL THEN RETURN NULL; END IF;
 PERFORM set_config('app.ai_front_desk_bridge',m.id::text,true);
 INSERT INTO clinic_app.ai_frontdeskthread
  (id,organization_id,clinic_id,conversation_id,state,handoff_reason,
   challenge_attempts,revision,created_at,updated_at)
 SELECT gen_random_uuid(),z.organization_id,z.clinic_id,z.id,'active','',0,1,
  statement_timestamp(),statement_timestamp()
 FROM clinic_app.comms_conversation z WHERE z.id=c.id
 ON CONFLICT (conversation_id) DO NOTHING;
 -- A thread waiting for a person stays with that person: no agent turn.
 SELECT h.id INTO thread FROM clinic_app.ai_frontdeskthread h
  WHERE h.conversation_id=c.id AND h.state<>'handoff_pending';
 IF thread IS NULL THEN
  PERFORM set_config('app.ai_front_desk_bridge','',true);
  RETURN NULL;
 END IF;
 INSERT INTO clinic_app.ai_frontdeskturn
  (id,organization_id,clinic_id,thread_id,inbound_id,state,intent,tool,outcome,
   policy_id,created_at)
 SELECT gen_random_uuid(),h.organization_id,h.clinic_id,h.id,m.id,'pending','',
  '','',policy,statement_timestamp()
 FROM clinic_app.ai_frontdeskthread h WHERE h.id=thread
 ON CONFLICT (inbound_id) DO NOTHING RETURNING id INTO turn;
 PERFORM set_config('app.ai_front_desk_bridge','',true);
 RETURN turn;
END
$f$;
CREATE FUNCTION clinic_app.ai_front_desk_pending()
RETURNS SETOF uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT u.id FROM clinic_app.ai_frontdeskturn u
 JOIN clinic_app.ai_frontdeskthread h ON h.id=u.thread_id
 JOIN clinic_app.comms_conversation c ON c.id=h.conversation_id
 WHERE u.state='pending' AND u.organization_id=__TENANT__ AND __OWNER__
 ORDER BY u.created_at,u.id LIMIT 20
$f$;
CREATE FUNCTION clinic_app.ai_front_desk_turn(requested_turn uuid,kek text)
RETURNS TABLE(turn_id uuid,thread_id uuid,clinic_id uuid,channel text,
 enrollment_id uuid,thread_state text,verified boolean,session_id uuid,
 challenge_pending boolean,attempts integer,offered_appointment uuid,
 offered_practitioner uuid,offered_start timestamptz,first_turn boolean,
 inbound_text text,policy_id uuid,tools text[],budget_left integer)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT u.id,h.id,c.clinic_id,c.channel::text,c.enrollment_id,h.state,
  (__PORTAL_OWNER__ OR
   (h.verified_until IS NOT NULL AND h.verified_until>statement_timestamp())),
  CASE WHEN c.channel='portal' THEN
   (SELECT s.session_id FROM clinic_app.current_patient_authority('messages') s)
   WHEN h.verified_until>statement_timestamp() THEN h.session_id END,
  h.challenge_until IS NOT NULL AND h.challenge_until>statement_timestamp(),
  h.challenge_attempts::integer,
  CASE WHEN h.offered_until>statement_timestamp() THEN h.offered_appointment_id END,
  CASE WHEN h.offered_until>statement_timestamp() THEN h.offered_practitioner_id END,
  CASE WHEN h.offered_until>statement_timestamp() THEN h.offered_start END,
  NOT EXISTS (SELECT 1 FROM clinic_app.ai_frontdeskturn p
   WHERE p.thread_id=h.id AND p.state='done'),
  convert_from(clinic_app.protected_decrypt(kek,'comms.message.body',m.body),'UTF8'),
  u.policy_id,
  ARRAY(SELECT k FROM jsonb_object_keys(v.tool_allowlist) k ORDER BY k),
  (v.budget_actions-(SELECT count(*) FROM clinic_app.ai_frontdeskturn b
   WHERE b.organization_id=u.organization_id AND b.policy_id=u.policy_id
    AND b.tool<>'' AND b.created_at>statement_timestamp()-__DAY__))::integer
 FROM clinic_app.ai_frontdeskturn u
 JOIN clinic_app.ai_frontdeskthread h ON h.id=u.thread_id
 JOIN clinic_app.comms_conversation c ON c.id=h.conversation_id
 JOIN clinic_app.comms_message m ON m.id=u.inbound_id
 JOIN clinic_app.ai_agentpolicyversion v ON v.id=u.policy_id
 WHERE u.id=requested_turn AND u.state='pending' AND u.organization_id=__TENANT__
  AND __OWNER__ AND v.id=__POLICY__
$f$;
CREATE FUNCTION clinic_app.ai_front_desk_articles(requested_turn uuid)
RETURNS TABLE(article_id uuid,key text,version integer,title text,body text,
 keywords text[],link text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT a.id,a.key::text,a.version::integer,a.title::text,a.body::text,
  a.keywords::text[],a.link::text
 FROM clinic_app.ai_frontdeskturn u
 JOIN clinic_app.ai_frontdeskthread h ON h.id=u.thread_id
 JOIN clinic_app.comms_conversation c ON c.id=h.conversation_id
 JOIN clinic_app.ai_knowledgearticle a ON a.organization_id=h.organization_id
  AND a.clinic_id=h.clinic_id AND a.state='approved'
 WHERE u.id=requested_turn AND u.state='pending' AND u.organization_id=__TENANT__
  AND __OWNER__
 ORDER BY a.key
$f$;
-- The challenge compares the date of birth of the thread's linked enrollment (a
-- verified contact link) server-side. At most three attempts; success mints a
-- booking-only patient session for 30 minutes, issued in the name of the
-- administrator who enabled the policy, the accountable authorizer.
CREATE FUNCTION clinic_app.ai_front_desk_verify(requested_turn uuid,
 birth text,kek text)
RETURNS TABLE(verified boolean,session_id uuid,exhausted boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE h clinic_app.ai_frontdeskthread; c clinic_app.comms_conversation;
 u clinic_app.ai_frontdeskturn; e clinic_app.intake_patientclinicenrollment;
 p clinic_app.intake_patient; v clinic_app.ai_agentpolicyversion;
 grant_id uuid; minted uuid; matched boolean;
BEGIN
 SELECT * INTO u FROM clinic_app.ai_frontdeskturn x WHERE x.id=requested_turn
  AND x.state='pending' AND x.organization_id=__TENANT__;
 SELECT * INTO h FROM clinic_app.ai_frontdeskthread x WHERE x.id=u.thread_id
  FOR UPDATE;
 SELECT * INTO c FROM clinic_app.comms_conversation x WHERE x.id=h.conversation_id;
 IF u.id IS NULL OR h.id IS NULL OR c.channel='portal'
  OR NOT (__OWNER__) OR u.policy_id IS DISTINCT FROM
  __POLICY__ OR h.challenge_until IS NULL OR h.challenge_until<=statement_timestamp()
  OR h.challenge_attempts>=3
 THEN RAISE EXCEPTION 'front desk challenge refused' USING ERRCODE='42501'; END IF;
 SELECT * INTO e FROM clinic_app.intake_patientclinicenrollment x
  WHERE x.id=c.enrollment_id AND x.organization_id=c.organization_id;
 SELECT * INTO p FROM clinic_app.intake_patient x WHERE x.id=e.patient_id;
 matched:=e.id IS NOT NULL AND p.id IS NOT NULL AND birth IS NOT NULL AND
  convert_from(clinic_app.protected_decrypt(kek,'intake.patient.birth_date',
   p.birth_date),'UTF8')=birth;
 PERFORM set_config('app.ai_front_desk_bridge',h.id::text,true);
 IF NOT matched THEN
  UPDATE clinic_app.ai_frontdeskthread SET challenge_attempts=challenge_attempts+1,
   revision=revision+1,updated_at=statement_timestamp() WHERE id=h.id;
  PERFORM set_config('app.ai_front_desk_bridge','',true);
  RETURN QUERY SELECT false,NULL::uuid,h.challenge_attempts+1>=3;
  RETURN;
 END IF;
 SELECT * INTO v FROM clinic_app.ai_agentpolicyversion x WHERE x.id=u.policy_id;
 grant_id:=gen_random_uuid();
 INSERT INTO clinic_app.intake_patientaccessgrant
  (id,organization_id,clinic_id,patient_id,enrollment_id,issued_by_id,
   issued_by_label,secret_hash,operations,expires_at,consumed_at,created_at)
 SELECT grant_id,x.organization_id,x.clinic_id,x.patient_id,x.id,v.created_by_id,
  'front-desk channel',clinic_app.digest(gen_random_uuid()::text,'sha256'),
  ARRAY['booking']::varchar[],statement_timestamp()+make_interval(mins=>__SESSION__),
  statement_timestamp(),statement_timestamp()
 FROM clinic_app.intake_patientclinicenrollment x WHERE x.id=e.id;
 minted:=gen_random_uuid();
 INSERT INTO clinic_app.intake_patientsession
  (id,organization_id,clinic_id,patient_id,enrollment_id,grant_id,operations,
   expires_at,idle_expires_at,created_at)
 SELECT minted,x.organization_id,x.clinic_id,x.patient_id,x.id,grant_id,
  ARRAY['booking']::varchar[],statement_timestamp()+make_interval(mins=>__SESSION__),
  statement_timestamp()+make_interval(mins=>__SESSION__),statement_timestamp()
 FROM clinic_app.intake_patientclinicenrollment x WHERE x.id=e.id;
 UPDATE clinic_app.ai_frontdeskthread SET verified_enrollment_id=e.id,
  verified_until=statement_timestamp()+make_interval(mins=>__SESSION__),
  session_id=minted,challenge_until=NULL,challenge_attempts=0,
  revision=revision+1,updated_at=statement_timestamp() WHERE id=h.id;
 PERFORM set_config('app.ai_front_desk_bridge','',true);
 RETURN QUERY SELECT true,minted,false;
END
$f$;
-- Turn bookkeeping: open a challenge, bind an offer, clear it, hand off.
CREATE FUNCTION clinic_app.ai_front_desk_state(requested_turn uuid,change text,
 appointment uuid,practitioner uuid,start timestamptz,reason text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE h clinic_app.ai_frontdeskthread; c clinic_app.comms_conversation;
 u clinic_app.ai_frontdeskturn;
BEGIN
 SELECT * INTO u FROM clinic_app.ai_frontdeskturn x WHERE x.id=requested_turn
  AND x.state='pending' AND x.organization_id=__TENANT__;
 SELECT * INTO h FROM clinic_app.ai_frontdeskthread x WHERE x.id=u.thread_id
  FOR UPDATE;
 SELECT * INTO c FROM clinic_app.comms_conversation x WHERE x.id=h.conversation_id;
 IF u.id IS NULL OR h.id IS NULL OR NOT (__OWNER__)
  OR u.policy_id IS DISTINCT FROM __POLICY__
  OR change NOT IN ('challenge','offer','clear','handoff')
  OR (change='offer' AND ((NOT (__PORTAL_OWNER__) AND
   (h.verified_until IS NULL OR h.verified_until<=statement_timestamp()))
   OR practitioner IS NULL OR start IS NULL OR start<=statement_timestamp()))
  OR (change='handoff' AND reason NOT IN ('urgent','clinical','human_request',
   'kb_missing','verification_failed','policy_limit'))
 THEN RAISE EXCEPTION 'front desk state refused' USING ERRCODE='42501'; END IF;
 PERFORM set_config('app.ai_front_desk_bridge',h.id::text,true);
 UPDATE clinic_app.ai_frontdeskthread SET
  challenge_until=CASE WHEN change='challenge'
   THEN statement_timestamp()+make_interval(mins=>__SESSION__)
   ELSE challenge_until END,
  offered_appointment_id=CASE WHEN change='offer' THEN appointment
   WHEN change='clear' THEN NULL ELSE offered_appointment_id END,
  offered_practitioner_id=CASE WHEN change='offer' THEN practitioner
   WHEN change='clear' THEN NULL ELSE offered_practitioner_id END,
  offered_start=CASE WHEN change='offer' THEN start
   WHEN change='clear' THEN NULL ELSE offered_start END,
  offered_until=CASE WHEN change='offer'
   THEN statement_timestamp()+make_interval(mins=>__OFFER__)
   WHEN change='clear' THEN NULL ELSE offered_until END,
  state=CASE WHEN change='handoff' THEN 'handoff_pending' ELSE state END,
  handoff_reason=CASE WHEN change='handoff' THEN reason ELSE handoff_reason END,
  revision=revision+1,updated_at=statement_timestamp()
 WHERE id=h.id;
 PERFORM set_config('app.ai_front_desk_bridge','',true);
END
$f$;
-- One reply per turn: the outbound message, the turn's provenance, done.
CREATE FUNCTION clinic_app.ai_front_desk_finish(requested_turn uuid,
 reply_body bytea,kek text,turn_intent text,turn_tool text,turn_outcome text,
 cited uuid)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE h clinic_app.ai_frontdeskthread; c clinic_app.comms_conversation;
 u clinic_app.ai_frontdeskturn; reply uuid := gen_random_uuid();
BEGIN
 SELECT * INTO u FROM clinic_app.ai_frontdeskturn x WHERE x.id=requested_turn
  AND x.state='pending' AND x.organization_id=__TENANT__ FOR UPDATE;
 SELECT * INTO h FROM clinic_app.ai_frontdeskthread x WHERE x.id=u.thread_id;
 SELECT * INTO c FROM clinic_app.comms_conversation x WHERE x.id=h.conversation_id;
 IF u.id IS NULL OR h.id IS NULL OR NOT (__OWNER__)
  OR u.policy_id IS DISTINCT FROM __POLICY__
  OR (turn_tool<>'' AND NOT EXISTS (SELECT 1 FROM clinic_app.ai_agentpolicyversion v
   WHERE v.id=u.policy_id AND v.tool_allowlist ? turn_tool))
  OR (cited IS NOT NULL AND NOT EXISTS (SELECT 1 FROM clinic_app.ai_knowledgearticle a
   WHERE a.id=cited AND a.organization_id=h.organization_id
    AND a.clinic_id=h.clinic_id AND a.state='approved'))
 THEN RAISE EXCEPTION 'front desk reply refused' USING ERRCODE='42501'; END IF;
 -- The body must decrypt for this tenant before it is stored.
 PERFORM clinic_app.protected_decrypt(kek,'comms.message.body',reply_body);
 PERFORM set_config('app.ai_front_desk_bridge',u.id::text,true);
 UPDATE clinic_app.ai_frontdeskturn SET state='finishing',reply_id=reply,
  intent=turn_intent,tool=turn_tool,outcome=turn_outcome,article_id=cited
 WHERE id=u.id;
 PERFORM set_config('app.ai_front_desk_reply',reply::text,true);
 INSERT INTO clinic_app.comms_message
  (id,organization_id,conversation_id,direction,body,sent_by_id,command_id,
   supported,provider_state,created_at)
 SELECT reply,x.organization_id,x.id,'out',reply_body,NULL,u.id,true,'sent',
  statement_timestamp()
 FROM clinic_app.comms_conversation x WHERE x.id=c.id;
 PERFORM set_config('app.ai_front_desk_reply','',true);
 UPDATE clinic_app.ai_frontdeskturn SET state='done' WHERE id=u.id;
 PERFORM set_config('app.ai_front_desk_bridge','',true);
 RETURN reply;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.ai_front_desk_enqueue(uuid),
 clinic_app.ai_front_desk_pending(),clinic_app.ai_front_desk_turn(uuid,text),
 clinic_app.ai_front_desk_articles(uuid),
 clinic_app.ai_front_desk_verify(uuid,text,text),
 clinic_app.ai_front_desk_state(uuid,text,uuid,uuid,timestamptz,text),
 clinic_app.ai_front_desk_finish(uuid,bytea,text,text,text,text,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_front_desk_enqueue(uuid),
 clinic_app.ai_front_desk_pending(),clinic_app.ai_front_desk_turn(uuid,text),
 clinic_app.ai_front_desk_articles(uuid),
 clinic_app.ai_front_desk_verify(uuid,text,text),
 clinic_app.ai_front_desk_state(uuid,text,uuid,uuid,timestamptz,text),
 clinic_app.ai_front_desk_finish(uuid,bytea,text,text,text,text,uuid) TO clinic_app;
RESET ROLE;
""".replace("__ALL__", _ALL)
    .replace("__OWNER__", _OWNER)
    .replace("__PORTAL_OWNER__", _PORTAL_OWNER)
    .replace("__POLICY__", _POLICY)
    .replace("__TENANT__", _TENANT)
    .replace("__DAY__", DAILY_WINDOW)
    .replace("__OFFER__", str(OFFER_MINUTES))
    .replace("__SESSION__", str(SESSION_MINUTES))
)

# Every write by the runtime role or the bridges passes this guard. The agent
# tables accept only bridge writes for their own row; a knowledge article only
# its administrator's draft, approval and retirement; a thread also the staff
# resolution of its handoff. Nothing is deleted or truncated.
_GUARD = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ai_front_desk_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE actor uuid := __ACTOR__; bridge text :=
 current_setting('app.ai_front_desk_bridge',true);
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION 'front desk history is retained' USING ERRCODE='23514';
 END IF;
 IF NEW.organization_id IS DISTINCT FROM __TENANT__ THEN
  RAISE EXCEPTION 'front desk scope mismatch' USING ERRCODE='42501';
 END IF;
 IF TG_TABLE_NAME='ai_knowledgearticle' THEN
  IF actor IS NULL OR NOT clinic_app.has_permission('configuration.clinic',
   NEW.clinic_id,NULL)
  OR (TG_OP='INSERT' AND (NEW.created_by_id IS DISTINCT FROM actor
   OR NEW.state<>'draft' OR NEW.revision<>1))
  OR (TG_OP='UPDATE' AND ((to_jsonb(NEW)-'state'-'approved_by_id'-'approved_at'
    -'retired_at'-'revision') IS DISTINCT FROM (to_jsonb(OLD)-'state'
    -'approved_by_id'-'approved_at'-'retired_at'-'revision')
   OR NEW.revision<>OLD.revision+1
   OR NOT ((OLD.state='draft' AND NEW.state='approved'
     AND NEW.approved_by_id IS NOT DISTINCT FROM actor)
    OR (OLD.state='approved' AND NEW.state='retired'
     AND NEW.approved_by_id IS NOT DISTINCT FROM OLD.approved_by_id))))
  THEN RAISE EXCEPTION 'knowledge article refused' USING ERRCODE='42501'; END IF;
  IF TG_OP='INSERT' THEN NEW.created_at:=statement_timestamp(); END IF;
  IF TG_OP='UPDATE' AND NEW.state='approved' THEN
   NEW.approved_at:=statement_timestamp();
  END IF;
  IF TG_OP='UPDATE' AND NEW.state='retired' THEN
   NEW.retired_at:=statement_timestamp();
  END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='ai_frontdeskthread' AND TG_OP='UPDATE' AND actor IS NOT NULL
 THEN
  -- Staff close a handoff once a person answered; nothing else changes.
  IF (to_jsonb(NEW)-'state'-'resolved_by_id'-'revision'-'updated_at')
     IS DISTINCT FROM (to_jsonb(OLD)-'state'-'resolved_by_id'-'revision'-'updated_at')
  OR OLD.state<>'handoff_pending' OR NEW.state<>'resolved'
  OR NEW.resolved_by_id IS DISTINCT FROM actor OR NEW.revision<>OLD.revision+1
  OR NOT clinic_app.has_permission('demographics.read',NEW.clinic_id,NULL)
  THEN RAISE EXCEPTION 'handoff resolution refused' USING ERRCODE='42501'; END IF;
  NEW.updated_at:=statement_timestamp();
  RETURN NEW;
 END IF;
 IF actor IS NOT NULL OR bridge IS NULL OR bridge='' THEN
  RAISE EXCEPTION 'front desk bridge required' USING ERRCODE='42501';
 END IF;
 -- Each branch names only its own table's columns (PL/pgSQL resolves NEW
 -- fields per row type when a statement first runs).
 IF TG_TABLE_NAME='ai_frontdeskthread' THEN
  IF NOT ((TG_OP='INSERT' AND EXISTS (SELECT 1 FROM clinic_app.comms_message m
     WHERE m.id::text=bridge AND m.conversation_id=NEW.conversation_id))
   OR (TG_OP='UPDATE' AND bridge=OLD.id::text)) THEN
   RAISE EXCEPTION 'front desk bridge required' USING ERRCODE='42501';
  END IF;
 ELSIF TG_TABLE_NAME='ai_frontdeskturn' THEN
  IF NOT ((TG_OP='INSERT' AND bridge=NEW.inbound_id::text)
   OR (TG_OP='UPDATE' AND bridge=OLD.id::text)) THEN
   RAISE EXCEPTION 'front desk bridge required' USING ERRCODE='42501';
  END IF;
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.ai_front_desk_guard() FROM PUBLIC;
-- The migration role attaches the triggers, then gives the grant back.
GRANT EXECUTE ON FUNCTION clinic_app.ai_front_desk_guard() TO clinic_owner;
RESET ROLE;
"""
SQL += _GUARD.replace("__ACTOR__", _ACTOR).replace("__TENANT__", _TENANT)
_READ: Final = {
    "ai_knowledgearticle": (
        "clinic_app.has_permission('configuration.clinic',clinic_id,NULL)"
    ),
    "ai_frontdeskthread": (
        "clinic_app.has_permission('demographics.read',clinic_id,NULL)"
    ),
    "ai_frontdeskturn": "clinic_app.has_permission('demographics.read',clinic_id,NULL)",
}
for _table in TABLES:
    SQL += f"""
ALTER TABLE clinic_app.{_table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{_table} FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{_table} ADD CONSTRAINT {_table}_clinic_fk
 FOREIGN KEY (organization_id,clinic_id)
 REFERENCES clinic_app.identity_clinic(organization_id,id);
CREATE POLICY front_desk_owner ON clinic_app.{_table} TO clinic_owner
 USING ({_SCOPE}) WITH CHECK ({_SCOPE});
CREATE POLICY front_desk_read ON clinic_app.{_table} FOR SELECT TO clinic_app
 USING ({_SCOPE} AND ({_READ[_table]}));
CREATE TRIGGER front_desk_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.{_table} FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ai_front_desk_guard();
CREATE TRIGGER front_desk_truncate BEFORE TRUNCATE ON clinic_app.{_table}
 FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.ai_front_desk_guard();
"""
SQL += f"""
CREATE POLICY front_desk_insert ON clinic_app.ai_knowledgearticle
 FOR INSERT TO clinic_app WITH CHECK ({_SCOPE});
CREATE POLICY front_desk_update ON clinic_app.ai_knowledgearticle
 FOR UPDATE TO clinic_app USING ({_SCOPE} AND ({_READ["ai_knowledgearticle"]}))
 WITH CHECK ({_SCOPE});
CREATE POLICY front_desk_update ON clinic_app.ai_frontdeskthread
 FOR UPDATE TO clinic_app USING ({_SCOPE} AND ({_READ["ai_frontdeskthread"]}))
 WITH CHECK ({_SCOPE});
ALTER TABLE clinic_app.ai_frontdeskturn ADD CONSTRAINT ai_frontdeskturn_thread_scope_fk
 FOREIGN KEY (organization_id,clinic_id,thread_id)
 REFERENCES clinic_app.ai_frontdeskthread(organization_id,clinic_id,id)
 DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE clinic_app.ai_frontdeskturn ADD CONSTRAINT ai_frontdeskturn_article_scope_fk
 FOREIGN KEY (organization_id,clinic_id,article_id)
 REFERENCES clinic_app.ai_knowledgearticle(organization_id,clinic_id,id)
 DEFERRABLE INITIALLY DEFERRED;
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ai_front_desk_guard() FROM clinic_owner;
RESET ROLE;
{GUARD_SQL}
"""


def _portal_reader(name: str) -> str:
    head = f"CREATE FUNCTION clinic_app.{name}("
    start = PORTAL_READERS.index(head)
    end = PORTAL_READERS.index("$f$;", start) + len("$f$;")
    return PORTAL_READERS[start:end].replace(
        "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
    )


# A local portal agent reply is delivered in the reply transaction itself.
# The patient's existing enrollment-bound readers admit only the exact reply
# of a completed turn; ordinary staff messages still need their outbox receipt.
_AGENT_VISIBLE = """(m.direction='in' OR (m.provider_state<>'failed'
 AND (op.status IN ('succeeded','delivered') OR EXISTS (
  SELECT 1 FROM clinic_app.ai_frontdeskturn u
  JOIN clinic_app.ai_frontdeskthread h ON h.id=u.thread_id
  WHERE u.reply_id=m.id AND u.state='done' AND u.organization_id=c.organization_id
   AND h.conversation_id=c.id))))"""
_PRIOR_READERS = (
    _portal_reader("comms_patient_conversations")
    + "\n"
    + _portal_reader("comms_patient_messages")
    + "\n"
    + _portal_reader("comms_patient_reply")
)
SQL += (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _PRIOR_READERS.replace(PORTAL_VISIBLE, _AGENT_VISIBLE).replace(
        " SELECT * INTO c FROM clinic_app.comms_conversation x\n",
        " PERFORM pg_advisory_xact_lock(hashtextextended('agent-send-v1',0));\n"
        " SELECT * INTO c FROM clinic_app.comms_conversation x\n",
        1,
    )
    + "\nRESET ROLE;\n"
)
SQL += """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ai_front_desk_audit_scope(requested_turn uuid)
RETURNS TABLE(principal_id uuid,organization_id uuid,clinic_id uuid)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT CASE WHEN c.channel='portal' THEN
  (SELECT s.session_id FROM clinic_app.current_patient_authority('messages') s)
  ELSE NULLIF(current_setting('app.current_principal',true),'')::uuid END,
  u.organization_id,u.clinic_id
 FROM clinic_app.ai_frontdeskturn u
 JOIN clinic_app.ai_frontdeskthread h ON h.id=u.thread_id
 JOIN clinic_app.comms_conversation c ON c.id=h.conversation_id
 JOIN clinic_app.comms_message m ON m.id=u.reply_id
 WHERE u.id=requested_turn AND u.state='done' AND u.organization_id=__TENANT__
  AND __OWNER__ AND m.command_id=u.id AND m.sent_by_id IS NULL
  AND m.created_at>=transaction_timestamp()
$f$;
REVOKE ALL ON FUNCTION clinic_app.ai_front_desk_audit_scope(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_front_desk_audit_scope(uuid)
 TO clinic_app,clinic_owner;
RESET ROLE;
CREATE FUNCTION clinic_app.ai_front_desk_audit(requested_turn uuid,
 occurred_at_utc timestamptz,content_hash bytea)
RETURNS bigint LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s RECORD; previous_hash bytea; inserted_seq bigint;
BEGIN
 SELECT * INTO s FROM clinic_app.ai_front_desk_audit_scope(requested_turn);
 IF s.principal_id IS NULL THEN
  RAISE EXCEPTION 'front desk audit denied' USING ERRCODE='42501'; END IF;
 IF current_setting('transaction_isolation')<>'read committed'
  OR content_hash IS NULL OR octet_length(content_hash)<>32
  OR occurred_at_utc IS NULL OR occurred_at_utc NOT BETWEEN
   clock_timestamp()-interval '5 minutes' AND clock_timestamp()+interval '5 minutes'
 THEN RAISE EXCEPTION 'invalid audit input' USING ERRCODE='22023'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
  ('clinic-audit:'||s.organization_id::text) COLLATE pg_catalog."C",0));
 SELECT a.seq INTO inserted_seq FROM clinic_app.audit_event a
 WHERE a.organization_id=s.organization_id AND a.event_type='ai.front_desk.replied'
  AND a.affected_record_id=requested_turn::text;
 IF inserted_seq IS NOT NULL THEN RETURN inserted_seq; END IF;
 SELECT a.curr_hash INTO previous_hash FROM clinic_app.audit_event a
 WHERE a.organization_id=s.organization_id ORDER BY a.seq DESC LIMIT 1;
 previous_hash:=coalesce(previous_hash,decode(repeat('00',32),'hex'));
 INSERT INTO clinic_app.audit_event
 (organization_id,actor_user_id,event_type,component_id,component_ip,
  affected_record_type,affected_record_id,occurred_at_utc,payload,prev_hash,curr_hash)
 VALUES (s.organization_id,s.principal_id,'ai.front_desk.replied','clinic-os-web',
  NULL,'ai.front_desk_turn',requested_turn::text,occurred_at_utc,
  jsonb_build_object('clinic_id',s.clinic_id::text,'object_verb','replied'),
  previous_hash,clinic_app.digest(content_hash||previous_hash,'sha256'))
 RETURNING seq INTO inserted_seq;
 RETURN inserted_seq;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.ai_front_desk_audit(uuid,timestamptz,bytea)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_front_desk_audit(uuid,timestamptz,bytea)
 TO clinic_app;
""".replace("__TENANT__", _TENANT).replace("__OWNER__", _OWNER)

# The batched schema operations drop the tables after this reverse runs (its
# RunSQL is the batch's last database operation), so it drops only what depends
# on the functions it removes.
_REVERSE_TABLE_OBJECTS = "".join(
    f"DROP TRIGGER front_desk_guard ON clinic_app.{table};\n"
    f"DROP TRIGGER front_desk_truncate ON clinic_app.{table};\n"
    f"DROP POLICY front_desk_owner ON clinic_app.{table};\n"
    f"DROP POLICY front_desk_read ON clinic_app.{table};\n"
    for table in TABLES
) + (
    # These reference the thread and article scope keys, which the schema
    # reversal drops before the tables themselves.
    "ALTER TABLE clinic_app.ai_frontdeskturn DROP CONSTRAINT "
    "ai_frontdeskturn_thread_scope_fk;\n"
    "ALTER TABLE clinic_app.ai_frontdeskturn DROP CONSTRAINT "
    "ai_frontdeskturn_article_scope_fk;\n"
)
REVERSE_SQL: Final = f"""
{PRIOR_GUARD_SQL}
DROP FUNCTION clinic_app.ai_front_desk_audit(uuid,timestamptz,bytea);
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.ai_front_desk_audit_scope(uuid);
{_PRIOR_READERS}
RESET ROLE;
REVOKE INSERT ON clinic_app.intake_patientaccessgrant FROM clinic_resolver;
{_REVERSE_TABLE_OBJECTS}
DROP POLICY front_desk_insert ON clinic_app.ai_knowledgearticle;
DROP POLICY front_desk_update ON clinic_app.ai_knowledgearticle;
DROP POLICY front_desk_update ON clinic_app.ai_frontdeskthread;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.ai_front_desk_guard();
DROP FUNCTION clinic_app.ai_front_desk_finish(uuid,bytea,text,text,text,text,uuid);
DROP FUNCTION clinic_app.ai_front_desk_state(uuid,text,uuid,uuid,timestamptz,text);
DROP FUNCTION clinic_app.ai_front_desk_verify(uuid,text,text);
DROP FUNCTION clinic_app.ai_front_desk_articles(uuid);
DROP FUNCTION clinic_app.ai_front_desk_turn(uuid,text);
DROP FUNCTION clinic_app.ai_front_desk_pending();
DROP FUNCTION clinic_app.ai_front_desk_enqueue(uuid);
RESET ROLE;
"""
