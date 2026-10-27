"""Execute the class/catalog witnesses inside the complete authority census."""

from __future__ import annotations

from contextlib import contextmanager
from functools import partial
from typing import TYPE_CHECKING

from apps.identity.models import UserClinicRole
from ops.testing.runtime_paths import runtime_directory

from prescription import test_catalog, test_catalog_guards, test_classes
from prescription.test_directions import test_unknown_record_links_share_the_same_denial
from prescription.test_renewals import (
    test_completed_non_prescription_source_is_not_renewable,
    test_renewal_retains_source_but_requires_a_new_manual_class,
)
from renewal.test_signatures import setup

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from identity.cut1_authority_census import ExecutedProofs
    from rbac_fixtures import RbacGraph


def run_prescription_census(
    proofs: ExecutedProofs,
    graph: Callable[[], RbacGraph],
    patch: pytest.MonkeyPatch,
    secret_root: Path,
) -> None:
    """Bind every new candidate to a real allow/refusal or owner-only witness."""
    for role in UserClinicRole.Role.values:
        for inactive in (False, True):
            proofs.run(
                f"catalog role/active/foreign-scope:{role}:{inactive}",
                partial(
                    test_catalog_guards.test_catalog_boundaries_decide_every_role_and_active_state,
                    graph(),
                    role,
                    inactive=inactive,
                ),
            )
    with runtime_directory(secret_root, purpose="prescription-oracles") as work:
        for catalog_oracle in (
            test_catalog.test_catalog_search_preserves_partial_input_without_save_errors_or_class,
            test_catalog.test_runtime_cannot_import_or_mutate_public_reference_data,
        ):
            proofs.run(catalog_oracle.__name__, partial(catalog_oracle, graph(), work))
    proofs.run(
        "unset class denies before any signature operation",
        partial(
            test_classes.test_unset_class_refuses_the_signing_service_before_any_operation,
            graph(),
        ),
    )
    for field in ("catalog_entry_id", "medication_statement_id"):
        proofs.run(
            f"unknown patient/catalog link:{field}",
            partial(test_unknown_record_links_share_the_same_denial, graph(), field),
        )
    for renewal_oracle, arguments in (
        (test_renewal_retains_source_but_requires_a_new_manual_class, (None,)),
        (test_completed_non_prescription_source_is_not_renewable, ()),
    ):
        with (
            patch.context() as local,
            contextmanager(setup._get_wrapped_function())(graph(), local) as subject,
        ):
            proofs.run(
                renewal_oracle.__name__, partial(renewal_oracle, subject, *arguments)
            )
