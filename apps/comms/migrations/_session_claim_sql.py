"""Bind an in-flight claim to the claimant's own live session.

The claim is taken on the worker's lock connection, by the session that holds
the subject's advisory lock. It generates a nonce inside the database, keeps it
only in that session's own setting (no other session can read it, and it is
never sent as a query literal) and stores ``sha256(pid:nonce)``. An outcome is
accepted only from the session whose pid and setting reproduce that digest and
which still holds the lock. A cancelled session cannot hand its claim over; a
dead one leaves it abandoned and visibly in flight, closed only by provider
evidence.

Attempts a pre-claim revision left in flight carry an unmatchable legacy
digest: no session can record them, and the runtime role cannot write their
status, so they are closed only by provider evidence or the staff hold.
"""

# Owner-side and in the migration's one transaction: RLS and user triggers
# are suspended only for this statement, as ``open_table_backfill`` does,
# and restored (ENABLE + FORCE) before commit.
LEGACY_IN_FLIGHT_SQL = """
SET CONSTRAINTS ALL IMMEDIATE;
ALTER TABLE clinic_app.comms_integrationoperation DISABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.comms_integrationoperation DISABLE TRIGGER USER;
UPDATE clinic_app.comms_integrationoperation AS o
   SET send_permit_claim = pg_catalog.sha256(
         pg_catalog.convert_to('legacy-in-flight', 'UTF8'))
 WHERE o.subject_type = 'comms.message' AND o.status = 'in_progress'
   AND o.send_permit = 'held' AND o.provider_reference IS NULL;
ALTER TABLE clinic_app.comms_integrationoperation ENABLE TRIGGER USER;
ALTER TABLE clinic_app.comms_integrationoperation ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.comms_integrationoperation FORCE ROW LEVEL SECURITY;
"""

SQL = """
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

CREATE FUNCTION clinic_app.comms_session_claim(requested uuid, fresh boolean)
RETURNS bytea LANGUAGE plpgsql VOLATILE
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE setting text := 'clinic_comms.claim_' || replace(requested::text, '-', '');
DECLARE nonce text;
BEGIN
 IF fresh THEN
  PERFORM pg_catalog.set_config(setting, pg_catalog.gen_random_uuid()::text, false);
 END IF;
 nonce := NULLIF(pg_catalog.current_setting(setting, true), '');
 IF nonce IS NULL THEN
  RETURN NULL;
 END IF;
 RETURN pg_catalog.sha256(pg_catalog.convert_to(
   pg_catalog.pg_backend_pid()::text || ':' || nonce, 'UTF8'));
END $f$;
REVOKE ALL ON FUNCTION clinic_app.comms_session_claim(uuid, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_session_claim(uuid, boolean)
 TO clinic_resolver;

SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.comms_record_send_outcome(uuid, uuid, text, text);
DROP FUNCTION clinic_app.comms_claim_send_permit(uuid);
-- Decides the message's single permit for ``requested``: ``refused`` when
-- another attempt is open, in flight or accepted without an outcome, or this
-- one is released or already claimed; ``eligible`` (nothing written) unless
-- the caller's own session holds the subject lock; else ``claimed``, bound to
-- this session.
CREATE FUNCTION clinic_app.comms_claim_send_permit(requested uuid)
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
REVOKE ALL ON FUNCTION clinic_app.comms_claim_send_permit(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_claim_send_permit(uuid) TO clinic_app;

-- Records the provider's answer for one claimed send: ``accepted`` with its
-- reference, ``rejected`` (synchronous refusal), ``retry`` (transient, not
-- sent) or ``withdrawn`` (the claimant's own final recheck refused before any
-- call). Only the claimant's own live session, still holding the subject
-- lock, may record; anything else gets the refusal of an unknown attempt.
CREATE FUNCTION clinic_app.comms_record_send_outcome(
  requested uuid, outcome text, reference text
) RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE target clinic_app.comms_integrationoperation;
DECLARE k bigint;
BEGIN
 SELECT * INTO target FROM clinic_app.comms_integrationoperation o
  WHERE o.id = requested AND o.subject_type = 'comms.message'
    AND o.status = 'in_progress' AND o.send_permit_claim IS NOT NULL
    AND o.send_permit_claim = clinic_app.comms_session_claim(requested, false)
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
 PERFORM pg_catalog.set_config(
   'clinic_comms.claim_' || replace(requested::text, '-', ''), '', false);
 RETURN true;
END $f$;
REVOKE ALL ON FUNCTION
 clinic_app.comms_record_send_outcome(uuid, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION
 clinic_app.comms_record_send_outcome(uuid, text, text) TO clinic_app;
RESET ROLE;
"""
