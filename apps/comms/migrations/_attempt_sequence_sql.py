"""Order message attempts by a database-assigned sequence and gate the next.

Every ``comms.message`` outbox row carries ``attempt_sequence``; the highest
is the message's current attempt. Rows that predate it are numbered once:
attempts with a terminal negative status first, then the rest, each group in
its historical ``(created_at, id)`` order, so an attempt that may still have
been accepted is never ranked behind a known failure.

A further attempt is admitted only when the current one carries its own
negative evidence and no attempt or legacy projection proves delivery, so at
most one provider-accepted attempt per message can be open at a time.
"""

COLUMN_SQL = """
ALTER TABLE clinic_app.comms_integrationoperation
 ADD COLUMN IF NOT EXISTS attempt_sequence bigint;
"""

# Touches only unnumbered message rows, appending after any number already
# assigned, so an interrupted pass resumes without renumbering.
BACKFILL_SQL = """
UPDATE clinic_app.comms_integrationoperation AS o
   SET attempt_sequence = r.n
  FROM (SELECT z.id,
               coalesce((SELECT max(s.attempt_sequence)
                           FROM clinic_app.comms_integrationoperation s
                          WHERE s.subject_type = z.subject_type
                            AND s.subject_id = z.subject_id), 0)
               + row_number() OVER (PARTITION BY z.subject_id
                    ORDER BY z.status NOT IN ('failed', 'cancelled'),
                             z.created_at, z.id) AS n
          FROM clinic_app.comms_integrationoperation z
         WHERE z.subject_type = 'comms.message'
           AND z.attempt_sequence IS NULL) AS r
 WHERE o.id = r.id;
"""

GUARD_SQL = """
ALTER TABLE clinic_app.comms_integrationoperation
 ADD CONSTRAINT comms_operation_attempt_sequence_uniq
 UNIQUE (subject_type, subject_id, attempt_sequence);
ALTER TABLE clinic_app.comms_integrationoperation
 ADD CONSTRAINT comms_operation_attempt_sequence_scope
 CHECK ((subject_type = 'comms.message' AND attempt_sequence IS NOT NULL)
        OR (NOT subject_type = 'comms.message' AND attempt_sequence IS NULL));

CREATE FUNCTION clinic_app.comms_message_attempt_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE current_attempt clinic_app.comms_integrationoperation;
BEGIN
 IF NEW.subject_type <> 'comms.message' THEN
  NEW.attempt_sequence := NULL;
  RETURN NEW;
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
CREATE TRIGGER comms_message_attempt_guard BEFORE INSERT
 ON clinic_app.comms_integrationoperation
 FOR EACH ROW EXECUTE FUNCTION clinic_app.comms_message_attempt_guard();
"""
