"""Prove provider answers with a MAC the database verifies itself.

Each provider account's MAC secret is stored in
``comms_providermacsecret``, readable only by ``clinic_resolver`` and the
owner-side definers; the runtime role holds no privilege on it. It is
provisioned by ``COPY`` (``apps.comms.mac_secrets``), never in SQL text. This is
plain storage protected by role ACL, a deliberate trade-off until server-side
binding lets it move into the tenant KEK envelope.

The bearer path is gone. After verifying the provider's signature over the
raw body, the inbound handler proves its own transaction with
``HMAC(secret, 'clinic-comms-inbound-v1:<account>:<xid>')``, so a MAC read from
another session's query text is useless outside the transaction it names. A
synchronous provider answer is proved with
``HMAC(secret, 'clinic-comms-outcome-v1:<attempt>:<outcome>:<reference>')``.
A replayed record changes nothing. The runtime role cannot write an
acceptance, a delivery, a reference or a delivery projection directly.
"""

SQL = """
CREATE TABLE clinic_app.comms_providermacsecret (
  -- No foreign key: verification always joins the live account row, and an
  -- orphaned secret authorizes nothing.
  account_id uuid PRIMARY KEY,
  secret bytea NOT NULL CHECK (pg_catalog.length(secret) >= 32),
  created_at timestamptz NOT NULL DEFAULT pg_catalog.statement_timestamp()
);
ALTER TABLE clinic_app.comms_providermacsecret OWNER TO clinic_owner;
REVOKE ALL ON clinic_app.comms_providermacsecret FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.comms_providermacsecret TO clinic_resolver;
GRANT SELECT (id) ON clinic_app.comms_provideraccount TO clinic_resolver;
ALTER TABLE clinic_app.comms_providermacsecret ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.comms_providermacsecret FORCE ROW LEVEL SECURITY;
-- No policy: only the BYPASSRLS resolver role reads or provisions it.

-- Constant-time check of ``proof`` (hex) against the account's MAC of
-- ``message``; false for a missing secret, so a missing secret fails closed.
CREATE FUNCTION clinic_app.comms_mac_ok(account uuid, message text, proof text)
RETURNS boolean LANGUAGE plpgsql STABLE
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE expected bytea;
DECLARE given bytea;
DECLARE acc integer := 0;
BEGIN
 IF proof IS NULL OR proof !~ '^[0-9a-f]{64}$' THEN
  RETURN false;
 END IF;
 SELECT clinic_app.hmac(pg_catalog.convert_to(message, 'UTF8'), s.secret, 'sha256')
   INTO expected
   FROM clinic_app.comms_providermacsecret s WHERE s.account_id = account;
 IF expected IS NULL THEN
  RETURN false;
 END IF;
 given := pg_catalog.decode(proof, 'hex');
 FOR i IN 0..31 LOOP
  acc := acc | (pg_catalog.get_byte(expected, i) # pg_catalog.get_byte(given, i));
 END LOOP;
 RETURN acc = 0;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.comms_mac_ok(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_mac_ok(uuid, text, text)
 TO clinic_resolver;

-- The claim's nonce lives in one fixed setting of the claimant session:
-- '<attempt>:<nonce>'; a worker session holds one claim at a time.
CREATE OR REPLACE FUNCTION clinic_app.comms_session_claim(requested uuid, fresh boolean)
RETURNS bytea LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE held text;
BEGIN
 IF fresh THEN
  PERFORM pg_catalog.set_config('clinic_comms.claim_nonce',
    requested::text || ':' || pg_catalog.gen_random_uuid()::text, false);
 END IF;
 held := NULLIF(pg_catalog.current_setting('clinic_comms.claim_nonce', true), '');
 IF held IS NULL OR pg_catalog.split_part(held, ':', 1) <> requested::text THEN
  RETURN NULL;
 END IF;
 RETURN pg_catalog.sha256(pg_catalog.convert_to(
   pg_catalog.pg_backend_pid()::text || ':' || held, 'UTF8'));
END $f$;

CREATE OR REPLACE FUNCTION clinic_app.comms_message_projection_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 -- The delivery projection is written only by a MAC-verified inbound
 -- transaction for the message's own provider account.
 IF current_user = 'clinic_app'
    AND NEW.provider_state IS DISTINCT FROM
        (CASE WHEN TG_OP = 'UPDATE' THEN OLD.provider_state ELSE '' END) THEN
  -- Nested: only the runtime role's own writes consult the verified context.
  IF NOT EXISTS (
      SELECT 1 FROM clinic_app.comms_conversation c
       WHERE c.id = NEW.conversation_id AND c.provider_account_id IS NOT NULL
         AND clinic_app.comms_ingest_account(c.clinic_id, c.channel)
             = c.provider_account_id) THEN
   RAISE EXCEPTION USING
    ERRCODE = '42501',
    MESSAGE = 'a delivery state needs a verified provider receipt';
  END IF;
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.comms_message_projection_guard() FROM PUBLIC;
CREATE TRIGGER comms_message_projection_guard
 BEFORE INSERT OR UPDATE OF provider_state ON clinic_app.comms_message
 FOR EACH ROW EXECUTE FUNCTION clinic_app.comms_message_projection_guard();

CREATE OR REPLACE FUNCTION clinic_app.comms_message_attempt_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF TG_OP = 'UPDATE' THEN
  -- An open claim, or a send left in flight by a pre-claim revision, is
  -- written only through comms_record_send_outcome by its own session.
  IF OLD.send_permit_claim IS NOT NULL AND current_user = 'clinic_app'
     AND (NEW.status IS DISTINCT FROM OLD.status
          OR NEW.provider_reference IS DISTINCT FROM OLD.provider_reference
          OR NEW.send_permit IS DISTINCT FROM OLD.send_permit
          OR NEW.send_permit_claim IS DISTINCT FROM OLD.send_permit_claim) THEN
   RAISE EXCEPTION USING
    ERRCODE = '23514',
    CONSTRAINT = 'comms_message_attempt_claim_check',
    MESSAGE = 'an in-flight send is recorded only by its claim';
  END IF;
  -- The runtime role never writes an acceptance, a delivery or a reference:
  -- those come only from the claim's MAC-verified record or a verified
  -- receipt, through resolvers.
  IF NEW.subject_type = 'comms.message' AND current_user = 'clinic_app'
     AND (NEW.provider_reference IS DISTINCT FROM OLD.provider_reference
          OR (NEW.status IS DISTINCT FROM OLD.status
              AND NEW.status IN ('succeeded', 'delivered'))) THEN
   RAISE EXCEPTION USING
    ERRCODE = '23514',
    CONSTRAINT = 'comms_message_attempt_outcome_check',
    MESSAGE = 'an attempt outcome needs provider evidence';
  END IF;
  IF OLD.attempt_sequence IS NOT NULL
     AND NEW.attempt_sequence IS DISTINCT FROM OLD.attempt_sequence THEN
   RAISE EXCEPTION USING
    ERRCODE = '23514',
    CONSTRAINT = 'comms_message_attempt_sequence_immutable_check',
    MESSAGE = 'message attempt order is immutable';
  END IF;
  IF NEW.subject_type = 'comms.message' AND OLD.provider_reference IS NOT NULL
     AND NEW.provider_reference IS DISTINCT FROM OLD.provider_reference THEN
   RAISE EXCEPTION USING
    ERRCODE = '23514',
    CONSTRAINT = 'comms_message_attempt_outcome_check',
    MESSAGE = 'an attempt outcome needs provider evidence';
  END IF;
  IF NEW.subject_type = 'comms.message' AND OLD.provider_reference IS NULL
     AND NEW.provider_reference IS NOT NULL THEN
   IF EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
               WHERE o.subject_type = NEW.subject_type
                 AND o.subject_id = NEW.subject_id AND o.id <> NEW.id
                 AND o.provider_reference IS NOT NULL
                 AND NOT clinic_app.comms_attempt_refuted(o)) THEN
    RAISE EXCEPTION USING
     ERRCODE = '23514',
     CONSTRAINT = 'comms_message_attempt_reference_check',
     MESSAGE = 'another provider-accepted attempt is still open';
   END IF;
  END IF;
  RETURN NEW;
 END IF;
 IF current_user = 'clinic_app' THEN
  NEW.send_permit := NULL;
  NEW.send_permit_claim := NULL;
  IF NEW.subject_type = 'comms.message'
     AND (NEW.status <> 'pending' OR NEW.provider_reference IS NOT NULL) THEN
   RAISE EXCEPTION USING
    ERRCODE = '23514',
    CONSTRAINT = 'comms_message_attempt_outcome_check',
    MESSAGE = 'an attempt outcome needs provider evidence';
  END IF;
 END IF;
 IF NEW.subject_type <> 'comms.message' THEN
  NEW.attempt_sequence := NULL;
  RETURN NEW;
 END IF;
 IF EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
             WHERE o.subject_type = NEW.subject_type
               AND o.subject_id = NEW.subject_id
               AND o.attempt_sequence IS NULL AND o.send_permit IS NULL) THEN
  RAISE EXCEPTION USING
   ERRCODE = '23514',
   CONSTRAINT = 'comms_message_attempt_reconciliation_check',
   MESSAGE = 'message attempt order awaits staff reconciliation';
 END IF;
 IF EXISTS (SELECT 1 FROM clinic_app.comms_message m
             WHERE m.id = NEW.subject_id
               AND m.provider_state IN ('delivered', 'read'))
  OR EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
              WHERE o.subject_type = NEW.subject_type
                AND o.subject_id = NEW.subject_id
                AND (o.status IN ('pending', 'in_progress', 'delivered')
                     OR ((o.send_permit IN ('held', 'open')
                          OR o.provider_reference IS NOT NULL)
                         AND NOT clinic_app.comms_attempt_refuted(o))))
 THEN
  RAISE EXCEPTION USING
   ERRCODE = '23514',
   CONSTRAINT = 'comms_message_attempt_open_check',
   MESSAGE = 'an attempt of this message has no negative outcome';
 END IF;
 SELECT coalesce(max(o.attempt_sequence), 0) + 1 INTO NEW.attempt_sequence
   FROM clinic_app.comms_integrationoperation o
  WHERE o.subject_type = NEW.subject_type AND o.subject_id = NEW.subject_id;
 RETURN NEW;
END $f$;

SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.comms_resolve_inbound(
    account pg_catalog.uuid, proof pg_catalog.text)
RETURNS TABLE(organization_id pg_catalog.uuid, clinic_id pg_catalog.uuid,
    principal_id pg_catalog.uuid, channel pg_catalog.text,
    provider pg_catalog.text, external_account_id pg_catalog.text)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT a.organization_id, a.clinic_id, a.principal_id, a.channel, a.provider,
        a.external_account_id
 FROM clinic_app.comms_provideraccount a
 JOIN clinic_app.identity_serviceprincipal p ON p.id = a.principal_id
  AND p.organization_id = a.organization_id AND p.clinic_id = a.clinic_id
 WHERE a.id = account AND a.active AND p.active
   AND p.purpose = 'messaging_ingest'
   AND clinic_app.comms_mac_ok(a.id, 'clinic-comms-inbound-v1:' || a.id::text
         || ':' || pg_catalog.pg_current_xact_id()::text, proof)
   AND NULLIF(current_setting('app.current_user_id', true), '') IS NULL
   AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
   AND NULLIF(current_setting('app.current_principal', true), '') IS NULL
$f$;

CREATE OR REPLACE FUNCTION clinic_app.comms_ingest_account(
    clinic pg_catalog.uuid, channel_name pg_catalog.text)
RETURNS pg_catalog.uuid
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT a.id
 FROM clinic_app.comms_provideraccount a
 JOIN clinic_app.identity_serviceprincipal p ON p.id = a.principal_id
  AND p.organization_id = a.organization_id AND p.clinic_id = a.clinic_id
 WHERE a.clinic_id = clinic AND a.channel = channel_name
   AND a.active AND p.active AND p.purpose = 'messaging_ingest'
   AND a.organization_id =
       NULLIF(current_setting('app.current_tenant', true), '')::pg_catalog.uuid
   AND a.principal_id =
       NULLIF(current_setting('app.current_principal', true), '')::pg_catalog.uuid
   AND NULLIF(current_setting('app.current_user_id', true), '') IS NULL
   AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
   AND clinic_app.comms_mac_ok(a.id, 'clinic-comms-inbound-v1:' || a.id::text
         || ':' || pg_catalog.pg_current_xact_id()::text,
         current_setting('app.comms_inbound_proof', true))
 LIMIT 1
$f$;

DROP FUNCTION clinic_app.comms_record_send_outcome(uuid, text, text);
CREATE FUNCTION clinic_app.comms_record_send_outcome(
  requested uuid, outcome text, reference text, proof text
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE target clinic_app.comms_integrationoperation;
DECLARE k bigint;
DECLARE account uuid;
BEGIN
 SELECT * INTO target FROM clinic_app.comms_integrationoperation o
  WHERE o.id = requested AND o.subject_type = 'comms.message'
    AND o.status = 'in_progress' AND o.send_permit_claim IS NOT NULL
    AND o.send_permit_claim = clinic_app.comms_session_claim(requested, false)
  FOR UPDATE;
 IF target.id IS NULL THEN
  -- A replayed record of an outcome already written changes nothing.
  IF EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
              WHERE o.id = requested AND o.subject_type = 'comms.message'
                AND o.send_permit_claim IS NULL AND o.status IN
                    ('succeeded', 'delivered', 'failed', 'pending', 'cancelled')) THEN
   RETURN false;
  END IF;
 ELSE
  k := clinic_app.comms_subject_lock_key(target.subject_id);
  SELECT c.provider_account_id INTO account
    FROM clinic_app.comms_message m
    JOIN clinic_app.comms_conversation c ON c.id = m.conversation_id
   WHERE m.id = target.subject_id;
 END IF;
 IF target.id IS NULL
    OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_locks l
         WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1
           AND l.database = (SELECT d.oid FROM pg_catalog.pg_database d
                              WHERE d.datname = pg_catalog.current_database())
           AND l.classid::text::bigint = (k >> 32) & 4294967295
           AND l.objid::text::bigint = k & 4294967295
           AND l.pid = pg_catalog.pg_backend_pid())
    OR outcome NOT IN ('accepted', 'rejected', 'retry', 'withdrawn')
    OR (outcome = 'accepted') IS DISTINCT FROM (reference IS NOT NULL)
    -- A provider answer carries the provider's MAC over exactly this
    -- attempt, outcome and reference; the portal has no external provider.
    OR (outcome IN ('accepted', 'rejected') AND CASE
          WHEN account IS NULL THEN proof IS NOT NULL
          ELSE NOT clinic_app.comms_mac_ok(account,
            'clinic-comms-outcome-v1:' || requested::text || ':' || outcome
              || ':' || coalesce(reference, ''), proof) END) THEN
  RAISE EXCEPTION 'send outcome unavailable' USING ERRCODE = '42501';
 END IF;
 UPDATE clinic_app.comms_integrationoperation AS o
    SET status = CASE outcome WHEN 'accepted' THEN 'succeeded'
                              WHEN 'rejected' THEN 'failed'
                              WHEN 'withdrawn' THEN 'cancelled' ELSE 'pending' END,
        provider_reference = reference,
        last_error = CASE outcome WHEN 'accepted' THEN ''
                                  WHEN 'rejected' THEN 'provider_rejected'
                                  WHEN 'withdrawn' THEN 'subject_ineligible'
                                  ELSE 'transient_send' END,
        send_permit_claim = NULL,
        updated_at = pg_catalog.statement_timestamp()
  WHERE o.id = target.id;
 PERFORM pg_catalog.set_config('clinic_comms.claim_nonce', '', false);
 RETURN true;
END $f$;
REVOKE ALL ON FUNCTION
 clinic_app.comms_record_send_outcome(uuid, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
 clinic_app.comms_record_send_outcome(uuid, text, text, text) TO clinic_app;

CREATE OR REPLACE FUNCTION clinic_app.comms_reconcile_attempts(
  requested_message uuid, requested_current uuid
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE scope clinic_app.comms_integrationoperation;
DECLARE chosen clinic_app.comms_integrationoperation;
BEGIN
 SELECT * INTO scope FROM clinic_app.comms_integrationoperation o
  WHERE o.subject_type = 'comms.message' AND o.subject_id = requested_message
    AND o.organization_id =
        NULLIF(pg_catalog.current_setting('app.current_tenant', true), '')::uuid
  ORDER BY o.id LIMIT 1;
 IF scope.id IS NULL OR NOT clinic_app.questionnaire_staff(scope.clinic_id,
     ARRAY['clinic_admin', 'clinic_manager', 'receptionist', 'scheduler',
           'nurse', 'physician']) THEN
  RAISE EXCEPTION 'message attempts unavailable' USING ERRCODE = '42501';
 END IF;
 PERFORM 1 FROM clinic_app.comms_integrationoperation o
  WHERE o.subject_type = 'comms.message' AND o.subject_id = requested_message
  FOR UPDATE;
 -- A held message never grants a permit, so a queued attempt or a claim
 -- without one provably never reached the provider; cancelling it records
 -- no provider outcome.
 UPDATE clinic_app.comms_integrationoperation AS o
    SET status = 'cancelled', last_error = 'subject_ineligible',
        updated_at = pg_catalog.statement_timestamp()
  WHERE o.subject_type = 'comms.message' AND o.subject_id = requested_message
    AND o.status IN ('pending', 'in_progress') AND o.send_permit IS NULL
    AND o.provider_reference IS NULL
    AND EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation h
                 WHERE h.subject_type = 'comms.message'
                   AND h.subject_id = requested_message
                   AND h.attempt_sequence IS NULL AND h.send_permit IS NULL);
 IF requested_current IS NOT NULL THEN
  SELECT * INTO chosen FROM clinic_app.comms_integrationoperation o
   WHERE o.id = requested_current AND o.subject_type = 'comms.message'
     AND o.subject_id = requested_message AND o.provider_reference IS NOT NULL
     AND o.attempt_sequence IS NULL;
 END IF;
 IF NOT EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
                 WHERE o.subject_type = 'comms.message'
                   AND o.subject_id = requested_message
                   AND ((o.attempt_sequence IS NULL AND o.send_permit IS NULL)
                        OR o.send_permit_claim = pg_catalog.sha256(
   pg_catalog.convert_to('legacy-in-flight', 'UTF8'))))
    OR (requested_current IS NULL AND EXISTS (
         SELECT 1 FROM clinic_app.comms_integrationoperation o
          WHERE o.subject_type = 'comms.message'
            AND o.subject_id = requested_message
            AND o.provider_reference IS NOT NULL))
    OR (requested_current IS NOT NULL AND chosen.id IS NULL)
    OR (chosen.id IS NOT NULL AND clinic_app.comms_attempt_refuted(chosen)
        AND EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
                     WHERE o.subject_type = 'comms.message'
                       AND o.subject_id = requested_message
                       AND o.id <> chosen.id
                       AND o.provider_reference IS NOT NULL
                       AND NOT clinic_app.comms_attempt_refuted(o)))
    OR EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
                WHERE o.subject_type = 'comms.message'
                  AND o.subject_id = requested_message
                  AND o.send_permit_claim IS DISTINCT FROM pg_catalog.sha256(
   pg_catalog.convert_to('legacy-in-flight', 'UTF8'))
                  AND (o.status = 'in_progress'
                       OR (o.send_permit = 'held'
                           AND o.provider_reference IS NULL
                           AND NOT clinic_app.comms_attempt_refuted(o)))) THEN
  RAISE EXCEPTION USING
   ERRCODE = '23514',
   CONSTRAINT = 'comms_message_attempt_reconciliation_check',
   MESSAGE = 'message attempts cannot be reconciled this way';
 END IF;
 -- A legacy in-flight send is closed by this decision: staff checked with
 -- the provider that it was not accepted, or chose the accepted one.
 UPDATE clinic_app.comms_integrationoperation AS o
    SET status = 'failed', last_error = 'outcome_unknown',
        send_permit = 'released', send_permit_claim = NULL,
        updated_at = pg_catalog.statement_timestamp()
  WHERE o.subject_type = 'comms.message' AND o.subject_id = requested_message
    AND o.send_permit_claim = pg_catalog.sha256(
   pg_catalog.convert_to('legacy-in-flight', 'UTF8'));
 -- Only which attempt is current is recorded: every other accepted attempt
 -- without its own failure stays open until the provider reports it.
 UPDATE clinic_app.comms_integrationoperation AS o
    SET send_permit = CASE
          WHEN o.provider_reference IS NOT NULL
               AND NOT clinic_app.comms_attempt_refuted(o) THEN 'open'
          ELSE 'released' END
  WHERE o.subject_type = 'comms.message' AND o.subject_id = requested_message
    AND o.attempt_sequence IS NULL
    AND o.id IS DISTINCT FROM chosen.id;
 UPDATE clinic_app.comms_integrationoperation AS o
    SET send_permit = CASE WHEN clinic_app.comms_attempt_refuted(o)
                           THEN 'released' ELSE 'held' END
  WHERE o.id = chosen.id;
END $f$;

-- A receipt for an in-flight send whose acceptance was never recorded: inside
-- a MAC-verified inbound transaction, the synthetic reference the adapter
-- derives from the attempt identifies it, and the receipt is the provider's
-- evidence of acceptance.
CREATE FUNCTION clinic_app.comms_adopt_late_reference(reference text)
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
 RETURN adopted;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.comms_adopt_late_reference(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_adopt_late_reference(text) TO clinic_app;
RESET ROLE;
"""
