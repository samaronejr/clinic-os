"""Only the producer's original run may retain its protected observation set."""

from apps.ai.migrations._care_sql import _edit, _resolver
from apps.ai.migrations._rollout_sql import _RECORD

_GUARD = _edit(
    _RECORD,
    "IF NEW.version<>previous+1",
    "IF NEW.domain_protocol_sha256<>"
    "'e7fa759c95e22220ded3e5f7be3909a3b6994a29d34763cc01be07423e54a784' "
    "OR NEW.version<>previous+1",
)
_STORE = r"""
CREATE FUNCTION clinic_app.ai_eval_store(
 clinic uuid, subject uuid, samples bytea, kek text)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE r clinic_app.ai_evaluationrun; p clinic_app.ai_evaluationprotocol;
 m clinic_app.ai_modelconfiguration; data jsonb; origin jsonb; failures integer;
 protocol_hash text;
 actor uuid:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
BEGIN
 SELECT * INTO r FROM clinic_app.ai_evaluationrun WHERE id=subject AND clinic_id=clinic
 AND organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid
 FOR UPDATE;
 IF r.id IS NULL OR actor IS NULL OR r.created_by_id IS DISTINCT FROM actor
 OR NOT clinic_app.ai_eval_staff(clinic,'clinical_owner') THEN
  RAISE EXCEPTION 'evaluation denied' USING ERRCODE='42501'; END IF;
 IF r.status<>'pending' OR r.scored_at IS NOT NULL OR samples IS NULL
 OR octet_length(samples) NOT BETWEEN 68 AND 2097152 THEN
  RAISE EXCEPTION 'invalid evaluation observations' USING ERRCODE='23514'; END IF;
 SELECT * INTO p FROM clinic_app.ai_evaluationprotocol WHERE id=r.protocol_id;
 SELECT * INTO m FROM clinic_app.ai_modelconfiguration WHERE id=r.configuration_id;
 data:=convert_from(clinic_app.protected_decrypt(kek,'ai.evaluation.samples.v1',samples),'UTF8')::jsonb;
 protocol_hash:=encode(clinic_app.digest(convert_to(concat_ws(E'\n',p.id,p.version,p.capability,
  p.thresholds_sha256,p.corpus_version,p.corpus_sha256,p.domain_protocol_sha256),'UTF8'),'sha256'),'hex');
 origin:=jsonb_build_object('run_id',r.id,'stage',r.stage,'protocol_id',p.id,
  'protocol_version',p.version,'protocol_sha256',protocol_hash,
  'domain_protocol_sha256',p.domain_protocol_sha256,'corpus_sha256',p.corpus_sha256,
  'thresholds_sha256',p.thresholds_sha256,'configuration_id',m.id,'configuration_version',m.version,
  'purpose',m.purpose,'provider',m.provider,'model_id',m.model_id,'versions',r.versions);
 IF p.domain_protocol_sha256='' OR data->'origin' IS DISTINCT FROM origin
 OR data->>'domain_protocol_sha256' IS DISTINCT FROM p.domain_protocol_sha256
 OR data->>'corpus_sha256' IS DISTINCT FROM p.corpus_sha256
 OR data->>'corpus_version' IS DISTINCT FROM p.corpus_version
 OR data->>'capability' IS DISTINCT FROM p.capability
 OR NOT (data @> '{"schema_version":2,"data_class":"synthetic",
 "real_world_performance":false,
 "human_review_complete":false,"tool_calls":0}'::jsonb)
 OR jsonb_typeof(data->'cases') IS DISTINCT FROM 'array'
 OR jsonb_array_length(data->'cases')<>jsonb_array_length(p.case_manifest)
 OR EXISTS(SELECT 1 FROM jsonb_array_elements(data->'cases') WITH ORDINALITY s(item,n)
  WHERE s.item->'origin' IS DISTINCT FROM origin OR jsonb_typeof(s.item->'schema_valid')
  IS DISTINCT FROM 'boolean' OR NOT s.item @> (p.case_manifest->(s.n::integer-1)))
 THEN RAISE EXCEPTION 'invalid evaluation origin' USING ERRCODE='23514'; END IF;
 SELECT count(*) INTO failures FROM jsonb_array_elements(data->'cases') s
 WHERE s->'schema_valid'='false'::jsonb;
 IF data->'automatic_failures' IS DISTINCT FROM to_jsonb(failures) THEN
  RAISE EXCEPTION 'invalid evaluation count' USING ERRCODE='23514'; END IF;
 UPDATE clinic_app.ai_evaluationrun SET sample_results=samples,
 automatic_failures=failures,scored_at=statement_timestamp() WHERE id=subject;
END $f$;
"""

SQL = (
    _resolver((_GUARD, _STORE))
    + """
REVOKE UPDATE(sample_results,automatic_failures,scored_at)
 ON clinic_app.ai_evaluationrun FROM clinic_app;
GRANT UPDATE(sample_results,automatic_failures,scored_at)
 ON clinic_app.ai_evaluationrun TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.ai_eval_store(uuid,uuid,bytea,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.ai_eval_store(uuid,uuid,bytea,text)
 TO clinic_owner,clinic_app;
RESET ROLE;
"""
)
REVERSE_SQL = (
    _resolver((_RECORD,))
    + """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.ai_eval_store(uuid,uuid,bytea,text);
RESET ROLE;
REVOKE UPDATE(sample_results,automatic_failures,scored_at)
 ON clinic_app.ai_evaluationrun FROM clinic_resolver;
GRANT UPDATE(sample_results,automatic_failures,scored_at)
 ON clinic_app.ai_evaluationrun TO clinic_app;
"""
)
