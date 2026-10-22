"""Restrictive source-note policy, revocable grants and copy review enforcement."""

from apps.ehr.migrations._note_grant_audit_sql import SQL as GRANT_AUDIT

FUNCTIONS = """
GRANT UPDATE (revoked_at) ON clinic_app.ehr_noteaccessgrant TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.ehr_note_grantee(requested_clinic uuid, requested_user uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r
 JOIN clinic_app.identity_user u ON u.id=r.user_id AND u.is_active
 WHERE r.organization_id=__TENANT__ AND r.clinic_id=requested_clinic
 AND r.user_id=requested_user AND r.role='physician' AND r.revoked_at IS NULL)
$f$;
CREATE FUNCTION clinic_app.ehr_note_visible(requested_document uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocument d
 JOIN clinic_app.ehr_encounter e ON e.id=d.encounter_id
 WHERE d.id=requested_document AND e.organization_id=__TENANT__
 AND (NOT EXISTS (SELECT 1 FROM clinic_app.ehr_restrictednote n
                  WHERE n.document_id=d.id)
 OR (clinic_app.ehr_patient_permission('clinical.read',e.clinic_id,e.patient_id)
 AND (e.physician_id=__ACTOR__ OR EXISTS (
  SELECT 1 FROM clinic_app.ehr_restrictednote n
  JOIN clinic_app.ehr_noteaccessgrant g ON g.note_id=n.id
  WHERE n.document_id=d.id AND n.organization_id=e.organization_id
  AND n.clinic_id=e.clinic_id AND g.organization_id=e.organization_id
  AND g.clinic_id=e.clinic_id AND g.grantee_id=__ACTOR__
  AND g.revoked_at IS NULL
  AND clinic_app.ehr_note_grantee(e.clinic_id,g.grantee_id))))))
$f$;
CREATE FUNCTION clinic_app.ehr_note_author(requested_document uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocument d
 JOIN clinic_app.ehr_encounter e ON e.id=d.encounter_id
 WHERE d.id=requested_document AND e.organization_id=__TENANT__
 AND clinic_app.ehr_assigned(e.id)
 AND clinic_app.ehr_patient_permission('clinical.write',e.clinic_id,e.patient_id))
$f$;
CREATE FUNCTION clinic_app.ehr_note_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE d clinic_app.ehr_clinicaldocument;
 e clinic_app.ehr_encounter; n clinic_app.ehr_restrictednote;
 source_e clinic_app.ehr_encounter; target_v clinic_app.ehr_clinicaldocumentversion;
BEGIN
 IF TG_TABLE_NAME='identity_user' THEN
   PERFORM pg_advisory_xact_lock(hashtextextended(
    'ehr-note-recipient:' || NEW.id::text,0));
   UPDATE clinic_app.ehr_noteaccessgrant SET revoked_at=statement_timestamp()
    WHERE grantee_id=NEW.id AND revoked_at IS NULL;
   RETURN NEW;
 ELSIF TG_TABLE_NAME='identity_userclinicrole' THEN
   PERFORM pg_advisory_xact_lock(hashtextextended(
    'ehr-note-recipient:' || NEW.user_id::text,0));
   UPDATE clinic_app.ehr_noteaccessgrant SET revoked_at=statement_timestamp()
    WHERE grantee_id=NEW.user_id AND organization_id=NEW.organization_id
    AND clinic_id=NEW.clinic_id AND revoked_at IS NULL;
   RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='ehr_restrictednote' THEN
   SELECT * INTO d FROM clinic_app.ehr_clinicaldocument WHERE id=NEW.document_id;
   SELECT * INTO e FROM clinic_app.ehr_encounter WHERE id=d.encounter_id;
   IF e.id IS NULL OR e.organization_id<>NEW.organization_id
      OR e.clinic_id<>NEW.clinic_id OR e.physician_id<>NEW.restricted_by_id THEN
     RAISE EXCEPTION 'invalid note restriction' USING ERRCODE='23514';
   END IF;
 ELSIF TG_TABLE_NAME='ehr_noteaccessgrant' THEN
   IF TG_OP='INSERT' THEN
     -- Serialize issuance with user deactivation and membership revocation.
     PERFORM pg_advisory_xact_lock(hashtextextended(
      'ehr-note-recipient:' || NEW.grantee_id::text,0));
   END IF;
   SELECT * INTO n FROM clinic_app.ehr_restrictednote WHERE id=NEW.note_id;
   IF n.id IS NULL OR n.organization_id<>NEW.organization_id
    OR n.clinic_id<>NEW.clinic_id OR n.restricted_by_id<>NEW.granted_by_id
    OR (TG_OP='INSERT' AND NOT EXISTS (
     SELECT 1 FROM clinic_app.identity_userclinicrole r
     JOIN clinic_app.identity_user u ON u.id=r.user_id AND u.is_active
     WHERE r.clinic_id=NEW.clinic_id AND r.organization_id=NEW.organization_id
     AND r.user_id=NEW.grantee_id AND r.role='physician'
     AND r.revoked_at IS NULL)) THEN
     RAISE EXCEPTION 'invalid note grant scope' USING ERRCODE='23514';
   END IF;
   IF TG_OP='INSERT' AND NEW.revoked_at IS NOT NULL
    OR TG_OP='UPDATE' AND (
      (to_jsonb(NEW)-'revoked_at') IS DISTINCT FROM (to_jsonb(OLD)-'revoked_at')
      OR OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL) THEN
     RAISE EXCEPTION 'invalid note grant transition' USING ERRCODE='23514';
   END IF;
 ELSE
   SELECT * INTO target_v FROM clinic_app.ehr_clinicaldocumentversion
    WHERE id=NEW.target_id;
   SELECT * INTO e FROM clinic_app.ehr_encounter
    WHERE id=(SELECT d2.encounter_id FROM clinic_app.ehr_clinicaldocument d2
     WHERE d2.id=target_v.document_id);
   SELECT * INTO source_e FROM clinic_app.ehr_encounter
    WHERE id=(SELECT d2.encounter_id FROM clinic_app.ehr_clinicaldocument d2
     JOIN clinic_app.ehr_clinicaldocumentversion v2 ON v2.document_id=d2.id
     WHERE v2.id=NEW.source_id AND v2.state IN ('finalized','superseded'));
   IF e.id IS NULL OR source_e.id IS NULL OR NEW.source_id=NEW.target_id
     OR e.organization_id<>NEW.organization_id OR e.clinic_id<>NEW.clinic_id
     OR (e.organization_id,e.clinic_id,e.patient_id) IS DISTINCT FROM
        (source_e.organization_id,source_e.clinic_id,source_e.patient_id)
     OR target_v.state<>'draft'
     OR NEW.section NOT IN ('subjective','objective','assessment','plan')
     OR NEW.inserted_revision>target_v.revision
     OR (EXISTS (SELECT 1 FROM clinic_app.ehr_restrictednote r
       JOIN clinic_app.ehr_clinicaldocumentversion v ON v.document_id=r.document_id
       WHERE v.id=NEW.source_id) AND NOT EXISTS (
       SELECT 1 FROM clinic_app.ehr_restrictednote r
       WHERE r.document_id=target_v.document_id)) THEN
     RAISE EXCEPTION 'invalid copy binding' USING ERRCODE='23514';
   END IF;
   IF TG_OP='INSERT' THEN
     IF NEW.reviewed_at IS NOT NULL OR NEW.reviewed_by_id IS NOT NULL
      OR NEW.reviewed_revision IS NOT NULL
      OR NEW.inserted_revision<>target_v.revision THEN
       RAISE EXCEPTION 'copy requires review' USING ERRCODE='23514';
     END IF;
   ELSIF (to_jsonb(NEW)-ARRAY['reviewed_at','reviewed_by_id','reviewed_revision'])
      IS DISTINCT FROM
      (to_jsonb(OLD)-ARRAY['reviewed_at','reviewed_by_id','reviewed_revision'])
      OR NEW.reviewed_at IS NULL OR NEW.reviewed_by_id<>target_v.author_id
      OR NEW.reviewed_revision<>target_v.revision THEN
     RAISE EXCEPTION 'invalid copy review' USING ERRCODE='23514';
   END IF;
 END IF;
 RETURN NEW;
END $f$;
CREATE FUNCTION clinic_app.ehr_copy_finalization_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF NEW.state='finalized' AND OLD.state='draft' AND EXISTS (
 SELECT 1 FROM clinic_app.ehr_copiednoteblock b WHERE b.target_id=NEW.id
 AND (b.reviewed_revision IS DISTINCT FROM NEW.revision
      OR b.reviewed_by_id IS DISTINCT FROM NEW.author_id)) THEN
   RAISE EXCEPTION 'copied_blocks_unreviewed' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.ehr_note_visible(uuid),
 clinic_app.ehr_note_author(uuid), clinic_app.ehr_note_grantee(uuid,uuid),
 clinic_app.ehr_note_guard(),
 clinic_app.ehr_copy_finalization_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ehr_note_visible(uuid),
 clinic_app.ehr_note_author(uuid), clinic_app.ehr_note_grantee(uuid,uuid) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.ehr_note_guard(),
 clinic_app.ehr_copy_finalization_guard() TO clinic_owner;
RESET ROLE;
"""

