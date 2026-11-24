"""Order signing attempts per document by a database-assigned sequence.

``attempt_sequence`` replaces application timestamps as the only order used
to pick a document's current signing operation. Existing rows are numbered
once: failed rows first, then the document's single unfailed row, so the
operation the unique indexes already single out stays current; failed rows
keep their historical ``(created_at, id)`` order among themselves.
"""

COLUMN_SQL = """
ALTER TABLE clinic_app.prescription_signatureoperation
 ADD COLUMN IF NOT EXISTS attempt_sequence bigint DEFAULT 0 NOT NULL;
"""

# Touches only unnumbered rows, appending after any number already assigned,
# so an interrupted pass resumes without renumbering.
BACKFILL_SQL = """
UPDATE clinic_app.prescription_signatureoperation AS o
   SET attempt_sequence = r.n
  FROM (SELECT z.id,
               coalesce((SELECT max(s.attempt_sequence)
                           FROM clinic_app.prescription_signatureoperation s
                          WHERE s.document_id = z.document_id), 0)
               + row_number() OVER (PARTITION BY z.document_id
                    ORDER BY z.state <> 'failed', z.created_at, z.id) AS n
          FROM clinic_app.prescription_signatureoperation z
         WHERE z.attempt_sequence = 0) AS r
 WHERE o.id = r.id;
"""

GUARD_SQL = """
ALTER TABLE clinic_app.prescription_signatureoperation
 ADD CONSTRAINT prescription_signature_sequence_unique
 UNIQUE (document_id, attempt_sequence);
ALTER TABLE clinic_app.prescription_signatureoperation
 ADD CONSTRAINT prescription_signature_sequence_assigned
 CHECK (attempt_sequence > 0);
-- One unresolved or verified operation per document: a completed signature
-- refuses every new signing request, and an unresolved one any replacement.
CREATE UNIQUE INDEX prescription_signature_settled_unique
 ON clinic_app.prescription_signatureoperation (document_id)
 WHERE state IN ('draft', 'prepared', 'signing', 'signed_unverified',
                 'issued', 'rehearsal_complete');

SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.prescription_signature_sequence()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 -- The service serializes requests per document; a concurrent peer that
 -- computes the same number fails on prescription_signature_sequence_unique.
 NEW.attempt_sequence := coalesce((
   SELECT max(o.attempt_sequence)
     FROM clinic_app.prescription_signatureoperation o
    WHERE o.document_id = NEW.document_id), 0) + 1;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.prescription_signature_sequence() FROM PUBLIC;
-- The owner needs EXECUTE only to bind the trigger, not to keep it.
GRANT EXECUTE ON FUNCTION clinic_app.prescription_signature_sequence()
 TO clinic_owner;
RESET ROLE;
CREATE TRIGGER prescription_signature_sequence BEFORE INSERT
 ON clinic_app.prescription_signatureoperation FOR EACH ROW
 EXECUTE FUNCTION clinic_app.prescription_signature_sequence();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.prescription_signature_sequence()
 FROM clinic_owner;
RESET ROLE;
"""
