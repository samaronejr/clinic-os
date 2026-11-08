"""Frozen initial insurance posture and database-enforced cross-domain bindings."""

from psycopg import sql

TABLES = (
    "insurance_payer",
    "insurance_payercontract",
    "insurance_tussterm",
    "insurance_pricetable",
    "insurance_insurancemembership",
    "insurance_guide",
    "insurance_batch",
    "insurance_batchguide",
    "insurance_submission",
    "insurance_return",
    "insurance_glosa",
    "insurance_appeal",
    "insurance_clinicalapproval",
    "insurance_paymentmatch",
    "insurance_payerrequest",
    "insurance_commandreceipt",
)
TENANT = "NULLIF(current_setting('app.current_tenant',true),'')::uuid"
ACTOR = "NULLIF(current_setting('app.current_user_id',true),'')::uuid"
_REFERENCES = {
    "insurance_payercontract": {"payer_id": "insurance_payer"},
    "insurance_pricetable": {"contract_id": "insurance_payercontract"},
    "insurance_insurancemembership": {
        "payer_id": "insurance_payer",
        "enrollment_id": "intake_patientclinicenrollment",
    },
    "insurance_guide": {
        "contract_id": "insurance_payercontract",
        "membership_id": "insurance_insurancemembership",
        "encounter_id": "ehr_encounter",
        "invoice_id": "billing_invoice",
        "price_table_id": "insurance_pricetable",
        "predecessor_id": "insurance_guide",
    },
    "insurance_batch": {"contract_id": "insurance_payercontract"},
    "insurance_submission": {"batch_id": "insurance_batch"},
    "insurance_batchguide": {
        "batch_id": "insurance_batch",
        "guide_id": "insurance_guide",
    },
    "insurance_return": {"batch_id": "insurance_batch"},
    "insurance_glosa": {"source_id": "insurance_return", "guide_id": "insurance_guide"},
    "insurance_appeal": {"glosa_id": "insurance_glosa"},
    "insurance_clinicalapproval": {"appeal_id": "insurance_appeal"},
    "insurance_paymentmatch": {
        "guide_id": "insurance_guide",
        "settlement_id": "billing_journalsettlement",
        "allocation_id": "billing_settlementallocation",
    },
    "insurance_payerrequest": {
        "appointment_id": "scheduling_appointment",
        "membership_id": "insurance_insurancemembership",
        "contract_id": "insurance_payercontract",
        "task_id": "workflows_task",
    },
}
_LINK_CASES = "".join(
    f"WHEN '{table}' THEN jsonb_build_object("
    + ",".join(f"'{key}','{target}'" for key, target in links.items())
    + ") "
    for table, links in _REFERENCES.items()
)
_LINK_MATCHES = "\n".join(
    sql.SQL(
        "WHEN {name} THEN EXISTS(SELECT 1 FROM {table} t {book_join} "
        "WHERE t.id=(row_data->>link.key)::uuid "
        "AND t.organization_id=NEW.organization_id "
        "AND {clinic}=NEW.clinic_id)"
    )
    .format(
        name=sql.Literal(table),
        table=sql.Identifier("clinic_app", table),
        book_join=sql.SQL(
            "JOIN clinic_app.billing_journalbook b ON b.id=t.book_id"
            if table in {"billing_journalsettlement", "billing_settlementallocation"}
            else ""
        ),
        clinic=sql.SQL(
            "b.clinic_id"
            if table in {"billing_journalsettlement", "billing_settlementallocation"}
            else "t.clinic_id"
        ),
    )
    .as_string()
    for table in sorted(
        {target for links in _REFERENCES.values() for target in links.values()}
    )
)
_RECEIPT_MATCHES = "\n".join(
    sql.SQL(
        "WHEN {kind} THEN EXISTS(SELECT 1 FROM {table} "
        "WHERE id=NEW.subject_id AND organization_id=NEW.organization_id "
        "AND clinic_id=NEW.clinic_id AND state=NEW.next_state)"
    )
    .format(
        kind=sql.Literal(kind),
        table=sql.Identifier("clinic_app", "insurance_" + kind),
    )
    .as_string()
    for kind in ("guide", "batch", "appeal")
)

