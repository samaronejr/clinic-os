"""Server-derived pending origins; no added table grants or ambient write token.

Freeze the consent-eligible keys at claim. Every protected export page intersects
that set with current choice. The worker hashes the keys it actually serialized;
finalization requires the same complete set and rechecks each key. A late
refusal, skipped page, substituted key or changed scope cannot seal the bytes.
"""

from apps.ai.migrations._care_sql import _edit
from apps.ai.migrations._pending_choice_sql import RELATIONS
from apps.ai.migrations._pending_read_sql import _EXPORT, _GUARD
from apps.identity.migrations._delegate_access_sql import _POPULATED
from apps.interop.migrations._export_v5_sql import EXPORT_V5

_DENY = "RAISE EXCEPTION 'interop export denied' USING ERRCODE='42501';"
_SIGNATURE = "clinic_app.interop_finalize_export(uuid,uuid,text)"
_AUTHORIZED = """
   NEW.organization_id IS NOT DISTINCT FROM
    nullif(current_setting('app.current_tenant',true),'')::uuid
   AND NEW.requested_by_id=(SELECT u.id FROM clinic_app.load_current_user() u)
   AND clinic_app.enterprise_staff_context() AND clinic_app.interop_manager(
    NEW.organization_id) IS TRUE
"""
_KEYS = "\nUNION ALL\n".join(
    (
        "SELECT '__TABLE__/'||t.id::text AS k FROM clinic_app.__TABLE__ t "
        "WHERE t.organization_id=NEW.organization_id "
        "AND clinic_app.ai_pending_choice('__TABLE__',t.id)"
    ).replace("__TABLE__", table)
    for table in RELATIONS
)


def _reader() -> str:
    sql = _edit(
        _EXPORT,
        "to_jsonb(t)-ARRAY['pending_origins']::text[]",
        "to_jsonb(t)-ARRAY['pending_origins','build_origins']::text[]",
    )
    for table in RELATIONS:
        predicate = f"clinic_app.ai_pending_choice('{table}',t.id)"
        sql = _edit(
            sql,
            predicate,
            f"(job.build_origins ? ('{table}/'||t.id::text)) AND {predicate}",
        )
    return _edit(
        sql,
        "IF job.id IS NULL OR NOT clinic_app.interop_manager",
        "IF job.id IS NULL OR NOT clinic_app.enterprise_staff_context() "
        "OR NOT clinic_app.interop_manager",
    )


_METADATA = """ARRAY['part_count','archive_bytes','archive_sha256','manifest_sha256',
    'dataset_count','record_count','attachment_count']"""
_GUARDED = _edit(
    _GUARD,
    "IF NEW.pending_origins IS NOT NULL OR NEW.state<>'requested'",
    "IF NEW.build_origins IS NOT NULL OR NEW.pending_origins IS NOT NULL "
    "OR NEW.state<>'requested'",
)
_GUARDED = _edit(
    _GUARDED,
    "IF NEW.pending_origins IS DISTINCT FROM OLD.pending_origins",
    f"""IF OLD.state='building' AND NEW.state='building' AND {_AUTHORIZED}
   AND (to_jsonb(NEW)-{_METADATA})=(to_jsonb(OLD)-{_METADATA}) THEN
   RETURN NEW;
  END IF;
  IF NEW.pending_origins IS DISTINCT FROM OLD.pending_origins""",
)
_GUARDED = _edit(
    _GUARDED,
    "NEW.started_at:=statement_timestamp();",
    """IF (__AUTHORIZED__) IS NOT TRUE THEN __DENY__ END IF;
    SELECT coalesce(jsonb_object_agg(keys.k,true),'{}'::jsonb)
    INTO NEW.build_origins FROM (__KEYS__) keys;
    NEW.started_at:=statement_timestamp();""".replace("__AUTHORIZED__", _AUTHORIZED)
    .replace("__DENY__", _DENY)
    .replace("__KEYS__", _KEYS),
)
_GUARDED = _edit(
    _GUARDED,
    "NEW.started_at:=OLD.started_at;",
    """IF OLD.build_origins IS NULL OR (__AUTHORIZED__) IS NOT TRUE
    THEN __DENY__ END IF;
    SELECT coalesce(jsonb_agg(jsonb_build_array(split_part(k,'/',1),
      split_part(k,'/',2)) ORDER BY k COLLATE "C"),'[]'::jsonb)
     INTO NEW.pending_origins FROM jsonb_object_keys(OLD.build_origins) k;
    NEW.build_origins:=NULL;
    NEW.started_at:=OLD.started_at;""".replace("__AUTHORIZED__", _AUTHORIZED).replace(
        "__DENY__", _DENY
    ),
)
_GUARDED = _edit(
    _GUARDED,
    "IF OLD.state IN ('requested','building') AND NEW.state='failed'",
    """IF OLD.state IN ('requested','building') AND NEW.state='failed' THEN
    NEW.build_origins:=NULL; NEW.part_count:=0; NEW.archive_bytes:=0;
    NEW.archive_sha256:=''; NEW.manifest_sha256:=''; NEW.dataset_count:=0;
    NEW.record_count:=0; NEW.attachment_count:=0;
   END IF;
   IF OLD.state IN ('requested','building') AND NEW.state='failed'""",
)
_GUARDED = _edit(
    _GUARDED,
    "WHERE e.id=NEW.export_id AND e.organization_id=NEW.organization_id;",
    """WHERE e.id=NEW.export_id AND e.organization_id=NEW.organization_id
    AND e.requested_by_id=(SELECT u.id FROM clinic_app.load_current_user() u)
    AND clinic_app.enterprise_staff_context() FOR SHARE;""",
)

