"""Give the evaluation row assignment one exact trigger relation."""

from apps.ai.migrations import _retained_proposal_sql
from apps.ai.migrations._care_sql import _edit, _resolver
from apps.ai.migrations._front_desk_authority_sql import _function

_PRIOR = _function(_retained_proposal_sql.SQL, "ai_rollout_guard")
_SHARED = _edit(_PRIOR, " ELSE r:=NEW; END IF;", " END IF;")
_RECEIPT = _PRIOR[_PRIOR.index(" IF r.stage='offline'") :]
_EVALUATION = (
    """
CREATE FUNCTION clinic_app.ai_evaluation_rollout_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE r clinic_app.ai_evaluationrun; c clinic_app.ai_modelchangerequest;
BEGIN
 r:=NEW;
"""
    + _RECEIPT
)

SQL = (
    _resolver((_SHARED, _EVALUATION))
    + """
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.ai_evaluation_rollout_guard()
 FROM PUBLIC,clinic_app,clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.ai_evaluation_rollout_guard() TO clinic_owner;
RESET ROLE;
DROP TRIGGER evaluation_rollout_guard ON clinic_app.ai_evaluationrun;
CREATE TRIGGER evaluation_rollout_guard BEFORE INSERT OR UPDATE
 ON clinic_app.ai_evaluationrun FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ai_evaluation_rollout_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ai_evaluation_rollout_guard() FROM clinic_owner;
RESET ROLE;
"""
)

REVERSE_SQL = (
    _resolver((_PRIOR,))
    + """
SET LOCAL ROLE clinic_resolver;
GRANT EXECUTE ON FUNCTION clinic_app.ai_rollout_guard() TO clinic_owner;
RESET ROLE;
DROP TRIGGER evaluation_rollout_guard ON clinic_app.ai_evaluationrun;
CREATE TRIGGER evaluation_rollout_guard BEFORE INSERT OR UPDATE
 ON clinic_app.ai_evaluationrun FOR EACH ROW
 EXECUTE FUNCTION clinic_app.ai_rollout_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ai_rollout_guard() FROM clinic_owner;
DROP FUNCTION clinic_app.ai_evaluation_rollout_guard();
RESET ROLE;
"""
)
