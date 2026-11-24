"""Reserve a message's single send permit in the database before any send.

Every ``comms.message`` attempt that may reach the provider first takes the
message's permit: ``send_permit = 'held'`` on that attempt, claimed by the
worker's final recheck under the subject and boundary session locks and
committed before the provider call. A partial unique index admits one held
permit per message, so no second send can even start while an accepted or
unresolved attempt is open. The permit is released (``'released'``) only by
the holder's own recorded outcome: a terminal failure or cancellation, a bound
failed receipt, or the next claimer finding exactly that evidence.

Whether an attempt is open or refuted is derived only from that attempt's own
record (``comms_attempt_refuted``), never from its sequence position; a
failure projected onto the message counts only for the original attempt, the
one enqueued under the message's own ``command_id``.

Legacy messages: a base revision admitted a retry only after it showed the
sole original as failed, so an original with any successor is released here
(the release is the record; no receipt is invented). A numbered attempt that is
open at upgrade holds the permit; so does an unnumbered attempt a base worker
left in flight, whose outcome no one can know.

A held legacy message is released only by a staff decision that never writes
an attempt sequence: under the message lock it cancels every queued attempt
and every claim that never held a permit (provably unsent), keeps the chosen
accepted attempt as the permit holder when it is unrefuted, and releases all
others. Naming a refuted attempt is refused while another accepted attempt is
unrefuted, and "none accepted" only when no attempt holds a reference.
"""

COLUMN_SQL = """
ALTER TABLE clinic_app.comms_integrationoperation
 ADD COLUMN IF NOT EXISTS send_permit text;

CREATE OR REPLACE FUNCTION clinic_app.comms_attempt_refuted(
  attempt clinic_app.comms_integrationoperation
) RETURNS boolean LANGUAGE sql STABLE
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT attempt.status IN ('failed', 'cancelled')
  OR attempt.send_permit IS NOT DISTINCT FROM 'released'
  OR EXISTS (SELECT 1 FROM clinic_app.comms_inboundevent e
              WHERE e.operation_id = attempt.id AND e.delivery_state = 'failed')
  OR EXISTS (SELECT 1 FROM clinic_app.comms_message m
              WHERE m.id = attempt.subject_id
                AND m.command_id = attempt.idempotency_key
                AND m.provider_state = 'failed')
$f$;
"""

# Touches only rows whose permit is still unset, so a resumed pass is a no-op.
BACKFILL_SQL = """
UPDATE clinic_app.comms_integrationoperation AS o
   SET send_permit = 'released'
  FROM clinic_app.comms_message m
 WHERE o.subject_type = 'comms.message' AND m.id = o.subject_id
   AND o.send_permit IS NULL AND o.idempotency_key = m.command_id
   AND EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation s
                WHERE s.subject_type = 'comms.message'
                  AND s.subject_id = o.subject_id AND s.id <> o.id);

UPDATE clinic_app.comms_integrationoperation AS o
   SET send_permit = 'held'
 WHERE o.subject_type = 'comms.message' AND o.send_permit IS NULL
   AND ((o.attempt_sequence IS NOT NULL
         AND (o.provider_reference IS NOT NULL OR o.status = 'in_progress')
         AND NOT clinic_app.comms_attempt_refuted(o))
        OR (o.attempt_sequence IS NULL AND o.status = 'in_progress'));
"""

GUARD_SQL = """
ALTER TABLE clinic_app.comms_integrationoperation
 ADD CONSTRAINT comms_operation_send_permit_check
 CHECK (send_permit IN ('held', 'released')
        AND subject_type = 'comms.message' OR send_permit IS NULL);
CREATE UNIQUE INDEX comms_operation_send_permit_uniq
 ON clinic_app.comms_integrationoperation (subject_type, subject_id)
 WHERE send_permit = 'held';

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
  -- Backstop to the permit: a provider reference is bound only while every
  -- other accepted attempt carries its own negative evidence.
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
 -- Every attempt, not only the latest, must be closed by its own outcome.
 -- A concurrent peer computes the same number and fails on
 -- comms_operation_attempt_sequence_uniq.
 IF EXISTS (SELECT 1 FROM clinic_app.comms_message m
             WHERE m.id = NEW.subject_id
               AND m.provider_state IN ('delivered', 'read'))
  OR EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
              WHERE o.subject_type = NEW.subject_type
                AND o.subject_id = NEW.subject_id
                AND (o.status IN ('pending', 'in_progress', 'delivered')
                     OR ((o.send_permit = 'held'
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

GRANT UPDATE (send_permit) ON clinic_app.comms_integrationoperation
 TO clinic_app;
GRANT UPDATE (status, last_error, updated_at, send_permit)
 ON clinic_app.comms_integrationoperation TO clinic_resolver;
GRANT SELECT (operation_id, delivery_state) ON clinic_app.comms_inboundevent
 TO clinic_resolver;
GRANT EXECUTE ON FUNCTION
 clinic_app.comms_attempt_refuted(clinic_app.comms_integrationoperation)
 TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
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
 -- without one (a live worker blocks this decision on the subject lock; a
 -- dead one left it behind) provably never reached the provider.
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
 -- Release every other attempt first, so the chosen one can hold the
 -- message's single permit.
 UPDATE clinic_app.comms_integrationoperation AS o
    SET send_permit = 'released'
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