POLICIES = """
CREATE POLICY restricted_note_visibility ON clinic_app.ehr_clinicaldocumentversion
 AS RESTRICTIVE TO clinic_app
 USING (clinic_app.ehr_note_visible(document_id))
 WITH CHECK (clinic_app.ehr_note_visible(document_id));
GRANT SELECT, INSERT ON clinic_app.ehr_restrictednote,
 clinic_app.ehr_noteaccessgrant, clinic_app.ehr_copiednoteblock TO clinic_app;
GRANT UPDATE (revoked_at) ON clinic_app.ehr_noteaccessgrant TO clinic_app;
GRANT UPDATE (reviewed_at,reviewed_by_id,reviewed_revision)
 ON clinic_app.ehr_copiednoteblock TO clinic_app;
CREATE POLICY note_restriction_read ON clinic_app.ehr_restrictednote
 FOR SELECT TO clinic_app USING (organization_id=__TENANT__
 AND clinic_app.ehr_note_visible(document_id));
CREATE POLICY note_restriction_insert ON clinic_app.ehr_restrictednote
 FOR INSERT TO clinic_app WITH CHECK (organization_id=__TENANT__
 AND restricted_by_id=__ACTOR__ AND clinic_app.ehr_note_author(document_id));
CREATE POLICY note_grant_read ON clinic_app.ehr_noteaccessgrant
 FOR SELECT TO clinic_app USING (organization_id=__TENANT__
 AND (grantee_id=__ACTOR__ OR granted_by_id=__ACTOR__)
 AND EXISTS (SELECT 1 FROM clinic_app.ehr_restrictednote n WHERE n.id=note_id));
CREATE POLICY note_grant_insert ON clinic_app.ehr_noteaccessgrant
 FOR INSERT TO clinic_app WITH CHECK (organization_id=__TENANT__
 AND granted_by_id=__ACTOR__ AND EXISTS (
 SELECT 1 FROM clinic_app.ehr_restrictednote n WHERE n.id=note_id
 AND clinic_app.ehr_note_author(n.document_id)));
CREATE POLICY note_grant_revoke ON clinic_app.ehr_noteaccessgrant
 FOR UPDATE TO clinic_app USING (organization_id=__TENANT__
 AND granted_by_id=__ACTOR__ AND EXISTS (
 SELECT 1 FROM clinic_app.ehr_restrictednote n WHERE n.id=note_id
 AND clinic_app.ehr_note_author(n.document_id)))
 WITH CHECK (organization_id=__TENANT__ AND granted_by_id=__ACTOR__);
CREATE POLICY copy_read ON clinic_app.ehr_copiednoteblock
 FOR SELECT TO clinic_app USING (organization_id=__TENANT__ AND EXISTS (
 SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v WHERE v.id=target_id));
CREATE POLICY copy_insert ON clinic_app.ehr_copiednoteblock
 FOR INSERT TO clinic_app WITH CHECK (organization_id=__TENANT__ AND EXISTS (
 SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v WHERE v.id=target_id
 AND v.author_id=__ACTOR__ AND clinic_app.ehr_note_author(v.document_id))
 AND EXISTS (SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v
 WHERE v.id=source_id AND v.state IN ('finalized','superseded')));
CREATE POLICY copy_review ON clinic_app.ehr_copiednoteblock
 FOR UPDATE TO clinic_app USING (organization_id=__TENANT__ AND EXISTS (
 SELECT 1 FROM clinic_app.ehr_clinicaldocumentversion v WHERE v.id=target_id
 AND v.state='draft' AND v.author_id=__ACTOR__
 AND clinic_app.ehr_note_author(v.document_id)))
 WITH CHECK (organization_id=__TENANT__ AND reviewed_by_id=__ACTOR__);
CREATE TRIGGER ehr_copy_finalize BEFORE UPDATE ON clinic_app.ehr_clinicaldocumentversion
 FOR EACH ROW EXECUTE FUNCTION clinic_app.ehr_copy_finalization_guard();
"""