FUNCTIONS = """
CREATE FUNCTION clinic_app.insurance_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE row_data jsonb; prior jsonb; links jsonb; link record; allowed boolean;
 matched boolean; old_state text; new_state text; approval_clinic uuid;
 guide clinic_app.insurance_guide; batch clinic_app.insurance_batch;
 appeal clinic_app.insurance_appeal; invoice clinic_app.billing_invoice;
 membership clinic_app.insurance_insurancemembership; source_clinic uuid;
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN
  RAISE EXCEPTION 'insurance history is immutable' USING ERRCODE='23514';
 END IF;
 row_data:=to_jsonb(NEW);
 IF NEW.organization_id IS DISTINCT FROM __TENANT__ OR NOT EXISTS(
 SELECT 1 FROM clinic_app.identity_clinic c WHERE c.id=NEW.clinic_id
 AND c.organization_id=NEW.organization_id) THEN
  RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
 'insurance-clinic-v1:'||NEW.clinic_id::text,0));
 allowed:=clinic_app.has_permission('tiss.manage',NEW.clinic_id,NULL);
 IF TG_TABLE_NAME='insurance_clinicalapproval' THEN
  allowed:=clinic_app.has_permission('tiss.clinical_approve',NEW.clinic_id,NULL);
 ELSIF TG_TABLE_NAME='insurance_commandreceipt'
  AND row_data->>'kind'='clinical_approved' THEN
  allowed:=clinic_app.has_permission('tiss.clinical_approve',NEW.clinic_id,NULL);
 ELSIF TG_TABLE_NAME='insurance_commandreceipt'
  AND row_data->>'kind'='coverage' THEN
  allowed:=clinic_app.has_permission('appointment.book',NEW.clinic_id,NULL)
   OR clinic_app.has_permission('appointment.book_own',NEW.clinic_id,NULL)
   OR clinic_app.has_permission('appointment.move',NEW.clinic_id,NULL)
   OR clinic_app.has_permission('appointment.move_own',NEW.clinic_id,NULL);
 END IF;
 IF NOT allowed THEN
  RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501';
 END IF;
 IF TG_OP='UPDATE' THEN
  prior:=to_jsonb(OLD);
  old_state:=prior->>'state'; new_state:=row_data->>'state';
  IF row_data-'state'<>prior-'state' OR old_state=new_state OR NOT (
   (TG_TABLE_NAME='insurance_guide' AND (
    (old_state='draft' AND new_state='validated') OR
    (old_state='validated' AND new_state='sealed')))
   OR (TG_TABLE_NAME='insurance_batch' AND (
    (old_state='draft' AND new_state='validated') OR
    (old_state='validated' AND new_state='sealed') OR
    (old_state='sealed' AND new_state='submitting') OR
    (old_state='submitting' AND new_state IN ('submitted','unknown')) OR
    (old_state='unknown' AND new_state IN ('submitted','acknowledged')) OR
    (old_state='submitted' AND new_state='acknowledged') OR
    (old_state='acknowledged' AND new_state='returned')))
   OR (TG_TABLE_NAME='insurance_appeal' AND (
    (old_state='draft' AND new_state='approved') OR
    (old_state='approved' AND new_state='submitted')))) THEN
   RAISE EXCEPTION 'insurance command conflict' USING ERRCODE='23514';
  END IF;
  IF TG_TABLE_NAME='insurance_appeal' THEN
   IF new_state IN ('approved','submitted') AND OLD.clinical_assertions IS NOT NULL THEN
   IF NOT EXISTS(SELECT 1 FROM clinic_app.insurance_clinicalapproval a
    JOIN clinic_app.identity_user u ON u.id=a.approved_by_id AND u.is_active
    JOIN clinic_app.identity_userclinicrole r ON r.user_id=u.id
     AND r.clinic_id=OLD.clinic_id AND r.organization_id=OLD.organization_id
     AND r.role='physician' AND r.revoked_at IS NULL
    JOIN clinic_app.identity_professionalregistration p ON p.user_id=u.id
     AND p.clinic_id=OLD.clinic_id AND p.organization_id=OLD.organization_id
     AND p.role=r.role AND p.status='regular' AND p.synthetic
     AND p.revoked_at IS NULL AND p.valid_from<=statement_timestamp()
     AND statement_timestamp()<p.valid_to
    JOIN clinic_app.identity_clinic c ON c.id=OLD.clinic_id
     AND p.jurisdiction=c.crm_uf
    WHERE a.appeal_id=OLD.id AND a.content_sha256=OLD.content_sha256
    AND (p.physician_profile_id IS NULL OR EXISTS(
     SELECT 1 FROM clinic_app.identity_physicianprofile legacy
     WHERE legacy.id=p.physician_profile_id AND legacy.user_id=u.id
      AND legacy.organization_id=OLD.organization_id
      AND legacy.jurisdiction=p.jurisdiction AND legacy.synthetic
      AND legacy.status='regular' AND legacy.last_checked_at<=statement_timestamp()
      AND statement_timestamp()<legacy.recheck_at
      AND (legacy.expires_at IS NULL OR statement_timestamp()<legacy.expires_at)))
    AND NOT EXISTS(SELECT 1 FROM clinic_app.identity_rolegrant n
     WHERE n.organization_id=OLD.organization_id AND n.clinic_id=OLD.clinic_id
     AND n.role=r.role AND n.permission='tiss.clinical_approve'
     AND n.bundle_version=2 AND n.effect='remove'
     AND n.valid_from<=statement_timestamp()
     AND (n.valid_to IS NULL OR statement_timestamp()<n.valid_to))) THEN
    RAISE EXCEPTION 'insurance command conflict' USING ERRCODE='23514';
   END IF;
   END IF;
  END IF;
  RETURN NEW;
 END IF;
 IF NEW.fingerprint!~'^[0-9a-f]{64}$' THEN
  RAISE EXCEPTION 'invalid insurance input' USING ERRCODE='23514';
 END IF;
 links:=CASE TG_TABLE_NAME __LINK_CASES__ ELSE '{}'::jsonb END;
 FOR link IN SELECT * FROM jsonb_each_text(links) LOOP
  IF row_data->>link.key IS NULL THEN CONTINUE; END IF;
  matched:=CASE link.value __LINK_MATCHES__ ELSE false END;
  IF NOT matched THEN
   RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501';
  END IF;
 END LOOP;
 CASE TG_TABLE_NAME
 WHEN 'insurance_payer' THEN
  IF NEW.ans_registry!~'^[0-9]{6}$' OR length(NEW.name)=0 THEN
   RAISE EXCEPTION 'invalid payer identity' USING ERRCODE='23514'; END IF;
 WHEN 'insurance_payercontract' THEN
  IF NEW.version<1 OR (NEW.ends_on IS NOT NULL AND NEW.ends_on<NEW.starts_on) THEN
   RAISE EXCEPTION 'invalid payer contract' USING ERRCODE='23514'; END IF;
 WHEN 'insurance_pricetable' THEN
  IF NEW.version<1 THEN
   RAISE EXCEPTION 'invalid price version' USING ERRCODE='23514'; END IF;
 WHEN 'insurance_tussterm' THEN
  IF NEW.component_version!~'^[0-9]{6}$' OR NEW.source_sha256!~'^[0-9a-f]{64}$'
   OR NEW.table_code!~'^[0-9]{2}$' OR NEW.code!~'^[0-9]{1,10}$'
   OR length(NEW.description) NOT BETWEEN 1 AND 500 THEN
   RAISE EXCEPTION 'invalid terminology version' USING ERRCODE='23514'; END IF;
 WHEN 'insurance_guide' THEN
  SELECT * INTO membership FROM clinic_app.insurance_insurancemembership
   WHERE id=NEW.membership_id;
  SELECT * INTO invoice FROM clinic_app.billing_invoice WHERE id=NEW.invoice_id;
  IF NEW.state<>'draft' OR NEW.kind NOT IN ('consulta','spsadt','honorarios')
   OR NEW.number!~'^[0-9]{1,20}$' OR NEW.total_minor<=0 OR NOT EXISTS(
    SELECT 1 FROM clinic_app.ehr_encounter e
    JOIN clinic_app.intake_patientclinicenrollment p ON p.id=membership.enrollment_id
    JOIN clinic_app.insurance_payercontract c ON c.id=NEW.contract_id
    JOIN clinic_app.insurance_pricetable t ON t.id=NEW.price_table_id
    JOIN clinic_app.billing_invoiceterms terms ON terms.invoice_id=invoice.id
    WHERE e.id=NEW.encounter_id AND p.patient_id=e.patient_id
    AND invoice.patient_id=e.patient_id AND c.payer_id=membership.payer_id
    AND t.contract_id=c.id AND terms.payer='insurer'
    AND terms.net_minor=NEW.total_minor AND terms.completion_entry_id IS NOT NULL
    AND (invoice.encounter_id=e.id OR invoice.appointment_id=e.appointment_id)
   ) OR (NEW.predecessor_id IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM clinic_app.insurance_guide g WHERE g.id=NEW.predecessor_id
    AND g.encounter_id=NEW.encounter_id AND g.invoice_id=NEW.invoice_id
    AND g.membership_id=NEW.membership_id AND g.contract_id=NEW.contract_id
    AND g.state='sealed')) THEN
   RAISE EXCEPTION 'invalid source-backed guide' USING ERRCODE='23514'; END IF;
 WHEN 'insurance_batch' THEN
  IF NEW.state<>'draft' OR NEW.number!~'^[0-9]{1,12}$'
   OR jsonb_typeof(NEW.guide_ids)<>'array'
   OR jsonb_array_length(NEW.guide_ids) NOT BETWEEN 1 AND 100
   OR NEW.content_sha256!~'^[0-9a-f]{64}$'
   OR EXISTS(SELECT 1 FROM jsonb_array_elements_text(NEW.guide_ids) gid
    WHERE NOT EXISTS(SELECT 1 FROM clinic_app.insurance_guide g
     WHERE g.id=gid::uuid AND g.organization_id=NEW.organization_id
     AND g.clinic_id=NEW.clinic_id AND g.contract_id=NEW.contract_id
     AND g.state='validated')) THEN
   RAISE EXCEPTION 'invalid immutable batch' USING ERRCODE='23514'; END IF;
 WHEN 'insurance_submission' THEN
  SELECT * INTO batch FROM clinic_app.insurance_batch WHERE id=NEW.batch_id;
  IF batch.state<>'submitting' OR NEW.content_sha256<>batch.content_sha256
   OR NEW.channel NOT IN ('portal','synthetic_webservice') THEN
   RAISE EXCEPTION 'invalid sealed submission' USING ERRCODE='23514'; END IF;
 WHEN 'insurance_batchguide' THEN
  IF NOT EXISTS(SELECT 1 FROM clinic_app.insurance_batch b
   JOIN clinic_app.insurance_guide g ON g.id=NEW.guide_id
   WHERE b.id=NEW.batch_id AND b.state='draft' AND g.state='validated'
   AND b.contract_id=g.contract_id AND b.guide_ids ? g.id::text) THEN
   RAISE EXCEPTION 'invalid batch guide binding' USING ERRCODE='23514'; END IF;
 WHEN 'insurance_return' THEN
  IF NOT EXISTS(SELECT 1 FROM clinic_app.insurance_batch b WHERE b.id=NEW.batch_id
   AND b.state IN ('acknowledged','returned')) OR NEW.source_sha256!~'^[0-9a-f]{64}$'
  THEN RAISE EXCEPTION 'unacknowledged return' USING ERRCODE='23514'; END IF;
 WHEN 'insurance_glosa' THEN
  IF NEW.sequence<1 OR NEW.charged_minor<>NEW.accepted_minor+NEW.denied_minor
   OR NOT EXISTS(SELECT 1 FROM clinic_app.insurance_return r
    JOIN clinic_app.insurance_batch b ON b.id=r.batch_id
    WHERE r.id=NEW.source_id AND b.guide_ids ? NEW.guide_id::text) THEN
   RAISE EXCEPTION 'invalid adjudication binding' USING ERRCODE='23514'; END IF;
 WHEN 'insurance_appeal' THEN
  IF NEW.state<>'draft' OR NEW.content_sha256!~'^[0-9a-f]{64}$'
   OR NOT EXISTS(SELECT 1 FROM clinic_app.insurance_glosa g
    WHERE g.id=NEW.glosa_id AND g.denied_minor>0) THEN
   RAISE EXCEPTION 'invalid denied appeal' USING ERRCODE='23514'; END IF;
 WHEN 'insurance_clinicalapproval' THEN
  SELECT * INTO appeal FROM clinic_app.insurance_appeal WHERE id=NEW.appeal_id;
  IF NEW.approved_by_id IS DISTINCT FROM __ACTOR__ OR appeal.state<>'draft'
   OR appeal.content_sha256<>NEW.content_sha256 THEN
   RAISE EXCEPTION 'insurance access denied' USING ERRCODE='42501'; END IF;
 WHEN 'insurance_paymentmatch' THEN
  SELECT * INTO guide FROM clinic_app.insurance_guide WHERE id=NEW.guide_id;
  IF NEW.amount_minor<=0 OR NOT EXISTS(
   SELECT 1 FROM clinic_app.billing_settlementallocation a
   JOIN clinic_app.billing_journalsettlement s ON s.id=a.settlement_id
   JOIN clinic_app.billing_invoiceterms t ON t.id=a.terms_id
   WHERE a.id=NEW.allocation_id AND a.settlement_id=NEW.settlement_id
   AND a.amount_minor=NEW.amount_minor AND t.invoice_id=guide.invoice_id
   AND t.payer='insurer' AND s.kind='insurer' AND s.state='posted'
   AND a.entry_id IS NOT NULL) THEN
   RAISE EXCEPTION 'unverified payer payment' USING ERRCODE='23514'; END IF;
 WHEN 'insurance_payerrequest' THEN
  IF NEW.kind NOT IN ('eligibility','authorization')
   OR NEW.outcome NOT IN ('eligible','authorized','denied','unknown')
   OR NOT EXISTS(SELECT 1 FROM clinic_app.insurance_payercontract c
    JOIN clinic_app.insurance_insurancemembership m ON m.payer_id=c.payer_id
    WHERE c.id=NEW.contract_id AND m.id=NEW.membership_id
    AND ((NEW.kind='eligibility' AND c.eligibility_supported)
    OR (NEW.kind='authorization' AND c.authorization_supported))) THEN
   RAISE EXCEPTION 'unsupported payer request' USING ERRCODE='23514'; END IF;
 WHEN 'insurance_commandreceipt' THEN
  IF NEW.kind NOT IN ('guide','batch','appeal','coverage') THEN
   RAISE EXCEPTION 'invalid transition receipt' USING ERRCODE='23514'; END IF;
  matched:=CASE NEW.kind WHEN 'coverage' THEN NEW.next_state='covered'
   __RECEIPT_MATCHES__ ELSE false END;
  IF NOT matched THEN
   RAISE EXCEPTION 'invalid transition receipt' USING ERRCODE='23514'; END IF;
 ELSE NULL;
 END CASE;
 NEW.created_at:=statement_timestamp();
 RETURN NEW;
END $f$;

CREATE FUNCTION clinic_app.insurance_guide_source(
 clinic uuid,encounter uuid,invoice uuid,membership uuid)
RETURNS TABLE(organization_id uuid,physician_id uuid,service_type_id uuid,
 performed_on date,amount_minor bigint)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT e.organization_id,e.physician_id,a.service_type_id,
 (coalesce(a.start_at,e.created_at) AT TIME ZONE c.timezone)::date,t.net_minor
 FROM clinic_app.ehr_encounter e
 JOIN clinic_app.identity_clinic c ON c.id=e.clinic_id
 JOIN clinic_app.billing_invoice i ON i.id=invoice AND i.clinic_id=e.clinic_id
  AND i.organization_id=e.organization_id AND i.patient_id=e.patient_id
  AND (i.encounter_id=e.id OR i.appointment_id=e.appointment_id)
 JOIN clinic_app.billing_invoiceterms t ON t.invoice_id=i.id AND t.payer='insurer'
  AND t.completion_entry_id IS NOT NULL
 JOIN clinic_app.insurance_insurancemembership m ON m.id=membership
  AND m.clinic_id=e.clinic_id AND m.organization_id=e.organization_id
 JOIN clinic_app.intake_patientclinicenrollment p ON p.id=m.enrollment_id
  AND p.patient_id=e.patient_id AND p.clinic_id=e.clinic_id
 LEFT JOIN clinic_app.scheduling_appointment a ON a.id=e.appointment_id
 WHERE e.id=encounter AND e.clinic_id=clinic AND e.organization_id=__TENANT__
 AND i.state IN ('open','paid')
 AND clinic_app.has_permission('tiss.manage',clinic,NULL)
$f$;
"""
FUNCTIONS = (
    FUNCTIONS.replace("__LINK_CASES__", _LINK_CASES)
    .replace("__LINK_MATCHES__", _LINK_MATCHES)
    .replace("__RECEIPT_MATCHES__", _RECEIPT_MATCHES)
    .replace("__TENANT__", TENANT)
    .replace("__ACTOR__", ACTOR)
)

