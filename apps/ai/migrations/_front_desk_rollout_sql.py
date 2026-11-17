"""Bind the released AI-04 bridges to evaluated exposure and fresh source proof."""

from apps.ai.migrations._care_sql import _edit
from apps.ai.migrations._front_desk_authority_sql import SQL as AUTHORITY
from apps.ai.migrations._front_desk_authority_sql import _function
from apps.ai.migrations._front_desk_sql import _OWNER, _TENANT
from apps.ai.migrations._front_desk_sql import SQL as BASE
from apps.ai.migrations._front_desk_switch_sql import SQL as SWITCH

_ALLOWED = r"""
CREATE FUNCTION clinic_app.ai_front_desk_allowed(
 approved uuid,clinic uuid,observed jsonb,autonomy boolean)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(
 SELECT 1 FROM clinic_app.ai_modelchangerequest permission
 JOIN clinic_app.ai_modelconfiguration m ON m.id=permission.configuration_id
 JOIN clinic_app.identity_clinic c ON c.id=permission.clinic_id
 JOIN clinic_app.ai_gatewayswitch gs ON gs.id=1 AND gs.enabled
 WHERE permission.id=approved AND permission.clinic_id=clinic
 AND current_setting('transaction_isolation')='read committed'
 AND permission.organization_id=c.organization_id
 AND clinic_app.ai_rollout_status(permission.id)=permission.stage
 AND permission.stage IN('shadow','reviewed_live','monitored_autonomy')
 AND (NOT autonomy OR permission.stage='monitored_autonomy')
 AND m.purpose='front_desk' AND m.route='primary'
 AND m.state IN('approved','active') AND (NOT autonomy OR m.state='active')
 AND m.provider IN('synthetic','recorded_fixture')
 AND m.approval_ref<>'' AND m.retention_terms<>'' AND m.training_terms<>''
 AND gs.approved_regions ? m.region
 AND observed=jsonb_build_object('prompt',m.prompt_sha256,
  'retrieval',m.retrieval_sha256,'schema',m.schema_sha256)
 AND NOT EXISTS(SELECT 1 FROM clinic_app.ai_aicircuit circuit
  WHERE circuit.organization_id=c.organization_id AND circuit.configuration_id=m.id
  AND circuit.closed_at IS NULL)
 AND EXISTS(SELECT 1 FROM clinic_app.providers_capabilityversion provider
  WHERE provider.id=(SELECT capability.current_version_id
   FROM clinic_app.providers_providercapability capability
   WHERE capability.key=m.capability_key
   AND (capability.clinic_id=c.id OR capability.clinic_id IS NULL)
   ORDER BY (capability.clinic_id IS NULL),capability.id LIMIT 1)
  AND provider.state NOT IN('degraded','revoked')
  AND provider.region IN('', 'synthetic',m.region)))
$f$;
"""

_CURRENT = r"""
CREATE FUNCTION clinic_app.ai_front_desk_current(requested uuid,autonomy boolean)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.ai_frontdeskturn u
 JOIN clinic_app.ai_frontdeskthread h ON h.id=u.thread_id
 JOIN clinic_app.comms_conversation c ON c.id=h.conversation_id
 JOIN clinic_app.ai_modelchangerequest permission ON permission.id=u.rollout_approval_id
 JOIN clinic_app.ai_modelconfiguration m ON m.id=permission.configuration_id
 WHERE u.id=requested AND u.organization_id=c.organization_id
 AND u.clinic_id=c.clinic_id AND u.source_xid=pg_current_xact_id()::text
 AND u.policy_id=__POLICY__
 AND clinic_app.ai_front_desk_allowed(permission.id,c.clinic_id,
  jsonb_build_object('prompt',m.prompt_sha256,'retrieval',m.retrieval_sha256,
   'schema',m.schema_sha256),autonomy))
$f$;
"""
# Retain the deployed policy and global-switch term without another policy copy.
_POLICY = _function(SWITCH, "ai_front_desk_enqueue")
_POLICY = _POLICY[
    _POLICY.index("policy:=") + len("policy:=") : _POLICY.index(";\n IF policy IS NULL")
]
_CURRENT = _CURRENT.replace("__POLICY__", _POLICY)

