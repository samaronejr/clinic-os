"""Owner-only replay of the current purge ledger over an older isolated backup."""

SQL = r"""
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.retention_restore_tombstones(current_tombstones jsonb)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE p clinic_app.retention_artifactpurge; item jsonb; result jsonb:='[]';
BEGIN
 IF NOT (current_setting('role')='clinic_owner' OR session_user='clinic_owner'
  OR EXISTS(SELECT 1 FROM pg_roles WHERE rolname=session_user AND rolsuper))
 OR jsonb_typeof(current_tombstones)<>'array' THEN
  RAISE EXCEPTION 'retention restore denied' USING ERRCODE='42501'; END IF;
 PERFORM set_config('app.retention_restore','true',true);
 FOR item IN SELECT value FROM jsonb_array_elements(current_tombstones)
 LOOP
  SELECT * INTO p FROM jsonb_populate_record(
   NULL::clinic_app.retention_artifactpurge,item);
  IF p.state NOT IN ('purging','purged') OR p.started_at IS NULL
  OR p.backup_copy_expires_at IS NULL OR p.record_class NOT IN (
   'raw_audio','ai_artifact','transcript_partial')
  OR jsonb_typeof(item->'objects') IS DISTINCT FROM 'array'
  OR (p.record_class='raw_audio' AND EXISTS(
    SELECT 1 FROM clinic_app.scribe_recordingsession s WHERE s.id=p.record_id
    AND (s.clinic_id,s.organization_id) IS DISTINCT FROM
     (p.clinic_id,p.organization_id)))
  OR (p.record_class IN ('ai_artifact','transcript_partial') AND EXISTS(
    SELECT 1 FROM clinic_app.ai_aiartifact a WHERE a.id=p.record_id
    AND (a.clinic_id,a.organization_id) IS DISTINCT FROM
     (p.clinic_id,p.organization_id))) THEN
   RAISE EXCEPTION 'invalid restore tombstone' USING ERRCODE='23514'; END IF;
  result:=result||(item->'objects');
  -- A tombstone can postdate the backup's clinic/record. Its objects still
  -- belong to the current ledger; a missing old row is not a resurrection.
  IF NOT EXISTS(SELECT 1 FROM clinic_app.identity_clinic c
   WHERE c.id=p.clinic_id AND c.organization_id=p.organization_id) THEN CONTINUE;
  END IF;
  IF clinic_app.retention_held(p.clinic_id,p.record_class,p.record_id) THEN
   RAISE EXCEPTION 'restored legal hold conflicts with destruction'
    USING ERRCODE='23514'; END IF;
  IF EXISTS(SELECT 1 FROM clinic_app.retention_artifactpurge prior
   WHERE prior.id=p.id AND (prior.organization_id,prior.clinic_id,prior.principal_id,
    prior.record_class,prior.record_id,prior.created_at) IS DISTINCT FROM
   (p.organization_id,p.clinic_id,p.principal_id,p.record_class,p.record_id,p.created_at))
  THEN RAISE EXCEPTION 'restore tombstone binding mismatch' USING ERRCODE='23514';
  END IF;
  INSERT INTO clinic_app.retention_artifactpurge SELECT p.*
  ON CONFLICT(id) DO UPDATE SET state=EXCLUDED.state,
   started_at=EXCLUDED.started_at,backup_copy_expires_at=EXCLUDED.backup_copy_expires_at,
   purged_at=EXCLUDED.purged_at,target_count=EXCLUDED.target_count,
   deleted_keys=EXCLUDED.deleted_keys;
  PERFORM clinic_app.retention_shred(p.id);
 END LOOP;
 -- Also replay tombstones already contained in the restored backup.
 FOR p IN SELECT * FROM clinic_app.retention_artifactpurge r
  WHERE r.started_at IS NOT NULL AND r.state IN ('purging','purged')
 LOOP
  PERFORM clinic_app.retention_shred(p.id);
  SELECT result||coalesce(jsonb_agg(jsonb_build_object(
   'key',c.storage_key,'location',c.storage_location)), '[]') INTO result
  FROM clinic_app.scribe_audiochunk c
  WHERE p.record_class='raw_audio' AND c.session_id=p.record_id;
 END LOOP;
 PERFORM set_config('app.retention_restore','',true);
 RETURN (SELECT coalesce(jsonb_agg(DISTINCT value),'[]')
  FROM jsonb_array_elements(result));
END $f$;
REVOKE ALL ON FUNCTION clinic_app.retention_restore_tombstones(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.retention_restore_tombstones(jsonb)
 TO clinic_owner;
RESET ROLE;
"""
