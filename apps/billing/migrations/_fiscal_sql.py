"""Immutable external reviews, exact Finance RLS and synthetic-only transitions."""

from apps.tenancy.migrations_support import revoke_default_dml

TABLES = ("fiscalconfiguration", "fiscaldocument", "fiscaltransition")
UPDATE_COLUMNS = (
    "status",
    "last_command_id",
    "xml_payload",
    "pdf_payload",
    "cancellation_payload",
    "xml_reference",
    "pdf_reference",
    "provider_reference",
    "rejection_code",
)

SQL = "".join(revoke_default_dml(f"billing_{table}", ()) for table in TABLES)
SQL += """
REVOKE ALL ON clinic_app.billing_fiscalconfiguration,
 clinic_app.billing_fiscaldocument,clinic_app.billing_fiscaltransition
 FROM PUBLIC,clinic_app,clinic_agent;
GRANT SELECT ON clinic_app.billing_fiscalconfiguration,
 clinic_app.billing_fiscaldocument,clinic_app.billing_fiscaltransition
 TO clinic_app,clinic_resolver;
GRANT INSERT ON clinic_app.billing_fiscaldocument TO clinic_app;
GRANT INSERT ON clinic_app.billing_fiscaltransition TO clinic_resolver;
"""
SQL += (
    "GRANT UPDATE("
    + ",".join(UPDATE_COLUMNS)
    + ") ON clinic_app.billing_fiscaldocument TO clinic_app;"
)
SQL += """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.billing_fiscal_access(requested_clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT NULLIF(current_setting('app.current_patient_session',true),'') IS NULL
 AND NULLIF(current_setting('app.current_principal',true),'') IS NULL
 AND clinic_app.has_permission('settlement.post',requested_clinic,NULL)
$f$;

CREATE FUNCTION clinic_app.billing_fiscal_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE cfg clinic_app.billing_fiscalconfiguration;
 charge clinic_app.billing_invoice;
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE')
 OR TG_TABLE_NAME='billing_fiscaltransition' THEN
  RAISE EXCEPTION 'fiscal history is immutable' USING ERRCODE='23514';
 END IF;
 IF TG_TABLE_NAME='billing_fiscalconfiguration' THEN
  IF TG_OP<>'INSERT' THEN
   RAISE EXCEPTION 'fiscal review is immutable' USING ERRCODE='23514';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM clinic_app.billing_journalbook b
  WHERE b.id=NEW.book_id AND b.clinic_id=NEW.clinic_id
  AND b.organization_id=NEW.organization_id)
  OR NEW.provider NOT IN ('national','aggregator')
  OR NEW.review_kind NOT IN ('unreviewed','synthetic_fixture','eg9')
  OR NEW.ibs_cbs_applicability NOT IN ('undecided','applicable','not_applicable')
  OR NEW.cancellation_window_seconds<1 THEN
   RAISE EXCEPTION 'invalid fiscal configuration' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
 END IF;
 IF NOT clinic_app.billing_fiscal_access(NEW.clinic_id) THEN
  RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
 END IF;
 SELECT * INTO cfg FROM clinic_app.billing_fiscalconfiguration
 WHERE id=NEW.configuration_id;
 IF cfg.id IS NULL OR cfg.organization_id IS DISTINCT FROM NEW.organization_id
 OR cfg.clinic_id IS DISTINCT FROM NEW.clinic_id
 OR cfg.book_id IS DISTINCT FROM NEW.book_id
 OR cfg.legal_entity_reference IS DISTINCT FROM NEW.legal_entity_reference
 OR cfg.provider IS DISTINCT FROM NEW.provider
 OR cfg.municipality_code IS DISTINCT FROM NEW.municipality_code
 OR cfg.service_code IS DISTINCT FROM NEW.service_code
 OR cfg.review_reference IS NULL OR cfg.reviewed_at IS NULL
 OR cfg.reviewed_at>statement_timestamp() OR cfg.expires_at IS NULL
 OR cfg.ibs_cbs_applicability NOT IN ('applicable','not_applicable')
 OR (NEW.rehearsal AND cfg.review_kind<>'synthetic_fixture')
 OR (NOT NEW.rehearsal AND cfg.review_kind<>'eg9') THEN
  RAISE EXCEPTION 'fiscal configuration unreviewed' USING ERRCODE='23514';
 END IF;
 IF NOT NEW.rehearsal AND NEW.status<>'draft' THEN
  RAISE EXCEPTION 'BLOCKED-ON-EG' USING ERRCODE='23514';
 END IF;
 IF TG_OP='INSERT' THEN
  SELECT * INTO charge FROM clinic_app.billing_invoice WHERE id=NEW.invoice_id;
  IF charge.id IS NULL OR charge.organization_id IS DISTINCT FROM NEW.organization_id
  OR charge.clinic_id IS DISTINCT FROM NEW.clinic_id
  OR charge.state NOT IN ('open','paid') OR charge.currency<>'BRL'
  OR NEW.amount_minor IS DISTINCT FROM charge.amount_minor
  OR cfg.expires_at<=statement_timestamp()
  OR NEW.status<>'draft' OR NEW.create_command_id<>NEW.last_command_id
  OR octet_length(NEW.create_fingerprint)<>32
  OR NEW.submitted_at IS NOT NULL OR NEW.authorized_at IS NOT NULL
  OR NEW.cancel_until IS NOT NULL OR NEW.cancelled_at IS NOT NULL
  OR NEW.xml_reference IS NOT NULL OR NEW.pdf_reference IS NOT NULL
  OR NEW.xml_payload IS NOT NULL OR NEW.pdf_payload IS NOT NULL
  OR NEW.cancellation_payload IS NOT NULL OR NEW.provider_reference IS NOT NULL
  OR NEW.rejection_code<>'' THEN
   RAISE EXCEPTION 'invalid fiscal document binding' USING ERRCODE='23514';
  END IF;
  -- The same purpose protects the reviewed terms and their immutable snapshot.
  -- A direct runtime INSERT cannot substitute unreviewed tax fields.
  NEW.tax_fields := cfg.tax_fields;
  RETURN NEW;
 END IF;
 IF to_jsonb(NEW)-ARRAY['status','last_command_id','xml_payload','pdf_payload',
 'cancellation_payload','xml_reference','pdf_reference','provider_reference',
 'rejection_code'] IS DISTINCT FROM
 to_jsonb(OLD)-ARRAY['status','last_command_id','xml_payload','pdf_payload',
 'cancellation_payload','xml_reference','pdf_reference','provider_reference',
 'rejection_code'] OR NEW.last_command_id=OLD.last_command_id THEN
  RAISE EXCEPTION 'immutable fiscal terms' USING ERRCODE='23514';
 END IF;
 IF OLD.status='draft' AND NEW.status='submitted' THEN
  IF cfg.expires_at<=statement_timestamp()
  OR ROW(NEW.xml_payload,NEW.pdf_payload,NEW.cancellation_payload,
   NEW.xml_reference,NEW.pdf_reference,NEW.provider_reference,NEW.rejection_code)
  IS DISTINCT FROM ROW(OLD.xml_payload,OLD.pdf_payload,OLD.cancellation_payload,
   OLD.xml_reference,OLD.pdf_reference,OLD.provider_reference,OLD.rejection_code) THEN
   RAISE EXCEPTION 'invalid fiscal submission' USING ERRCODE='23514';
  END IF;
  NEW.submitted_at := statement_timestamp();
 ELSIF OLD.status='submitted' AND NEW.status='authorized' THEN
  IF NEW.xml_payload IS NULL OR NEW.xml_reference IS DISTINCT FROM NEW.id
  OR NEW.provider_reference IS NULL OR NEW.rejection_code<>''
  OR NEW.cancellation_payload IS NOT NULL
  OR (NEW.pdf_payload IS NULL AND NEW.pdf_reference IS NOT NULL)
  OR (NEW.pdf_payload IS NOT NULL AND NEW.pdf_reference IS DISTINCT FROM NEW.id) THEN
   RAISE EXCEPTION 'invalid fiscal authorization' USING ERRCODE='23514';
  END IF;
  NEW.authorized_at := statement_timestamp();
  NEW.cancel_until := NEW.authorized_at
   +make_interval(secs=>cfg.cancellation_window_seconds);
 ELSIF OLD.status='submitted' AND NEW.status='rejected' THEN
  IF NEW.rejection_code !~ '^[A-Z][A-Z0-9_]{0,15}$'
  OR NEW.xml_payload IS NOT NULL OR NEW.pdf_payload IS NOT NULL
  OR NEW.cancellation_payload IS NOT NULL OR NEW.xml_reference IS NOT NULL
  OR NEW.pdf_reference IS NOT NULL OR NEW.provider_reference IS NOT NULL THEN
   RAISE EXCEPTION 'invalid fiscal rejection' USING ERRCODE='23514';
  END IF;
 ELSIF OLD.status IN ('draft','authorized') AND NEW.status='cancelled' THEN
  IF ROW(NEW.xml_payload,NEW.pdf_payload,NEW.xml_reference,NEW.pdf_reference,
   NEW.provider_reference,NEW.rejection_code)
  IS DISTINCT FROM ROW(OLD.xml_payload,OLD.pdf_payload,OLD.xml_reference,
   OLD.pdf_reference,OLD.provider_reference,OLD.rejection_code)
  OR (OLD.status='authorized' AND (OLD.cancel_until IS NULL
   OR statement_timestamp()>=OLD.cancel_until
   OR NEW.cancellation_payload IS NULL))
  OR (OLD.status='draft' AND NEW.cancellation_payload IS NOT NULL) THEN
   RAISE EXCEPTION 'fiscal cancellation refused' USING ERRCODE='23514';
  END IF;
  NEW.cancelled_at := statement_timestamp();
 ELSE
  RAISE EXCEPTION 'fiscal transition denied' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $f$;

CREATE FUNCTION clinic_app.billing_fiscal_snapshot()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 INSERT INTO clinic_app.billing_fiscaltransition
 (id,organization_id,document_id,command_id,from_status,to_status,actor_id,created_at)
 VALUES(gen_random_uuid(),NEW.organization_id,NEW.id,NEW.last_command_id,
 CASE WHEN TG_OP='INSERT' THEN '' ELSE OLD.status END,NEW.status,
 NULLIF(current_setting('app.current_user_id',true),'')::uuid,
 statement_timestamp());
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.billing_fiscal_access(uuid),
 clinic_app.billing_fiscal_guard(),clinic_app.billing_fiscal_snapshot()
 FROM PUBLIC,clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.billing_fiscal_access(uuid) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.billing_fiscal_access(uuid),
 clinic_app.billing_fiscal_guard(),clinic_app.billing_fiscal_snapshot()
 TO clinic_owner;
RESET ROLE;
"""
for _table in TABLES:
    _name = f"billing_{_table}"
    SQL += (
        f"ALTER TABLE clinic_app.{_name} ENABLE ROW LEVEL SECURITY;"
        f"ALTER TABLE clinic_app.{_name} FORCE ROW LEVEL SECURITY;"
    )
    _predicate = (
        "clinic_app.billing_fiscal_access(clinic_id)"
        if _table != "fiscaltransition"
        else "EXISTS(SELECT 1 FROM clinic_app.billing_fiscaldocument d "
        "WHERE d.id=document_id AND clinic_app.billing_fiscal_access(d.clinic_id))"
    )
    SQL += (
        f"CREATE POLICY fiscal_read ON clinic_app.{_name} FOR SELECT TO clinic_app "
        f"USING(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid"
        f" AND ({_predicate}));"
        f"CREATE TRIGGER fiscal_no_truncate BEFORE TRUNCATE ON clinic_app.{_name}"
        " FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.billing_fiscal_guard();"
    )
    if _table == "fiscaltransition":
        SQL += (
            f"CREATE TRIGGER fiscal_immutable BEFORE UPDATE OR DELETE "
            f"ON clinic_app.{_name} FOR EACH ROW "
            "EXECUTE FUNCTION clinic_app.billing_fiscal_guard();"
        )
    else:
        SQL += (
            f"CREATE TRIGGER fiscal_guard BEFORE INSERT OR UPDATE OR DELETE "
            f"ON clinic_app.{_name} FOR EACH ROW "
            "EXECUTE FUNCTION clinic_app.billing_fiscal_guard();"
        )
SQL += """
CREATE POLICY fiscal_review_owner ON clinic_app.billing_fiscalconfiguration
FOR ALL TO clinic_owner
USING(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid)
WITH CHECK(organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid);
CREATE POLICY fiscal_insert ON clinic_app.billing_fiscaldocument
FOR INSERT TO clinic_app WITH CHECK(organization_id=
NULLIF(current_setting('app.current_tenant',true),'')::uuid
AND clinic_app.billing_fiscal_access(clinic_id));
CREATE POLICY fiscal_update ON clinic_app.billing_fiscaldocument
FOR UPDATE TO clinic_app
USING(clinic_app.billing_fiscal_access(clinic_id))
WITH CHECK(clinic_app.billing_fiscal_access(clinic_id));
CREATE TRIGGER fiscal_snapshot AFTER INSERT OR UPDATE
ON clinic_app.billing_fiscaldocument FOR EACH ROW
EXECUTE FUNCTION clinic_app.billing_fiscal_snapshot();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.billing_fiscal_access(uuid),
 clinic_app.billing_fiscal_guard(),clinic_app.billing_fiscal_snapshot()
 FROM clinic_owner;
RESET ROLE;
"""
