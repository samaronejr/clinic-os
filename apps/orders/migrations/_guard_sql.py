"""Independent tenant, source, lifecycle and professional write guards."""

FUNCTIONS = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.orders_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE o clinic_app.orders_order; v clinic_app.orders_resultversion;
 previous clinic_app.orders_orderevent; matched clinic_app.orders_resultmatch;
 enrollment uuid; permission text; valid boolean;
BEGIN
 IF TG_OP<>'INSERT' THEN
  IF TG_TABLE_NAME='orders_labaccount' AND TG_OP='UPDATE'
  AND session_user='clinic_owner' AND current_setting('role')<>'clinic_app'
  AND OLD.active AND NOT NEW.active AND
  (to_jsonb(NEW)-'active')=(to_jsonb(OLD)-'active') THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'order/result history is immutable' USING ERRCODE='23514';
 END IF;
 IF NEW.organization_id IS DISTINCT FROM __TENANT__ OR NOT EXISTS(
 SELECT 1 FROM clinic_app.identity_clinic c WHERE c.id=NEW.clinic_id
 AND c.organization_id=NEW.organization_id) THEN
 RAISE EXCEPTION 'invalid result scope' USING ERRCODE='23514'; END IF;
 CASE TG_TABLE_NAME
 WHEN 'orders_labaccount' THEN
  IF session_user<>'clinic_owner' OR current_setting('role')='clinic_app'
  OR NEW.provider<>'synthetic-lab-v1' OR NEW.signing_key_digest!~'^[0-9a-f]{64}$'
  OR NOT EXISTS(SELECT 1 FROM clinic_app.identity_serviceprincipal p
   WHERE p.id=NEW.principal_id AND p.clinic_id=NEW.clinic_id
   AND p.organization_id=NEW.organization_id AND p.active AND p.purpose='lab_ingest')
  THEN RAISE EXCEPTION 'invalid lab account' USING ERRCODE='42501'; END IF;
 WHEN 'orders_order' THEN
  IF NEW.ordered_by_id IS DISTINCT FROM __ACTOR__
  OR NOT clinic_app.has_permission('order.place',NEW.clinic_id,NEW.enrollment_id)
  OR NOT EXISTS(SELECT 1 FROM clinic_app.intake_patientclinicenrollment e
  WHERE e.id=NEW.enrollment_id AND e.clinic_id=NEW.clinic_id
  AND e.organization_id=NEW.organization_id)
  OR NEW.fingerprint!~'^[0-9a-f]{64}$' OR (NEW.lab_account_id IS NOT NULL
  AND NOT EXISTS(SELECT 1 FROM clinic_app.orders_labaccount a
  WHERE a.id=NEW.lab_account_id AND a.clinic_id=NEW.clinic_id
  AND a.organization_id=NEW.organization_id AND a.active))
  THEN RAISE EXCEPTION 'order denied' USING ERRCODE='42501'; END IF;
 WHEN 'orders_orderitem', 'orders_orderevent' THEN
  SELECT * INTO o FROM clinic_app.orders_order r WHERE r.id=NEW.order_id
  AND r.clinic_id=NEW.clinic_id AND r.organization_id=NEW.organization_id;
  IF o.id IS NULL OR NOT clinic_app.has_permission(
  'order.place',NEW.clinic_id,o.enrollment_id) THEN
   RAISE EXCEPTION 'order denied' USING ERRCODE='42501'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('orders-series:'||o.id,0));
  SELECT * INTO previous FROM clinic_app.orders_orderevent e
   WHERE e.order_id=o.id ORDER BY e.sequence DESC LIMIT 1;
  IF TG_TABLE_NAME='orders_orderitem' THEN
   IF previous.state IS NOT NULL OR NEW.catalog_version<>1
   OR NEW.exam_code NOT IN ('cbc','urinalysis') THEN
   RAISE EXCEPTION 'order items immutable' USING ERRCODE='23514'; END IF;
  ELSE
   IF NEW.actor_id IS DISTINCT FROM __ACTOR__
   OR NEW.sequence<>coalesce(previous.sequence,0)+1 OR NOT (
    (previous.id IS NULL AND NEW.state='draft') OR
    (previous.state='draft' AND NEW.state='placed') OR
    (previous.state='placed' AND NEW.state IN ('collected','in_process')) OR
    (previous.state='collected' AND NEW.state='in_process'))
   THEN RAISE EXCEPTION 'order transition denied' USING ERRCODE='23514'; END IF;
  END IF;
 WHEN 'orders_result' THEN
  IF NOT (clinic_app.orders_lab_account(NEW.clinic_id) IS NOT NULL
  OR clinic_app.has_permission('demographics.read',NEW.clinic_id,NULL))
  THEN RAISE EXCEPTION 'result denied' USING ERRCODE='42501'; END IF;
 WHEN 'orders_resultversion' THEN
  IF NOT clinic_app.orders_ingest_allowed(NEW.clinic_id,NEW.proposed_order_id)
  OR NEW.uploader_id IS DISTINCT FROM __ACTOR__
  OR NEW.lab_account_id IS DISTINCT FROM clinic_app.orders_lab_account(NEW.clinic_id)
  OR NOT EXISTS(SELECT 1 FROM clinic_app.orders_result r
  WHERE r.id=NEW.result_id AND r.clinic_id=NEW.clinic_id
  AND r.organization_id=NEW.organization_id) THEN
   RAISE EXCEPTION 'result denied' USING ERRCODE='42501'; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('orders-series:'||NEW.result_id,0));
  SELECT * INTO v FROM clinic_app.orders_resultversion r
   WHERE r.result_id=NEW.result_id ORDER BY r.number DESC LIMIT 1;
  IF NEW.number<>coalesce(v.number,0)+1 OR NEW.predecessor_id IS DISTINCT FROM v.id
  OR (v.id IS NOT NULL AND (NEW.proposed_order_id IS DISTINCT FROM v.proposed_order_id
   OR NEW.lab_account_id IS DISTINCT FROM v.lab_account_id))
  OR NEW.storage_key!~'^[0-9a-f]{64}$' OR NEW.sha256!~'^[0-9a-f]{64}$'
  OR NEW.fingerprint!~'^[0-9a-f]{64}$' OR octet_length(NEW.identity)<68
  OR NEW.size_bytes NOT BETWEEN 1 AND 10485760
  OR NEW.content_type NOT IN ('application/pdf','image/jpeg','image/png')
  THEN RAISE EXCEPTION 'invalid source version' USING ERRCODE='23514'; END IF;
 WHEN 'orders_resultobservation' THEN
  SELECT * INTO v FROM clinic_app.orders_resultversion r WHERE r.id=NEW.version_id;
  IF v.clinic_id IS DISTINCT FROM NEW.clinic_id
  OR v.organization_id IS DISTINCT FROM NEW.organization_id
  OR NOT clinic_app.orders_ingest_allowed(NEW.clinic_id,v.proposed_order_id)
  OR v.lab_account_id IS DISTINCT FROM clinic_app.orders_lab_account(NEW.clinic_id)
  OR v.uploader_id IS DISTINCT FROM __ACTOR__
  OR octet_length(NEW.source)<68
  OR NEW.position<>(SELECT count(*) FROM clinic_app.orders_resultobservation r
    WHERE r.version_id=v.id)
  OR EXISTS(SELECT 1 FROM clinic_app.orders_resultmatch m WHERE m.version_id=v.id)
  OR EXISTS(SELECT 1 FROM clinic_app.orders_resultquarantine q WHERE q.version_id=v.id)
  THEN RAISE EXCEPTION 'invalid source observation' USING ERRCODE='23514'; END IF;
 ELSE
  SELECT * INTO v FROM clinic_app.orders_resultversion r WHERE r.id=NEW.version_id;
  IF v.clinic_id IS DISTINCT FROM NEW.clinic_id
  OR v.organization_id IS DISTINCT FROM NEW.organization_id THEN
  RAISE EXCEPTION 'invalid result scope' USING ERRCODE='23514'; END IF;
  IF TG_TABLE_NAME IN ('orders_resultacknowledgment','orders_resultrelease',
    'orders_resultrejection') THEN
   PERFORM pg_advisory_xact_lock(hashtextextended('orders-series:'||v.result_id,0));
   IF NOT clinic_app.orders_is_current(v.id) THEN
    RAISE EXCEPTION 'source superseded' USING ERRCODE='40001'; END IF;
   SELECT * INTO matched FROM clinic_app.orders_resultmatch m WHERE m.version_id=v.id;
   SELECT r.enrollment_id INTO enrollment FROM clinic_app.orders_order r
    WHERE r.id=matched.order_id;
   IF TG_TABLE_NAME='orders_resultrejection' THEN
    valid:=clinic_app.has_permission('order.route',NEW.clinic_id,NULL)
     AND NEW.operator_id IS NOT DISTINCT FROM __ACTOR__
     AND matched.id IS NULL AND NEW.evidence_code IN (
      'wrong_patient_confirmed','invalid_source_confirmed')
     AND EXISTS(SELECT 1 FROM clinic_app.orders_resultquarantine q
       WHERE q.version_id=v.id);
   ELSE
    permission:=CASE WHEN TG_TABLE_NAME='orders_resultacknowledgment'
     THEN 'result.acknowledge' ELSE 'result.release' END;
    valid:=matched.id IS NOT NULL AND
     clinic_app.has_permission(permission,NEW.clinic_id,enrollment);
    IF TG_TABLE_NAME='orders_resultacknowledgment' THEN
     valid:=valid AND NEW.reviewer_id IS NOT DISTINCT FROM __ACTOR__
      AND NEW.disposition IN ('no_action','follow_up','urgent_review');
    ELSE
     valid:=valid AND NEW.released_by_id IS NOT DISTINCT FROM __ACTOR__
      AND EXISTS(SELECT 1 FROM clinic_app.orders_resultacknowledgment a
       WHERE a.id=NEW.acknowledgment_id AND a.version_id=v.id)
      AND ((NEW.audience='patient' AND NEW.delegate_grant_id IS NULL)
       OR (NEW.audience='delegate' AND clinic_app.orders_delegate_allowed(
        v.id,NEW.delegate_grant_id)));
    END IF;
   END IF;
   IF valid IS DISTINCT FROM true THEN
    RAISE EXCEPTION 'result action denied' USING ERRCODE='42501'; END IF;
  END IF;
 END CASE;
 RETURN NEW;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.orders_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.orders_guard() TO clinic_owner;
RESET ROLE;
"""


def triggers(tables: tuple[str, ...]) -> str:
    """Every domain row rejects UPDATE/DELETE, independently of runtime ACLs."""
    return (
        "".join(
            f"""
CREATE TRIGGER orders_binding BEFORE INSERT OR UPDATE OR DELETE
 ON clinic_app.{table} FOR EACH ROW EXECUTE FUNCTION clinic_app.orders_guard();
"""
            for table in tables
        )
        + """
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.orders_guard() FROM clinic_owner;
RESET ROLE;
"""
    )
