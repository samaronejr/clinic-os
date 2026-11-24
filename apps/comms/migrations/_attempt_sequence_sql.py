"""Order message attempts by a database-assigned sequence and gate the next.

Every ``comms.message`` outbox row carries ``attempt_sequence``; the highest
is the message's current attempt. Attempts that predate the sequence are
ordered only by a link the send itself recorded, never by a clock or a UUID:
``send_message`` enqueues the original attempt under the message's own
``command_id`` in the message's insert transaction, and every other attempt
is a later retry. So a message with one attempt, or with that original plus a
single retry, has a causal order. Anything else (several retries, or no
attempt bound to the send) is left unnumbered: the message is held for staff
reconciliation, no attempt of it may send, and no successor is admitted until
a staff member records which attempt the provider confirms as current.

A further attempt is admitted only when the current one carries its own
negative evidence and no attempt or legacy projection proves delivery, so at
most one provider-accepted attempt per message can be open at a time.
"""

COLUMN_SQL = """
ALTER TABLE clinic_app.comms_integrationoperation
 ADD COLUMN IF NOT EXISTS attempt_sequence bigint;
"""

# Numbers only messages whose order the send recorded and none of whose
# attempts is numbered yet, so an interrupted pass resumes unchanged.
BACKFILL_SQL = """
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
                         AS original
                  FROM clinic_app.comms_integrationoperation s
                  JOIN clinic_app.comms_message sm ON sm.id = s.subject_id
                 WHERE s.subject_type = 'comms.message'
                 GROUP BY s.subject_id) AS c ON c.subject_id = z.subject_id
         WHERE z.subject_type = 'comms.message'
           AND c.numbered = 0
           AND (c.total = 1 OR (c.total = 2 AND c.original = 1))) AS r
 WHERE o.id = r.id;
"""

GUARD_SQL = """
ALTER TABLE clinic_app.comms_integrationoperation
 ADD CONSTRAINT comms_operation_attempt_sequence_uniq
 UNIQUE (subject_type, subject_id, attempt_sequence);
ALTER TABLE clinic_app.comms_integrationoperation
 ADD CONSTRAINT comms_operation_attempt_sequence_scope
 CHECK (subject_type = 'comms.message' OR attempt_sequence IS NULL);

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
  OR NOT (current_attempt.status IN ('failed', 'cancelled')
   OR (current_attempt.status = 'succeeded' AND (
     EXISTS (SELECT 1 FROM clinic_app.comms_inboundevent e
              WHERE e.operation_id = current_attempt.id
                AND e.delivery_state = 'failed')
     -- A pre-attribution failure names no attempt: it counts only for a
     -- message that never had a second one.
     OR (current_attempt.attempt_sequence = 1 AND EXISTS (
       SELECT 1 FROM clinic_app.comms_message m
        WHERE m.id = NEW.subject_id AND m.provider_state = 'failed')))))
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
SET LOCAL ROLE clinic_resolver;
-- Records a staff member's decision for an unordered legacy message: the
-- chosen attempt becomes current; the others precede it. Their mutual order
-- is not evidence, it only fills the numbers.
CREATE FUNCTION clinic_app.comms_reconcile_attempts(
  requested_message uuid, requested_current uuid
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE scope clinic_app.comms_integrationoperation;
BEGIN
 SELECT * INTO scope FROM clinic_app.comms_integrationoperation o
  WHERE o.id = requested_current AND o.subject_type = 'comms.message'
    AND o.subject_id = requested_message
    AND o.organization_id =
        NULLIF(pg_catalog.current_setting('app.current_tenant', true), '')::uuid
  FOR UPDATE;
 IF scope.id IS NULL OR NOT clinic_app.questionnaire_staff(scope.clinic_id,
     ARRAY['clinic_admin', 'clinic_manager', 'receptionist', 'scheduler',
           'nurse', 'physician']) THEN
  RAISE EXCEPTION 'message attempts unavailable' USING ERRCODE = '42501';
 END IF;
 PERFORM 1 FROM clinic_app.comms_integrationoperation o
  WHERE o.subject_type = 'comms.message' AND o.subject_id = requested_message
  FOR UPDATE;
 IF EXISTS (SELECT 1 FROM clinic_app.comms_integrationoperation o
             WHERE o.subject_type = 'comms.message'
               AND o.subject_id = requested_message
               AND o.attempt_sequence IS NOT NULL)
    OR (SELECT count(*) FROM clinic_app.comms_integrationoperation o
         WHERE o.subject_type = 'comms.message'
           AND o.subject_id = requested_message) < 2 THEN
  RAISE EXCEPTION USING
   ERRCODE = '23514',
   CONSTRAINT = 'comms_message_attempt_reconciliation_check',
   MESSAGE = 'message attempt order is not awaiting reconciliation';
 END IF;
 UPDATE clinic_app.comms_integrationoperation AS o
    SET attempt_sequence = r.n
   FROM (SELECT z.id, row_number() OVER (
                  ORDER BY z.id = requested_current, z.id) AS n
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
