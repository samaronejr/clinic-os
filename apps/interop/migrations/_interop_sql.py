# ruff: noqa: S608 - DDL text built from migration literals, never caller input
"""Frozen partner API, webhook, export posture: grants, FORCE RLS, guards, resolvers.

Partner calls never hold a staff GUC: every resolver authenticates the presented
token digest itself and re-checks the approving administrator's current
organization authority. Staff management requires ``configuration.organization``
on every clinic of the organization (``interop_manager``).
"""

from typing import Final

TABLES: Final = (
    "interop_apiclient",
    "interop_apiclientsecret",
    "interop_apiaccesstoken",
    "interop_apiwritereceipt",
    "interop_webhooksubscription",
    "interop_webhookdelivery",
    "interop_webhookattempt",
    "interop_organizationexport",
    "interop_organizationexportpart",
)
# Runtime reads every staff-visible table through the management policy only.
RUNTIME_READ_TABLES: Final = (
    "interop_apiclient",
    "interop_apiwritereceipt",
    "interop_webhooksubscription",
    "interop_webhookdelivery",
    "interop_webhookattempt",
    "interop_organizationexport",
    "interop_organizationexportpart",
)
EXPORT_UPDATE_COLUMNS: Final = (
    "state",
    "revision",
    "part_count",
    "archive_bytes",
    "archive_sha256",
    "manifest_sha256",
    "dataset_count",
    "record_count",
    "attachment_count",
    "failure_reason",
)

_SCOPES: Final = (
    "ARRAY['appointments:read','availability:read','invoices.status:read',"
    "'patients.demographics:read','webhooks:manage']::varchar[]"
)
_EVENTS: Final = (
    "ARRAY['appointment.created','appointment.status_changed',"
    "'appointment.rescheduled','invoice.status_changed']::varchar[]"
)
_TENANT: Final = "NULLIF(current_setting('app.current_tenant',true),'')::uuid"
_ALL: Final = ",".join(f"clinic_app.{table}" for table in TABLES)
_READ: Final = ",".join(f"clinic_app.{table}" for table in RUNTIME_READ_TABLES)
_RESOLVER_INSERT: Final = ",".join(
    f"clinic_app.{table}"
    for table in TABLES
    if table != "interop_organizationexportpart"
)
_MANAGER: Final = (
    f"(organization_id={_TENANT} AND (SELECT clinic_app.interop_manager({_TENANT})))"
)

_CONSTRAINTS: Final = f"""
ALTER TABLE clinic_app.interop_apiclient ADD CONSTRAINT interop_client_contract
 CHECK (state IN ('active','revoked') AND revision>=1
  AND rate_limit_per_minute BETWEEN 1 AND 6000
  AND char_length(name) BETWEEN 1 AND 80
  AND cardinality(scopes) BETWEEN 1 AND 5 AND scopes <@ {_SCOPES}
  AND revocation_reason IN ('','admin','offboarding')
  AND ((state='active' AND revoked_at IS NULL AND revocation_reason='')
   OR (state='revoked' AND revoked_at IS NOT NULL AND revocation_reason<>'')));
ALTER TABLE clinic_app.interop_apiclientsecret
 ADD CONSTRAINT interop_secret_contract
 CHECK (version>=1 AND secret_digest ~ '^[0-9a-f]{{64}}$'
  AND (expires_at IS NULL OR expires_at>created_at));
ALTER TABLE clinic_app.interop_apiaccesstoken
 ADD CONSTRAINT interop_token_contract
 CHECK (token_digest ~ '^[0-9a-f]{{64}}$' AND expires_at>issued_at
  AND cardinality(scopes) BETWEEN 1 AND 5 AND scopes <@ {_SCOPES});
ALTER TABLE clinic_app.interop_apiwritereceipt
 ADD CONSTRAINT interop_write_contract
 CHECK (idempotency_key ~ '^[A-Za-z0-9._:-]{{1,64}}$'
  AND operation IN ('webhook_create','webhook_revoke','webhook_rotate')
  AND request_digest ~ '^[0-9a-f]{{64}}$' AND response_status BETWEEN 200 AND 499);
ALTER TABLE clinic_app.interop_webhooksubscription
 ADD CONSTRAINT interop_webhook_contract
 CHECK (state IN ('active','revoked') AND revision>=1 AND secret_version>=1
  AND char_length(url) BETWEEN 12 AND 2048
  AND url ~ '^https://[A-Za-z0-9.-]+(:[0-9]{{1,5}})?(/[A-Za-z0-9._~!$&''()*+,;=:@%/-]*)?$'
  AND cardinality(events) BETWEEN 1 AND 4 AND events <@ {_EVENTS}
  AND revocation_reason IN ('','client','admin','client_revoked','offboarding')
  AND ((state='active' AND revoked_at IS NULL AND revocation_reason='')
   OR (state='revoked' AND revoked_at IS NOT NULL AND revocation_reason<>'')));
ALTER TABLE clinic_app.interop_webhookdelivery
 ADD CONSTRAINT interop_delivery_contract
 CHECK (event_type IN ('appointment.created','appointment.status_changed',
  'appointment.rescheduled','invoice.status_changed')
  AND payload_digest ~ '^[0-9a-f]{{64}}$'
  AND char_length(subject_state) BETWEEN 1 AND 32);
ALTER TABLE clinic_app.interop_webhookattempt
 ADD CONSTRAINT interop_attempt_contract
 CHECK (attempt>=1 AND outcome IN ('delivered','http_error','timeout','network_error')
  AND (http_status IS NULL OR http_status BETWEEN 100 AND 599));
ALTER TABLE clinic_app.interop_organizationexport
 ADD CONSTRAINT interop_export_contract
 CHECK (state IN ('requested','building','ready','failed','expired') AND revision>=1
  AND failure_reason IN ('','authority_revoked','attachment_unavailable',
   'attachment_mismatch','decrypt_failed','too_large','internal_error')
  AND (archive_sha256='' OR archive_sha256 ~ '^[0-9a-f]{{64}}$')
  AND (manifest_sha256='' OR manifest_sha256 ~ '^[0-9a-f]{{64}}$')
  AND ((state='requested' AND started_at IS NULL AND finished_at IS NULL)
   OR (state='building' AND started_at IS NOT NULL AND finished_at IS NULL)
   OR (state IN ('ready','expired') AND finished_at IS NOT NULL
    AND expires_at IS NOT NULL AND part_count>=1 AND archive_sha256<>''
    AND manifest_sha256<>'' AND failure_reason='')
   OR (state='failed' AND finished_at IS NOT NULL AND failure_reason<>'')));
ALTER TABLE clinic_app.interop_organizationexportpart
 ADD CONSTRAINT interop_part_contract
 CHECK (part>=1 AND sha256 ~ '^[0-9a-f]{{64}}$' AND size_bytes BETWEEN 1 AND 8388608);
"""

