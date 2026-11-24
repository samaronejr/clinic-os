"""Guard new readers and model types independently of the product's source map."""

from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path

import pytest
from django.db import connection

from ai.pending_boundary_inventory import inventory, unclassified, violations

ROOT = Path(__file__).resolve().parents[2]
BOUNDARY = "apps.ai.pending_outputs"


def test_pending_models_have_only_one_data_access_boundary() -> None:
    spec = find_spec(BOUNDARY)
    assert spec is not None, "no model-derived pending-output boundary"
    assert spec.origin is not None
    declared = inventory()
    assert declared.fields
    assert not unclassified()
    found = {
        str(p.relative_to(ROOT)): issues
        for p in (ROOT / "apps").rglob("*.py")
        if "migrations" not in p.parts and p != Path(spec.origin)
        if (issues := violations(p.read_text(), declared))
    }
    assert found == {}


def test_direct_export_projection_is_rejected_by_the_guard() -> None:
    assert find_spec(BOUNDARY) is not None
    export = (ROOT / "apps/interop/export_builder.py").read_text()
    declared = inventory()
    assert not violations(export, declared)
    # Source-only mutation: no grant changes, no bypass query is executed.
    mutant = (
        export
        + """
def direct_export():
    from apps.ai.models import CareProposal as Proposal
    return list(Proposal.objects.values("id", "kind"))
"""
    )
    assert {reason for _, reason in violations(mutant, declared)} == {
        "direct pending model query"
    }
    field_only = (
        export
        + """
from apps.ai.models import CareProposal as Proposal
def direct_column(row: Proposal):
    return {"kind": row.kind}
async def direct_scalar(row: "Proposal"):
    return row.kind
def keyword_record(row: Proposal):
    return dict(kind=row.kind)
"""
    )
    fields = violations(field_only, declared)
    assert len(fields) == 3
    assert {reason for _, reason in fields} == {
        "pending field serialized without reselection"
    }
    serializer = (
        export
        + """
from apps.ai.models import CareProposal as Proposal
from django.core.serializers import serialize as render_rows
def direct_serializer(row: Proposal):
    return render_rows("json", [row])
"""
    )
    assert {reason for _, reason in violations(serializer, declared)} == {
        "direct pending serialization"
    }


@pytest.mark.django_db
def test_model_derived_inventory_matches_restrictive_select_and_privileged_export() -> (
    None
):
    assert find_spec(BOUNDARY) is not None
    declared = inventory().tables
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT c.relname,p.polcmd,p.polpermissive,c.relforcerowsecurity,
                   pg_get_expr(p.polqual,p.polrelid),
                   ARRAY(SELECT role_id::regrole::text
                         FROM unnest(p.polroles) role_id ORDER BY 1)
            FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
            WHERE c.relnamespace='clinic_app'::regnamespace
              AND p.polname='pending_output_read'
        """)
        policies = cursor.fetchall()
        cursor.execute(
            "SELECT prosrc FROM pg_proc WHERE oid="
            "'clinic_app.interop_export_rows(uuid,text,text,integer)'::regprocedure"
        )
        body = cursor.fetchone()[0]
    assert {r[0] for r in policies} == declared
    for table, command, permissive, forced, expression, roles in policies:
        assert (command, permissive, forced) == ("r", False, True)
        assert roles == ["clinic_agent", "clinic_app"]
        assert "enterprise_staff_context()" in expression
        assert f"ai_pending_visible('{table}'::text, id)" in expression
        assert f"ai_pending_choice('{table}',t.id)" in body
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT c.relname,p.polcmd,p.polpermissive,c.relforcerowsecurity,
                   pg_get_expr(p.polqual,p.polrelid),
                   ARRAY(SELECT role_id::regrole::text
                         FROM unnest(p.polroles) role_id ORDER BY 1)
            FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
            WHERE c.relnamespace='clinic_app'::regnamespace
              AND p.polname='pending_archive_read'
        """)
        containers = cursor.fetchall()
    assert {r[0] for r in containers} == inventory().archives
    for _, command, permissive, forced, expression, roles in containers:
        assert (command, permissive, forced) == ("r", False, True)
        assert roles == ["clinic_app"]
        assert "enterprise_staff_context()" in expression
        assert "ai_archive_current(export_id)" in expression
