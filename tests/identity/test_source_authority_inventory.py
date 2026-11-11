"""Source-only authority discovery never substitutes for the deployed PG gate."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from identity import legacy_sql_inventory, staff_state_analysis

if TYPE_CHECKING:
    import pytest


class _ForbiddenCatalog:
    def cursor(self) -> Never:
        msg = "Source-only analysis attempted database access."
        raise AssertionError(msg)


def test_source_mode_keeps_sql_roots_and_staff_edges_without_a_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an unavailable deployed catalog, not a fabricated catalog response.
    monkeypatch.setattr(legacy_sql_inventory, "connection", _ForbiddenCatalog())
    monkeypatch.setattr(staff_state_analysis, "connection", _ForbiddenCatalog())
    # When the explicit source-only mode is selected.
    discovered = legacy_sql_inventory.discover_sql(include_deployed=False)
    analysis = staff_state_analysis.python_staff_analysis(legacy_sql_inventory.ROOT)
    staff_state_analysis.add_sql_staff_analysis(analysis, include_deployed=False)
    # Then declared roots and transitive staff reads remain visible.
    assert "clinic_app.enterprise_org_admin" in discovered
    assert "clinic_app.has_permission" in analysis.reachable(
        "clinic_app.enterprise_org_admin"
    )
    assert analysis.evidence("clinic_app.enterprise_org_admin")
    assert discovered["clinic_app.enterprise_org_admin"]["signatures"] == []
