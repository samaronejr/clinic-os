"""Todo 27: unscheduled encounters, episodes and durable draft autosave (H-11).

The encounter binding guard now admits exactly two shapes. A scheduled-bound
encounter still needs an ``arrived``/``in_progress`` appointment and carries no
unscheduled reason. An unscheduled encounter has no appointment, a closed
reason and an enrolled patient; its insert policy additionally requires
``has_permission('encounter.open_unscheduled', clinic)``. ``ehr_assigned``
treats the unscheduled encounter's own physician as its assignee, so every
existing clinical policy covers it unchanged.

New tables never store clinical text outside an envelope: the episode title is
an envelope column, draft state holds only counters and editor-session ids,
and receipts hold revisions and a request digest.
"""

from apps.ehr.migrations._arrival_sql import _BINDING_GUARD

UNSCHEDULED_REASONS = ("walk_in", "phone_follow_up", "documentation_only")
_REASONS_SQL = ", ".join(f"'{reason}'" for reason in UNSCHEDULED_REASONS)
NEW_TABLES = (
    "ehr_episode",
    "ehr_episodeencounter",
    "ehr_drafteditstate",
    "ehr_draftsavereceipt",
)

_TENANT = "NULLIF(current_setting('app.current_tenant', true), '')::uuid"
_ACTOR = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"


def _render(text: str) -> str:
    """Expand the fixed tokens; every token value is a literal of this module."""
    return (
        text.replace("__DRAFT_AUTHOR__", _DRAFT_AUTHOR)
        .replace(
            "__EPISODE_ENROLLMENT__", _ENROLLMENT_OF.replace("__ROW__", "ehr_episode")
        )
        .replace("__EP_ENROLLMENT__", _ENROLLMENT_OF.replace("__ROW__", "ep"))
        .replace("__REASONS__", _REASONS_SQL)
        .replace("__TENANT__", _TENANT)
        .replace("__ACTOR__", _ACTOR)
    )


# ---------------------------------------------------------------------------
# Resolver rewrites (owned by clinic_resolver).
# ---------------------------------------------------------------------------
_ASSIGNED = """
CREATE OR REPLACE FUNCTION clinic_app.ehr_assigned(requested_encounter uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.ehr_encounter e
 LEFT JOIN clinic_app.scheduling_appointment a ON a.id = e.appointment_id
 WHERE e.id = requested_encounter
 AND e.organization_id =
   __TENANT__
 AND e.physician_id =
   __ACTOR__
 AND ((e.appointment_id IS NOT NULL AND a.practitioner_id = e.physician_id)
   OR (e.appointment_id IS NULL AND e.unscheduled_reason IN (__REASONS__)))
 AND clinic_app.questionnaire_staff(e.clinic_id, ARRAY['physician']))
$f$;
"""
_ASSIGNED_V1 = """
CREATE OR REPLACE FUNCTION clinic_app.ehr_assigned(requested_encounter uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.ehr_encounter e
 JOIN clinic_app.scheduling_appointment a ON a.id = e.appointment_id
 WHERE e.id = requested_encounter
 AND e.organization_id =
   __TENANT__
 AND e.physician_id =
   __ACTOR__
 AND a.practitioner_id = e.physician_id
 AND clinic_app.questionnaire_staff(e.clinic_id, ARRAY['physician']))
$f$;
"""
# The scope answers "this version exists in this clinic for clinic staff"; it
# returned the appointment id, which an unscheduled encounter lacks.
_VERSION_SCOPE_TEMPLATE = """
CREATE OR REPLACE FUNCTION clinic_app.ehr_version_scope(
 requested_clinic uuid, requested_version uuid)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT e.__SCOPE_COLUMN__ FROM clinic_app.ehr_clinicaldocumentversion v
 JOIN clinic_app.ehr_clinicaldocument d ON d.id = v.document_id
 JOIN clinic_app.ehr_encounter e ON e.id = d.encounter_id
 WHERE v.id = requested_version AND e.clinic_id = requested_clinic
 AND e.organization_id =
   __TENANT__
 AND clinic_app.questionnaire_staff(e.clinic_id,
   ARRAY['physician','receptionist','owner','clinic_admin'])
$f$;
"""
_VERSION_SCOPE = _VERSION_SCOPE_TEMPLATE.replace("__SCOPE_COLUMN__", "id")
_VERSION_SCOPE_V1 = _VERSION_SCOPE_TEMPLATE.replace(
    "__SCOPE_COLUMN__", "appointment_id"
)

