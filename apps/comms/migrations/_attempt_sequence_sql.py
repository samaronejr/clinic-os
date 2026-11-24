"""Order message attempts by a database-assigned sequence and gate the next.

Every ``comms.message`` outbox row carries ``attempt_sequence``; the highest
is the message's current attempt. Attempts that predate the sequence are
ordered only by evidence the base recorded, never by a clock or a UUID:

* ``send_message`` enqueued the original attempt under the message's own
  ``command_id`` in the message's insert transaction; every other attempt is
  a later retry. A message with one attempt, or with that original plus a
  single retry, has a causal order.
* A WhatsApp status receipt's event id is ``{provider reference}:{status}:
  {timestamp}``, so each legacy WhatsApp receipt is bound to the attempt that
  owns the reference, exactly as new receipts are.

Anything else is left unnumbered and held for staff reconciliation: two or
more retries, an original that cannot be identified, or a retried email/SMS
message whose provider failure was projected onto the message without naming
an attempt. While held no attempt may send and no successor is admitted.

A successor is admitted, and a provider reference bound, only while no other
attempt holds a reference without its own negative evidence, so at most one
provider-accepted attempt per message is open at a time.
"""

COLUMN_SQL = """
ALTER TABLE clinic_app.comms_integrationoperation
 ADD COLUMN IF NOT EXISTS attempt_sequence bigint;
"""

# Binds legacy WhatsApp status receipts to the attempt their own event id
# names, then numbers only messages whose order the send recorded. Both touch
# only rows still pending, so an interrupted pass resumes unchanged.
BACKFILL_SQL = """
UPDATE clinic_app.comms_inboundevent AS e
   SET operation_id = o.id,
       delivery_state = substring(e.event_id
         FROM ':(sent|delivered|read|failed):[^:]*$')
  FROM clinic_app.comms_integrationoperation o
  JOIN clinic_app.comms_message m ON m.id = o.subject_id
  JOIN clinic_app.comms_conversation c ON c.id = m.conversation_id
 WHERE e.kind = 'status' AND e.operation_id IS NULL
   AND e.event_id ~ '^.+:(sent|delivered|read|failed):[^:]*$'
   AND o.subject_type = 'comms.message' AND o.channel = 'whatsapp'
   AND o.organization_id = e.organization_id
   AND c.provider_account_id = e.provider_account_id
   AND o.provider_reference = substring(e.event_id
         FROM '^(.+):(?:sent|delivered|read|failed):[^:]*$');

UPDATE clinic_app.comms_integrationoperation AS o
   SET attempt_sequence = r.n
  FROM (SELECT z.id,
               CASE WHEN z.idempotency_key = m.command_id OR c.total = 1
                    THEN 1 ELSE 2 END AS n
          FROM clinic_app.comms_integrationoperation z
          JOIN clinic_app.comms_message m ON m.id = z.subject_id
          JOIN (SELECT s.subject_id, count(*) AS total,
                       count(s.attempt_sequence) AS numbered,
                       count(*) FILTER (WHERE s.idempotency_key = sm.command_id)
                         AS original,
                       bool_or(s.channel <> 'whatsapp') AS unattributed
                  FROM clinic_app.comms_integrationoperation s
                  JOIN clinic_app.comms_message sm ON sm.id = s.subject_id
                 WHERE s.subject_type = 'comms.message'
                 GROUP BY s.subject_id) AS c ON c.subject_id = z.subject_id
         WHERE z.subject_type = 'comms.message'
           AND c.numbered = 0
           AND (c.total = 1
                OR (c.total = 2 AND c.original = 1
                    AND NOT (c.unattributed AND m.provider_state = 'failed')))) AS r
 WHERE o.id = r.id;
"""

