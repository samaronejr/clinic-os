"""The same native retained-output writes must work before and stop after upgrade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.core.secrets import secret_store
from apps.tenancy.envelope import KEK_SECRET_NAME, encrypt
from django.db import DatabaseError, connection, transaction

if TYPE_CHECKING:
    from collections.abc import Mapping


def _write(
    statement: str,
    parameters: list[str | bytes],
    reason: str | None,
    *,
    runtime: bool = False,
) -> bool:
    """Roll back each write and require the intended rollout fence on denial."""
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            if runtime:
                cursor.execute("SET LOCAL ROLE clinic_app")
            cursor.execute(statement, parameters)
            if cursor.rowcount != 1:
                message = "retained probe must reach exactly one row"
                raise RuntimeError(message)
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            transaction.set_rollback(True)
    except DatabaseError as error:
        if reason is None:
            message = "historical retained write must succeed"
            raise RuntimeError(message) from error
        return (
            reason is not None
            and reason in str(error.__cause__)
            and getattr(error.__cause__, "sqlstate", None) in {"23514", "42501"}
        )
    return reason is None


def retained_checks(history: Mapping[str, str], *, upgraded: bool) -> dict[str, bool]:
    """Exercise AI-01, AI-03, AI-05 and AI-07 with restored native guards."""
    results: dict[str, bool] = {}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_user_id',%s,true)", [history["physician"]]
        )
        applied = encrypt(purpose="scribe.review.applied", plaintext=b'"Synthetic."')
        source = encrypt(
            purpose="orders.observation.source",
            plaintext=(
                '{"code":"synthetic","value":"1","parsed_value":"1","unit":"mg/dL",'
                '"reference_interval":"","abnormal_flag":"","provenance":"imported",'
                '"confirmer":"' + history["physician"] + '","page":1,'
                '"bbox":[0,0,100,100],"raw_text":"Synthetic 1 mg/dL",'
                '"extraction_candidate":"' + history["extraction_candidate"] + '"}'
            ).encode(),
        )
        results["retained_draft_acceptance"] = _write(
            "INSERT INTO clinic_app.scribe_reviewdecision "
            "(id,organization_id,version_id,suggestion_id,reviewed_by_id,command_id,"
            "request_digest,action,section,expected_revision,result_revision,"
            "result_sha256,applied_text,capability_snapshot,created_at) "
            "SELECT gen_random_uuid(),organization_id,version_id,%s,reviewed_by_id,"
            "gen_random_uuid(),request_digest,'accepted','subjective',"
            "expected_revision,result_revision,result_sha256,%s,capability_snapshot,"
            "statement_timestamp() FROM clinic_app.scribe_reviewdecision WHERE id=%s",
            [history["pending_suggestion"], applied, history["review_decision"]],
            "draft invocation permission required" if upgraded else None,
        )
        results["retained_extraction_confirmation"] = _write(
            "SELECT * FROM clinic_app.ai_review_extraction(%s,%s,'confirmed',%s,%s)",
            [
                history["extraction_candidate"],
                history["result_version"],
                source,
                secret_store().get_secret(KEK_SECRET_NAME),
            ],
            "extraction invocation permission required" if upgraded else None,
            runtime=True,
        )
        results["retained_extraction_direct_write"] = _write(
            "UPDATE clinic_app.ai_extractioncandidate SET status='confirmed',"
            "observation_id=%s,reviewed_by_id=%s,reviewed_at=statement_timestamp() "
            "WHERE id=%s",
            [
                history["spare_observation"],
                history["physician"],
                history["extraction_candidate"],
            ],
            "extraction invocation permission required" if upgraded else None,
        )
        cursor.execute(
            "SELECT clinic_app.ai_imported_observation(%s,%s,%s)",
            [
                history["imported_observation"],
                history["result_version"],
                history["clinic"],
            ],
        )
        results["retained_imported_observation"] = cursor.fetchone() == (not upgraded,)
        results["retained_care_acceptance"] = _write(
            "UPDATE clinic_app.ai_careproposal SET state='accepted',"
            "revision=revision+1,"
            "decided_by_id=%s,care_action_id=%s WHERE id=%s",
            [history["physician"], history["care_action"], history["care_proposal"]],
            "care invocation permission required" if upgraded else None,
        )
        cursor.execute(
            "SELECT set_config('app.current_user_id',%s,true)", [history["owner"]]
        )
        results["retained_analyst_review"] = _write(
            "INSERT INTO clinic_app.analytics_analystreview "
            "(id,organization_id,clinic_id,user_id,invocation_id,descriptor,"
            "window_start,window_end,authorization_version,decision,created_at) "
            "SELECT gen_random_uuid(),organization_id,clinic_id,user_id,%s,descriptor,"
            "window_start,window_end,clinic_app.analytics_authorization(clinic_id),"
            "'accepted',statement_timestamp() FROM clinic_app.analytics_analystreview "
            "WHERE id=%s",
            [history["analyst_invocation"], history["analyst_review"]],
            "metric access denied" if upgraded else None,
            runtime=True,
        )
    return results