_GRANTS: Final = f"""
REVOKE ALL ON {_ALL} FROM PUBLIC, clinic_app;
GRANT SELECT ON {_READ} TO clinic_app;
GRANT UPDATE ({",".join(EXPORT_UPDATE_COLUMNS)})
 ON clinic_app.interop_organizationexport TO clinic_app;
GRANT INSERT ON clinic_app.interop_organizationexportpart TO clinic_app;
GRANT SELECT ON {_ALL} TO clinic_resolver;
GRANT INSERT ON {_RESOLVER_INSERT} TO clinic_resolver;
GRANT UPDATE (state,revision,revoked_at,revocation_reason,approved_by_id)
 ON clinic_app.interop_apiclient TO clinic_resolver;
GRANT UPDATE (expires_at) ON clinic_app.interop_apiclientsecret,
 clinic_app.interop_apiaccesstoken TO clinic_resolver;
GRANT UPDATE (state,revision,revoked_at,revocation_reason,secret_version,
 previous_secret_until) ON clinic_app.interop_webhooksubscription TO clinic_resolver;
GRANT UPDATE (state,revision,failure_reason)
 ON clinic_app.interop_organizationexport TO clinic_resolver;
GRANT UPDATE (data) ON clinic_app.interop_organizationexportpart TO clinic_resolver;
"""

_POLICIES: Final = (
    "\n".join(
        f"""ALTER TABLE clinic_app.{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{table} FORCE ROW LEVEL SECURITY;"""
        for table in TABLES
    )
    + "\n"
    + "\n".join(
        f"""CREATE POLICY interop_manage_read ON clinic_app.{table}
 AS PERMISSIVE FOR SELECT TO PUBLIC USING {_MANAGER};"""
        for table in RUNTIME_READ_TABLES
    )
    + f"""
CREATE POLICY interop_export_update ON clinic_app.interop_organizationexport
 AS PERMISSIVE FOR UPDATE TO PUBLIC USING {_MANAGER} WITH CHECK {_MANAGER};
CREATE POLICY interop_part_insert ON clinic_app.interop_organizationexportpart
 AS PERMISSIVE FOR INSERT TO PUBLIC WITH CHECK {_MANAGER};
"""
)

_AUTHORITY: Final = f"""
CREATE FUNCTION clinic_app.interop_authority(requested_org uuid, requested_actor uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT requested_org IS NOT NULL AND requested_actor IS NOT NULL
 AND EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
  WHERE c.organization_id=requested_org)
 AND NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
  WHERE c.organization_id=requested_org
  AND NOT clinic_app.identity_actor_permission('configuration.organization',
   c.id,NULL,requested_actor,requested_org))
$f$;
REVOKE ALL ON FUNCTION clinic_app.interop_authority(uuid,uuid) FROM PUBLIC;

CREATE FUNCTION clinic_app.interop_manager(requested_org uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT requested_org IS NOT NULL AND requested_org={_TENANT}
 AND clinic_app.user_has_org(requested_org)
 AND clinic_app.interop_authority(requested_org,
  (SELECT u.id FROM clinic_app.load_current_user() u))
$f$;
REVOKE ALL ON FUNCTION clinic_app.interop_manager(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_manager(uuid) TO clinic_app,clinic_owner;

CREATE FUNCTION clinic_app.interop_api_session(presented_token text)
RETURNS TABLE(client_id uuid, organization_id uuid, clinic_id uuid,
 scopes varchar[], rate_limit integer)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT c.id,c.organization_id,c.clinic_id,t.scopes::varchar[],
  c.rate_limit_per_minute::integer
 FROM clinic_app.interop_apiaccesstoken t
 JOIN clinic_app.interop_apiclient c ON c.id=t.client_id
  AND c.organization_id=t.organization_id
 WHERE t.token_digest=presented_token AND t.expires_at>statement_timestamp()
  AND c.state='active'
  AND clinic_app.interop_authority(c.organization_id,c.approved_by_id)
$f$;
REVOKE ALL ON FUNCTION clinic_app.interop_api_session(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_api_session(text) TO clinic_app;
"""

