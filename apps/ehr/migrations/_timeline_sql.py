"""Durable metadata projection, independent of an editing actor's authority."""

SOURCE_ROWS = """
 SELECT m.id source_id, 'medication'::varchar kind, m.organization_id,m.clinic_id,
 m.patient_id,m.effective_at,m.provenance,m.revision source_revision,
 NULL::uuid encounter_id,NULL::uuid episode_id FROM clinic_app.ehr_medicationstatement m
 UNION ALL
 SELECT o.id,'observation',o.organization_id,o.clinic_id,o.patient_id,o.effective_at,
 o.provenance,o.revision,NULL,NULL FROM clinic_app.ehr_observation o
 UNION ALL
 SELECT v.id,'note',e.organization_id,e.clinic_id,e.patient_id,
 coalesce(v.finalized_at,v.created_at),
 CASE WHEN v.state IN ('finalized','superseded') THEN 'clinician_confirmed'
 ELSE 'observed' END,v.revision,e.id,l.episode_id
 FROM clinic_app.ehr_clinicaldocumentversion v
 JOIN clinic_app.ehr_clinicaldocument d ON d.id=v.document_id
 JOIN clinic_app.ehr_encounter e ON e.id=d.encounter_id
 LEFT JOIN clinic_app.ehr_episodeencounter l ON l.encounter_id=e.id
 UNION ALL
 SELECT e.id,'encounter',e.organization_id,e.clinic_id,e.patient_id,e.created_at,
 'observed',e.revision,e.id,l.episode_id FROM clinic_app.ehr_encounter e
 LEFT JOIN clinic_app.ehr_episodeencounter l ON l.encounter_id=e.id
 UNION ALL
 SELECT a.id,'addendum',a.organization_id,a.clinic_id,a.patient_id,a.updated_at,
 'observed',a.revision,a.encounter_id,l.episode_id
 FROM clinic_app.ehr_encounteraddendum a
 LEFT JOIN clinic_app.ehr_episodeencounter l ON l.encounter_id=a.encounter_id
"""