_AUTHORIZE = r"""
CREATE FUNCTION clinic_app.ai_front_desk_authorize(
 requested uuid,receipt bytea,kek text)
RETURNS text LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE u clinic_app.ai_frontdeskturn; h clinic_app.ai_frontdeskthread;
 c clinic_app.comms_conversation; permission clinic_app.ai_modelchangerequest;
 facts jsonb; admitted boolean;
BEGIN
 PERFORM pg_advisory_xact_lock_shared(hashtextextended('ai-global-rollout-v1',0));
 SELECT * INTO u FROM clinic_app.ai_frontdeskturn WHERE id=requested;
 SELECT * INTO h FROM clinic_app.ai_frontdeskthread WHERE id=u.thread_id;
 SELECT * INTO c FROM clinic_app.comms_conversation WHERE id=h.conversation_id;
 IF u.id IS NULL OR NOT (__OWNER__ OR
  (c.channel<>'portal' AND h.session_id IS NOT NULL
   AND h.session_id=NULLIF(current_setting('app.current_patient_session',true),'')::uuid
   AND NULLIF(current_setting('app.current_user_id',true),'') IS NULL
   AND NULLIF(current_setting('app.current_principal',true),'') IS NULL))
 THEN RETURN ''; END IF;
 facts:=convert_from(clinic_app.protected_decrypt(
  kek,'ai.front_desk.sources',receipt),'UTF8')::jsonb;
 IF facts IS DISTINCT FROM jsonb_build_object('subject',requested::text,
  'kind',CASE u.state WHEN 'pending' THEN 'turn' WHEN 'done' THEN 'completed_turn'
   ELSE '' END,'transaction',pg_current_xact_id()::text,'sources',facts->'sources')
 THEN RETURN ''; END IF;
 SELECT * INTO permission FROM clinic_app.ai_modelchangerequest
 WHERE id=u.rollout_approval_id;
 admitted:=u.policy_id=__POLICY__ AND clinic_app.ai_front_desk_allowed(
  permission.id,c.clinic_id,facts->'sources',permission.stage='monitored_autonomy');
 PERFORM set_config('app.ai_front_desk_bridge',u.id::text,true);
 UPDATE clinic_app.ai_frontdeskturn SET source_xid=
  CASE WHEN admitted THEN pg_current_xact_id()::text ELSE '' END WHERE id=u.id;
 PERFORM set_config('app.ai_front_desk_bridge','',true);
 RETURN CASE WHEN admitted THEN permission.stage ELSE '' END;
END $f$;
""".replace("__OWNER__", _OWNER).replace("__POLICY__", _POLICY)

_PROPOSE = r"""
CREATE FUNCTION clinic_app.ai_front_desk_propose(
 requested uuid,proposed_intent text,proposed_tool text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE u clinic_app.ai_frontdeskturn; c clinic_app.comms_conversation;
BEGIN
 SELECT turn.* INTO u FROM clinic_app.ai_frontdeskturn turn
 WHERE turn.id=requested AND turn.state='pending';
 SELECT conversation.* INTO c FROM clinic_app.comms_conversation conversation
 JOIN clinic_app.ai_frontdeskthread h ON h.conversation_id=conversation.id
 WHERE h.id=u.thread_id;
 IF u.id IS NULL OR NOT (__OWNER__)
 OR NOT clinic_app.ai_front_desk_current(u.id,false)
 OR clinic_app.ai_front_desk_current(u.id,true)
 THEN RAISE EXCEPTION 'front desk proposal refused' USING ERRCODE='42501'; END IF;
 PERFORM set_config('app.ai_front_desk_bridge',u.id::text,true);
 UPDATE clinic_app.ai_frontdeskturn SET state='proposed',
  intent=proposed_intent,tool=proposed_tool,outcome='proposed' WHERE id=u.id;
 PERFORM set_config('app.ai_front_desk_bridge','',true);
END $f$;
""".replace("__OWNER__", _OWNER)

