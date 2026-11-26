"""Owner-issued single-assignment receipt; the ordinary writer can only verify."""

from apps.ai.migrations import _evaluation_origin_sql as prior
from apps.ai.migrations._care_sql import _edit, _function, _resolver
from apps.ai.migrations._evaluation_origin_sql import _STORE as _PRIOR_STORE

_GUARD = _edit(
    _function(
        prior.SQL.replace(
            "CREATE OR REPLACE FUNCTION clinic_app.ai_eval_record_guard(",
            "CREATE FUNCTION clinic_app.ai_eval_record_guard(",
        ),
        "ai_eval_record_guard",
    ),
    "IF TG_OP='INSERT' THEN\n   expected_capability:=",
    """IF TG_OP='INSERT' THEN
   IF NEW.producer_commitment IS NOT NULL THEN
    RAISE EXCEPTION 'invalid evaluation receipt' USING ERRCODE='23514'; END IF;
   expected_capability:=""",
)
_GUARD = _edit(
    _GUARD,
    "   IF OLD.status<>'pending' OR",
    """   IF NEW.producer_commitment IS DISTINCT FROM OLD.producer_commitment THEN
    IF OLD.producer_commitment IS NOT NULL OR OLD.status<>'pending'
     OR OLD.scored_at IS NOT NULL OR OLD.sample_results IS NOT NULL
     OR octet_length(NEW.producer_commitment) IS DISTINCT FROM 32
     OR NEW.created_by_id IS DISTINCT FROM actor
     OR NOT clinic_app.ai_eval_staff(NEW.clinic_id,'clinical_owner')
     OR (to_jsonb(NEW)-'producer_commitment')<>(to_jsonb(OLD)-'producer_commitment')
    THEN RAISE EXCEPTION 'invalid evaluation receipt' USING ERRCODE='23514'; END IF;
    RETURN NEW;
   END IF;
   IF OLD.status<>'pending' OR""",
)
_STORE = _edit(
    _PRIOR_STORE,
    "CREATE FUNCTION clinic_app.ai_eval_store(",
    "CREATE OR REPLACE FUNCTION clinic_app.ai_eval_store(",
)
_STORE = _edit(_STORE, " protocol_hash text;", " payload bytea;\n protocol_hash text;")
_STORE = _edit(
    _STORE,
    "RAISE EXCEPTION 'invalid evaluation observations'",
    "RAISE EXCEPTION 'invalid evaluation receipt'",
)
_STORE = _edit(
    _STORE,
    " data:=convert_from(clinic_app.protected_decrypt(kek,'ai.evaluation.samples.v1',"
    "samples),'UTF8')::jsonb;",
    """ payload:=clinic_app.protected_decrypt(kek,'ai.evaluation.samples.v1',samples);
 IF r.producer_commitment IS NULL
 OR r.producer_commitment IS DISTINCT FROM clinic_app.digest(payload,'sha256')
 THEN RAISE EXCEPTION 'invalid evaluation receipt' USING ERRCODE='23514'; END IF;
 data:=convert_from(payload,'UTF8')::jsonb;
 IF jsonb_typeof(data->'receipt') IS DISTINCT FROM 'string'
 OR jsonb_typeof(data->'input_sha256') IS DISTINCT FROM 'string'
 OR data->>'input_sha256' !~ '^[0-9a-f]{64}$'
 OR data->>'scorer_id' IS DISTINCT FROM r.versions->>'schema'
 THEN RAISE EXCEPTION 'invalid evaluation receipt' USING ERRCODE='23514'; END IF;""",
)

# The installed resolver reads the committed digest via its existing SELECT;
# clinic_app cannot UPDATE it or seed one on INSERT. No new signing privilege.
SQL = (
    _resolver((_GUARD, _STORE))
    + """
REVOKE UPDATE(producer_commitment) ON clinic_app.ai_evaluationrun
 FROM PUBLIC,clinic_app,clinic_resolver,clinic_agent;
"""
)