GUARD_SQL = """
ALTER TABLE clinic_app.comms_integrationoperation
 ADD CONSTRAINT comms_operation_attempt_sequence_uniq
 UNIQUE (subject_type, subject_id, attempt_sequence);
ALTER TABLE clinic_app.comms_integrationoperation
 ADD CONSTRAINT comms_operation_attempt_sequence_scope
 CHECK (subject_type = 'comms.message' OR attempt_sequence IS NULL);

-- Whether one attempt carries its own negative evidence: a terminal failure,
-- a bound failed receipt, or (for the sole original) a pre-attribution
-- failure projected onto the message.
CREATE FUNCTION clinic_app.comms_attempt_refuted(
  attempt clinic_app.comms_integrationoperation
) RETURNS boolean LANGUAGE sql STABLE
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT attempt.status IN ('failed', 'cancelled')
  OR EXISTS (SELECT 1 FROM clinic_app.comms_inboundevent e
              WHERE e.operation_id = attempt.id AND e.delivery_state = 'failed')
  OR (attempt.attempt_sequence = 1 AND EXISTS (
       SELECT 1 FROM clinic_app.comms_message m
        WHERE m.id = attempt.subject_id AND m.provider_state = 'failed'))
$f$;
REVOKE ALL ON FUNCTION
 clinic_app.comms_attempt_refuted(clinic_app.comms_integrationoperation)
 FROM PUBLIC;

CREATE FUNCTION clinic_app.comms_message_attempt_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE current_attempt clinic_app.comms_integrationoperation;
BEGIN
 IF TG_OP = 'UPDATE' THEN
  IF OLD.attempt_sequence IS NOT NULL
     AND NEW.attempt_sequence IS DISTINCT FROM OLD.attempt_sequence THEN
   RAISE EXCEPTION USING
    ERRCODE = '23514',
    CONSTRAINT = 'comms_message_attempt_sequence_immutable_check',
    MESSAGE = 'message attempt order is immutable';
  END IF;
  -- A provider reference is bound only while every other accepted attempt
  -- of the message carries its own negative evidence. Nested so the
  -- evidence is read only when a reference is actually being bound.
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
               AND o.attempt_sequence IS NULL) THEN
  RAISE EXCEPTION USING
   ERRCODE = '23514',
   CONSTRAINT = 'comms_message_attempt_reconciliation_check',
   MESSAGE = 'message attempt order awaits staff reconciliation';
 END IF;
 SELECT * INTO current_attempt FROM clinic_app.comms_integrationoperation o
  WHERE o.subject_type = NEW.subject_type AND o.subject_id = NEW.subject_id
  ORDER BY o.attempt_sequence DESC LIMIT 1;
 IF current_attempt.id IS NULL THEN
  NEW.attempt_sequence := 1;
  RETURN NEW;
 END IF;
 -- A concurrent peer that read the same current attempt computes the same
 -- number and fails on comms_operation_attempt_sequence_uniq.
 IF EXISTS (SELECT 1 FROM clinic_app.comms_message m
             WHERE m.id = NEW.subject_id
               AND m.provider_state IN ('delivered', 'read'))
  OR EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
              WHERE o.subject_type = NEW.subject_type
                AND o.subject_id = NEW.subject_id AND o.status = 'delivered')
  OR EXISTS (SELECT 1 FROM clinic_app.comms_inboundevent e
               JOIN clinic_app.comms_integrationoperation o ON o.id = e.operation_id
              WHERE o.subject_type = NEW.subject_type
                AND o.subject_id = NEW.subject_id
                AND e.delivery_state IN ('delivered', 'read'))
  OR current_attempt.status NOT IN ('failed', 'cancelled', 'succeeded')
  OR NOT clinic_app.comms_attempt_refuted(current_attempt)
 THEN
  RAISE EXCEPTION USING
   ERRCODE = '23514',
   CONSTRAINT = 'comms_message_attempt_open_check',
   MESSAGE = 'current message attempt has no negative outcome';
 END IF;
 NEW.attempt_sequence := current_attempt.attempt_sequence + 1;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.comms_message_attempt_guard() FROM PUBLIC;
CREATE TRIGGER comms_message_attempt_guard BEFORE INSERT OR UPDATE
 ON clinic_app.comms_integrationoperation
 FOR EACH ROW EXECUTE FUNCTION clinic_app.comms_message_attempt_guard();

GRANT UPDATE (attempt_sequence) ON clinic_app.comms_integrationoperation
 TO clinic_resolver;
GRANT EXECUTE ON FUNCTION
 clinic_app.comms_attempt_refuted(clinic_app.comms_integrationoperation)
 TO clinic_app;
SET LOCAL ROLE clinic_resolver;
-- Records a staff decision for a held legacy message under the caller's
-- message lock. ``requested_current`` must be an attempt the provider
-- accepted (it holds a reference); NULL records that none was accepted and
-- is allowed only when no attempt holds one. Every queued attempt is
-- cancelled in the same transaction, so none can send after the decision;
-- an attempt a worker holds in progress refuses the decision until that
-- worker's recheck has cancelled it.
CREATE FUNCTION clinic_app.comms_reconcile_attempts(
  requested_message uuid, requested_current uuid
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE scope clinic_app.comms_integrationoperation;
DECLARE chosen uuid;
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
 IF requested_current IS NULL THEN
  SELECT o.id INTO chosen FROM clinic_app.comms_integrationoperation o
   WHERE o.subject_type = 'comms.message' AND o.subject_id = requested_message
     AND NOT EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation a
                      WHERE a.subject_type = 'comms.message'
                        AND a.subject_id = requested_message
                        AND a.provider_reference IS NOT NULL)
   ORDER BY o.id DESC LIMIT 1;
 ELSE
  SELECT o.id INTO chosen FROM clinic_app.comms_integrationoperation o
   WHERE o.id = requested_current AND o.subject_type = 'comms.message'
     AND o.subject_id = requested_message AND o.provider_reference IS NOT NULL;
 END IF;
 IF chosen IS NULL
    OR EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
                WHERE o.subject_type = 'comms.message'
                  AND o.subject_id = requested_message
                  AND (o.attempt_sequence IS NOT NULL
                       OR o.status = 'in_progress'))
    OR (SELECT count(*) FROM clinic_app.comms_integrationoperation o
         WHERE o.subject_type = 'comms.message'
           AND o.subject_id = requested_message) < 2 THEN
  RAISE EXCEPTION USING
   ERRCODE = '23514',
   CONSTRAINT = 'comms_message_attempt_reconciliation_check',
   MESSAGE = 'message attempts cannot be reconciled this way';
 END IF;
 UPDATE clinic_app.comms_integrationoperation AS o
    SET status = 'cancelled', last_error = 'subject_ineligible',
        updated_at = pg_catalog.statement_timestamp()
  WHERE o.subject_type = 'comms.message' AND o.subject_id = requested_message
    AND o.status = 'pending';
 UPDATE clinic_app.comms_integrationoperation AS o
    SET attempt_sequence = r.n
   FROM (SELECT z.id, row_number() OVER (
                  ORDER BY z.id = chosen, z.id) AS n
           FROM clinic_app.comms_integrationoperation z
          WHERE z.subject_type = 'comms.message'
            AND z.subject_id = requested_message) AS r
  WHERE o.id = r.id;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.comms_reconcile_attempts(uuid, uuid)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_reconcile_attempts(uuid, uuid)
 TO clinic_app;
RESET ROLE;
"""