_ENQUEUE_PRIOR = _function(SWITCH, "ai_front_desk_enqueue")
_ENQUEUE = _edit(
    _ENQUEUE_PRIOR,
    "ai_front_desk_enqueue(requested_message uuid)",
    "ai_front_desk_enqueue(requested_message uuid,receipt bytea,kek text)",
)
_ENQUEUE = _edit(
    _ENQUEUE,
    " policy uuid; thread uuid; turn uuid;",
    " policy uuid; thread uuid; turn uuid; approved uuid; facts jsonb;",
)
_ENQUEUE = _edit(
    _ENQUEUE,
    " IF policy IS NULL THEN RETURN NULL; END IF;",
    """ IF policy IS NULL THEN RETURN NULL; END IF;
 PERFORM pg_advisory_xact_lock_shared(hashtextextended('ai-global-rollout-v1',0));
 facts:=convert_from(clinic_app.protected_decrypt(
  kek,'ai.front_desk.sources',receipt),'UTF8')::jsonb;
 IF facts IS DISTINCT FROM jsonb_build_object('subject',requested_message::text,
  'kind','message','transaction',pg_current_xact_id()::text,'sources',facts->'sources')
 THEN RETURN NULL; END IF;
 SELECT permission.id INTO approved FROM clinic_app.ai_modelchangerequest permission
 WHERE permission.clinic_id=c.clinic_id AND permission.organization_id=c.organization_id
 AND clinic_app.ai_front_desk_allowed(permission.id,c.clinic_id,facts->'sources',false)
 ORDER BY permission.decided_at DESC,permission.id DESC LIMIT 1;
 IF approved IS NULL OR NOT clinic_app.ai_front_desk_allowed(
  approved,c.clinic_id,facts->'sources',
  EXISTS(SELECT 1 FROM clinic_app.ai_modelchangerequest permission
   WHERE permission.id=approved AND permission.stage='monitored_autonomy'))
 THEN RETURN NULL; END IF;""",
)
_ENQUEUE = _edit(
    _ENQUEUE,
    "policy_id,created_at,received_offer_id)",
    "policy_id,created_at,received_offer_id,rollout_approval_id,source_xid)",
)
_ENQUEUE = _edit(
    _ENQUEUE,
    "THEN h.offered_turn_id END",
    "THEN h.offered_turn_id END,approved,pg_current_xact_id()::text",
)

_TURN_PRIOR = _function(SWITCH, "ai_front_desk_turn")
_TURN = _edit(
    _TURN_PRIOR,
    "AND v.id=",
    "AND clinic_app.ai_front_desk_current(u.id,false) AND v.id=",
)
_TURN = _edit(_TURN, "AND b.tool<>''", "AND b.state='done' AND b.tool<>''")
_ARTICLES_PRIOR = _function(AUTHORITY, "ai_front_desk_articles")
_ARTICLES = _edit(
    _ARTICLES_PRIOR,
    "AND u.id=requested_turn",
    "AND clinic_app.ai_front_desk_current(u.id,false) AND u.id=requested_turn",
)

_VERIFY_PRIOR = _function(SWITCH, "ai_front_desk_verify")
_VERIFY = _edit(
    _VERIFY_PRIOR,
    "IF u.id IS NULL OR h.id IS NULL",
    "IF NOT clinic_app.ai_front_desk_current(u.id,true)"
    " OR u.id IS NULL OR h.id IS NULL",
)
_STATE_PRIOR = _function(SWITCH, "ai_front_desk_state")
_STATE = _edit(
    _STATE_PRIOR,
    "IF u.id IS NULL OR h.id IS NULL",
    "IF NOT clinic_app.ai_front_desk_current(u.id,true)"
    " OR u.id IS NULL OR h.id IS NULL",
)
_FINISH_PRIOR = _function(SWITCH, "ai_front_desk_finish")
_FINISH = _edit(
    _FINISH_PRIOR,
    "IF u.id IS NULL OR h.id IS NULL",
    "IF NOT clinic_app.ai_front_desk_current(u.id,true)"
    " OR u.id IS NULL OR h.id IS NULL",
)
_AUDIT_PRIOR = _function(BASE, "ai_front_desk_audit_scope")
_AUDIT = _edit(
    _AUDIT_PRIOR,
    "WHERE u.id=requested_turn",
    "WHERE clinic_app.ai_front_desk_current(u.id,true) AND u.id=requested_turn",
)
_GUARD_PRIOR = _function(BASE, "ai_front_desk_guard")
_GUARD = _edit(_GUARD_PRIOR, _TENANT, "clinic_app.protected_tenant()")
_GUARD = _edit(
    _GUARD,
    " ELSIF TG_TABLE_NAME='ai_frontdeskturn' THEN",
    """ ELSIF TG_TABLE_NAME='ai_frontdeskturn' THEN
  IF TG_OP='UPDATE' AND NEW.rollout_approval_id IS DISTINCT FROM OLD.rollout_approval_id
  THEN RAISE EXCEPTION 'front desk rollout binding retained' USING ERRCODE='42501';
  END IF;""",
)
_PATIENT_PRIOR = _function(AUTHORITY, "patient_authority")
_PATIENT = _edit(
    _PATIENT_PRIOR,
    "WHERE h.session_id=s.id AND h.organization_id=s.organization_id",
    """WHERE h.session_id=s.id AND h.organization_id=s.organization_id
    AND EXISTS(SELECT 1 FROM clinic_app.ai_frontdeskturn permitted
     WHERE permitted.thread_id=h.id AND permitted.state='pending'
     AND clinic_app.ai_front_desk_current(permitted.id,true))""",
)
_MESSAGE_PRIOR = _function(BASE, "comms_message_guard_v1")
_MESSAGE = _edit(
    _MESSAGE_PRIOR,
    "WHERE u.reply_id=NEW.id AND u.state='finishing'",
    "WHERE u.reply_id=NEW.id AND u.state='finishing'"
    " AND clinic_app.ai_front_desk_current(u.id,true)",
)

