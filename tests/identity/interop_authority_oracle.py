"""Bind todo 66's import authority, step-up and worker cases into the census."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from interop import test_authority, test_import, test_sources

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from identity.cut1_authority_census import ExecutedProofs
    from rbac_fixtures import RbacGraph


def run_interop_census(
    proofs: ExecutedProofs,
    graph: Callable[[], RbacGraph],
    patch: pytest.MonkeyPatch,
    database_url: str,
) -> None:
    """Issue no certificates until the real import boundaries execute."""
    for role in ("clinic_manager", "receptionist"):
        proofs.run(
            f"interop import authority:{role}",
            partial(
                test_authority.test_import_authority_follows_the_staff_management_bundle,
                graph(),
                role,
            ),
        )
    proofs.run(
        "interop identical unknown and foreign denial",
        partial(test_authority.test_unknown_and_foreign_jobs_share_one_denial, graph()),
    )
    with patch.context() as local:
        proofs.run(
            "interop commit, worker, provenance and rollback hide",
            partial(
                test_import.test_rollback_hides_batch_and_retains_used_patients,
                graph(),
                database_url,
                local,
            ),
        )
    with patch.context() as local:
        proofs.run(
            "interop imported history through clinical scope only",
            partial(
                test_import.test_provenance_keeps_original_author_and_time_never_the_importer,
                graph(),
                local,
            ),
        )
    with patch.context() as local:
        proofs.run(
            "interop clinical document scope",
            partial(
                test_sources.test_imported_documents_are_readable_only_through_clinical_scope,
                graph(),
                local,
            ),
        )