TRIGGERS = (
    "".join(
        f"""
CREATE TRIGGER ehr_note_binding BEFORE {events} ON clinic_app.{table}
 FOR EACH ROW EXECUTE FUNCTION clinic_app.ehr_note_guard();
"""
        for table, events in (
            ("ehr_restrictednote", "INSERT"),
            ("ehr_noteaccessgrant", "INSERT OR UPDATE"),
            ("ehr_copiednoteblock", "INSERT OR UPDATE"),
        )
    )
    + """
CREATE TRIGGER ehr_restriction_immutable BEFORE UPDATE ON clinic_app.ehr_restrictednote
 FOR EACH ROW EXECUTE FUNCTION clinic_app.questionnaire_immutable();
CREATE TRIGGER ehr_note_recipient_deactivated
 BEFORE UPDATE OF is_active ON clinic_app.identity_user
 FOR EACH ROW WHEN (OLD.is_active AND NOT NEW.is_active)
 EXECUTE FUNCTION clinic_app.ehr_note_guard();
CREATE TRIGGER ehr_note_recipient_revoked
 BEFORE UPDATE OF revoked_at ON clinic_app.identity_userclinicrole
 FOR EACH ROW WHEN (NEW.role='physician' AND OLD.revoked_at IS NULL
                   AND NEW.revoked_at IS NOT NULL)
 EXECUTE FUNCTION clinic_app.ehr_note_guard();
"""
    + GRANT_AUDIT
)
