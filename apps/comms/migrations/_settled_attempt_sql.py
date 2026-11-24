"""Move a claimed or sent message attempt only through its resolvers.

Once an attempt has left ``pending`` (claimed by the outbox, sent, accepted
or closed) the runtime role cannot change its status, reference or claim at
all; only the claimant's own session (``comms_record_send_outcome``), a
MAC-verified receipt, ``comms_close_unsent_attempt`` for an in-flight attempt
that provably never reached the provider (no permit claim and no reference),
or the staff hold may. A claim is taken only on an outbox-claimed attempt, so
no session can squat a queued one. A claim whose session no longer holds the
subject lock is abandoned: it stays visibly in flight and is closed only by
provider evidence or the staff hold, which warns that a retry may send twice.
"""

SQL = """
-- A claim nobody still holds: no other session holds the subject's lock.
CREATE FUNCTION clinic_app.comms_claim_abandoned(subject uuid)
RETURNS boolean LANGUAGE sql STABLE
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT NOT EXISTS (
   SELECT 1 FROM pg_catalog.pg_locks l
    WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1
      AND l.pid <> pg_catalog.pg_backend_pid()
      AND l.database = (SELECT d.oid FROM pg_catalog.pg_database d
                         WHERE d.datname = pg_catalog.current_database())
      AND l.classid::text::bigint = (pg_catalog.hashtextextended(
            'clinic-lock-v1:comms-subject:comms.message:' || subject::text, 0)
            >> 32) & 4294967295
      AND l.objid::text::bigint = pg_catalog.hashtextextended(
            'clinic-lock-v1:comms-subject:comms.message:' || subject::text, 0)
            & 4294967295)
$f$;
REVOKE ALL ON FUNCTION clinic_app.comms_claim_abandoned(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_claim_abandoned(uuid)
 TO clinic_app, clinic_resolver;

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
  -- Once an attempt left 'pending' (claimed, sent, accepted or closed), the
  -- runtime role cannot move it at all: only the claimant's session, a
  -- MAC-verified receipt or the staff hold may, through resolvers.
  IF NEW.subject_type = 'comms.message' AND current_user = 'clinic_app'
     AND OLD.status <> 'pending' AND NEW.status IS DISTINCT FROM OLD.status THEN
   RAISE EXCEPTION USING
    ERRCODE = '23514',
    CONSTRAINT = 'comms_message_attempt_settled_check',
    MESSAGE = 'a claimed or sent attempt is moved only by its resolvers';
  END IF;
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
 UPDATE clinic_app.comms_integrationoperation AS o
    SET send_permit = 'held',
        send_permit_claim = clinic_app.comms_session_claim(target.id, true)
  WHERE o.id = target.id;
 RETURN 'claimed';
END $f$;

-- Closes an outbox-claimed attempt that provably never reached the provider:
-- it never took the send permit's claim and holds no reference.
CREATE FUNCTION clinic_app.comms_close_unsent_attempt(
  requested uuid, outcome text, reason text
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF outcome NOT IN ('cancelled', 'failed', 'pending')
    OR reason IS NULL OR reason !~ '^[a-z_]{1,64}$' THEN
  RAISE EXCEPTION 'attempt unavailable' USING ERRCODE = '42501';
 END IF;
 UPDATE clinic_app.comms_integrationoperation AS o
    SET status = outcome, last_error = reason,
        updated_at = pg_catalog.statement_timestamp()
  WHERE o.id = requested AND o.subject_type = 'comms.message'
    AND o.status = 'in_progress' AND o.provider_reference IS NULL
    AND o.send_permit_claim IS NULL;
 RETURN FOUND;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.comms_close_unsent_attempt(uuid, text, text)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_close_unsent_attempt(uuid, text, text)
 TO clinic_app;

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
                        OR (o.status = 'in_progress'
                            AND (o.send_permit_claim = pg_catalog.sha256(
   pg_catalog.convert_to('legacy-in-flight', 'UTF8'))
                            OR clinic_app.comms_claim_abandoned(o.subject_id)))))
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
                  AND NOT (o.send_permit_claim IS NOT NULL
                           AND clinic_app.comms_claim_abandoned(o.subject_id))
                  AND (o.status = 'in_progress'
                       OR (o.send_permit = 'held'
                           AND o.provider_reference IS NULL
                           AND NOT clinic_app.comms_attempt_refuted(o)))) THEN
  RAISE EXCEPTION USING
   ERRCODE = '23514',
   CONSTRAINT = 'comms_message_attempt_reconciliation_check',
   MESSAGE = 'message attempts cannot be reconciled this way';
 END IF;
 -- A legacy or abandoned in-flight send is closed by this decision: staff checked with
 -- the provider that it was not accepted, or chose the accepted one.
 UPDATE clinic_app.comms_integrationoperation AS o
    SET status = 'failed', last_error = 'outcome_unknown',
        send_permit = 'released', send_permit_claim = NULL,
        updated_at = pg_catalog.statement_timestamp()
  WHERE o.subject_type = 'comms.message' AND o.subject_id = requested_message
    AND o.status = 'in_progress' AND o.send_permit_claim IS NOT NULL
    AND (o.send_permit_claim = pg_catalog.sha256(
   pg_catalog.convert_to('legacy-in-flight', 'UTF8'))
         OR clinic_app.comms_claim_abandoned(o.subject_id));
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
RESET ROLE;
"""
