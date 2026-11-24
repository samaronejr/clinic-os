"""Observe ordinary ZIP parts or normal role reads at a domain test's decision point."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from apps.ai.models import AIArtifact
from apps.interop.errors import InteropAccessDeniedError

from identity.permission_support import owner_context
from interop.support import org_admin
from interop.test_org_export import _archive, _build, _files, _jsonl, _receipt, _request

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from apps.interop.management import ExportView

    from rbac_fixtures import RbacGraph


@dataclass(frozen=True)
class ReaderCase:
    relation: str
    keys: frozenset[str]
    displays: Callable[[bool], bool]
    change_choice: Callable[[], None]
    remains_visible: bool = False


@dataclass
class ArchiveProbe:
    """Require both fresh exports and previously issued part handles to honor choice."""

    graph: RbacGraph
    admin: UUID
    before: ExportView | None = None
    observed: list[bool] = field(default_factory=list)

    @classmethod
    def create(cls, graph: RbacGraph) -> ArchiveProbe:
        return cls(graph, org_admin(graph))

    def check(
        self,
        relation: str,
        keys: set[str] | frozenset[str],
        *,
        visible: bool,
        alongside: ReaderCase | None = None,
    ) -> None:
        assert keys
        g = self.graph
        job = _request(g, self.admin)
        assert _build(g, self.admin, job) == "ready"
        ready = _receipt(g, self.admin, job.export_id)
        archive = _files(_archive(g, self.admin, ready))
        json_ids = _ids(archive, relation)
        if alongside is not None:
            assert alongside.remains_visible
            assert alongside.keys <= _ids(archive, alongside.relation)
        if visible:
            assert keys <= json_ids
        else:
            assert keys.isdisjoint(json_ids)
            assert self.before is not None
            with pytest.raises(InteropAccessDeniedError):
                _archive(g, self.admin, self.before)
        self.observed.append(visible)
        if self.before is None:
            self.before = ready

    def artifact(self, invocation: UUID, *, visible: bool) -> None:
        # Fixture identity only. The thing under test is the manager's actual
        # organization download and parsed stored bytes, never an owner read.
        with owner_context(self.graph.organization_a):
            row = AIArtifact.objects.only("pk").get(invocation_id=invocation)
        self.check(AIArtifact._meta.db_table, {str(row.pk)}, visible=visible)


def _ids(archive: dict[str, bytes], relation: str) -> set[str]:
    json_ids = {str(row["id"]) for row in _jsonl(archive[f"data/{relation}.jsonl"])}
    csv_ids = {
        row["id"]
        for row in csv.DictReader(io.StringIO(archive[f"data/{relation}.csv"].decode()))
    }
    assert json_ids == csv_ids
    return json_ids
