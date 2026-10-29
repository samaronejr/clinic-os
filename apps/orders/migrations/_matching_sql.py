"""The only chart-filing boundary; exact factors or quarantine, never an override."""

FUNCTIONS = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.orders_verify_identity(
 clinic uuid, version uuid, requested_order uuid, kek text)
RETURNS TABLE(state text,created boolean,task_id uuid)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE v clinic_app.orders_resultversion; o clinic_app.orders_order;
 p clinic_app.intake_patient; evidence jsonb; named text; born text;
 identifier text; existing_order uuid; reason text; factors jsonb;
BEGIN
 SELECT * INTO v FROM clinic_app.orders_resultversion s
 WHERE s.id=version AND s.clinic_id=clinic AND s.organization_id=__TENANT__;
 IF v.id IS NULL OR NOT clinic_app.orders_ingest_allowed(clinic,requested_order)
 THEN RAISE EXCEPTION 'result access denied' USING ERRCODE='42501'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('orders-series:'||v.result_id,0));
 IF NOT clinic_app.orders_is_current(version) OR EXISTS(
 SELECT 1 FROM clinic_app.orders_resultrejection r WHERE r.version_id=version)
 THEN RAISE EXCEPTION 'source superseded or rejected' USING ERRCODE='40001'; END IF;
 SELECT m.order_id INTO existing_order FROM clinic_app.orders_resultmatch m
 WHERE m.version_id=version;
 IF existing_order IS NOT NULL THEN
  IF existing_order IS DISTINCT FROM requested_order THEN
   RAISE EXCEPTION 'match immutable' USING ERRCODE='40001'; END IF;
  RETURN QUERY SELECT 'matched'::text,false,NULL::uuid; RETURN;
 END IF;
 SELECT * INTO o FROM clinic_app.orders_order r WHERE r.id=requested_order
 AND r.clinic_id=clinic AND r.organization_id=__TENANT__;
 reason:='unknown_order';
 IF o.id IS NOT NULL THEN
  evidence:=convert_from(clinic_app.tenant_decrypt(
    kek,'orders.result.identity',v.identity),'UTF8')::jsonb;
  SELECT patient.* INTO p FROM clinic_app.intake_patient patient
  JOIN clinic_app.intake_patientclinicenrollment e ON e.patient_id=patient.id
  AND e.organization_id=patient.organization_id WHERE e.id=o.enrollment_id
  AND e.clinic_id=clinic AND e.organization_id=__TENANT__;
  named:=lower(regexp_replace(btrim(convert_from(clinic_app.tenant_decrypt(
    kek,'intake.patient.full_name',p.full_name),'UTF8')),'\\s+',' ','g'));
  born:=CASE WHEN p.birth_date IS NULL THEN '' ELSE
   convert_from(clinic_app.tenant_decrypt(
    kek,'intake.patient.birth_date',p.birth_date),'UTF8') END;
  identifier:=NULL;
  IF coalesce(evidence->>'identifier','')<>'' THEN
   SELECT CASE WHEN i.retired_at IS NULL THEN convert_from(
    clinic_app.tenant_decrypt(kek,'intake.patientidentifier.value',i.value),'UTF8')
    END INTO identifier FROM clinic_app.intake_patientidentifier i
   WHERE i.patient_id=p.id AND i.organization_id=__TENANT__
   AND i.kind=evidence->>'identifier_kind' ORDER BY i.version DESC LIMIT 1;
  END IF;
  reason:='ambiguous';
  IF coalesce(evidence->>'birth_date','')<>'' AND born<>'' AND
   (coalesce(evidence->>'name','')<>'' OR coalesce(evidence->>'identifier','')<>'')
  THEN
   reason:='identity_mismatch';
   IF born=evidence->>'birth_date'
   AND (coalesce(evidence->>'name','')='' OR named=evidence->>'name')
   AND (coalesce(evidence->>'identifier','')='' OR identifier=evidence->>'identifier')
   THEN
    factors:=CASE WHEN coalesce(evidence->>'name','')<>''
     THEN '["birth_date","name"]'::jsonb
     ELSE '["birth_date","identifier"]'::jsonb END;
    INSERT INTO clinic_app.orders_resultmatch
    (id,organization_id,clinic_id,created_at,version_id,order_id,factors,
     operator_id,evidence_code)
    VALUES(gen_random_uuid(),v.organization_id,clinic,statement_timestamp(),
     v.id,o.id,factors,__ACTOR__,'source_identity_verified');
    RETURN QUERY SELECT 'matched'::text,true,w.task_id
     FROM clinic_app.orders_resultworkitem w WHERE w.version_id=version
     AND w.kind='review'; RETURN;
   END IF;
  END IF;
 END IF;
 IF EXISTS(SELECT 1 FROM clinic_app.orders_resultquarantine q
  WHERE q.version_id=version)
 THEN RETURN QUERY SELECT 'quarantined'::text,false,NULL::uuid; RETURN; END IF;
 INSERT INTO clinic_app.orders_resultquarantine
 (id,organization_id,clinic_id,created_at,version_id,reason)
 VALUES(gen_random_uuid(),v.organization_id,clinic,statement_timestamp(),version,reason);
 RETURN QUERY SELECT 'quarantined'::text,true,w.task_id
 FROM clinic_app.orders_resultworkitem w WHERE w.version_id=version
 AND w.kind='quarantine';
END $f$;
REVOKE ALL ON FUNCTION clinic_app.orders_verify_identity(uuid,uuid,uuid,text)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.orders_verify_identity(uuid,uuid,uuid,text)
 TO clinic_app;
RESET ROLE;
"""