_ARRIVAL_BRANCH = """ IF TG_TABLE_NAME = 'ehr_encounter' THEN
   IF NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment a
     JOIN clinic_app.intake_patientclinicenrollment p
       ON p.patient_id = a.patient_id AND p.clinic_id = a.clinic_id
       AND p.organization_id = a.organization_id
     WHERE a.id = NEW.appointment_id AND a.organization_id = NEW.organization_id
       AND a.clinic_id = NEW.clinic_id AND a.patient_id = NEW.patient_id
       AND a.practitioner_id = NEW.physician_id AND a.status __ENCOUNTER_STATUSES__)
     OR NEW.state <> 'open' OR NEW.revision <> 1 THEN
"""
_TWO_SHAPE_BRANCH = """ IF TG_TABLE_NAME = 'ehr_encounter' THEN
   IF (NEW.appointment_id IS NOT NULL AND (NEW.unscheduled_reason <> ''
       OR NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment a
     JOIN clinic_app.intake_patientclinicenrollment p
       ON p.patient_id = a.patient_id AND p.clinic_id = a.clinic_id
       AND p.organization_id = a.organization_id
     WHERE a.id = NEW.appointment_id AND a.organization_id = NEW.organization_id
       AND a.clinic_id = NEW.clinic_id AND a.patient_id = NEW.patient_id
       AND a.practitioner_id = NEW.physician_id
       AND a.status IN ('arrived', 'in_progress'))))
     OR (NEW.appointment_id IS NULL AND (
       NEW.unscheduled_reason NOT IN (__REASONS__)
       OR NOT EXISTS (SELECT 1 FROM clinic_app.intake_patientclinicenrollment p
         WHERE p.patient_id = NEW.patient_id AND p.clinic_id = NEW.clinic_id
         AND p.organization_id = NEW.organization_id)))
     OR NEW.state <> 'open' OR NEW.revision <> 1 THEN
"""
if _BINDING_GUARD.count(_ARRIVAL_BRANCH) != 1:  # pragma: no cover - import contract
    message = "arrival binding branch moved"
    raise RuntimeError(message)
_BINDING = _BINDING_GUARD.replace(_ARRIVAL_BRANCH, _TWO_SHAPE_BRANCH, 1)
_BINDING_ARRIVAL = _BINDING_GUARD.replace(
    "__ENCOUNTER_STATUSES__", "IN ('arrived', 'in_progress')", 1
)

_CLOSE_GUARD_TEMPLATE = """
CREATE OR REPLACE FUNCTION clinic_app.ehr_encounter_close_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF __IMMUTABLE__
    OR OLD.state <> 'open' OR NEW.state <> 'closed'
    OR NEW.closed_at IS NULL THEN
   RAISE EXCEPTION 'invalid encounter transition' USING ERRCODE = '23514';
 END IF;
 -- Serialize against in-flight version inserts: they take the same
 -- transaction-scoped advisory lock before checking this encounter's state,
 -- so a live draft here is a real contract violation, not a stale read.
 PERFORM pg_catalog.pg_advisory_xact_lock(
   hashtextextended('ehr-encounter:' || OLD.id::text, 0));
 IF EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
   JOIN clinic_app.ehr_clinicaldocument d ON d.id = v.document_id
   WHERE d.encounter_id = OLD.id AND v.state = 'draft') THEN
   RAISE EXCEPTION 'draft_in_progress' USING ERRCODE = '23514';
 END IF;
 RETURN NEW;
END
$f$;
"""
# Every column except the two closure columns is immutable, including the
# plan item 27 unscheduled reason and any column a later migration adds.
_CLOSE_GUARD = _CLOSE_GUARD_TEMPLATE.replace(
    "__IMMUTABLE__",
    "(pg_catalog.to_jsonb(NEW) - ARRAY['state','closed_at'])\n"
    "    IS DISTINCT FROM (pg_catalog.to_jsonb(OLD) - ARRAY['state','closed_at'])",
)
_CLOSE_GUARD_V1 = _CLOSE_GUARD_TEMPLATE.replace(
    "__IMMUTABLE__",
    "(NEW.id,NEW.organization_id,NEW.clinic_id,NEW.appointment_id,NEW.patient_id,\n"
    "     NEW.physician_id,NEW.revision,NEW.created_at)\n"
    "    IS DISTINCT FROM\n"
    "    (OLD.id,OLD.organization_id,OLD.clinic_id,OLD.appointment_id,OLD.patient_id,\n"
    "     OLD.physician_id,OLD.revision,OLD.created_at)",
)

