"""Resolver-owned guarded fail transition for teleconsult sessions."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

_SQL = """
GRANT UPDATE (state,revision,started_at,ended_at,failure_reason)
 ON clinic_app.teleconsult_teleconsultsession TO clinic_resolver;
GRANT INSERT ON clinic_app.teleconsult_teleconsultevent TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.teleconsult_fail(
 requested_session uuid, reason text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE s RECORD;
BEGIN
 IF reason NOT IN ('encounter_closed','consent_revoked','room_unavailable') THEN
   RETURN false;
 END IF;
 SELECT * INTO s FROM clinic_app.teleconsult_teleconsultsession
   WHERE id=requested_session FOR UPDATE;
 IF s.id IS NULL OR s.state IN ('ended','failed') THEN
   RETURN false;
 END IF;
 -- Only the session's own stored authority may terminate it: the bound
 -- physician inside the session's tenant, or the bound patient session.
 IF NOT clinic_app.teleconsult_assigned(s.id)
    AND NOT clinic_app.teleconsult_patient_match(s.id) THEN
   RETURN false;
 END IF;
 -- The claimed failure must exist in stored rows, not in the argument.
 IF reason='encounter_closed' THEN
   IF EXISTS (SELECT 1 FROM clinic_app.ehr_encounter e
     WHERE e.id=s.encounter_id AND e.state='open') THEN
     RETURN false;
   END IF;
 ELSIF reason='consent_revoked' THEN
   IF EXISTS (SELECT 1 FROM clinic_app.consent_consentacceptance a
     JOIN clinic_app.consent_consenttext t ON t.id=a.text_id
     WHERE a.id=s.consent_id
     AND NOT EXISTS (SELECT 1 FROM clinic_app.consent_consentrevocation r
       WHERE r.acceptance_id=a.id)
     AND NOT EXISTS (SELECT 1 FROM clinic_app.consent_consenttext newer
       WHERE newer.clinic_id=t.clinic_id AND newer.purpose=t.purpose
       AND newer.version>t.version)) THEN
     RETURN false;
   END IF;
 ELSE
   IF NOT EXISTS (SELECT 1 FROM clinic_app.teleconsult_teleconsultroom r
     JOIN clinic_app.comms_integrationoperation o ON o.id=r.operation_id
     WHERE r.session_id=s.id AND o.status IN ('failed','cancelled')) THEN
     RETURN false;
   END IF;
 END IF;
 UPDATE clinic_app.teleconsult_teleconsultsession
   SET state='failed', failure_reason=reason,
       ended_at=statement_timestamp(), revision=revision+1
   WHERE id=s.id;
 INSERT INTO clinic_app.teleconsult_teleconsultevent
   (organization_id,session_id,kind,actor_role,reason_code,created_at)
   VALUES (s.organization_id,s.id,'failed','',reason,statement_timestamp());
 RETURN true;
END
$f$;
RESET ROLE;
-- ACL changes on resolver-owned functions must run as the resolver: the
-- migration role holds membership with INHERIT FALSE, so revoking or
-- granting as clinic_owner is a silent no-op that leaves PUBLIC EXECUTE.
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.teleconsult_fail(uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.teleconsult_fail(uuid,text)
 TO clinic_app, clinic_owner;
RESET ROLE;
"""

_REVERSE_SQL = """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.teleconsult_fail(uuid,text);
RESET ROLE;
"""


class Migration(migrations.Migration):
    """Persist terminal failures under either staff or patient authority."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("teleconsult", "0002_teleconsult_policy")
    ]
    operations: ClassVar[Sequence[Operation]] = [
        migrations.RunSQL(_SQL, reverse_sql=_REVERSE_SQL)
    ]