_FINALIZE = (
    """
CREATE FUNCTION clinic_app.interop_finalize_export(
 requested_export uuid, requested_clinic uuid, issued_hash text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE org uuid:=nullif(current_setting('app.current_tenant',true),'')::uuid;
 actor uuid:=(SELECT u.id FROM clinic_app.load_current_user() u);
 job clinic_app.interop_organizationexport; calculated text; allowed boolean;
 part_total bigint; last_part integer; byte_total bigint;
BEGIN
 IF NOT clinic_app.enterprise_staff_context() OR clinic_app.interop_manager(org)
  IS NOT TRUE OR actor IS NULL THEN __DENY__ END IF;
 SELECT * INTO job FROM clinic_app.interop_organizationexport e
 WHERE (e.organization_id,e.clinic_id,e.requested_by_id,e.id)=
       (org,requested_clinic,actor,requested_export) AND e.state='building' FOR UPDATE;
 IF NOT FOUND OR job.build_origins IS NULL THEN __DENY__ END IF;
 SELECT encode(sha256(convert_to(coalesce(
   string_agg(k||chr(10),'' ORDER BY k COLLATE "C"),''),'UTF8')),'hex'),
   coalesce(bool_and(clinic_app.ai_pending_choice(split_part(k,'/',1),
        split_part(k,'/',2)::uuid)),true) INTO calculated,allowed
 FROM jsonb_object_keys(job.build_origins) k;
 SELECT count(*),max(p.part),sum(p.size_bytes) INTO part_total,last_part,byte_total
 FROM clinic_app.interop_organizationexportpart p WHERE p.export_id=job.id
  AND p.organization_id=org AND p.data IS NOT NULL;
 IF allowed IS NOT TRUE OR issued_hash IS DISTINCT FROM calculated
  OR job.archive_sha256 !~ '^[0-9a-f]{64}$' OR job.manifest_sha256 !~ '^[0-9a-f]{64}$'
  OR job.dataset_count<>__DATASETS__ OR job.record_count<0
  OR job.attachment_count NOT BETWEEN 0 AND job.record_count
  OR job.part_count<1 OR job.archive_bytes<1
  OR (job.part_count,job.part_count,job.archive_bytes) IS DISTINCT FROM
    (part_total,last_part,byte_total)
 THEN __DENY__ END IF;
 UPDATE clinic_app.interop_organizationexport SET state='ready',revision=revision+1
 WHERE id=job.id AND state='building';
 IF NOT FOUND THEN __DENY__ END IF;
END $f$;
REVOKE ALL ON FUNCTION __SIGNATURE__ FROM PUBLIC;
GRANT EXECUTE ON FUNCTION __SIGNATURE__ TO clinic_app;
""".replace("__SIGNATURE__", _SIGNATURE)
    .replace("__DENY__", _DENY)
    .replace("__DATASETS__", str(len(EXPORT_V5)))
)
# The app may still prepare the existing archive metadata. Only the finalizer
# can actually make it downloadable. No new privilege even for clinic_resolver:
# the existing BEFORE guard fills NEW just as it already fills ready timestamps.
FORWARD = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _FINALIZE
    + _GUARDED
    + """\nRESET ROLE;
CREATE POLICY interop_ready_via_finalizer ON clinic_app.interop_organizationexport
 AS RESTRICTIVE FOR UPDATE TO clinic_app USING (
  organization_id=nullif(current_setting('app.current_tenant',true),'')::uuid
  AND clinic_app.enterprise_staff_context())
 WITH CHECK (state<>'ready' AND clinic_app.enterprise_staff_context());
"""
    + _reader()
)
_UNUSED = """SET LOCAL ROLE clinic_resolver;
DO $populated$ BEGIN
 IF __POPULATED__ OR EXISTS(SELECT 1 FROM clinic_app.interop_organizationexport)
 THEN RAISE EXCEPTION 'populated export or identity rolls back only by restore'
 USING ERRCODE='55000'; END IF;
END $populated$;
RESET ROLE;\n""".replace("__POPULATED__", _POPULATED)
REVERSE = (
    _UNUSED + _EXPORT + "DROP POLICY interop_ready_via_finalizer "
    "ON clinic_app.interop_organizationexport;"
    + "\nSET LOCAL ROLE clinic_resolver;\n"
    + _GUARD
    + f"\nDROP FUNCTION {_SIGNATURE};\nRESET ROLE;\n"
)
