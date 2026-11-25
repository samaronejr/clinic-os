"""Guard new readers and model types independently of the product's source map."""

from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path

import pytest
from django.conf import settings
from django.db import connection

from ai.pending_boundary_inventory import (
    INTERNAL,
    ISSUED,
    RAW,
    checked_presenter_mutations,
    inventory,
    permit_issuance,
    unclassified,
    violations,
)
from ai.pending_declarations import framework_classes
from ai.pending_serializer_routes import resolved_serializer_violations

ROOT = Path(__file__).resolve().parents[2]
BOUNDARY = "apps.ai.pending_outputs"


def test_pending_models_have_only_one_data_access_boundary() -> None:
    bindings = resolved_serializer_violations()
    assert not bindings, "\n".join(bindings)
    spec = find_spec(BOUNDARY)
    assert spec is not None, "no model-derived pending-output boundary"
    assert spec.origin is not None
    declared = inventory()
    assert declared.fields
    assert not unclassified()
    assert not permit_issuance(Path(spec.origin).read_text(), declared, boundary=True)
    paths = [
        p
        for directory in ("apps", "config")
        for p in (ROOT / directory).rglob("*.py")
        if "migrations" not in p.parts and p != Path(spec.origin)
    ]
    families = framework_classes(p.read_text() for p in paths)
    processors = {
        s
        for engine in settings.TEMPLATES
        for options in [engine.get("OPTIONS")]
        if isinstance(options, dict)
        for s in options.get("context_processors", ())
    }
    found = {
        str(p.relative_to(ROOT)): issues
        for p in paths
        if (
            issues := violations(
                p.read_text(),
                declared,
                project_families=families,
                processors=frozenset(
                    s.rsplit(".", 1)[1]
                    for s in processors
                    if s.rsplit(".", 1)[0]
                    == ".".join(p.relative_to(ROOT).with_suffix("").parts)
                ),
            )
        )
    }
    assert found == {}
    assert declared.permits


def test_raw_presenter_and_out_of_scope_permit_mutations_fail() -> None:
    from inspect import getsource  # noqa: PLC0415

    from apps.ai.agent_presenter import present_proposal  # noqa: PLC0415

    declared = inventory()
    presenter = getsource(present_proposal)
    assert not violations(presenter, declared)
    mutation = presenter.replace("VisiblePending[ActionProposal]", "ActionProposal", 1)
    assert mutation != presenter
    assert {reason for _, reason in violations(mutation, declared)} == {RAW}
    # The round-four presenter shape: fields of a model loaded earlier.
    retained = """
from apps.ai.models import ActionProposal
def present_proposal(proposal: ActionProposal) -> ProposalRow:
    data = parse_payload(proposal.payload).data()
    return ProposalRow(proposal.pk, proposal.digest, data["expires_at"])
"""
    assert {reason for _, reason in violations(retained, declared)} == {RAW}
    forged = """
from apps.ai.pending_outputs import VisiblePending as Permit
def counterfeit(selector, issuer):
    return Permit(selector, None, issuer)
"""
    assert {reason for _, reason in violations(forged, declared)} == {ISSUED}
    internals = """
from apps.ai.pending_outputs import _ISSUER, RenderSelection
def replay(selector):
    return selector, _ISSUER
def stash(batch: RenderSelection):
    return batch._records
"""
    lines = {
        line for line, reason in violations(internals, declared) if reason == INTERNAL
    }
    assert lines == {2, 4, 6}


def test_every_checked_presenter_fails_the_guard_when_given_its_raw_model() -> None:
    # Derived from annotations: each function outside the boundary taking a
    # VisiblePending[...] argument, mutated one at a time to take the model.
    declared = inventory()
    mutations = list(checked_presenter_mutations(ROOT))
    assert mutations
    missed = [
        (path, line)
        for path, line, mutated in mutations
        if (line, RAW) not in violations(mutated, declared)
    ]
    assert missed == []


def test_validation_only_read_cannot_treat_a_raw_model_as_a_permit() -> None:
    source = """
from apps.scribe.models import AIDraft
from apps.ai.pending_outputs import VisiblePending as Checked
def validate(row: Checked[AIDraft], live: tuple[str, ...]) -> tuple[str, ...]:
    if len(row.record.source_refs) != len(live):
        raise ValueError("source mismatch")
    return live
"""
    declared = inventory()
    assert not violations(source, declared)
    raw = source.replace("row: Checked[AIDraft]", "row: AIDraft")
    assert (4, RAW) in violations(raw, declared)


def test_raw_queryset_alias_into_dto_constructor_is_detected() -> None:
    mutant = """
from apps.ai.models import CareProposal as Care
from django.db.models import QuerySet
type Pending = QuerySet[Care]
def summary(items: Pending | None) -> BundleRow | None:
    if not items:
        return None
    return BundleRow(tuple(ItemRow(item.pk, item.state) for item in items))
"""
    assert "raw pending model at presentation boundary" in {
        reason for _, reason in violations(mutant, inventory())
    }


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
    field_reason = "pending field serialized without reselection"
    assert len({line for line, reason in fields if reason == field_reason}) == 3
    # The raw-argument rule also reaches the two returned projections.
    assert {reason for _, reason in fields} == {field_reason, RAW}
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
        "direct pending serialization",
        RAW,
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
