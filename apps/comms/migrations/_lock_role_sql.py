"""Run the outbox lock session as clinic_lock, a role clinic_app cannot signal.

The send permit binds each claim to its lock session, so that session must be
one no runtime session can terminate or cancel: it logs in as ``clinic_lock``
(``ops/db/bootstrap.sql``), which ``clinic_app`` neither is nor belongs to.
Only clinic_lock may claim, record or close a message attempt, and it reads
no table directly (``comms_attempt_scope``). The claim's final write
re-checks the attempt under its row lock, so a close racing a claim leaves no
stale claim. The
unsent close is tenant scoped from the session GUCs (a foreign or unknown
attempt gets one denial) and takes only the outbox's fixed reasons. A receipt
for an attempt staff closed as unaccepted is recorded against it and shown as
a possible duplicate.
"""

SQL = """
GRANT EXECUTE ON FUNCTION clinic_app.audit_append(
 pg_catalog.text, pg_catalog.text, pg_catalog.inet, pg_catalog.text,
 pg_catalog.text, pg_catalog.timestamptz, pg_catalog.jsonb, pg_catalog.bytea
)
 TO clinic_lock;

CREATE OR REPLACE FUNCTION clinic_app.comms_status_attempt_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF NEW.kind = 'status' AND NOT EXISTS (
  SELECT 1 FROM clinic_app.comms_integrationoperation o
  JOIN clinic_app.comms_message m ON m.id=o.subject_id AND m.direction='out'
  JOIN clinic_app.comms_conversation c ON c.id=m.conversation_id
  WHERE o.id=NEW.operation_id AND o.kind='communication'
   AND o.subject_type='comms.message'
   -- A send staff closed with no recorded acceptance still takes the
   -- provider's later receipt: it is the possible-duplicate evidence.
   AND (o.provider_reference IS NOT NULL
        OR (o.status = 'failed' AND o.last_error = 'outcome_unknown'))
   AND o.organization_id=NEW.organization_id AND m.organization_id=o.organization_id
   AND c.organization_id=o.organization_id AND c.clinic_id=o.clinic_id
   AND c.provider_account_id=NEW.provider_account_id AND c.channel=o.channel
   AND clinic_app.comms_ingest_account(c.clinic_id,c.channel)=NEW.provider_account_id
 ) THEN
  RAISE EXCEPTION 'status requires its authenticated attempt' USING ERRCODE='42501';
 END IF;
 RETURN NEW;
END $f$;

SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.comms_claim_send_permit(requested uuid)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE target clinic_app.comms_integrationoperation;
DECLARE k bigint;
BEGIN
 SELECT * INTO target FROM clinic_app.comms_integrationoperation o
  WHERE o.id = requested AND o.subject_type = 'comms.message';
 IF target.id IS NULL THEN
  RETURN 'refused';
 END IF;
 IF EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
             WHERE o.subject_type = 'comms.message'
               AND o.subject_id = target.subject_id AND o.id <> target.id
               AND (o.status = 'in_progress'
                    OR ((o.send_permit IN ('held', 'open')
                         OR o.provider_reference IS NOT NULL)
                        AND NOT clinic_app.comms_attempt_refuted(o))))
    OR target.status <> 'in_progress'
    OR target.send_permit IS NOT DISTINCT FROM 'released'
    OR target.send_permit_claim IS NOT NULL THEN
  RETURN 'refused';
 END IF;
 k := clinic_app.comms_subject_lock_key(target.subject_id);
 IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_locks l
         WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1
           AND l.database = (SELECT d.oid FROM pg_catalog.pg_database d
                              WHERE d.datname = pg_catalog.current_database())
           AND l.classid::text::bigint = (k >> 32) & 4294967295
           AND l.objid::text::bigint = k & 4294967295
           AND l.pid = pg_catalog.pg_backend_pid()) THEN
  RETURN 'eligible';
 END IF;
 UPDATE clinic_app.comms_integrationoperation AS o
    SET send_permit = 'released'
  WHERE o.subject_type = 'comms.message' AND o.subject_id = target.subject_id
    AND o.id <> target.id AND o.send_permit IN ('held', 'open')
    AND clinic_app.comms_attempt_refuted(o);
 -- The final write re-checks the attempt under its row lock: a close that
 -- committed after the reads above leaves nothing to claim.
 UPDATE clinic_app.comms_integrationoperation AS o
    SET send_permit = 'held',
        send_permit_claim = clinic_app.comms_session_claim(target.id, true)
  WHERE o.id = target.id AND o.status = 'in_progress'
    AND o.provider_reference IS NULL AND o.send_permit_claim IS NULL
    AND o.send_permit IS DISTINCT FROM 'released';
 IF NOT FOUND THEN
  RETURN 'refused';
 END IF;
 RETURN 'claimed';
END $f$;
REVOKE ALL ON FUNCTION clinic_app.comms_claim_send_permit(uuid)
 FROM PUBLIC, clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.comms_claim_send_permit(uuid) TO clinic_lock;
REVOKE ALL ON FUNCTION
 clinic_app.comms_record_send_outcome(uuid, text, text, text) FROM PUBLIC, clinic_app;
GRANT EXECUTE ON FUNCTION
 clinic_app.comms_record_send_outcome(uuid, text, text, text) TO clinic_lock;

-- What the lock session needs to know about one message attempt in the
-- session's tenant: its status and the provider account whose MAC proves
-- an answer. clinic_lock reads no table directly.
CREATE FUNCTION clinic_app.comms_attempt_scope(
  requested uuid, OUT status text, OUT account uuid
) LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT o.status, c.provider_account_id
   FROM clinic_app.comms_integrationoperation o
   JOIN clinic_app.comms_message m ON m.id = o.subject_id
   JOIN clinic_app.comms_conversation c ON c.id = m.conversation_id
  WHERE o.id = requested AND o.subject_type = 'comms.message'
    AND o.organization_id = NULLIF(
          pg_catalog.current_setting('app.current_tenant', true), '')::uuid
$f$;
REVOKE ALL ON FUNCTION clinic_app.comms_attempt_scope(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_attempt_scope(uuid) TO clinic_lock;

CREATE OR REPLACE FUNCTION clinic_app.comms_close_unsent_attempt(
  requested uuid, outcome text, reason text
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE target uuid;
BEGIN
 SELECT o.id INTO target FROM clinic_app.comms_integrationoperation o
  WHERE o.id = requested AND o.subject_type = 'comms.message'
    AND o.organization_id = NULLIF(
          pg_catalog.current_setting('app.current_tenant', true), '')::uuid
  FOR UPDATE;
 IF target IS NULL
    OR outcome NOT IN ('cancelled', 'failed', 'pending')
    OR reason IS NULL
    OR reason NOT IN ('subject_ineligible', 'authority_revoked', 'provider_rejected',
                   'prepare_failed', 'adapter_unavailable', 'attempts_exhausted',
                   'invalid_provider_reference', 'transient_send') THEN
  RAISE EXCEPTION 'attempt unavailable' USING ERRCODE = '42501';
 END IF;
 UPDATE clinic_app.comms_integrationoperation AS o
    SET status = outcome, last_error = reason,
        updated_at = pg_catalog.statement_timestamp()
  WHERE o.id = target AND o.status = 'in_progress'
    AND o.provider_reference IS NULL AND o.send_permit_claim IS NULL;
 RETURN FOUND;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.comms_close_unsent_attempt(uuid, text, text)
 FROM PUBLIC, clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.comms_close_unsent_attempt(uuid, text, text)
 TO clinic_lock;

CREATE OR REPLACE FUNCTION clinic_app.comms_adopt_late_reference(reference text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE adopted uuid;
BEGIN
 UPDATE clinic_app.comms_integrationoperation AS o
    SET provider_reference = reference, status = 'succeeded', last_error = '',
        send_permit_claim = NULL, updated_at = pg_catalog.statement_timestamp()
   FROM clinic_app.comms_message m
   JOIN clinic_app.comms_conversation c ON c.id = m.conversation_id
  WHERE o.subject_type = 'comms.message' AND m.id = o.subject_id
    AND o.status = 'in_progress' AND o.provider_reference IS NULL
    AND o.send_permit = 'held'
    AND reference = 'synthetic:' || o.channel || ':' || o.id::text
    AND c.provider_account_id IS NOT NULL
    AND clinic_app.comms_ingest_account(c.clinic_id, c.channel)
        = c.provider_account_id
  RETURNING o.id INTO adopted;
 IF adopted IS NOT NULL THEN
  RETURN adopted;
 END IF;
 -- Staff closed it as "no provider acceptance is recorded" and the provider
 -- did accept it. The closed attempt is terminal and stays as it is; the
 -- receipt is recorded against it, which shows staff a possible duplicate.
 SELECT o.id INTO adopted
   FROM clinic_app.comms_integrationoperation o
   JOIN clinic_app.comms_message m ON m.id = o.subject_id
   JOIN clinic_app.comms_conversation c ON c.id = m.conversation_id
  WHERE o.subject_type = 'comms.message'
    AND o.status = 'failed' AND o.last_error = 'outcome_unknown'
    AND o.provider_reference IS NULL
    AND reference = 'synthetic:' || o.channel || ':' || o.id::text
    AND c.provider_account_id IS NOT NULL
    AND clinic_app.comms_ingest_account(c.clinic_id, c.channel)
        = c.provider_account_id;
 RETURN adopted;
END $f$;
RESET ROLE;
"""