_GUARD: Final = r"""
CREATE FUNCTION clinic_app.interop_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE parent clinic_app.interop_organizationexport;
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION 'interop history is retained' USING ERRCODE='23514';
 END IF;
 IF TG_OP='INSERT' THEN
  IF TG_TABLE_NAME='interop_apiclient' THEN
   IF NEW.state<>'active' OR NEW.revision<>1 OR NEW.scopes<>ARRAY(
    SELECT DISTINCT s FROM unnest(NEW.scopes) s ORDER BY s) THEN
    RAISE EXCEPTION 'invalid api client' USING ERRCODE='23514';
   END IF;
   NEW.created_at:=statement_timestamp();
  ELSIF TG_TABLE_NAME='interop_apiclientsecret' THEN
   NEW.created_at:=statement_timestamp();
  ELSIF TG_TABLE_NAME='interop_webhooksubscription' THEN
   IF NEW.state<>'active' OR NEW.revision<>1 OR NEW.secret_version<>1
    OR NEW.previous_secret_until IS NOT NULL OR NEW.events<>ARRAY(
    SELECT DISTINCT e FROM unnest(NEW.events) e ORDER BY e) THEN
    RAISE EXCEPTION 'invalid webhook subscription' USING ERRCODE='23514';
   END IF;
   NEW.created_at:=statement_timestamp();
  ELSIF TG_TABLE_NAME='interop_webhookattempt' THEN
   IF NOT EXISTS (SELECT 1 FROM clinic_app.interop_webhookdelivery d
    JOIN clinic_app.comms_integrationoperation o ON o.id=d.operation_id
     AND o.organization_id=d.organization_id
    WHERE d.id=NEW.delivery_id AND d.organization_id=NEW.organization_id
     AND o.status='in_progress' AND o.attempt_count=NEW.attempt) THEN
    RAISE EXCEPTION 'webhook attempt is not an in-flight send' USING ERRCODE='23514';
   END IF;
   NEW.recorded_at:=statement_timestamp();
  ELSIF TG_TABLE_NAME='interop_organizationexport' THEN
   IF NEW.state<>'requested' OR NEW.revision<>1 OR NEW.part_count<>0
    OR NEW.archive_sha256<>'' OR NEW.failure_reason<>'' THEN
    RAISE EXCEPTION 'invalid organization export' USING ERRCODE='23514';
   END IF;
   NEW.created_at:=statement_timestamp();
  ELSIF TG_TABLE_NAME='interop_organizationexportpart' THEN
   SELECT * INTO parent FROM clinic_app.interop_organizationexport e
   WHERE e.id=NEW.export_id AND e.organization_id=NEW.organization_id;
   IF parent.id IS NULL OR parent.state<>'building' OR NEW.data IS NULL THEN
    RAISE EXCEPTION 'export part requires a building export' USING ERRCODE='23514';
   END IF;
   NEW.created_at:=statement_timestamp();
  END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='interop_apiclient' THEN
  IF OLD.state='active' AND NEW.revision=OLD.revision+1 AND (
   (NEW.state='revoked' AND (to_jsonb(NEW)-ARRAY['state','revision','revoked_at',
    'revocation_reason'])=(to_jsonb(OLD)-ARRAY['state','revision','revoked_at',
    'revocation_reason']))
   OR (NEW.state='active' AND (to_jsonb(NEW)-ARRAY['revision','approved_by_id'])
    =(to_jsonb(OLD)-ARRAY['revision','approved_by_id']))) THEN
   IF NEW.state='revoked' THEN NEW.revoked_at:=statement_timestamp(); END IF;
   RETURN NEW;
  END IF;
 ELSIF TG_TABLE_NAME IN ('interop_apiclientsecret','interop_apiaccesstoken') THEN
  -- An expiry may be set or brought forward, never extended or removed.
  IF NEW.expires_at IS NOT NULL
   AND (OLD.expires_at IS NULL OR NEW.expires_at<OLD.expires_at)
   AND (to_jsonb(NEW)-'expires_at')=(to_jsonb(OLD)-'expires_at') THEN
   RETURN NEW;
  END IF;
 ELSIF TG_TABLE_NAME='interop_webhooksubscription' THEN
  IF OLD.state='active' AND NEW.revision=OLD.revision+1 AND (
   (NEW.state='revoked' AND (to_jsonb(NEW)-ARRAY['state','revision','revoked_at',
    'revocation_reason'])=(to_jsonb(OLD)-ARRAY['state','revision','revoked_at',
    'revocation_reason']))
   OR (NEW.state='active' AND NEW.secret_version=OLD.secret_version+1
    AND NEW.previous_secret_until IS NOT NULL
    AND (to_jsonb(NEW)-ARRAY['revision','secret_version','previous_secret_until'])
    =(to_jsonb(OLD)-ARRAY['revision','secret_version','previous_secret_until'])))
  THEN
   IF NEW.state='revoked' THEN NEW.revoked_at:=statement_timestamp(); END IF;
   RETURN NEW;
  END IF;
 ELSIF TG_TABLE_NAME='interop_organizationexport' THEN
  IF NEW.revision=OLD.revision+1
   AND (to_jsonb(NEW)-ARRAY['state','revision','started_at','finished_at',
    'expires_at','part_count','archive_bytes','archive_sha256','manifest_sha256',
    'dataset_count','record_count','attachment_count','failure_reason'])
   =(to_jsonb(OLD)-ARRAY['state','revision','started_at','finished_at',
    'expires_at','part_count','archive_bytes','archive_sha256','manifest_sha256',
    'dataset_count','record_count','attachment_count','failure_reason']) THEN
   IF OLD.state='requested' AND NEW.state='building'
    AND ROW(NEW.part_count,NEW.archive_bytes,NEW.archive_sha256,
     NEW.manifest_sha256,NEW.dataset_count,NEW.record_count,NEW.attachment_count,
     NEW.failure_reason)
    IS NOT DISTINCT FROM ROW(OLD.part_count,OLD.archive_bytes,OLD.archive_sha256,
     OLD.manifest_sha256,OLD.dataset_count,OLD.record_count,OLD.attachment_count,
     OLD.failure_reason) THEN
    NEW.started_at:=statement_timestamp();
    RETURN NEW;
   END IF;
   IF OLD.state='building' AND NEW.state='ready' AND NEW.failure_reason=''
    AND NEW.part_count=(SELECT count(*)
     FROM clinic_app.interop_organizationexportpart p
     WHERE p.export_id=NEW.id AND p.data IS NOT NULL)
    AND NEW.archive_bytes=(SELECT coalesce(sum(p.size_bytes),0)
     FROM clinic_app.interop_organizationexportpart p WHERE p.export_id=NEW.id) THEN
    NEW.started_at:=OLD.started_at;
    NEW.finished_at:=statement_timestamp();
    NEW.expires_at:=statement_timestamp()+interval '7 days';
    RETURN NEW;
   END IF;
   IF OLD.state IN ('requested','building') AND NEW.state='failed'
    AND NEW.failure_reason<>'' AND NEW.part_count=0 AND NEW.archive_sha256='' THEN
    NEW.started_at:=coalesce(OLD.started_at,statement_timestamp());
    NEW.finished_at:=statement_timestamp();
    RETURN NEW;
   END IF;
   IF OLD.state='ready' AND NEW.state='expired'
    AND OLD.expires_at<=statement_timestamp()
    AND (to_jsonb(NEW)-ARRAY['state','revision'])
     =(to_jsonb(OLD)-ARRAY['state','revision']) THEN
    RETURN NEW;
   END IF;
  END IF;
 ELSIF TG_TABLE_NAME='interop_organizationexportpart' THEN
  IF OLD.data IS NOT NULL AND NEW.data IS NULL
   AND (to_jsonb(NEW)-'data')=(to_jsonb(OLD)-'data')
   AND EXISTS (SELECT 1 FROM clinic_app.interop_organizationexport e
    WHERE e.id=NEW.export_id AND e.state IN ('expired','failed')) THEN
   RETURN NEW;
  END IF;
 END IF;
 RAISE EXCEPTION 'interop transition denied' USING ERRCODE='23514';
END $f$;
REVOKE ALL ON FUNCTION clinic_app.interop_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_guard() TO clinic_owner;
"""