_AUTOSAVE_GUARD = """
CREATE FUNCTION clinic_app.ehr_autosave_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 episode_row clinic_app.ehr_episode;
 encounter_row clinic_app.ehr_encounter;
 version_row clinic_app.ehr_clinicaldocumentversion;
BEGIN
 IF TG_TABLE_NAME = 'ehr_episode' THEN
   IF TG_OP = 'INSERT' THEN
     IF NOT EXISTS (SELECT 1 FROM clinic_app.intake_patientclinicenrollment p
          WHERE p.patient_id = NEW.patient_id AND p.clinic_id = NEW.clinic_id
          AND p.organization_id = NEW.organization_id)
        OR NEW.state <> 'open' OR NEW.closed_at IS NOT NULL
        OR NEW.closed_by_id IS NOT NULL OR NEW.title IS NULL THEN
       RAISE EXCEPTION 'invalid episode binding' USING ERRCODE = '23514';
     END IF;
   ELSIF (pg_catalog.to_jsonb(NEW) - ARRAY['state','closed_at','closed_by_id'])
         IS DISTINCT FROM
         (pg_catalog.to_jsonb(OLD) - ARRAY['state','closed_at','closed_by_id'])
      OR OLD.state <> 'open' OR NEW.state <> 'closed'
      OR NEW.closed_at IS NULL OR NEW.closed_by_id IS NULL THEN
     RAISE EXCEPTION 'invalid episode transition' USING ERRCODE = '23514';
   END IF;
 ELSIF TG_TABLE_NAME = 'ehr_episodeencounter' THEN
   SELECT * INTO episode_row FROM clinic_app.ehr_episode
     WHERE id = NEW.episode_id;
   SELECT * INTO encounter_row FROM clinic_app.ehr_encounter
     WHERE id = NEW.encounter_id;
   IF episode_row.id IS NULL OR encounter_row.id IS NULL
      OR episode_row.organization_id <> NEW.organization_id
      OR encounter_row.organization_id <> NEW.organization_id
      OR episode_row.clinic_id <> encounter_row.clinic_id
      OR episode_row.patient_id <> encounter_row.patient_id
      OR episode_row.state <> 'open' THEN
     RAISE EXCEPTION 'invalid episode membership' USING ERRCODE = '23514';
   END IF;
 ELSE
   SELECT * INTO version_row FROM clinic_app.ehr_clinicaldocumentversion
     WHERE id = NEW.version_id;
   IF version_row.id IS NULL
      OR version_row.organization_id <> NEW.organization_id
      OR version_row.state <> 'draft' THEN
     RAISE EXCEPTION 'invalid draft binding' USING ERRCODE = '23514';
   END IF;
   IF TG_TABLE_NAME = 'ehr_draftsavereceipt' THEN
     -- A receipt acknowledges the revision the same transaction committed.
     IF NEW.revision <> version_row.revision
        OR NEW.request_sha256 !~ '^[0-9a-f]{64}$' THEN
       RAISE EXCEPTION 'invalid autosave receipt' USING ERRCODE = '23514';
     END IF;
   ELSE
     IF NEW.author_id <> version_row.author_id
        OR pg_catalog.jsonb_typeof(NEW.section_edit_epochs) <> 'object'
        OR (SELECT pg_catalog.array_agg(k ORDER BY k)
            FROM pg_catalog.jsonb_object_keys(NEW.section_edit_epochs) k)
           IS DISTINCT FROM ARRAY['assessment','objective','plan','subjective']
        OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_each(NEW.section_edit_epochs) e
           WHERE pg_catalog.jsonb_typeof(e.value) <> 'number'
           OR (e.value)::numeric < 0 OR (e.value)::numeric > 2147483647
           OR (e.value)::numeric <> pg_catalog.trunc((e.value)::numeric)) THEN
       RAISE EXCEPTION 'invalid draft state' USING ERRCODE = '23514';
     END IF;
     IF TG_OP = 'UPDATE' AND (
        (NEW.id,NEW.organization_id,NEW.version_id,NEW.author_id)
        IS DISTINCT FROM (OLD.id,OLD.organization_id,OLD.version_id,OLD.author_id)
        OR EXISTS (SELECT 1 FROM pg_catalog.jsonb_each(NEW.section_edit_epochs) n
           JOIN pg_catalog.jsonb_each(OLD.section_edit_epochs) o ON o.key = n.key
           WHERE (n.value)::numeric < (o.value)::numeric)) THEN
       RAISE EXCEPTION 'draft epochs only move forward' USING ERRCODE = '23514';
     END IF;
   END IF;
 END IF;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.ehr_autosave_guard() FROM PUBLIC;
"""