SQL = """
ALTER TABLE clinic_app.ai_frontdeskturn ADD CONSTRAINT ai_frontdesk_rollout_scope
 FOREIGN KEY(organization_id,clinic_id,rollout_approval_id)
 REFERENCES clinic_app.ai_modelchangerequest(organization_id,clinic_id,id);
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.ai_front_desk_enqueue(uuid);
"""
SQL += f"""
{_ALLOWED}
{_CURRENT}
{_AUTHORIZE}
{_PROPOSE}
{_ENQUEUE}
{_TURN}
{_ARTICLES}
{_VERIFY}
{_STATE}
{_FINISH}
{_AUDIT}
{_GUARD}
{_PATIENT}
"""
SQL += """
REVOKE ALL ON FUNCTION clinic_app.ai_front_desk_allowed(uuid,uuid,jsonb,boolean),
 clinic_app.ai_front_desk_current(uuid,boolean),
 clinic_app.ai_front_desk_authorize(uuid,bytea,text),
 clinic_app.ai_front_desk_propose(uuid,text,text),
 clinic_app.ai_front_desk_enqueue(uuid,bytea,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_front_desk_authorize(uuid,bytea,text),
 clinic_app.ai_front_desk_propose(uuid,text,text),
 clinic_app.ai_front_desk_enqueue(uuid,bytea,text) TO clinic_app;
RESET ROLE;
"""
SQL += _MESSAGE

REVERSE_SQL = """
SET LOCAL ROLE clinic_resolver;
DO $rollback$
BEGIN
 IF EXISTS(SELECT 1 FROM clinic_app.ai_frontdeskturn
  WHERE rollout_approval_id IS NOT NULL OR state='proposed')
 THEN RAISE EXCEPTION 'retained front desk rollout requires restore'
  USING ERRCODE='23514'; END IF;
END $rollback$;
DROP FUNCTION clinic_app.ai_front_desk_enqueue(uuid,bytea,text);
"""
REVERSE_SQL += f"""
{_ENQUEUE_PRIOR}
{_TURN_PRIOR}
{_ARTICLES_PRIOR}
{_VERIFY_PRIOR}
{_STATE_PRIOR}
{_FINISH_PRIOR}
{_AUDIT_PRIOR}
{_GUARD_PRIOR}
{_PATIENT_PRIOR}
"""
REVERSE_SQL += """
DROP FUNCTION clinic_app.ai_front_desk_propose(uuid,text,text);
DROP FUNCTION clinic_app.ai_front_desk_authorize(uuid,bytea,text);
DROP FUNCTION clinic_app.ai_front_desk_current(uuid,boolean);
DROP FUNCTION clinic_app.ai_front_desk_allowed(uuid,uuid,jsonb,boolean);
REVOKE ALL ON FUNCTION clinic_app.ai_front_desk_enqueue(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_front_desk_enqueue(uuid) TO clinic_app;
RESET ROLE;
ALTER TABLE clinic_app.ai_frontdeskturn DROP CONSTRAINT ai_frontdesk_rollout_scope;
"""
REVERSE_SQL += _MESSAGE_PRIOR