_MANAGE: Final = r"""
CREATE FUNCTION clinic_app.interop_manage(requested_clinic uuid,
 requested_action text, requested_subject uuid, expected_revision integer,
 presented_secret text, parameters jsonb)
RETURNS TABLE(record_id uuid, record_clinic uuid, record_revision integer,
 record_state text, changed boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE org uuid; actor uuid; client clinic_app.interop_apiclient;
 sub clinic_app.interop_webhooksubscription; job clinic_app.interop_organizationexport;
 wanted varchar[]; label text; rate integer; next_version integer;
BEGIN
 SELECT c.organization_id INTO org FROM clinic_app.identity_clinic c
 WHERE c.id=requested_clinic
  AND c.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 IF org IS NULL OR NOT clinic_app.interop_manager(org) THEN
  RAISE EXCEPTION 'interop management denied' USING ERRCODE='42501';
 END IF;
 SELECT u.id INTO actor FROM clinic_app.load_current_user() u;
 IF requested_action='client_create' THEN
  IF jsonb_typeof(parameters->'name')<>'string'
   OR jsonb_typeof(parameters->'scopes')<>'array'
   OR jsonb_typeof(parameters->'rate_limit')<>'number'
   OR presented_secret !~ '^[0-9a-f]{64}$' THEN RETURN; END IF;
  label:=parameters->>'name';
  rate:=(parameters->>'rate_limit')::integer;
  wanted:=ARRAY(SELECT DISTINCT s FROM jsonb_array_elements_text(parameters->'scopes')
   s ORDER BY s)::varchar[];
  SELECT * INTO client FROM clinic_app.interop_apiclient x WHERE x.id=requested_subject;
  IF client.id IS NOT NULL THEN
   IF client.organization_id=org AND client.clinic_id=requested_clinic
    AND client.name=label AND client.scopes=wanted
    AND client.rate_limit_per_minute=rate AND EXISTS (
     SELECT 1 FROM clinic_app.interop_apiclientsecret s
     WHERE s.client_id=client.id AND s.version=1
      AND s.secret_digest=presented_secret) THEN
    RETURN QUERY SELECT client.id,client.clinic_id,client.revision::integer,
     client.state::text,false;
   END IF;
   RETURN;
  END IF;
  INSERT INTO clinic_app.interop_apiclient (id,organization_id,clinic_id,name,
   scopes,rate_limit_per_minute,state,revision,approved_by_id,created_at,
   revoked_at,revocation_reason)
  VALUES (requested_subject,org,requested_clinic,label,wanted,rate,'active',1,
   actor,statement_timestamp(),NULL,'') RETURNING * INTO client;
  INSERT INTO clinic_app.interop_apiclientsecret (id,organization_id,client_id,
   version,secret_digest,created_at,expires_at)
  VALUES (gen_random_uuid(),org,client.id,1,presented_secret,statement_timestamp(),
   NULL);
  RETURN QUERY SELECT client.id,client.clinic_id,1,'active'::text,true;
 ELSIF requested_action IN ('client_rotate','client_revoke') THEN
  SELECT * INTO client FROM clinic_app.interop_apiclient x
  WHERE x.id=requested_subject AND x.organization_id=org FOR UPDATE;
  IF client.id IS NULL THEN RETURN; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
   'clinic-lock-v1:comms-subject:interop-client:' || client.id::text,0));
  IF requested_action='client_rotate' THEN
   IF client.revision=expected_revision+1 AND client.state='active' AND EXISTS (
    SELECT 1 FROM clinic_app.interop_apiclientsecret s WHERE s.client_id=client.id
     AND s.expires_at IS NULL AND s.secret_digest=presented_secret) THEN
    RETURN QUERY SELECT client.id,client.clinic_id,client.revision::integer,
     client.state::text,false;
    RETURN;
   END IF;
   IF client.state<>'active' OR client.revision<>expected_revision
    OR presented_secret !~ '^[0-9a-f]{64}$' THEN RETURN; END IF;
   UPDATE clinic_app.interop_apiclientsecret s
   SET expires_at=statement_timestamp()+interval '24 hours'
   WHERE s.client_id=client.id AND s.expires_at IS NULL;
   SELECT coalesce(max(s.version),0)+1 INTO next_version
   FROM clinic_app.interop_apiclientsecret s WHERE s.client_id=client.id;
   INSERT INTO clinic_app.interop_apiclientsecret (id,organization_id,client_id,
    version,secret_digest,created_at,expires_at)
   VALUES (gen_random_uuid(),org,client.id,next_version,presented_secret,
    statement_timestamp(),NULL);
   UPDATE clinic_app.interop_apiclient x SET revision=x.revision+1,
    approved_by_id=actor WHERE x.id=client.id;
  ELSE
   IF client.state='revoked' AND client.revision=expected_revision+1 THEN
    RETURN QUERY SELECT client.id,client.clinic_id,client.revision::integer,
     client.state::text,false;
    RETURN;
   END IF;
   IF client.state<>'active' OR client.revision<>expected_revision THEN RETURN;
   END IF;
   UPDATE clinic_app.interop_apiclient x SET state='revoked',
    revoked_at=statement_timestamp(),revocation_reason='admin',
    revision=x.revision+1 WHERE x.id=client.id;
   UPDATE clinic_app.interop_webhooksubscription w SET state='revoked',
    revoked_at=statement_timestamp(),revocation_reason='client_revoked',
    revision=w.revision+1 WHERE w.client_id=client.id AND w.state='active';
  END IF;
  RETURN QUERY SELECT x.id,x.clinic_id,x.revision::integer,x.state::text,true
  FROM clinic_app.interop_apiclient x WHERE x.id=client.id;
 ELSIF requested_action='webhook_revoke' THEN
  SELECT * INTO sub FROM clinic_app.interop_webhooksubscription x
  WHERE x.id=requested_subject AND x.organization_id=org FOR UPDATE;
  IF sub.id IS NULL THEN RETURN; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended(
   'clinic-lock-v1:comms-subject:interop-client:' || sub.client_id::text,0));
  IF sub.state='revoked' AND sub.revision=expected_revision+1 THEN
   RETURN QUERY SELECT sub.id,sub.clinic_id,sub.revision::integer,
    sub.state::text,false;
   RETURN;
  END IF;
  IF sub.state<>'active' OR sub.revision<>expected_revision THEN RETURN; END IF;
  UPDATE clinic_app.interop_webhooksubscription x SET state='revoked',
   revoked_at=statement_timestamp(),revocation_reason='admin',revision=x.revision+1
  WHERE x.id=sub.id;
  RETURN QUERY SELECT x.id,x.clinic_id,x.revision::integer,x.state::text,true
  FROM clinic_app.interop_webhooksubscription x WHERE x.id=sub.id;
 ELSIF requested_action='export_request' THEN
  SELECT * INTO job FROM clinic_app.interop_organizationexport x
  WHERE x.id=requested_subject;
  IF job.id IS NOT NULL THEN
   IF job.organization_id=org AND job.requested_by_id=actor
    AND job.clinic_id=requested_clinic THEN
    RETURN QUERY SELECT job.id,job.clinic_id,job.revision::integer,
     job.state::text,false;
   END IF;
   RETURN;
  END IF;
  INSERT INTO clinic_app.interop_organizationexport (id,organization_id,
   clinic_id,requested_by_id,state,revision,created_at,started_at,finished_at,
   expires_at,part_count,archive_bytes,archive_sha256,manifest_sha256,
   dataset_count,record_count,attachment_count,failure_reason)
  VALUES (requested_subject,org,requested_clinic,actor,'requested',1,
   statement_timestamp(),NULL,NULL,NULL,0,0,'','',0,0,0,'');
  RETURN QUERY SELECT requested_subject,requested_clinic,1,'requested'::text,true;
 END IF;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.interop_manage(uuid,text,uuid,integer,text,jsonb)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_manage(uuid,text,uuid,integer,text,
 jsonb) TO clinic_app;
"""

_ISSUANCE: Final = r"""
CREATE FUNCTION clinic_app.interop_api_token(requested_client uuid,
 presented_secret text, requested_scopes varchar[], issued_token text)
RETURNS TABLE(outcome text, granted_scopes varchar[], expires_at timestamptz)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE client clinic_app.interop_apiclient; granted varchar[]; expiry timestamptz;
BEGIN
 SELECT * INTO client FROM clinic_app.interop_apiclient c
 WHERE c.id=requested_client AND c.state='active';
 IF client.id IS NULL OR presented_secret !~ '^[0-9a-f]{64}$'
  OR issued_token !~ '^[0-9a-f]{64}$' OR NOT EXISTS (
   SELECT 1 FROM clinic_app.interop_apiclientsecret s
   WHERE s.client_id=client.id AND s.secret_digest=presented_secret
    AND (s.expires_at IS NULL OR s.expires_at>statement_timestamp()))
  OR NOT clinic_app.interop_authority(client.organization_id,client.approved_by_id)
 THEN
  RETURN;
 END IF;
 IF requested_scopes IS NULL OR cardinality(requested_scopes)=0 THEN
  granted:=client.scopes;
 ELSIF requested_scopes <@ client.scopes THEN
  granted:=ARRAY(SELECT DISTINCT s FROM unnest(requested_scopes) s ORDER BY s);
 ELSE
  RETURN QUERY SELECT 'invalid_scope'::text,NULL::varchar[],NULL::timestamptz;
  RETURN;
 END IF;
 expiry:=statement_timestamp()+interval '15 minutes';
 INSERT INTO clinic_app.interop_apiaccesstoken (id,organization_id,client_id,
  token_digest,scopes,issued_at,expires_at)
 VALUES (gen_random_uuid(),client.organization_id,client.id,issued_token,granted,
  statement_timestamp(),expiry);
 RETURN QUERY SELECT 'issued'::text,granted,expiry;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.interop_api_token(uuid,text,varchar[],text)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_api_token(uuid,text,varchar[],text)
 TO clinic_app;
"""

