"""Release a message's send permit only on recorded provider evidence.

An accepted attempt (one holding a provider reference) is refuted only by a
bound failed status receipt. The receipt guard already requires the inbound
proof that ``comms_resolve_inbound`` verified against the provider secret, so
neither staff nor raw SQL as the runtime role can write that evidence. An
attempt the provider never accepted is refuted by its own terminal status.
A legacy original whose failure the base recorded is released by this
migration (the release is the record of that base evidence).

The runtime role cannot write ``send_permit`` at all: the worker claims the
permit through ``comms_claim_send_permit``, which releases a holder only on
that evidence and refuses while any other attempt is open. Staff resolution
records only which attempt is current: it keeps the chosen attempt as the
holder and marks every other accepted, unrefuted attempt ``open``; it never
writes a provider outcome. Until each accepted attempt has its own outcome
the message admits no further attempt, and staff see how many are awaiting.
"""

BACKFILL_SQL = """
UPDATE clinic_app.comms_integrationoperation AS o
   SET send_permit = 'released'
  FROM clinic_app.comms_message m
 WHERE o.subject_type = 'comms.message' AND m.id = o.subject_id
   AND o.idempotency_key = m.command_id AND m.provider_state = 'failed'
   AND o.send_permit IS DISTINCT FROM 'released';

UPDATE clinic_app.comms_integrationoperation AS o
   SET send_permit = 'open'
  FROM clinic_app.comms_message m
 WHERE o.subject_type = 'comms.message' AND m.id = o.subject_id
   AND o.send_permit = 'released' AND o.provider_reference IS NOT NULL
   AND o.idempotency_key IS DISTINCT FROM m.command_id
   AND NOT EXISTS (SELECT 1 FROM clinic_app.comms_inboundevent e
                    WHERE e.operation_id = o.id AND e.delivery_state = 'failed');
"""

SQL = """
ALTER TABLE clinic_app.comms_integrationoperation
 DROP CONSTRAINT comms_operation_send_permit_check;
ALTER TABLE clinic_app.comms_integrationoperation
 ADD CONSTRAINT comms_operation_send_permit_check
 CHECK (send_permit IN ('held', 'open', 'released')
        AND subject_type = 'comms.message' OR send_permit IS NULL);
REVOKE UPDATE (send_permit) ON clinic_app.comms_integrationoperation
 FROM clinic_app;

CREATE OR REPLACE FUNCTION clinic_app.comms_attempt_refuted(
  attempt clinic_app.comms_integrationoperation
) RETURNS boolean LANGUAGE sql STABLE
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT CASE WHEN attempt.provider_reference IS NOT NULL THEN
   attempt.send_permit IS NOT DISTINCT FROM 'released'
   OR EXISTS (SELECT 1 FROM clinic_app.comms_inboundevent e
               WHERE e.operation_id = attempt.id
                 AND e.delivery_state = 'failed')
 ELSE attempt.status IN ('failed', 'cancelled') END
$f$;

CREATE OR REPLACE FUNCTION clinic_app.comms_message_attempt_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF TG_OP = 'UPDATE' THEN
  IF OLD.attempt_sequence IS NOT NULL
     AND NEW.attempt_sequence IS DISTINCT FROM OLD.attempt_sequence THEN
   RAISE EXCEPTION USING
    ERRCODE = '23514',
    CONSTRAINT = 'comms_message_attempt_sequence_immutable_check',
    MESSAGE = 'message attempt order is immutable';
  END IF;
  -- Acceptance is permanent, and a claimed send is never cancelled after
  -- its final recheck: neither may be rewritten into a negative outcome.
  IF NEW.subject_type = 'comms.message'
     AND ((OLD.provider_reference IS NOT NULL
           AND NEW.provider_reference IS DISTINCT FROM OLD.provider_reference)
          OR (OLD.send_permit = 'held' AND OLD.provider_reference IS NULL
              AND NEW.status = 'cancelled' AND OLD.status <> 'cancelled')) THEN
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
-- Claims the message's single permit for ``requested`` under the message's
-- row locks; the caller is the worker's final recheck. A holder is released
-- only when its own record refutes it, and the claim is refused while any
-- other attempt is open, in flight, or accepted without an outcome.
CREATE FUNCTION clinic_app.comms_claim_send_permit(requested uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE target clinic_app.comms_integrationoperation;
BEGIN
 SELECT * INTO target FROM clinic_app.comms_integrationoperation o
  WHERE o.id = requested AND o.subject_type = 'comms.message'
    AND o.organization_id =
        NULLIF(pg_catalog.current_setting('app.current_tenant', true), '')::uuid;
 IF target.id IS NULL THEN
  RETURN false;
 END IF;
 PERFORM 1 FROM clinic_app.comms_integrationoperation o
  WHERE o.subject_type = 'comms.message' AND o.subject_id = target.subject_id
  ORDER BY o.id FOR UPDATE;
 UPDATE clinic_app.comms_integrationoperation AS o
    SET send_permit = 'released'
  WHERE o.subject_type = 'comms.message' AND o.subject_id = target.subject_id
    AND o.id <> target.id AND o.send_permit IN ('held', 'open')
    AND clinic_app.comms_attempt_refuted(o);
 IF EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
             WHERE o.subject_type = 'comms.message'
               AND o.subject_id = target.subject_id AND o.id <> target.id
               AND (o.status = 'in_progress'
                    OR ((o.send_permit IN ('held', 'open')
                         OR o.provider_reference IS NOT NULL)
                        AND NOT clinic_app.comms_attempt_refuted(o)))) THEN
  RETURN false;
 END IF;
 UPDATE clinic_app.comms_integrationoperation AS o
    SET send_permit = 'held'
  WHERE o.id = target.id AND o.send_permit IS NULL;
 RETURN EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
                 WHERE o.id = target.id AND o.send_permit = 'held');
END $f$;
REVOKE ALL ON FUNCTION clinic_app.comms_claim_send_permit(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_claim_send_permit(uuid) TO clinic_app;

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
                   AND o.attempt_sequence IS NULL AND o.send_permit IS NULL)
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
                  AND (o.status = 'in_progress'
                       OR (o.send_permit = 'held'
                           AND o.provider_reference IS NULL
                           AND NOT clinic_app.comms_attempt_refuted(o)))) THEN
  RAISE EXCEPTION USING
   ERRCODE = '23514',
   CONSTRAINT = 'comms_message_attempt_reconciliation_check',
   MESSAGE = 'message attempts cannot be reconciled this way';
 END IF;
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