GRANTS = (
    "".join(
        f"GRANT SELECT ON clinic_app.{table} TO clinic_resolver;\n" for table in TABLES
    )
    + "GRANT INSERT ON clinic_app.insurance_commandreceipt TO clinic_resolver;\n"
    + "SET LOCAL ROLE clinic_resolver;\n"
)
FUNCTION_PRIVILEGES = """
REVOKE ALL ON FUNCTION clinic_app.insurance_guard()
 FROM PUBLIC,clinic_app,clinic_agent,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.insurance_guard() TO clinic_owner;
REVOKE ALL ON FUNCTION clinic_app.insurance_guide_source(uuid,uuid,uuid,uuid)
 FROM PUBLIC,clinic_agent,clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.insurance_guide_source(uuid,uuid,uuid,uuid)
 TO clinic_app;
RESET ROLE;
"""


def posture() -> str:
    """Generate only migration-owned identifiers and closed permission constants."""
    statements: list[str] = []
    for table in TABLES:
        read = "clinic_app.has_permission('tiss.read',clinic_id,NULL)"
        write = "clinic_app.has_permission('tiss.manage',clinic_id,NULL)"
        if table in {"insurance_appeal", "insurance_clinicalapproval"}:
            clinical = (
                "clinic_app.has_permission('tiss.clinical_approve',clinic_id,NULL)"
            )
            read = f"({read} OR {clinical})"
            if table == "insurance_clinicalapproval":
                write = clinical
        if table == "insurance_commandreceipt":
            write = (
                f"(({write} OR (kind='clinical_approved' AND "
                "clinic_app.has_permission('tiss.clinical_approve',clinic_id,NULL)))"
                " AND kind<>'coverage')"
            )
            read = (
                f"({read} OR (kind='clinical_approved' AND "
                "clinic_app.has_permission('tiss.clinical_approve',clinic_id,NULL)))"
            )
        statements.append(
            f"REVOKE ALL ON clinic_app.{table} FROM clinic_app,clinic_agent,PUBLIC;"
            f"GRANT SELECT,INSERT ON clinic_app.{table} TO clinic_app;"
            f"ALTER TABLE clinic_app.{table} ENABLE ROW LEVEL SECURITY;"
            f"ALTER TABLE clinic_app.{table} FORCE ROW LEVEL SECURITY;"
            f"CREATE POLICY insurance_scope ON clinic_app.{table} TO clinic_app "
            f"USING (organization_id={TENANT} AND {read}) "
            f"WITH CHECK (organization_id={TENANT} AND {write});"
            f"CREATE TRIGGER insurance_guard BEFORE INSERT OR UPDATE OR DELETE "
            f"ON clinic_app.{table} FOR EACH ROW "
            "EXECUTE FUNCTION clinic_app.insurance_guard();"
            f"CREATE TRIGGER insurance_no_truncate BEFORE TRUNCATE "
            f"ON clinic_app.{table} FOR EACH STATEMENT "
            "EXECUTE FUNCTION clinic_app.insurance_guard();"
        )
        if table in {"insurance_guide", "insurance_batch", "insurance_appeal"}:
            statements.append(
                f"GRANT UPDATE(state) ON clinic_app.{table} TO clinic_app;"
            )
    statements.extend(
        (
            "CREATE UNIQUE INDEX insurance_guide_number ON "
            "clinic_app.insurance_guide(clinic_id,number);",
            "CREATE UNIQUE INDEX insurance_guide_invoice_root ON "
            "clinic_app.insurance_guide(clinic_id,invoice_id) "
            "WHERE predecessor_id IS NULL;",
            "CREATE UNIQUE INDEX insurance_batch_number ON "
            "clinic_app.insurance_batch(clinic_id,number);",
            "CREATE UNIQUE INDEX insurance_glosa_line ON "
            "clinic_app.insurance_glosa(source_id,guide_id,sequence);",
            "CREATE UNIQUE INDEX insurance_contract_version ON "
            "clinic_app.insurance_payercontract(clinic_id,payer_id,version);",
            "CREATE UNIQUE INDEX insurance_price_version ON "
            "clinic_app.insurance_pricetable(clinic_id,contract_id,version);",
            "SET LOCAL ROLE clinic_resolver;",
            "REVOKE EXECUTE ON FUNCTION clinic_app.insurance_guard() "
            "FROM clinic_owner;",
            "RESET ROLE;",
        )
    )
    return "\n".join(statements)