_READER: Final = r"""
CREATE FUNCTION clinic_app.interop_api_read(presented_token text, kek text,
 requested_resource text, selector uuid, window_start timestamptz,
 window_end timestamptz, after_start timestamptz, after_key uuid, page_size integer)
RETURNS SETOF jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s record; required text; prior text;
BEGIN
 SELECT * INTO s FROM clinic_app.interop_api_session(presented_token);
 required:=CASE requested_resource
  WHEN 'appointments' THEN 'appointments:read'
  WHEN 'appointment' THEN 'appointments:read'
  WHEN 'patient' THEN 'patients.demographics:read'
  WHEN 'availability' THEN 'availability:read'
  WHEN 'invoices' THEN 'invoices.status:read'
  WHEN 'invoice' THEN 'invoices.status:read'
  WHEN 'webhooks' THEN 'webhooks:manage' END;
 IF s.client_id IS NULL OR required IS NULL OR NOT required=ANY(s.scopes) THEN
  RAISE EXCEPTION 'interop api denied' USING ERRCODE='42501';
 END IF;
 IF page_size IS NULL OR page_size NOT BETWEEN 1 AND 1001
  OR (requested_resource IN ('appointments','availability','invoices') AND (
   window_start IS NULL OR window_end IS NULL OR window_end<=window_start
   OR window_end-window_start>interval '31 days'))
  OR (requested_resource IN ('appointment','patient','invoice') AND selector IS NULL)
 THEN
  RAISE EXCEPTION 'interop api input invalid' USING ERRCODE='22023';
 END IF;
 IF requested_resource IN ('appointments','appointment') THEN
  RETURN QUERY SELECT jsonb_build_object('id',a.id,'status',a.status,
   'start_at',a.start_at,'end_at',a.end_at,'patient',a.patient_id,
   'practitioner',a.practitioner_id,'service_type',a.service_type_id,
   'created_at',a.created_at,'updated_at',a.updated_at)
  FROM clinic_app.scheduling_appointment a
  WHERE a.organization_id=s.organization_id AND a.clinic_id=s.clinic_id
   AND ((requested_resource='appointment' AND a.id=selector)
    OR (requested_resource='appointments' AND a.start_at>=window_start
     AND a.start_at<window_end AND (after_key IS NULL
      OR (a.start_at,a.id)>(after_start,after_key))))
  ORDER BY a.start_at,a.id LIMIT page_size;
 ELSIF requested_resource='patient' THEN
  prior:=current_setting('app.current_tenant',true);
  PERFORM set_config('app.current_tenant',s.organization_id::text,true);
  RETURN QUERY SELECT jsonb_build_object('id',p.id,
   'display_name',coalesce(
    CASE WHEN d.social_name IS NULL THEN NULL ELSE NULLIF(convert_from(
     clinic_app.tenant_decrypt(kek,'intake.patientdemographics.social_name',
      d.social_name),'UTF8'),'') END,
    CASE WHEN d.legal_name IS NULL THEN NULL ELSE NULLIF(convert_from(
     clinic_app.tenant_decrypt(kek,'intake.patientdemographics.legal_name',
      d.legal_name),'UTF8'),'') END,
    convert_from(clinic_app.tenant_decrypt(kek,'intake.patient.full_name',
     p.full_name),'UTF8')),
   'birth_date',CASE WHEN p.birth_date IS NULL THEN NULL ELSE convert_from(
    clinic_app.tenant_decrypt(kek,'intake.patient.birth_date',p.birth_date),
    'UTF8') END)
  FROM clinic_app.intake_patient p
  LEFT JOIN LATERAL (SELECT x.social_name,x.legal_name
   FROM clinic_app.intake_patientdemographics x
   WHERE x.patient_id=p.id AND x.organization_id=p.organization_id
   ORDER BY x.version DESC LIMIT 1) d ON true
  WHERE p.id=selector AND p.organization_id=s.organization_id
   AND EXISTS (SELECT 1 FROM clinic_app.intake_patientclinicenrollment e
    WHERE e.patient_id=p.id AND e.organization_id=p.organization_id
     AND e.clinic_id=s.clinic_id);
  PERFORM set_config('app.current_tenant',coalesce(prior,''),true);
 ELSIF requested_resource='availability' THEN
  RETURN QUERY SELECT item FROM (
   SELECT jsonb_build_object('kind','window','practitioner',b.practitioner_id,
    'start_at',greatest(b.start_at,window_start),
    'end_at',least(b.end_at,window_end)) AS item,
    greatest(b.start_at,window_start) AS sort_at,b.id AS sort_id
   FROM clinic_app.scheduling_availabilityblock b
   WHERE b.organization_id=s.organization_id AND b.clinic_id=s.clinic_id
    AND b.practitioner_id IS NOT NULL AND b.retired_at IS NULL
    AND b.start_at<window_end AND b.end_at>window_start
    AND (b.template_id IS NULL OR EXISTS (
     SELECT 1 FROM clinic_app.scheduling_availabilitytemplate t
     WHERE t.id=b.template_id AND t.active))
   UNION ALL
   SELECT jsonb_build_object('kind','busy','practitioner',a.practitioner_id,
    'start_at',lower(clinic_app.scheduling_effective_interval(a.start_at,a.end_at,
     a.buffer_before,a.buffer_after)),
    'end_at',upper(clinic_app.scheduling_effective_interval(a.start_at,a.end_at,
     a.buffer_before,a.buffer_after))),
    lower(clinic_app.scheduling_effective_interval(a.start_at,a.end_at,
     a.buffer_before,a.buffer_after)),a.id
   FROM clinic_app.scheduling_appointment a
   WHERE a.organization_id=s.organization_id AND a.clinic_id=s.clinic_id
    AND a.status IN ('held','scheduled','arrived','in_progress')
    AND clinic_app.scheduling_effective_interval(a.start_at,a.end_at,
     a.buffer_before,a.buffer_after) && tstzrange(window_start,window_end,'[)')
   UNION ALL
   SELECT jsonb_build_object('kind','busy','practitioner',x.practitioner_id,
    'start_at',x.start_at,'end_at',x.end_at),x.start_at,x.id
   FROM clinic_app.scheduling_absence x
   WHERE x.organization_id=s.organization_id AND x.clinic_id=s.clinic_id
    AND x.active AND x.practitioner_id IS NOT NULL
    AND x.start_at<window_end AND x.end_at>window_start
   UNION ALL
   SELECT jsonb_build_object('kind','closed','practitioner',NULL,
    'start_at',h.start_at,'end_at',h.end_at),h.start_at,h.id
   FROM clinic_app.scheduling_holiday h
   WHERE h.organization_id=s.organization_id AND h.clinic_id=s.clinic_id
    AND h.active AND h.start_at<window_end AND h.end_at>window_start
  ) entries ORDER BY sort_at,sort_id LIMIT page_size;
 ELSIF requested_resource='webhooks' THEN
  RETURN QUERY SELECT jsonb_build_object('id',w.id,'url',w.url,
   'events',to_jsonb(w.events),'state',w.state,'secret_version',w.secret_version,
   'created_at',w.created_at,'revoked_at',w.revoked_at)
  FROM clinic_app.interop_webhooksubscription w
  WHERE w.client_id=s.client_id AND w.organization_id=s.organization_id
  ORDER BY w.created_at,w.id LIMIT page_size;
 ELSIF requested_resource IN ('invoices','invoice') THEN
  RETURN QUERY SELECT jsonb_build_object('id',i.id,'status',i.state,
   'amount_minor',i.amount_minor,'currency',i.currency,
   'appointment',i.appointment_id,'patient',i.patient_id,'issued_at',i.issued_at)
  FROM clinic_app.billing_invoice i
  WHERE i.organization_id=s.organization_id AND i.clinic_id=s.clinic_id
   AND i.issued_at IS NOT NULL
   AND ((requested_resource='invoice' AND i.id=selector)
    OR (requested_resource='invoices' AND i.issued_at>=window_start
     AND i.issued_at<window_end AND (after_key IS NULL
      OR (i.issued_at,i.id)>(after_start,after_key))))
  ORDER BY i.issued_at,i.id LIMIT page_size;
 END IF;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.interop_api_read(text,text,text,uuid,timestamptz,
 timestamptz,timestamptz,uuid,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_api_read(text,text,text,uuid,
 timestamptz,timestamptz,timestamptz,uuid,integer) TO clinic_app;
"""

