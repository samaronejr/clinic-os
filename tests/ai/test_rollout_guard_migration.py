"""The typed trigger split must preserve every released rollout predicate."""

import pytest
from apps.ai.migrations import _retained_proposal_sql, _rollout_evaluation_sql
from apps.ai.migrations._front_desk_authority_sql import _function
from django.db import connection, transaction


def test_shared_guard_changes_only_the_evaluation_assignment() -> None:
    original = _function(_retained_proposal_sql.SQL, "ai_rollout_guard")
    assert (
        original.replace(" ELSE r:=NEW; END IF;", " END IF;", 1)
        == _rollout_evaluation_sql._SHARED
    )
    assert "r:=NEW" not in _rollout_evaluation_sql._SHARED
    assert (
        _rollout_evaluation_sql._EVALUATION.split(" r:=NEW;\n", 1)[1]
        == (original[original.index(" IF r.stage='offline'") :])
    )
    assert _rollout_evaluation_sql._PRIOR in _rollout_evaluation_sql.REVERSE_SQL


@pytest.mark.django_db(transaction=True)
def test_guard_split_reverse_and_forward_restore_the_exact_definition() -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_functiondef('clinic_app.ai_rollout_guard()'::regprocedure)"
        )
        original = cursor.fetchone()
        cursor.execute(_rollout_evaluation_sql.REVERSE_SQL)
        cursor.execute(
            "SELECT pg_get_functiondef('clinic_app.ai_rollout_guard()'::regprocedure)"
        )
        restored = cursor.fetchone()
        assert restored is not None
        assert "ELSE r:=NEW;" in restored[0]
        cursor.execute(_rollout_evaluation_sql.SQL)
        cursor.execute(
            "SELECT pg_get_functiondef('clinic_app.ai_rollout_guard()'::regprocedure)"
        )
        assert cursor.fetchone() == original
        cursor.execute(
            "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "WHERE t.tgfoid='clinic_app.ai_evaluation_rollout_guard()'::regprocedure "
            "AND NOT t.tgisinternal"
        )
        assert cursor.fetchall() == [("ai_evaluationrun",)]
        transaction.set_rollback(True)
