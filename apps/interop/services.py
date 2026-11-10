"""Interop service entrypoints: governed data import (todo 66) and deferred RNDS."""

from typing import NoReturn

from apps.interop.access import (
    ImportAccessDeniedError,
    ImportConflictError,
    ImportInputError,
    ImportStepUpRequiredError,
)
from apps.interop.commit import (
    BatchOutcome,
    CommitReceipt,
    ReconciliationReport,
    commit_import,
    reconciliation_report,
    run_batch,
)
from apps.interop.contract import MappingSuggestion
from apps.interop.jobs import (
    CutoverPlan,
    DryRunReport,
    ImportJobView,
    ImportMappingView,
    ImportProfile,
    apply_mapping,
    approve_import,
    dry_run_import,
    dry_run_report,
    export_import_report,
    import_job,
    import_jobs,
    profile_import,
    save_mapping,
    suggest_mapping,
    upload_import,
)
from apps.interop.records import (
    ImportedAttachment,
    ImportedHistoryItem,
    imported_attachment,
    imported_history,
)
from apps.interop.rollback import RollbackReceipt, rollback_import

__all__ = (
    "BatchOutcome",
    "CommitReceipt",
    "CutoverPlan",
    "DryRunReport",
    "ImportAccessDeniedError",
    "ImportConflictError",
    "ImportInputError",
    "ImportJobView",
    "ImportMappingView",
    "ImportProfile",
    "ImportStepUpRequiredError",
    "ImportedAttachment",
    "ImportedHistoryItem",
    "MappingSuggestion",
    "ReconciliationReport",
    "RollbackReceipt",
    "apply_mapping",
    "approve_import",
    "commit_import",
    "dry_run_import",
    "dry_run_report",
    "exchange_clinical_record",
    "export_import_report",
    "import_job",
    "import_jobs",
    "imported_attachment",
    "imported_history",
    "profile_import",
    "reconciliation_report",
    "rollback_import",
    "run_batch",
    "save_mapping",
    "suggest_mapping",
    "upload_import",
)


def exchange_clinical_record() -> NoReturn:
    """Exchange a clinical record when the interop domain is implemented."""
    message = "Phase >=1"
    raise NotImplementedError(message)