_ENROLLMENT_OF = """(SELECT p.id FROM clinic_app.intake_patientclinicenrollment p
 WHERE p.clinic_id = __ROW__.clinic_id AND p.patient_id = __ROW__.patient_id
 AND p.organization_id = __ROW__.organization_id)"""


_DRAFT_AUTHOR = """EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
 JOIN clinic_app.ehr_clinicaldocument d ON d.id = v.document_id
 WHERE v.id = version_id AND v.state = 'draft'
 AND v.author_id = __ACTOR__
 AND clinic_app.ehr_assigned(d.encounter_id))"""

_TABLE_SQL = """
DROP POLICY clinical_insert ON clinic_app.ehr_encounter;
CREATE POLICY clinical_insert ON clinic_app.ehr_encounter FOR INSERT TO clinic_app
 WITH CHECK (organization_id =
   __TENANT__
 AND physician_id = __ACTOR__
 AND clinic_app.questionnaire_staff(clinic_id, ARRAY['physician'])
 AND (appointment_id IS NOT NULL
   OR clinic_app.has_permission('encounter.open_unscheduled', clinic_id, NULL)));
GRANT UPDATE (state, closed_at, closed_by_id) ON clinic_app.ehr_episode TO clinic_app;
GRANT UPDATE (section_edit_epochs, lock_holder, lock_expires_at,
 handover_requested_by, updated_at) ON clinic_app.ehr_drafteditstate TO clinic_app;
CREATE POLICY episode_read ON clinic_app.ehr_episode FOR SELECT TO clinic_app
 USING (organization_id = __TENANT__
 AND clinic_app.has_permission('clinical.read', clinic_id,
   __EPISODE_ENROLLMENT__));
CREATE POLICY episode_insert ON clinic_app.ehr_episode FOR INSERT TO clinic_app
 WITH CHECK (organization_id = __TENANT__
 AND opened_by_id = __ACTOR__
 AND clinic_app.has_permission('clinical.write', clinic_id,
   __EPISODE_ENROLLMENT__));
CREATE POLICY episode_close ON clinic_app.ehr_episode FOR UPDATE TO clinic_app
 USING (state = 'open' AND clinic_app.has_permission('clinical.write', clinic_id,
   __EPISODE_ENROLLMENT__))
 WITH CHECK (closed_by_id = __ACTOR__
 AND clinic_app.has_permission('clinical.write', clinic_id,
   __EPISODE_ENROLLMENT__));
CREATE POLICY episode_member_read ON clinic_app.ehr_episodeencounter
 FOR SELECT TO clinic_app
 USING (organization_id = __TENANT__
 AND EXISTS (SELECT 1 FROM clinic_app.ehr_episode ep WHERE ep.id = episode_id));
CREATE POLICY episode_member_insert ON clinic_app.ehr_episodeencounter
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id = __TENANT__
 AND linked_by_id = __ACTOR__
 AND clinic_app.ehr_assigned(encounter_id)
 AND EXISTS (SELECT 1 FROM clinic_app.ehr_episode ep WHERE ep.id = episode_id
   AND ep.state = 'open'
   AND clinic_app.has_permission('clinical.write', ep.clinic_id,
     __EP_ENROLLMENT__)));
CREATE POLICY draft_state_author ON clinic_app.ehr_drafteditstate TO clinic_app
 USING (organization_id = __TENANT__
 AND author_id = __ACTOR__ AND __DRAFT_AUTHOR__)
 WITH CHECK (organization_id = __TENANT__
 AND author_id = __ACTOR__ AND __DRAFT_AUTHOR__);
CREATE POLICY draft_receipt_read ON clinic_app.ehr_draftsavereceipt
 FOR SELECT TO clinic_app
 USING (organization_id = __TENANT__ AND __DRAFT_AUTHOR__);
CREATE POLICY draft_receipt_insert ON clinic_app.ehr_draftsavereceipt
 FOR INSERT TO clinic_app
 WITH CHECK (organization_id = __TENANT__ AND __DRAFT_AUTHOR__);
"""


