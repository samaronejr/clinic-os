"""A bound security review task does not grant generic workflow authority."""

from ._workflow_review_source import guard_sql

SQL = """
CREATE FUNCTION clinic_app.enterprise_security_task(
 candidate jsonb, operation text)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.identity_breakglassgrant g
 WHERE g.id=(candidate->>'idempotency_key')::uuid
 AND g.clinic_id=(candidate->>'clinic_id')::uuid
 AND g.organization_id=(candidate->>'organization_id')::uuid
 AND candidate->>'kind'='review' AND candidate->>'priority'='high'
 AND candidate->'subject_ref'=jsonb_build_object('kind','clinic','id',g.clinic_id::text)
 AND candidate->'due_at'=to_jsonb(g.expires_at+interval '24 hours')
 AND ((operation='create' AND g.review_task_id IS NULL
 AND g.clinician_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.enterprise_break_glass_usable(g.id,g.clinic_id))
 OR (operation='review' AND g.review_task_id=(candidate->>'id')::uuid
 AND clinic_app.enterprise_manager(g.clinic_id))))
$f$;

CREATE FUNCTION clinic_app.enterprise_create_review(requested_grant uuid)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
DECLARE g clinic_app.identity_breakglassgrant; result uuid:=gen_random_uuid();
BEGIN
 SELECT * INTO g FROM clinic_app.identity_breakglassgrant
 WHERE id=requested_grant FOR UPDATE;
 IF NOT FOUND OR NOT clinic_app.enterprise_break_glass_usable(g.id,g.clinic_id)
 THEN RAISE EXCEPTION 'emergency review denied' USING ERRCODE='42501'; END IF;
 IF g.review_task_id IS NOT NULL THEN RETURN g.review_task_id; END IF;
 INSERT INTO clinic_app.workflows_task
 (id,organization_id,clinic_id,kind,subject_ref,created_by_id,owner_user_id,owner_role,
 due_at,priority,state,escalation_policy_version,escalated_at,depends_on_id,
 completion_evidence,revision,idempotency_key,fingerprint,last_command_key,
 last_command_digest,created_at,updated_at)
 VALUES(result,g.organization_id,g.clinic_id,'review',
 jsonb_build_object('kind','clinic','id',g.clinic_id::text),g.clinician_id,NULL,'',
 g.expires_at+interval '24 hours','high','open',1,NULL,NULL,'{}',1,g.id,
 encode(sha256(convert_to(g.id::text,'UTF8')),'hex'),NULL,'',
 statement_timestamp(),statement_timestamp());
 UPDATE clinic_app.identity_breakglassgrant SET review_task_id=result WHERE id=g.id;
 RETURN result;
END $f$;

CREATE FUNCTION clinic_app.enterprise_finish_review(requested_grant uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
DECLARE g clinic_app.identity_breakglassgrant; actor uuid:=
 NULLIF(current_setting('app.current_user_id',true),'')::uuid;
BEGIN
 SELECT * INTO g FROM clinic_app.identity_breakglassgrant
 WHERE id=requested_grant FOR UPDATE;
 IF NOT FOUND OR NOT clinic_app.enterprise_manager(g.clinic_id)
 OR g.expires_at>statement_timestamp() AND g.revoked_at IS NULL THEN
 RAISE EXCEPTION 'post event review denied' USING ERRCODE='42501'; END IF;
 IF g.reviewed_at IS NOT NULL THEN RETURN false; END IF;
 UPDATE clinic_app.workflows_task SET owner_user_id=actor,state='assigned',
 revision=revision+1,last_command_key=gen_random_uuid(),
 last_command_digest=encode(sha256(convert_to('assign:'||g.id::text,'UTF8')),'hex'),
 updated_at=statement_timestamp() WHERE id=g.review_task_id AND state='open';
 UPDATE clinic_app.workflows_task SET state='in_progress',revision=revision+1,
 last_command_key=gen_random_uuid(),
 last_command_digest=encode(sha256(convert_to('start:'||g.id::text,'UTF8')),'hex'),
 updated_at=statement_timestamp()
 WHERE id=g.review_task_id AND state='assigned' AND owner_user_id=actor;
 UPDATE clinic_app.workflows_task SET state='done',revision=revision+1,
 last_command_key=gen_random_uuid(),
 last_command_digest=encode(sha256(convert_to('review:'||g.id::text,'UTF8')),'hex'),
 completion_evidence=jsonb_build_object('outcome','reviewed','record',
 jsonb_build_object('kind','clinic','id',g.clinic_id::text)),
 updated_at=statement_timestamp()
 WHERE id=g.review_task_id AND state='in_progress' AND owner_user_id=actor;
 IF NOT FOUND THEN
 RAISE EXCEPTION 'post event review conflicts' USING ERRCODE='23514'; END IF;
 UPDATE clinic_app.identity_breakglassgrant
 SET reviewed_at=statement_timestamp(),reviewed_by_id=actor WHERE id=g.id;
 RETURN true;
END $f$;
__WORKFLOW_GUARD__
""".replace("__WORKFLOW_GUARD__", guard_sql())