_WRITER: Final = r"""
CREATE FUNCTION clinic_app.interop_api_write(presented_token text,
 requested_operation text, idempotency_key text, request_digest text,
 selector uuid, parameters jsonb)
RETURNS TABLE(response_status integer, response_body jsonb, replayed boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE s record; receipt clinic_app.interop_apiwritereceipt;
 sub clinic_app.interop_webhooksubscription; status integer; body jsonb;
 subject uuid; wanted varchar[]; target text;
BEGIN
 SELECT * INTO s FROM clinic_app.interop_api_session(presented_token);
 IF s.client_id IS NULL OR NOT 'webhooks:manage'=ANY(s.scopes) THEN
  RAISE EXCEPTION 'interop api denied' USING ERRCODE='42501';
 END IF;
 IF idempotency_key IS NULL OR idempotency_key !~ '^[A-Za-z0-9._:-]{1,64}$'
  OR request_digest IS NULL OR request_digest !~ '^[0-9a-f]{64}$'
  OR requested_operation NOT IN ('webhook_create','webhook_revoke','webhook_rotate')
  OR selector IS NULL THEN
  RAISE EXCEPTION 'interop api input invalid' USING ERRCODE='22023';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('clinic-lock-v1:interop-write:'
  || s.client_id::text || ':' || idempotency_key,0));
 SELECT * INTO receipt FROM clinic_app.interop_apiwritereceipt r
 WHERE r.client_id=s.client_id AND r.idempotency_key=interop_api_write.idempotency_key;
 IF receipt.id IS NOT NULL THEN
  IF receipt.request_digest=interop_api_write.request_digest
   AND receipt.operation=requested_operation THEN
   RETURN QUERY SELECT receipt.response_status::integer,receipt.response_body,true;
  ELSE
   RETURN QUERY SELECT 409,jsonb_build_object('code','idempotency_conflict'),false;
  END IF;
  RETURN;
 END IF;
 subject:=NULL;
 IF requested_operation='webhook_create' THEN
  target:=parameters->>'url';
  IF jsonb_typeof(parameters->'url')<>'string'
   OR jsonb_typeof(parameters->'events')<>'array'
   OR jsonb_array_length(parameters->'events') NOT BETWEEN 1 AND 4 THEN
   status:=400; body:=jsonb_build_object('code','invalid_input');
  ELSE
   wanted:=ARRAY(SELECT DISTINCT e FROM jsonb_array_elements_text(parameters->'events')
    e ORDER BY e)::varchar[];
   IF NOT wanted <@ ARRAY['appointment.created','appointment.status_changed',
     'appointment.rescheduled','invoice.status_changed']::varchar[]
    OR char_length(target) NOT BETWEEN 12 AND 2048
    OR target !~ '^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~!$&''()*+,;=:@%/-]*)?$'
    OR EXISTS (SELECT 1 FROM unnest(wanted) e WHERE NOT (CASE WHEN e LIKE 'invoice.%'
     THEN 'invoices.status:read' ELSE 'appointments:read' END)=ANY(s.scopes))
    OR (SELECT count(*) FROM clinic_app.interop_webhooksubscription w
     WHERE w.client_id=s.client_id AND w.state='active')>=10
    OR EXISTS (SELECT 1 FROM clinic_app.interop_webhooksubscription w
     WHERE w.id=selector) THEN
    status:=400; body:=jsonb_build_object('code','invalid_input');
   ELSE
    INSERT INTO clinic_app.interop_webhooksubscription (id,organization_id,
     clinic_id,client_id,url,events,state,revision,secret_version,
     previous_secret_until,created_at,revoked_at,revocation_reason)
    VALUES (selector,s.organization_id,s.clinic_id,s.client_id,target,wanted,
     'active',1,1,NULL,statement_timestamp(),NULL,'') RETURNING * INTO sub;
    status:=201; subject:=sub.id;
    body:=jsonb_build_object('id',sub.id,'url',sub.url,'events',to_jsonb(sub.events),
     'state',sub.state,'secret_version',sub.secret_version,
     'created_at',sub.created_at,'revoked_at',sub.revoked_at);
   END IF;
  END IF;
 ELSE
  SELECT * INTO sub FROM clinic_app.interop_webhooksubscription w
  WHERE w.id=selector AND w.client_id=s.client_id FOR UPDATE;
  PERFORM pg_advisory_xact_lock(hashtextextended(
   'clinic-lock-v1:comms-subject:interop-client:' || s.client_id::text,0));
  IF sub.id IS NULL THEN
   status:=404; body:=jsonb_build_object('code','not_found');
  ELSIF requested_operation='webhook_revoke' THEN
   IF sub.state='active' THEN
    UPDATE clinic_app.interop_webhooksubscription w SET state='revoked',
     revoked_at=statement_timestamp(),revocation_reason='client',
     revision=w.revision+1 WHERE w.id=sub.id RETURNING * INTO sub;
   END IF;
   status:=200; subject:=sub.id;
   body:=jsonb_build_object('id',sub.id,'url',sub.url,'events',to_jsonb(sub.events),
    'state',sub.state,'secret_version',sub.secret_version,
    'created_at',sub.created_at,'revoked_at',sub.revoked_at);
  ELSIF sub.state<>'active' THEN
   status:=409; body:=jsonb_build_object('code','subscription_revoked');
  ELSE
   UPDATE clinic_app.interop_webhooksubscription w
   SET secret_version=w.secret_version+1,
    previous_secret_until=statement_timestamp()+interval '24 hours',
    revision=w.revision+1 WHERE w.id=sub.id RETURNING * INTO sub;
   status:=200; subject:=sub.id;
   body:=jsonb_build_object('id',sub.id,'url',sub.url,'events',to_jsonb(sub.events),
    'state',sub.state,'secret_version',sub.secret_version,
    'created_at',sub.created_at,'revoked_at',sub.revoked_at);
  END IF;
 END IF;
 INSERT INTO clinic_app.interop_apiwritereceipt (id,organization_id,client_id,
  idempotency_key,operation,request_digest,response_status,response_body,
  subject_id,created_at)
 VALUES (gen_random_uuid(),s.organization_id,s.client_id,
  interop_api_write.idempotency_key,requested_operation,
  interop_api_write.request_digest,status,body,subject,statement_timestamp());
 RETURN QUERY SELECT status,body,false;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.interop_api_write(text,text,text,text,uuid,jsonb)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_api_write(text,text,text,text,uuid,
 jsonb) TO clinic_app;
"""