def _table_posture(table: str, grants: str) -> str:
    return """
ALTER TABLE clinic_app.__TABLE__ ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.__TABLE__ FORCE ROW LEVEL SECURITY;
CREATE POLICY setup_tenant ON clinic_app.__TABLE__ TO clinic_owner
 USING (organization_id = __TENANT__)
 WITH CHECK (organization_id = __TENANT__);
REVOKE ALL ON clinic_app.__TABLE__ FROM PUBLIC, clinic_app;
GRANT __GRANTS__ ON clinic_app.__TABLE__ TO clinic_app;
""".replace("__TABLE__", table).replace("__GRANTS__", grants)


_TRIGGERS = """
CREATE TRIGGER ehr_autosave_binding BEFORE INSERT OR UPDATE
 ON clinic_app.ehr_episode FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ehr_autosave_guard();
CREATE TRIGGER ehr_autosave_immutable BEFORE DELETE
 ON clinic_app.ehr_episode FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE TRIGGER ehr_autosave_binding BEFORE INSERT
 ON clinic_app.ehr_episodeencounter FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ehr_autosave_guard();
CREATE TRIGGER ehr_autosave_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.ehr_episodeencounter FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE TRIGGER ehr_autosave_binding BEFORE INSERT OR UPDATE
 ON clinic_app.ehr_drafteditstate FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ehr_autosave_guard();
CREATE TRIGGER ehr_autosave_immutable BEFORE DELETE
 ON clinic_app.ehr_drafteditstate FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE TRIGGER ehr_autosave_binding BEFORE INSERT
 ON clinic_app.ehr_draftsavereceipt FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ehr_autosave_guard();
CREATE TRIGGER ehr_autosave_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.ehr_draftsavereceipt FOR EACH ROW
 EXECUTE FUNCTION clinic_app.questionnaire_immutable();
"""

# The guard reads episodes while binding memberships.
_RESOLVER_READS = "GRANT SELECT ON clinic_app.ehr_episode TO clinic_resolver;\n"

SQL = _render(
    _RESOLVER_READS
    + "SET LOCAL ROLE clinic_resolver;\n"
    + _ASSIGNED
    + _VERSION_SCOPE
    + _BINDING
    + _CLOSE_GUARD
    + _AUTOSAVE_GUARD
    + "GRANT EXECUTE ON FUNCTION clinic_app.ehr_autosave_guard(),\n"
    " clinic_app.questionnaire_immutable() TO clinic_owner;\n"
    "RESET ROLE;\n"
    + _table_posture("ehr_episode", "SELECT, INSERT")
    + _table_posture("ehr_episodeencounter", "SELECT, INSERT")
    + _table_posture("ehr_drafteditstate", "SELECT, INSERT")
    + _table_posture("ehr_draftsavereceipt", "SELECT, INSERT")
    + _TRIGGERS
    + _TABLE_SQL
    + "SET LOCAL ROLE clinic_resolver;\n"
    "REVOKE EXECUTE ON FUNCTION clinic_app.ehr_autosave_guard(),\n"
    " clinic_app.questionnaire_immutable() FROM clinic_owner;\n"
    "RESET ROLE;\n"
)

# Rollback refuses once an unscheduled encounter exists (the column cannot
# become NOT NULL again); recovery is then a restore, never a data rewrite.
# The model operations drop the new tables after this SQL removes the triggers
# that depend on the guard function.
REVERSE_SQL = _render(
    "".join(
        f"DROP TRIGGER ehr_autosave_binding ON clinic_app.{table};\n"
        f"DROP TRIGGER ehr_autosave_immutable ON clinic_app.{table};\n"
        for table in NEW_TABLES
    )
    + """
DROP POLICY clinical_insert ON clinic_app.ehr_encounter;
CREATE POLICY clinical_insert ON clinic_app.ehr_encounter FOR INSERT TO clinic_app
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND physician_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND clinic_app.questionnaire_staff(clinic_id, ARRAY['physician']));
"""
    + "SET LOCAL ROLE clinic_resolver;\n"
    "DROP FUNCTION clinic_app.ehr_autosave_guard();\n"
    + _ASSIGNED_V1
    + _VERSION_SCOPE_V1
    + _BINDING_ARRIVAL
    + _CLOSE_GUARD_V1
    + "RESET ROLE;\n"
)