FUNCTIONS = """
GRANT INSERT ON clinic_app.ehr_timelinerefresh, clinic_app.ehr_timelineprojection
 TO clinic_resolver;
GRANT UPDATE (revision,refreshed_revision,refreshed_at)
 ON clinic_app.ehr_timelinerefresh TO clinic_resolver;
GRANT UPDATE (effective_at,provenance,source_revision,encounter_id,episode_id)
 ON clinic_app.ehr_timelineprojection TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ehr_timeline_visible(
 source_kind text, source uuid, requested_clinic uuid, requested_patient uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT CASE source_kind
 WHEN 'observation' THEN EXISTS (SELECT 1 FROM clinic_app.ehr_observation o
  WHERE o.id=source AND o.organization_id=__TENANT__
  AND o.clinic_id=requested_clinic AND o.patient_id=requested_patient
  AND (clinic_app.ehr_patient_permission('clinical.read',o.clinic_id,o.patient_id)
   OR clinic_app.ehr_patient_permission('observation.write',o.clinic_id,o.patient_id)))
 WHEN 'medication' THEN EXISTS (SELECT 1 FROM clinic_app.ehr_medicationstatement m
  WHERE m.id=source AND m.organization_id=__TENANT__
  AND m.clinic_id=requested_clinic AND m.patient_id=requested_patient
  AND clinic_app.ehr_patient_permission('clinical.read',m.clinic_id,m.patient_id))
 WHEN 'note' THEN EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
  JOIN clinic_app.ehr_clinicaldocument d ON d.id=v.document_id
  JOIN clinic_app.ehr_encounter e ON e.id=d.encounter_id
  WHERE v.id=source AND e.organization_id=__TENANT__
  AND e.clinic_id=requested_clinic AND e.patient_id=requested_patient
  AND clinic_app.ehr_patient_permission('clinical.read',e.clinic_id,e.patient_id)
  AND clinic_app.ehr_note_visible(d.id)
  AND ((v.state='draft' AND v.author_id=__ACTOR__ AND clinic_app.ehr_assigned(e.id))
   OR (v.state IN ('finalized','superseded')
     AND (clinic_app.ehr_assigned(e.id) OR clinic_app.ehr_care(e.id)))))
 WHEN 'encounter' THEN EXISTS (SELECT 1 FROM clinic_app.ehr_encounter e
  WHERE e.id=source AND e.organization_id=__TENANT__
  AND e.clinic_id=requested_clinic AND e.patient_id=requested_patient
  AND clinic_app.ehr_patient_permission('clinical.read',e.clinic_id,e.patient_id))
 WHEN 'addendum' THEN EXISTS (SELECT 1 FROM clinic_app.ehr_encounteraddendum a
  WHERE a.id=source AND a.organization_id=__TENANT__
  AND a.clinic_id=requested_clinic AND a.patient_id=requested_patient
  AND a.author_id=__ACTOR__
  AND clinic_app.ehr_patient_permission('clinical.read',a.clinic_id,a.patient_id))
 ELSE false END
$f$;
CREATE FUNCTION clinic_app.ehr_timeline_enqueue()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE scope_row record;
BEGIN
 IF TG_TABLE_NAME='ehr_clinicaldocumentversion' THEN
   SELECT e.organization_id,e.clinic_id,e.patient_id INTO scope_row
   FROM clinic_app.ehr_clinicaldocument d
   JOIN clinic_app.ehr_encounter e ON e.id=d.encounter_id WHERE d.id=NEW.document_id;
 ELSIF TG_TABLE_NAME='ehr_episodeencounter' THEN
   SELECT organization_id,clinic_id,patient_id INTO scope_row
   FROM clinic_app.ehr_encounter WHERE id=NEW.encounter_id;
 ELSE
   SELECT NEW.organization_id,NEW.clinic_id,NEW.patient_id INTO scope_row;
 END IF;
 IF scope_row.organization_id IS NULL OR NOT EXISTS (
  SELECT 1 FROM clinic_app.intake_patientclinicenrollment p
  WHERE p.organization_id=scope_row.organization_id
  AND p.clinic_id=scope_row.clinic_id AND p.patient_id=scope_row.patient_id) THEN
   RAISE EXCEPTION 'invalid projection scope' USING ERRCODE='23514';
 END IF;
 INSERT INTO clinic_app.ehr_timelinerefresh
 (id,organization_id,clinic_id,patient_id,revision,refreshed_revision,refreshed_at)
 VALUES (gen_random_uuid(),scope_row.organization_id,scope_row.clinic_id,
 scope_row.patient_id,1,0,NULL)
 ON CONFLICT (clinic_id,patient_id) DO UPDATE
 SET revision=ehr_timelinerefresh.revision+1;
 RETURN NEW;
END $f$;
CREATE FUNCTION clinic_app.ehr_timeline_due()
RETURNS SETOF uuid LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT id FROM clinic_app.ehr_timelinerefresh
 WHERE revision>refreshed_revision
 AND NULLIF(current_setting('app.current_user_id',true),'') IS NULL
 AND NULLIF(current_setting('app.current_tenant',true),'') IS NULL
 AND NULLIF(current_setting('app.current_patient_session',true),'') IS NULL
 AND NULLIF(current_setting('app.current_principal',true),'') IS NULL
 ORDER BY id LIMIT 1000
$f$;
CREATE FUNCTION clinic_app.ehr_timeline_refresh(requested_refresh uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE q clinic_app.ehr_timelinerefresh;
BEGIN
 IF NULLIF(current_setting('app.current_user_id',true),'') IS NOT NULL
 OR NULLIF(current_setting('app.current_tenant',true),'') IS NOT NULL
 OR NULLIF(current_setting('app.current_patient_session',true),'') IS NOT NULL
 OR NULLIF(current_setting('app.current_principal',true),'') IS NOT NULL THEN
   RETURN false;
 END IF;
 SELECT * INTO q FROM clinic_app.ehr_timelinerefresh WHERE id=requested_refresh
 AND revision>refreshed_revision FOR UPDATE SKIP LOCKED;
 IF q.id IS NULL THEN RETURN false; END IF;
 INSERT INTO clinic_app.ehr_timelineprojection
 (id,organization_id,clinic_id,patient_id,source_id,kind,effective_at,provenance,
 source_revision,encounter_id,episode_id)
 SELECT gen_random_uuid(),s.organization_id,s.clinic_id,s.patient_id,s.source_id,
 s.kind,s.effective_at,s.provenance,s.source_revision,s.encounter_id,s.episode_id
 FROM (__SOURCE_ROWS__) s WHERE s.organization_id=q.organization_id
 AND s.clinic_id=q.clinic_id AND s.patient_id=q.patient_id
 ON CONFLICT (kind,source_id) DO UPDATE SET
 effective_at=excluded.effective_at,provenance=excluded.provenance,
 source_revision=excluded.source_revision,encounter_id=excluded.encounter_id,
 episode_id=excluded.episode_id
 WHERE (ehr_timelineprojection.organization_id,ehr_timelineprojection.clinic_id,
        ehr_timelineprojection.patient_id) =
       (excluded.organization_id,excluded.clinic_id,excluded.patient_id);
 UPDATE clinic_app.ehr_timelinerefresh SET refreshed_revision=q.revision,
 refreshed_at=statement_timestamp() WHERE id=q.id;
 RETURN true;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.ehr_timeline_visible(text,uuid,uuid,uuid),
 clinic_app.ehr_timeline_enqueue(),clinic_app.ehr_timeline_due(),
 clinic_app.ehr_timeline_refresh(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ehr_timeline_visible(text,uuid,uuid,uuid),
 clinic_app.ehr_timeline_due(),clinic_app.ehr_timeline_refresh(uuid) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.ehr_timeline_enqueue() TO clinic_owner;
RESET ROLE;
""".replace("__SOURCE_ROWS__", SOURCE_ROWS)

POLICIES = """
GRANT SELECT ON clinic_app.ehr_timelineprojection TO clinic_app;
CREATE POLICY timeline_read ON clinic_app.ehr_timelineprojection
 FOR SELECT TO clinic_app USING (organization_id=__TENANT__
 AND clinic_app.ehr_timeline_visible(kind,source_id,clinic_id,patient_id));
"""

TRIGGERS = "".join(
    f"""
CREATE TRIGGER ehr_timeline_changed AFTER INSERT OR UPDATE ON clinic_app.{table}
 FOR EACH ROW EXECUTE FUNCTION clinic_app.ehr_timeline_enqueue();
"""
    for table in (
        "ehr_medicationstatement",
        "ehr_observation",
        "ehr_clinicaldocumentversion",
        "ehr_encounter",
        "ehr_encounteraddendum",
        "ehr_episodeencounter",
    )
)

BACKFILL = """
SET LOCAL ROLE clinic_resolver;
INSERT INTO clinic_app.ehr_timelinerefresh
 (id,organization_id,clinic_id,patient_id,revision,refreshed_revision,refreshed_at)
 SELECT gen_random_uuid(),e.organization_id,e.clinic_id,e.patient_id,1,0,NULL
 FROM clinic_app.ehr_encounter e GROUP BY e.organization_id,e.clinic_id,e.patient_id
 ON CONFLICT (clinic_id,patient_id) DO NOTHING;
RESET ROLE;
"""