_WEBHOOKS: Final = r"""
-- One capture function per table: each trigger's NEW has exactly one row type.
CREATE FUNCTION clinic_app.interop_capture_event()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE kind text; sub record; delivery uuid; payload_digest_value text;
BEGIN
 IF TG_OP='INSERT' THEN kind:='appointment.created';
 ELSIF NEW.status IS DISTINCT FROM OLD.status THEN kind:='appointment.status_changed';
 ELSIF ROW(NEW.start_at,NEW.end_at) IS DISTINCT FROM ROW(OLD.start_at,OLD.end_at)
 THEN kind:='appointment.rescheduled';
 ELSE RETURN NULL; END IF;
 FOR sub IN SELECT w.id,c.approved_by_id
  FROM clinic_app.interop_webhooksubscription w
  JOIN clinic_app.interop_apiclient c ON c.id=w.client_id
   AND c.organization_id=w.organization_id
  WHERE w.organization_id=NEW.organization_id AND w.clinic_id=NEW.clinic_id
   AND w.state='active' AND c.state='active' AND kind=ANY(w.events)
   AND 'appointments:read'=ANY(c.scopes)
  ORDER BY w.id
 LOOP
  delivery:=gen_random_uuid();
  payload_digest_value:=encode(clinic_app.digest('clinic-webhook-delivery-v1:'
   || delivery::text || ':' || kind || ':' || NEW.id::text || ':' || NEW.status,
   'sha256'),'hex');
  INSERT INTO clinic_app.interop_webhookdelivery (id,organization_id,clinic_id,
   subscription_id,event_type,subject_id,subject_state,starts_at,ends_at,
   occurred_at,operation_id,payload_digest)
  VALUES (delivery,NEW.organization_id,NEW.clinic_id,sub.id,kind,NEW.id,
   NEW.status,NEW.start_at,NEW.end_at,statement_timestamp(),delivery,
   payload_digest_value);
  INSERT INTO clinic_app.comms_integrationoperation (id,organization_id,clinic_id,
   actor_id,kind,channel,payload_digest,provider,subject_type,subject_id,status,
   attempt_count,max_attempts,provider_reference,last_callback_event_id,last_error,
   idempotency_key,not_before,created_at,updated_at)
  VALUES (delivery,NEW.organization_id,NEW.clinic_id,sub.approved_by_id,'action','',
   payload_digest_value,'interop-webhook-v1','interop.webhook_delivery',delivery,
   'pending',0,8,
   NULL,NULL,'',delivery,NULL,statement_timestamp(),statement_timestamp());
 END LOOP;
 RETURN NULL;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.interop_capture_event() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_capture_event() TO clinic_owner;

CREATE FUNCTION clinic_app.interop_capture_invoice()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE sub record; delivery uuid; payload_digest_value text;
BEGIN
 IF NEW.state IS NOT DISTINCT FROM OLD.state OR NEW.issued_at IS NULL THEN
  RETURN NULL;
 END IF;
 FOR sub IN SELECT w.id,c.approved_by_id
  FROM clinic_app.interop_webhooksubscription w
  JOIN clinic_app.interop_apiclient c ON c.id=w.client_id
   AND c.organization_id=w.organization_id
  WHERE w.organization_id=NEW.organization_id AND w.clinic_id=NEW.clinic_id
   AND w.state='active' AND c.state='active' AND 'invoice.status_changed'=ANY(w.events)
   AND 'invoices.status:read'=ANY(c.scopes)
  ORDER BY w.id
 LOOP
  delivery:=gen_random_uuid();
  payload_digest_value:=encode(clinic_app.digest('clinic-webhook-delivery-v1:'
   || delivery::text || ':invoice.status_changed:' || NEW.id::text || ':'
   || NEW.state,'sha256'),'hex');
  INSERT INTO clinic_app.interop_webhookdelivery (id,organization_id,clinic_id,
   subscription_id,event_type,subject_id,subject_state,occurred_at,operation_id,
   payload_digest)
  VALUES (delivery,NEW.organization_id,NEW.clinic_id,sub.id,
   'invoice.status_changed',NEW.id,NEW.state,statement_timestamp(),delivery,
   payload_digest_value);
  INSERT INTO clinic_app.comms_integrationoperation (id,organization_id,clinic_id,
   actor_id,kind,channel,payload_digest,provider,subject_type,subject_id,status,
   attempt_count,max_attempts,provider_reference,last_callback_event_id,last_error,
   idempotency_key,not_before,created_at,updated_at)
  VALUES (delivery,NEW.organization_id,NEW.clinic_id,sub.approved_by_id,'action','',
   payload_digest_value,'interop-webhook-v1','interop.webhook_delivery',delivery,
   'pending',0,8,
   NULL,NULL,'',delivery,NULL,statement_timestamp(),statement_timestamp());
 END LOOP;
 RETURN NULL;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.interop_capture_invoice() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_capture_invoice() TO clinic_owner;

CREATE FUNCTION clinic_app.interop_webhook_backoff()
RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 NEW.not_before:=statement_timestamp()+make_interval(mins=>least(60,
  (2^greatest(NEW.attempt_count-1,0))::integer));
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.interop_webhook_backoff() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_webhook_backoff() TO clinic_owner;

CREATE FUNCTION clinic_app.interop_webhook_attempt(operation uuid,
 requested_outcome text, http_status integer)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE o clinic_app.comms_integrationoperation;
BEGIN
 SELECT * INTO o FROM clinic_app.comms_integrationoperation x
 WHERE x.id=operation AND x.subject_type='interop.webhook_delivery'
  AND x.provider='interop-webhook-v1' AND x.status='in_progress';
 IF o.id IS NULL OR requested_outcome IS NULL OR requested_outcome NOT IN
  ('delivered','http_error','timeout','network_error') THEN
  RAISE EXCEPTION 'webhook attempt denied' USING ERRCODE='42501';
 END IF;
 INSERT INTO clinic_app.interop_webhookattempt (id,organization_id,delivery_id,
  attempt,outcome,http_status,recorded_at)
 VALUES (gen_random_uuid(),o.organization_id,o.subject_id,o.attempt_count,
  requested_outcome,http_status,statement_timestamp())
 ON CONFLICT (delivery_id,attempt) DO NOTHING;
 RETURN o.attempt_count;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.interop_webhook_attempt(uuid,text,integer)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_webhook_attempt(uuid,text,integer)
 TO clinic_app;

CREATE FUNCTION clinic_app.interop_offboarding()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE client uuid;
BEGIN
 FOR client IN SELECT c.id FROM clinic_app.interop_apiclient c
  WHERE c.organization_id=NEW.organization_id AND c.state='active' ORDER BY c.id
 LOOP
  PERFORM pg_advisory_xact_lock(hashtextextended(
   'clinic-lock-v1:comms-subject:interop-client:' || client::text,0));
 END LOOP;
 UPDATE clinic_app.interop_apiclient c SET state='revoked',
  revoked_at=statement_timestamp(),revocation_reason='offboarding',
  revision=c.revision+1
 WHERE c.organization_id=NEW.organization_id AND c.state='active';
 UPDATE clinic_app.interop_webhooksubscription w SET state='revoked',
  revoked_at=statement_timestamp(),revocation_reason='offboarding',
  revision=w.revision+1
 WHERE w.organization_id=NEW.organization_id AND w.state='active';
 RETURN NULL;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.interop_offboarding() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_offboarding() TO clinic_owner;

CREATE FUNCTION clinic_app.interop_due_exports()
RETURNS TABLE(export_id uuid, organization_id uuid, requested_by_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT e.id,e.organization_id,e.requested_by_id
 FROM clinic_app.interop_organizationexport e
 WHERE e.state='requested' ORDER BY e.created_at,e.id LIMIT 20
$f$;
REVOKE ALL ON FUNCTION clinic_app.interop_due_exports() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_due_exports() TO clinic_app;

CREATE FUNCTION clinic_app.interop_expire_exports()
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE job clinic_app.interop_organizationexport; changed integer:=0;
BEGIN
 FOR job IN SELECT * FROM clinic_app.interop_organizationexport e
  WHERE (e.state='ready' AND e.expires_at<=statement_timestamp())
   OR (e.state IN ('requested','building')
    AND NOT clinic_app.interop_authority(e.organization_id,e.requested_by_id))
   OR (e.state='building' AND e.started_at<=statement_timestamp()-interval '6 hours')
  ORDER BY e.created_at,e.id LIMIT 100 FOR UPDATE SKIP LOCKED
 LOOP
  IF job.state='ready' THEN
   UPDATE clinic_app.interop_organizationexport e SET state='expired',
    revision=e.revision+1 WHERE e.id=job.id;
  ELSE
   UPDATE clinic_app.interop_organizationexport e SET state='failed',
    failure_reason=CASE WHEN clinic_app.interop_authority(job.organization_id,
     job.requested_by_id) THEN 'internal_error' ELSE 'authority_revoked' END,
    revision=e.revision+1 WHERE e.id=job.id;
  END IF;
  changed:=changed+1;
 END LOOP;
 -- Expired and failed archives keep their receipt rows, never their ciphertext.
 UPDATE clinic_app.interop_organizationexportpart p SET data=NULL
 FROM clinic_app.interop_organizationexport e
 WHERE p.export_id=e.id AND e.state IN ('expired','failed') AND p.data IS NOT NULL;
 RETURN changed;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.interop_expire_exports() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.interop_expire_exports() TO clinic_app;
"""

