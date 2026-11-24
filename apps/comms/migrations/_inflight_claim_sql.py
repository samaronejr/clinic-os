"""Bind an in-flight send to its claimer: only the claim may record its outcome.

The worker's final recheck claims the message's permit while its session
holds the subject's advisory lock. The claim stores only a digest of a fresh
token (``send_permit_claim``) and returns the token to that worker process.
While a claim is open the runtime role cannot change the attempt's status,
provider reference or permit columns directly at all; the outcome is recorded
only by ``comms_record_send_outcome``, which needs the token and needs the
calling session to hold the subject lock itself. Anything else gets the same
refusal as an unknown attempt.

A claim abandoned by a dead session is never closed by default: recovery may
mark its outcome unknown, and the attempt stays visibly in flight until
provider evidence closes it.
"""

SQL = """
ALTER TABLE clinic_app.comms_integrationoperation
 ADD COLUMN send_permit_claim bytea;
ALTER TABLE clinic_app.comms_integrationoperation
 ADD CONSTRAINT comms_operation_send_permit_claim_check
 CHECK (send_permit_claim IS NULL
        OR (send_permit = 'held' AND provider_reference IS NULL));
GRANT UPDATE (send_permit_claim, provider_reference)
 ON clinic_app.comms_integrationoperation TO clinic_resolver;

-- The subject lock key the worker's session lock uses for a message.
CREATE FUNCTION clinic_app.comms_subject_lock_key(subject uuid)
RETURNS bigint LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT pg_catalog.hashtextextended(
   'clinic-lock-v1:comms-subject:comms.message:' || subject::text, 0)
$f$;
REVOKE ALL ON FUNCTION clinic_app.comms_subject_lock_key(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_subject_lock_key(uuid)
 TO clinic_resolver;

CREATE OR REPLACE FUNCTION clinic_app.comms_message_attempt_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF TG_OP = 'UPDATE' THEN
  -- An open claim is written only through comms_record_send_outcome.
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
  IF OLD.attempt_sequence IS NOT NULL
     AND NEW.attempt_sequence IS DISTINCT FROM OLD.attempt_sequence THEN
   RAISE EXCEPTION USING
    ERRCODE = '23514',
    CONSTRAINT = 'comms_message_attempt_sequence_immutable_check',
    MESSAGE = 'message attempt order is immutable';
  END IF;
  IF NEW.subject_type = 'comms.message'
     AND ((OLD.provider_reference IS NOT NULL
           AND NEW.provider_reference IS DISTINCT FROM OLD.provider_reference)
          OR (OLD.send_permit = 'held' AND OLD.provider_reference IS NULL
              AND NEW.status = 'cancelled' AND OLD.status <> 'cancelled'
              AND current_user = 'clinic_app')) THEN
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
  OR EXISTS (SELECT 1 FROM clinic_app.comms_inboundevent e
               JOIN clinic_app.comms_integrationoperation o ON o.id = e.operation_id
              WHERE o.subject_type = NEW.subject_type
                AND o.subject_id = NEW.subject_id
                AND e.delivery_state IN ('delivered', 'read'))
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
DROP FUNCTION clinic_app.comms_claim_send_permit(uuid);
-- Decides the message's single permit for ``requested``. ``refused`` when
-- another attempt is open, in flight or accepted without an outcome;
-- ``eligible`` (nothing written) while no session holds the subject lock,
-- as at prepare; otherwise the permit is claimed and a fresh token returned,
-- whose digest alone is stored.
CREATE FUNCTION clinic_app.comms_claim_send_permit(requested uuid)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE target clinic_app.comms_integrationoperation;
DECLARE k bigint;
DECLARE token uuid;
BEGIN
 SELECT * INTO target FROM clinic_app.comms_integrationoperation o
  WHERE o.id = requested AND o.subject_type = 'comms.message'
    AND o.organization_id =
        NULLIF(pg_catalog.current_setting('app.current_tenant', true), '')::uuid;
 IF target.id IS NULL THEN
  RETURN 'refused';
 END IF;
 PERFORM 1 FROM clinic_app.comms_integrationoperation o
  WHERE o.subject_type = 'comms.message' AND o.subject_id = target.subject_id
  ORDER BY o.id FOR UPDATE;
 IF EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
             WHERE o.subject_type = 'comms.message'
               AND o.subject_id = target.subject_id AND o.id <> target.id
               AND (o.status = 'in_progress'
                    OR ((o.send_permit IN ('held', 'open')
                         OR o.provider_reference IS NOT NULL)
                        AND NOT clinic_app.comms_attempt_refuted(o))))
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
           ) THEN
  RETURN 'eligible';
 END IF;
 UPDATE clinic_app.comms_integrationoperation AS o
    SET send_permit = 'released'
  WHERE o.subject_type = 'comms.message' AND o.subject_id = target.subject_id
    AND o.id <> target.id AND o.send_permit IN ('held', 'open')
    AND clinic_app.comms_attempt_refuted(o);
 token := pg_catalog.gen_random_uuid();
 UPDATE clinic_app.comms_integrationoperation AS o
    SET send_permit = 'held',
        send_permit_claim = pg_catalog.sha256(pg_catalog.uuid_send(token))
  WHERE o.id = target.id;
 RETURN token::text;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.comms_claim_send_permit(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_claim_send_permit(uuid) TO clinic_app;

-- Records the provider's answer for one claimed send: ``accepted`` with its
-- reference, ``rejected`` (synchronous refusal) or ``retry`` (transient, not
-- sent); ``withdrawn`` when the claimer's own final recheck refused before
-- any provider call. Needs the claim's token and the caller's own session to hold the
-- subject lock; otherwise the same refusal as an unknown attempt.
CREATE FUNCTION clinic_app.comms_record_send_outcome(
  requested uuid, token uuid, outcome text, reference text
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE target clinic_app.comms_integrationoperation;
DECLARE k bigint;
BEGIN
 SELECT * INTO target FROM clinic_app.comms_integrationoperation o
  WHERE o.id = requested AND o.subject_type = 'comms.message'
    AND o.status = 'in_progress' AND o.send_permit_claim IS NOT NULL
    AND o.send_permit_claim = pg_catalog.sha256(pg_catalog.uuid_send(token))
  FOR UPDATE;
 IF target.id IS NOT NULL THEN
  k := clinic_app.comms_subject_lock_key(target.subject_id);
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
    OR (outcome = 'accepted') IS DISTINCT FROM (reference IS NOT NULL) THEN
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
 RETURN true;
END $f$;
REVOKE ALL ON FUNCTION
 clinic_app.comms_record_send_outcome(uuid, uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
 clinic_app.comms_record_send_outcome(uuid, uuid, text, text) TO clinic_app;
RESET ROLE;
"""