_TRIGGERS: Final = (
    "\n".join(
        f"""CREATE TRIGGER interop_guard BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.{table} FOR EACH ROW EXECUTE FUNCTION clinic_app.interop_guard();
CREATE TRIGGER interop_retained_truncate BEFORE TRUNCATE ON clinic_app.{table}
 FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.interop_guard();"""
        for table in TABLES
    )
    + """
CREATE TRIGGER interop_capture_event
 AFTER INSERT OR UPDATE OF status, start_at, end_at ON clinic_app.scheduling_appointment
 FOR EACH ROW EXECUTE FUNCTION clinic_app.interop_capture_event();
CREATE TRIGGER interop_capture_event AFTER UPDATE OF state ON clinic_app.billing_invoice
 FOR EACH ROW EXECUTE FUNCTION clinic_app.interop_capture_invoice();
CREATE TRIGGER interop_webhook_backoff BEFORE UPDATE OF status
 ON clinic_app.comms_integrationoperation FOR EACH ROW
 WHEN (NEW.subject_type='interop.webhook_delivery' AND OLD.status='in_progress'
  AND NEW.status='pending')
 EXECUTE FUNCTION clinic_app.interop_webhook_backoff();
CREATE TRIGGER interop_offboarding AFTER UPDATE OF state
 ON clinic_app.subscriptions_subscription FOR EACH ROW
 WHEN (NEW.state='offboarded' AND OLD.state IS DISTINCT FROM 'offboarded')
 EXECUTE FUNCTION clinic_app.interop_offboarding();
"""
)

SQL: Final = (
    _CONSTRAINTS
    + _GRANTS
    + "SET LOCAL ROLE clinic_resolver;\n"
    + _AUTHORITY
    + _GUARD
    + _MANAGE
    + _ISSUANCE
    + _READER
    + _WRITER
    + _WEBHOOKS
    + "RESET ROLE;\n"
    + _POLICIES
    + _TRIGGERS
    + """SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.interop_guard(),
 clinic_app.interop_capture_event(), clinic_app.interop_capture_invoice(),
 clinic_app.interop_webhook_backoff(), clinic_app.interop_offboarding()
 FROM clinic_owner;
RESET ROLE;
"""
)

# Retained partner and export history refuses schema removal, as every other
# retained ledger does. Unapply runs the export reverse first in the same
# transaction, so this refusal rolls both back.
REVERSE: Final = (
    "SET LOCAL ROLE clinic_resolver;\nDO $f$\nBEGIN\n IF "
    + "\n OR ".join(f"EXISTS(SELECT 1 FROM clinic_app.{table})" for table in TABLES)
    + " THEN\n  RAISE EXCEPTION 'interop history requires restore'"
    " USING ERRCODE='55000';\n END IF;\nEND $f$;\nRESET ROLE;\n"
    + "\n".join(
        f"DROP POLICY interop_manage_read ON clinic_app.{table};"
        for table in RUNTIME_READ_TABLES
    )
    + """
DROP POLICY interop_export_update ON clinic_app.interop_organizationexport;
DROP POLICY interop_part_insert ON clinic_app.interop_organizationexportpart;
DROP TRIGGER interop_offboarding ON clinic_app.subscriptions_subscription;
DROP TRIGGER interop_webhook_backoff ON clinic_app.comms_integrationoperation;
DROP TRIGGER interop_capture_event ON clinic_app.billing_invoice;
DROP TRIGGER interop_capture_event ON clinic_app.scheduling_appointment;
"""
    + "\n".join(
        f"DROP TRIGGER interop_retained_truncate ON clinic_app.{table};\n"
        f"DROP TRIGGER interop_guard ON clinic_app.{table};"
        for table in TABLES
    )
    + """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.interop_expire_exports();
DROP FUNCTION clinic_app.interop_due_exports();
DROP FUNCTION clinic_app.interop_offboarding();
DROP FUNCTION clinic_app.interop_webhook_attempt(uuid,text,integer);
DROP FUNCTION clinic_app.interop_webhook_backoff();
DROP FUNCTION clinic_app.interop_capture_event();
DROP FUNCTION clinic_app.interop_capture_invoice();
DROP FUNCTION clinic_app.interop_api_write(text,text,text,text,uuid,jsonb);
DROP FUNCTION clinic_app.interop_api_read(text,text,text,uuid,timestamptz,
 timestamptz,timestamptz,uuid,integer);
DROP FUNCTION clinic_app.interop_api_token(uuid,text,varchar[],text);
DROP FUNCTION clinic_app.interop_manage(uuid,text,uuid,integer,text,jsonb);
DROP FUNCTION clinic_app.interop_guard();
DROP FUNCTION clinic_app.interop_api_session(text);
DROP FUNCTION clinic_app.interop_manager(uuid);
DROP FUNCTION clinic_app.interop_authority(uuid,uuid);
RESET ROLE;
"""
)
