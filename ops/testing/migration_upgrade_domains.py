"""Representative synthetic record graphs for every domain added after the base.

``migration_upgrade_stages`` loads each entry right after the migration that
first brings its model into the migration state, through the *historical*
model of that state, inside the witness tenant whose base fixture already
holds a patient, encounter, finalized SOAP, prescription, signature,
attachment, invoice, consent and teleconsult graph. An entry lists only the
values that make one coherent, representative row graph (state-machine
states, provenance and unknown values, encrypted content, cross-domain
links); every other column is filled generically from the historical field.
Every model the candidate adds needs an explicit entry here: a model without
one is reported unseeded and fails the gate.

All values are synthetic: "Synthetic ..." names, ``*.invalid`` mail, TEST-NET
addresses. Nothing here is real patient, clinic or provider data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date, time, timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, Final, cast
from uuid import UUID, uuid4, uuid5

from django.db.models import F
from django.db.models.expressions import RawSQL

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import datetime

Row = dict[str, object]
DIGEST: Final = "a" * 64
ADDENDUM_TEXT: Final = "Synthetic addendum."
BYTES32: Final = bytes(range(32))


@dataclass(frozen=True, slots=True)
class SeedContext:
    """Identities and already-seeded rows visible to one stage."""

    organization: UUID
    clinic: UUID
    owner: UUID
    physician: UUID
    patient: UUID
    enrollment: UUID
    companion_patient: UUID
    companion_enrollment: UUID
    open_encounter: UUID
    live_session: UUID
    patient_session: UUID
    signed_document: UUID
    signed_operation: UUID
    now: datetime
    seeded: Mapping[str, Sequence[object]]
    legacy: Callable[[str, Mapping[str, object]], object]
    fetch: Callable[[str, str, Mapping[str, object]], object]
    call: Callable[..., object]

    def value(self, label: str, name: str, **filters: object) -> object:
        """One stored column of the base row ``ref(label, **filters)`` names."""
        return self.fetch(label, name, filters)

    def key(self, name: str) -> UUID:
        """Return a stable synthetic id, so one row can name another first."""
        return uuid5(self.organization, name)

    @property
    def future(self) -> datetime:
        """A month after the stage time."""
        return self.now + timedelta(days=30)

    def ref(self, label: str, index: int = 0, /, **filters: object) -> object:
        """Primary key of a seeded new-domain row, else of a base row."""
        rows = self.seeded.get(label)
        if rows and not filters:
            return rows[index]
        return self.legacy(label, filters)


def _one(_context: SeedContext) -> Sequence[Row]:
    return ({},)


@dataclass(frozen=True, slots=True)
class Seed:
    """How one model's representative rows are loaded, through live guards.

    ``rows`` returns per-row overrides (``_actor`` and ``_settings`` pick a
    row's own writer and request settings); ``actor`` selects the default
    writer; ``support`` loads rows of already-existing models the graph needs
    first (an extra staff role, a verified session) through the same historical
    state, each with its own ``updates``; ``prepare`` moves earlier rows through
    product transitions before the model's rows (a staff opt-in the guard
    checks); ``calls`` then invokes the product resolver functions whose own
    inserts write the model's rows; ``adopt`` names why the model's rows come
    from such a call or from the creating migration's backfill, which the stage
    records instead of inserting; ``after`` names same-stage models whose rows a
    value refers to without a foreign key, so they load first; ``updates``
    moves the rows through their product transitions in order, each as the
    row's recorded writer, and re-records them; ``settings`` sets request GUCs
    from the row's columns or literals, as the writing request would.

    No trigger is ever disabled: every row is written in an initial state and
    moved only through legal transitions, so each guard runs. To seed a model
    a later migration adds, add one entry under its domain's dict (see the
    claim's "Adding a stage" section).
    """

    rows: Callable[[SeedContext], Sequence[Row]] = field(default=_one)
    actor: str = "owner"  # "owner", "physician" or "patient" (portal session)
    adopt: str = ""
    support: tuple[tuple[str, Seed], ...] = ()
    after: tuple[str, ...] = ()
    updates: tuple[tuple[str, Callable[[SeedContext], tuple[object, Row]]], ...] = ()
    prepare: tuple[tuple[str, Callable[[SeedContext], tuple[object, Row]]], ...] = ()
    settings: tuple[tuple[str, str], ...] = ()  # (GUC, column or "=literal")
    calls: tuple[Callable[[SeedContext], object], ...] = ()
    # "agent": rows are written over the service principal's own clinic_agent
    # login (session_user is its registered identity), after its support rows
    # commit; the principal comes from the ``app.current_principal`` setting.
    session: str = ""


def _rows(*rows: Row) -> Callable[[SeedContext], Sequence[Row]]:
    return lambda _context: rows


def _version_step(
    changes: Row,
) -> Callable[[SeedContext], tuple[object, Row]]:
    def step(c: SeedContext) -> tuple[object, Row]:
        values = {key: value for key, value in changes.items() if key != "approval"}
        if changes.get("approval"):
            values["approval_id"] = c.ref("providers.CapabilityApproval")
        return c.ref("providers.CapabilityVersion", 1), values

    return step


def _move(
    key: Callable[[SeedContext], object], **changes: object
) -> Callable[[SeedContext], tuple[object, Row]]:
    """Return one transition of the row ``key`` names, to fixed ``changes``."""
    return lambda c: (key(c), dict(changes))


def _step(
    label: str, index: int, changes: Row
) -> Callable[[SeedContext], tuple[object, Row]]:
    """One revisioned product transition of a seeded row."""
    return lambda c: (c.ref(label, index), {**changes, "revision": F("revision") + 1})


def _command_step(
    label: str, index: int, changes: Callable[[SeedContext], Row]
) -> Callable[[SeedContext], tuple[object, Row]]:
    """One revisioned, command-keyed product transition of a seeded row."""

    def step(c: SeedContext) -> tuple[object, Row]:
        values = changes(c)
        values.update(
            revision=F("revision") + 1,
            last_command_key=uuid4(),
            last_command_digest=DIGEST,
        )
        return c.ref(label, index), values

    return step


def _bundle_two(who: str) -> Callable[[SeedContext], tuple[object, Row]]:
    def step(c: SeedContext) -> tuple[object, Row]:
        user = c.owner if who == "owner" else c.physician
        key = c.ref("identity.UserClinicRole", user_id=user, role=who)
        return key, {"workflow_bundle_version": 2}

    return step


def _owner_role(role: str) -> Callable[[SeedContext], Sequence[Row]]:
    return lambda c: ({"user_id": c.owner, "role": role},)


def _reviewer(name: str) -> Callable[[SeedContext], Sequence[Row]]:
    return lambda c: (
        {
            "id": c.key(name),
            "username": f"upgrade-{name}-{c.key(name).hex[:8]}",
            "password": "!",
            "is_active": True,
        },
    )


def _reviewer_role(name: str) -> Callable[[SeedContext], Sequence[Row]]:
    return lambda c: ({"user_id": c.key(name), "role": "physician"},)


TRANSACTION_TIME: Final = RawSQL("transaction_timestamp()", ())


# The owner's verified browser session for organization governance; the
# guards read its key hash as the request's enterprise factor.
ENTERPRISE_SESSION_KEY: Final = sha256(b"synthetic-upgrade-enterprise-session").digest()
CLINICIAN_SESSION_KEY: Final = sha256(b"synthetic-upgrade-break-glass-session").digest()
CLINICIAN_SETTINGS_MAP: Final = {"app.enterprise_factor": CLINICIAN_SESSION_KEY.hex()}
CLINICIAN_SETTINGS: Final = (
    ("app.enterprise_factor", "=" + CLINICIAN_SESSION_KEY.hex()),
)
ENTERPRISE_SETTINGS_MAP: Final = {"app.enterprise_factor": ENTERPRISE_SESSION_KEY.hex()}
ENTERPRISE_SETTINGS: Final = (
    ("app.enterprise_factor", "=" + ENTERPRISE_SESSION_KEY.hex()),
)

# The evaluation protocol's acceptance thresholds, as registered text.
THRESHOLDS: Final = json.dumps(
    {
        "minimum_cases": 2,
        "minimum_encounters": 2,
        "maximum_critical_errors": 0,
        "maximum_error_rate_ppm": 0,
        "minimum_citation_support_ppm": 1000000,
        "minimum_action_correctness_ppm": 1000000,
        "bootstrap_seed": 7,
        "bootstrap_iterations": 100,
    },
    sort_keys=True,
)

# The adjudicator's verified browser session: the merge guards bind the
# decision to this session record through the hex of its key hash.
MERGE_SESSION_KEY: Final = sha256(b"synthetic-upgrade-merge-session").digest()
MERGE_SETTINGS: Final = (("app.current_merge_session", "=" + MERGE_SESSION_KEY.hex()),)


INVOCATION_LIMITS: Final[Row] = {
    "config_version": 1,
    "prompt_sha256": DIGEST,
    "limit_tokens_in": 10,
    "limit_tokens_out": 10,
    "limit_audio_seconds": 10,
    # 10 tokens in + 10 out + 10 audio seconds at 1 micro each.
    "reserved_micros": 30,
    "reason_code": "",
}


MEDIA_STORAGE: Final[dict[str, object]] = {
    "capability__key": "object_storage_media",
    "capability__clinic_id": None,
}


def _media_snapshot(c: SeedContext) -> Row:
    """Return the registered media-storage version a capture session binds to."""
    fields = ("provider", "account", "environment", "api_version", "region")
    return {
        "capability": "object_storage_media",
        **{
            name: c.value("providers.CapabilityVersion", name, **MEDIA_STORAGE)
            for name in (*fields, "retention_terms")
        },
    }


# Draft -> approved (by review) -> active: a configuration's only path to use.
MODEL_APPROVAL: Final[tuple[Row, ...]] = (
    {"state": "approved", "approval_ref": "synthetic-review"},
    {"state": "active"},
)


def _model_row(purpose: str, model: str, capability: str = "llm_inference") -> Row:
    """Return a draft synthetic model configuration for one AI purpose."""
    return {
        "purpose": purpose,
        "capability_key": capability,
        "provider": "synthetic",
        "model_id": model,
        "region": "br-synthetic",
        "jurisdiction_class": "BR",
        "endpoint": "https://ai.synthetic.invalid/",
        "retention_terms": "synthetic",
        "training_terms": "synthetic",
        "version": 1,
        "route": "primary",
        "state": "draft",
        "approval_ref": "",
        "input_micros": 1,
        "output_micros": 1,
        "cached_micros": 1,
        "audio_micros": 1,
    }


def _model_step(
    index: int, changes: Row
) -> Callable[[SeedContext], tuple[object, Row]]:
    return lambda c: (c.ref("ai.ModelConfiguration", index), dict(changes))


# The organization's AI policy: brief and analyst calls within budget.
AI_POLICY: Final[Row] = {
    "enabled": True,
    "purposes": ["brief", "analyst", "scribe_asr", "scribe_draft", "extraction"],
    "monthly_budget_micros": 1000000,
    "purpose_budgets": {
        "brief": 200000,
        "analyst": 200000,
        "scribe_asr": 200000,
        "scribe_draft": 200000,
        "extraction": 200000,
    },
}


def _ai_configuration(
    c: SeedContext, *purposes: str, **changes: object
) -> Sequence[Row]:
    """Return the owner's next published clinic configuration, opting ``purposes`` in.

    The policy extends the latest published one (the base policy until AI is
    first enabled), as the settings screen republishes the whole document.
    """
    copied = (
        "display_name",
        "contact_email",
        "contact_phone",
        "brand_token",
        "reminder_hours",
        "queue_quotas",
    )
    latest = int(str(c.value("identity.ClinicConfiguration", "max:version")))
    current = c.value("identity.ClinicConfiguration", "ai_policy", version=latest)
    base = (
        current if isinstance(current, dict) and current.get("enabled") else AI_POLICY
    )
    policy = {
        **base,
        "purposes": [*cast("list[str]", base["purposes"]), *purposes],
        "purpose_budgets": {
            **cast("dict[str, int]", base["purpose_budgets"]),
            **dict.fromkeys(purposes, 200000),
        },
    }
    return (
        {
            **{name: c.value("identity.ClinicConfiguration", name) for name in copied},
            "version": latest + 1,
            "ai_policy": policy,
            "published_by_id": c.owner,
            **changes,
        },
    )


def _ingest(c: SeedContext) -> dict[str, str]:
    """Return the provider webhook's request: the ingest principal and its proof."""
    return {
        "app.current_principal": str(c.key("messaging-principal")),
        "app.comms_inbound_proof": INBOUND_PROOF,
    }


def _operation(
    c: SeedContext, name: str, provider: str, subject_type: str, subject: str
) -> Row:
    """Return an action outbox operation the physician prepared for one subject."""
    return {
        "id": c.key(name),
        "actor_id": c.physician,
        "kind": "action",
        "channel": "",
        "payload_digest": DIGEST,
        "provider": provider,
        "subject_type": subject_type,
        "subject_id": c.key(subject),
        "idempotency_key": c.key(name + "-idempotency"),
    }


def _latest_version(c: SeedContext, patient: object) -> int:
    """Return the patient's current demographics version (0 before the first)."""
    return int(
        str(c.value("intake.PatientDemographics", "max:version", patient_id=patient))
    )


def _final_encounter(c: SeedContext) -> object:
    """Return the closed encounter the witness's finalized SOAP belongs to."""
    return c.value(
        "ehr.ClinicalDocument", "encounter_id", id=_finalized(c, "document_id")
    )


# The WhatsApp provider proves each inbound delivery with this synthetic
# secret; its account stores only the SHA-256.
INBOUND_PROOF: Final = "synthetic-upgrade-inbound-proof"


def _ai_call(purpose: str, name: str, method: str) -> tuple[tuple[str, Seed], ...]:
    """Return a physician's reserved AI call with its input and gateway operation.

    The call is reserved first, then bound to its outbox operation, as the
    gateway client does before dispatch.
    """
    return (
        (
            "ai.AIArtifact",
            Seed(
                lambda c: (
                    {
                        "id": c.key(name + "-input"),
                        "purpose": purpose,
                        "kind": "input",
                        "method": method,
                        "content": "Synthetic AI input; no clinical data.",
                        "retention_class": "transient_input",
                        "patient_enrollment_id": c.enrollment,
                        "expires_at": c.future,
                        "user_id": c.physician,
                    },
                ),
                actor="physician",
            ),
        ),
        (
            "comms.IntegrationOperation",
            Seed(
                lambda c: (
                    _operation(c, name + "-op", "ai_gateway", "ai.invocation", name),
                ),
                actor="physician",
            ),
        ),
        (
            "ai.AIInvocation",
            Seed(
                lambda c: (
                    {
                        "id": c.key(name),
                        "purpose": purpose,
                        "user_id": c.physician,
                        "inputs_ref_id": c.key(name + "-input"),
                        "configuration_id": c.ref(
                            "ai.ModelConfiguration", purpose=purpose
                        ),
                        **INVOCATION_LIMITS,
                    },
                ),
                actor="physician",
                updates=(
                    (
                        "ai.AIInvocation",
                        lambda c: (c.key(name), {"operation_id": c.key(name + "-op")}),
                    ),
                ),
            ),
        ),
    )


IMPORT_SESSION_KEY: Final = sha256(b"synthetic-upgrade-import-session").digest()
IMPORT_SETTINGS_MAP: Final = {"app.current_import_session": IMPORT_SESSION_KEY.hex()}
IMPORT_SETTINGS: Final = (
    ("app.current_import_session", "=" + IMPORT_SESSION_KEY.hex()),
)
IMPORT_COUNTS: Final[Row] = {
    "row_count": 1,
    "valid_count": 1,
    "rejected_count": 0,
    "duplicate_count": 0,
}


def _import_key(job: object) -> UUID:
    """Return the enrollment command key the import guard recomputes for row 1."""
    return UUID(hex=sha256(f"{job}:patients:1".encode()).hexdigest()[:32])


def _import_step(  # noqa: PLR0913 - one transition's binding
    c: SeedContext,
    sequence: int,
    state: str,
    *,
    mapped: bool = False,
    report: bool = False,
    counted: bool = False,
) -> Row:
    """One import transition the owner (the import authority) records."""
    return {
        "sequence": sequence,
        "state": state,
        "actor_id": c.owner,
        "mapping_id": c.ref("interop.ImportMapping") if mapped else None,
        "report": {"rows": 1, "synthetic": True} if report else None,
        "digest": DIGEST if counted else "",
        **(IMPORT_COUNTS if counted else dict.fromkeys(IMPORT_COUNTS, 0)),
    }


def _owner_session(name: str, key: bytes) -> tuple[str, Seed]:
    """Return the owner's freshly step-up-verified browser session."""
    return (
        "identity.SessionRecord",
        Seed(
            lambda c: (
                {
                    "id": c.key(name),
                    "user_id": c.owner,
                    "session_key_hash": key,
                    "verification_device_id": c.ref(
                        "otp_totp.TOTPDevice", user_id=c.owner
                    ),
                    "ip_network": "192.0.2.0/24",
                    "user_agent_family": "firefox",
                    "verified_at": TRANSACTION_TIME,
                    "last_seen": c.now,
                    "expires_at": c.future,
                },
            )
        ),
    )


def _attended_encounter(c: SeedContext) -> object:
    """Return the fixture visit's own (closed) encounter."""
    return c.value("ehr.Encounter", "id", state="closed")


def _attended_visit(c: SeedContext) -> object:
    """Return the upgrade fixture's own booked appointment, its closed visit's."""
    return c.value("ehr.Encounter", "appointment_id", state="closed")


def _fixture_visit(c: SeedContext) -> object:
    """Return the open second visit's booked appointment (lifecycle v2 receipts)."""
    return c.value("ehr.Encounter", "appointment_id", id=c.open_encounter)


RATING_LABELS: Final = (
    "significant_omission",
    "hallucination",
    "speaker_attribution",
    "negation",
    "chronology",
    "units_decimals",
    "medication_name_dose",
    "allergy_handling",
    "unsupported_citation",
    "incorrect_action",
    "policy_violation",
    "unauthorized_effect",
    "duplicate_effect",
    "missed_escalation",
    "critical_error",
)


def _visit_citation(c: SeedContext) -> Row:
    """Return a draft citation of the open visit itself, at its current revision."""
    return {
        "section": "subjective",
        "kind": "encounter",
        "id": str(c.open_encounter),
        "version": c.value("ehr.Encounter", "revision", id=c.open_encounter),
        "digest": DIGEST,
    }


def _protocol_row(c: SeedContext, capability: str) -> Row:
    """Return a first-version protocol: two synthetic cases from two encounters."""
    return {
        "capability": capability,
        "version": 1,
        "corpus_version": "engineering-v1",
        "corpus_sha256": DIGEST,
        "case_manifest": [
            {
                "id": f"synthetic-case-{index}",
                "encounter": f"synthetic-encounter-0{index}",
                "specialty": "general",
                "device": "synthetic-microphone",
                "accent": "flite-kal",
                "adversarial": "noise",
            }
            for index in (1, 2)
        ],
        "thresholds_document": THRESHOLDS,
        "thresholds_sha256": sha256(THRESHOLDS.encode()).hexdigest(),
        "clinical_owner_ref": "synthetic-not-clinical-approval",
        "registered_by_id": c.owner,
    }


def _run_scores() -> Row:
    """Return the automatic scoring of both cases, recorded once on a pending run."""
    return {
        "sample_results": {
            "cases": [
                {"id": f"synthetic-case-{index}", "automatic": "pass"}
                for index in (1, 2)
            ]
        },
        "automatic_failures": 0,
        "scored_at": TRANSACTION_TIME,
    }


def _care_configuration() -> tuple[tuple[str, Seed], ...]:
    """Return the care-coordination configuration's evaluated path to active."""
    raters = (("rater_one", "colleague"), ("rater_two", "rater-two"))
    return (
        (
            "ai.ModelConfiguration",
            Seed(
                lambda c: (
                    {
                        **_model_row("care_coordination", "upgrade-synthetic-care"),
                        "id": c.key("care-configuration"),
                        "prompt_sha256": DIGEST,
                        "retrieval_sha256": DIGEST,
                        "schema_sha256": DIGEST,
                    },
                )
            ),
        ),
        (
            "ai.EvaluationProtocol",
            Seed(
                lambda c: (
                    {
                        **_protocol_row(c, "AI-05"),
                        "id": c.key("care-protocol"),
                    },
                )
            ),
        ),
        (
            "ai.EvaluationRun",
            Seed(
                lambda c: (
                    {
                        "id": c.key("care-run"),
                        "protocol_id": c.key("care-protocol"),
                        "configuration_id": c.key("care-configuration"),
                        "configuration_version": 1,
                        "versions": {
                            "prompt": DIGEST,
                            "retrieval": DIGEST,
                            "schema": DIGEST,
                            "model": DIGEST,
                            "version": "1",
                        },
                        "rater_one_id": c.key("colleague"),
                        "rater_two_id": c.key("rater-two"),
                        "adjudicator_id": c.key("adjudicator"),
                        "created_by_id": c.owner,
                        "status": "pending",
                    },
                ),
                updates=(
                    (
                        "ai.EvaluationRun",
                        lambda c: (c.key("care-run"), _run_scores()),
                    ),
                ),
            ),
        ),
        (
            "ai.EvaluationRating",
            Seed(
                lambda c: tuple(
                    {
                        "run_id": c.key("care-run"),
                        "case_id": f"synthetic-case-{index}",
                        "slot": slot,
                        "user_id": c.key(rater),
                        "_actor": f"key:{rater}",
                        "labels": {
                            **dict.fromkeys(RATING_LABELS, 0),
                            "correction_ms": 0,
                        },
                    }
                    for index in (1, 2)
                    for slot, rater in raters
                ),
            ),
        ),
        ("identity.User", Seed(_reviewer("co-owner"))),
        (
            "identity.UserClinicRole",
            Seed(lambda c: ({"user_id": c.key("co-owner"), "role": "owner"},)),
        ),
        (
            "ai.ModelChangeRequest",
            Seed(
                lambda c: (
                    {
                        "id": c.key("care-change"),
                        "run_id": c.key("care-run"),
                        "configuration_id": c.key("care-configuration"),
                        "status": "requested",
                        "approval_ref": "",
                        "requested_by_id": c.key("co-owner"),
                        "_actor": "key:co-owner",
                    },
                ),
                # Both raters agreed on every case: the clinical owner
                # finalizes the verdict (passed) before the change request.
                calls=(
                    lambda c: c.call(
                        "ai_eval_finalize", c.clinic, c.key("care-run"), actor="owner"
                    ),
                ),
                updates=(
                    (
                        "ai.ModelChangeRequest",
                        lambda c: (
                            c.key("care-change"),
                            {
                                "status": "approved",
                                "approved_by_id": c.owner,
                                "approval_ref": "synthetic-owner-approval",
                                "decided_at": c.now,
                                "_actor": "owner",
                            },
                        ),
                    ),
                    *(
                        (
                            "ai.ModelConfiguration",
                            lambda c, changes=changes: (
                                c.key("care-configuration"),
                                dict(changes),
                            ),
                        )
                        for changes in MODEL_APPROVAL
                    ),
                ),
            ),
        ),
    )


def _ai_output(purpose: str, name: str, method: str) -> tuple[str, Seed]:
    """Return the gateway's output for a reserved call, after which it settles."""
    return (
        "ai.AIArtifact",
        Seed(
            lambda c: (
                {
                    "purpose": purpose,
                    "kind": "output",
                    "method": method,
                    "invocation_id": c.key(name),
                    "content": "Synthetic AI output; no clinical data.",
                    "retention_class": "review_draft",
                    "patient_enrollment_id": c.enrollment,
                    "expires_at": c.future,
                    "user_id": c.physician,
                },
            ),
            actor="physician",
            updates=(
                (
                    "ai.AIInvocation",
                    lambda c: (
                        c.key(name),
                        {
                            "status": "succeeded",
                            "tokens_in": 4,
                            "tokens_out": 3,
                            "cost_micros": 7,
                            "cost_minor": 1,
                            "settled_at": c.now,
                        },
                    ),
                ),
            ),
        ),
    )


PACKS: Final = (
    ("general", [{"key": "bmi", "version": 1}, {"key": "egfr", "version": 1}]),
    (
        "pediatrics",
        [
            {"key": "corrected_age", "version": 1},
            {"key": "growth_percentile", "version": 1},
        ],
    ),
)


def _pack_sources(key: str) -> tuple[tuple[str, Seed], ...]:
    """Return a pack's published versioned template and its questionnaire."""
    return (
        (
            "ehr.SpecialtyTemplate",
            Seed(
                lambda c: (
                    {
                        "id": c.key(f"pack-template-{key}"),
                        "key": f"pack_{key}_v1",
                        "version": 1,
                        "title": f"Synthetic {key} pack",
                        "prompts": {
                            "schema_version": 2,
                            "state": "published",
                            "fields": [],
                            "phrases": [],
                            "optional_sections": [],
                            "subjective": "Synthetic subjective prompt.",
                            "objective": "Synthetic objective prompt.",
                            "assessment": "Synthetic assessment prompt.",
                            "plan": "Synthetic plan prompt.",
                        },
                    },
                )
            ),
        ),
        (
            "intake.QuestionnaireTemplate",
            Seed(
                lambda c: (
                    {
                        "id": c.key(f"pack-questionnaire-{key}"),
                        "key": f"pack_{key}_v1",
                        "version": 1,
                    },
                )
            ),
        ),
    )


def _finalized(c: SeedContext, name: str) -> object:
    """Return a column of the witness's finalized SOAP version."""
    return c.value("ehr.ClinicalDocumentVersion", name, state="finalized")


# The first reviewed neutral marketing body (apps/crm/rules.py).
MARKETING_BODY: Final = (
    "A clinica tem horarios disponiveis. Consulte a agenda no portal. "
    "Voce pode desativar mensagens de divulgacao nas preferencias do portal."
)


# Columns whose names look secret-bearing but hold public values: recorded
# as-is, each with the reason.
PUBLIC_COLUMNS: Final[dict[str, str]] = {
    "identity.ClinicConfiguration.brand_token": "a named visual design token",
    "ai.ActionReceipt.key_id": "the attestation key's public identifier",
    "ai.Approval.key_id": "the attestation key's public identifier",
    "ai.KnowledgeArticle.key": "an article's catalog key",
    "ai.ModelConfiguration.capability_key": "a provider registry capability name",
    "ehr.SpecialtyPack.key": "a specialty pack's catalog name",
    "ehr.SpecialtyTemplate.key": "a pack template's catalog name",
    "identity.OnboardingProgress.step_key": "an onboarding step name",
    "identity.SsoConnection.provider_key": "a provider registry capability name",
    "intake.QuestionnaireTemplate.key": "a questionnaire's catalog name",
    "interop.ApiWriteReceipt.idempotency_key": "a client-chosen replay key",
    "orders.ResultVersion.storage_key": "a content-addressed object name",
    "providers.ProviderCapability.key": "a provider registry capability name",
    "retention.ArtifactPurge.deleted_keys": "names of the purged storage objects",
    "scribe.AudioChunk.storage_key": "a content-addressed object name",
    "workflows.PROAssignment.instrument_key": "a PRO instrument's catalog name",
    "workflows.WorkflowDefinitionVersion.key": "a workflow definition's name",
}

# Columns holding secret material: the record keeps only their SHA-256.
SECRET_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        "identity.TotpSecret.secret",
        "identity.User.password",
        "scribe.AudioChunk.wrapped_key",
        # The TOTP seed itself until identity.0050 moves it to TotpSecret.
        "otp_totp.TOTPDevice.key",
        "otp_static.StaticToken.token",
        "identity.Passkey.credential_id",
        "identity.Passkey.public_key",
    }
)

# The database role a model loads and reads back as when its table admits no
# owner-side write: ``clinic_app`` where the runtime policy and grants are
# the write path, ``clinic_resolver`` where only SECURITY DEFINER resolvers
# insert. Every other model loads as the migration owner under FORCE RLS.
LOAD_ROLES: Final[dict[str, str]] = {
    "comms.Message": "clinic_app",
    "identity.AuthenticatorRetirement": "clinic_app",
    "otp_totp.TOTPDevice": "clinic_app",
    "identity.RecoveryBatch": "clinic_app",
    "otp_static.StaticDevice": "clinic_app",
    "analytics.AnalystReview": "clinic_app",
    "analytics.MetricDefinitionVersion": "clinic_resolver",
    "analytics.ReadModelRefresh": "clinic_resolver",
    "analytics.SavedReport": "clinic_app",
    "billing.Account": "clinic_resolver",
    "billing.CashSession": "clinic_app",
    "billing.CashSessionEvent": "clinic_resolver",
    "billing.FiscalDocument": "clinic_app",
    "billing.FiscalTransition": "clinic_resolver",
    "billing.InvoiceTerms": "clinic_resolver",
    "billing.JournalBook": "clinic_app",
    "billing.JournalEntry": "clinic_resolver",
    "billing.JournalLine": "clinic_resolver",
    "billing.JournalSettlement": "clinic_resolver",
    "billing.Payout": "clinic_app",
    "billing.PractitionerPayoutRule": "clinic_app",
    "billing.PspBinding": "clinic_app",
    "billing.PspEvidence": "clinic_app",
    "billing.PspIntent": "clinic_app",
    "billing.PspPolicy": "clinic_app",
    "billing.PspReceipt": "clinic_app",
    "billing.Refund": "clinic_app",
    "billing.SettlementAllocation": "clinic_resolver",
    "identity.SavedView": "clinic_app",
    "identity.TotpSecret": "clinic_resolver",
    "identity.UserPreference": "clinic_app",
    "insurance.Appeal": "clinic_app",
    "insurance.Batch": "clinic_app",
    "insurance.BatchGuide": "clinic_app",
    "insurance.ClinicalApproval": "clinic_app",
    "insurance.CommandReceipt": "clinic_app",
    "insurance.Glosa": "clinic_app",
    "insurance.Guide": "clinic_app",
    "insurance.InsuranceMembership": "clinic_app",
    "insurance.Payer": "clinic_app",
    "insurance.PayerContract": "clinic_app",
    "insurance.PayerRequest": "clinic_app",
    "insurance.PaymentMatch": "clinic_app",
    "insurance.PriceTable": "clinic_app",
    "insurance.Return": "clinic_app",
    "insurance.Submission": "clinic_app",
    "insurance.TussTerm": "clinic_app",
    "interop.ApiAccessToken": "clinic_resolver",
    "interop.ApiClient": "clinic_resolver",
    "interop.ApiClientSecret": "clinic_resolver",
    "interop.ApiWriteReceipt": "clinic_resolver",
    "interop.OrganizationExport": "clinic_resolver",
    "interop.OrganizationExportPart": "clinic_app",
    "interop.RndsSubmission": "clinic_resolver",
    "interop.WebhookAttempt": "clinic_resolver",
    "interop.WebhookDelivery": "clinic_resolver",
    "interop.WebhookSubscription": "clinic_resolver",
}

# Cross-row invariants the product's guards and deferred constraints enforce,
# re-checked over the seeded tenant at the leaf (clinic_super, read only).
# Each query counts violating rows; any non-zero count fails the gate.
LEAF_INVARIANTS: Final[dict[str, str]] = {
    # Lifecycle and vocabulary rules the binding triggers enforce per row; the
    # round-2 review found each broken while those triggers were suspended.
    # (A task product code opens itself, such as a PRO review, has no creator.)
    "appointment_receipt_is_lifecycle_edge": """
        SELECT count(*) FROM clinic_app.scheduling_appointmenttransition r
        JOIN clinic_app.scheduling_appointment a ON a.id=r.appointment_id
        WHERE r.organization_id=%(organization)s AND (r.revision>a.revision
            OR (r.from_status<>'' AND r.from_status||'>'||r.to_status NOT IN (
                'requested>held','requested>scheduled','requested>cancelled',
                'held>scheduled','held>cancelled','held>expired',
                'scheduled>arrived','scheduled>cancelled','scheduled>no_show',
                'arrived>in_progress','arrived>cancelled','in_progress>completed')))""",
    "order_history_starts_draft": """
        SELECT count(*) FROM clinic_app.orders_order o
        WHERE o.organization_id=%(organization)s AND (SELECT e.state
            FROM clinic_app.orders_orderevent e WHERE e.order_id=o.id
            ORDER BY e.sequence LIMIT 1) IS DISTINCT FROM 'draft'""",
    "order_item_from_catalog": """
        SELECT count(*) FROM clinic_app.orders_orderitem i
        WHERE i.organization_id=%(organization)s
        AND i.exam_code NOT IN ('cbc','urinalysis')""",
    "result_storage_key_is_digest": """
        SELECT count(*) FROM clinic_app.orders_resultversion v
        WHERE v.organization_id=%(organization)s
        AND v.storage_key !~ '^[0-9a-f]{64}$'""",
    "acknowledgment_disposition_vocabulary": """
        SELECT count(*) FROM clinic_app.orders_resultacknowledgment a
        WHERE a.organization_id=%(organization)s
        AND a.disposition NOT IN ('no_action','follow_up','urgent_review')""",
    "rejected_version_unmatched": """
        SELECT count(*) FROM clinic_app.orders_resultrejection r
        JOIN clinic_app.orders_resultmatch m ON m.version_id=r.version_id
        WHERE r.organization_id=%(organization)s""",
    "rejected_version_never_released": """
        SELECT count(*) FROM clinic_app.orders_resultrelease l
        JOIN clinic_app.orders_resultrejection r ON r.version_id=l.version_id
        WHERE l.organization_id=%(organization)s""",
    "task_revision_counts_transitions": """
        SELECT count(*) FROM clinic_app.workflows_task t
        WHERE t.organization_id=%(organization)s AND t.created_by_id IS NOT NULL
        AND t.state<>'open' AND t.revision=1""",
    "recording_session_revision_counts_transitions": """
        SELECT count(*) FROM clinic_app.scribe_recordingsession s
        WHERE s.organization_id=%(organization)s
        AND s.state<>'created' AND s.revision=1""",
    "compensation_follows_failure": """
        SELECT count(*) FROM clinic_app.ai_compensation c
        JOIN clinic_app.ai_actionexecution e ON e.id=c.execution_id
        WHERE c.organization_id=%(organization)s
        AND e.state NOT IN ('failure','compensated')""",
    "break_glass_review_and_notices": """
        SELECT count(*) FROM clinic_app.identity_breakglassgrant g
        WHERE g.organization_id=%(organization)s AND (NOT EXISTS (
            SELECT 1 FROM clinic_app.workflows_task t
            WHERE t.id=g.review_task_id AND t.kind='review'
            AND t.idempotency_key=g.id AND t.priority='high'
            AND t.due_at=g.expires_at+interval '24 hours'
            AND t.subject_ref=jsonb_build_object(
                'kind','clinic','id',g.clinic_id::text))
        OR (SELECT count(*) FROM clinic_app.identity_governancenotice n
            WHERE n.break_glass_id=g.id)<>2)""",
    "note_copy_from_other_finalized_version": """
        SELECT count(*) FROM clinic_app.ehr_copiednoteblock b
        JOIN clinic_app.ehr_clinicaldocumentversion s ON s.id=b.source_id
        JOIN clinic_app.ehr_clinicaldocumentversion t ON t.id=b.target_id
        JOIN clinic_app.ehr_clinicaldocument sd ON sd.id=s.document_id
        JOIN clinic_app.ehr_clinicaldocument td ON td.id=t.document_id
        JOIN clinic_app.ehr_encounter se ON se.id=sd.encounter_id
        JOIN clinic_app.ehr_encounter te ON te.id=td.encounter_id
        WHERE b.organization_id=%(organization)s AND (b.source_id=b.target_id
            OR s.state NOT IN ('finalized','superseded')
            OR se.patient_id<>te.patient_id OR b.inserted_revision>t.revision)""",
    "campaign_send_from_sending_marketing_thread": """
        SELECT count(*) FROM clinic_app.crm_campaignsend cs
        JOIN clinic_app.crm_campaign c ON c.id=cs.campaign_id
        JOIN clinic_app.comms_templateversion tv ON tv.id=c.template_id
        LEFT JOIN clinic_app.comms_message m ON m.id=cs.message_id
        LEFT JOIN clinic_app.comms_conversation cv ON cv.id=m.conversation_id
        WHERE cs.organization_id=%(organization)s AND (c.state NOT IN ('sending','sent')
            OR tv.purpose<>'marketing' OR tv.category<>'marketing'
            OR tv.approval_status<>'approved' OR m.direction IS DISTINCT FROM 'out'
            OR m.template_id IS DISTINCT FROM c.template_id
            OR cv.purpose IS DISTINCT FROM 'marketing'
            OR cv.enrollment_id IS DISTINCT FROM cs.enrollment_id)""",
    "evaluation_reviewers_distinct_physicians": """
        SELECT count(*) FROM clinic_app.ai_evaluationrun r
        WHERE r.organization_id=%(organization)s AND (r.rater_one_id=r.rater_two_id
            OR r.rater_one_id=r.adjudicator_id OR r.rater_two_id=r.adjudicator_id
            OR r.created_by_id IN (r.rater_one_id,r.rater_two_id,r.adjudicator_id)
            OR 3<>(SELECT count(DISTINCT role.user_id)
                FROM clinic_app.identity_userclinicrole role
                WHERE role.user_id IN (r.rater_one_id,r.rater_two_id,r.adjudicator_id)
                AND role.clinic_id=r.clinic_id AND role.role='physician'
                AND role.revoked_at IS NULL))""",
    "merge_decision_receipt": """
        SELECT count(*) FROM clinic_app.intake_mergedecision d
        LEFT JOIN clinic_app.intake_duplicatecandidate c ON c.id=d.candidate_id
        WHERE d.organization_id=%(organization)s AND (c.state IS DISTINCT FROM
            CASE d.decision WHEN 'merge' THEN 'merged' ELSE 'dismissed' END
            OR (d.decision='merge' AND NOT EXISTS (SELECT 1
                FROM clinic_app.intake_patientalias a
                WHERE a.merge_decision_id=d.id)))""",
    "duplicate_candidate_ordered_pair": """
        SELECT count(*) FROM clinic_app.intake_duplicatecandidate c
        WHERE c.organization_id=%(organization)s
        AND c.enrollment_a_id>=c.enrollment_b_id""",
    "demographics_version_has_receipt": """
        SELECT count(*) FROM clinic_app.intake_patientdemographics v
        WHERE v.organization_id=%(organization)s AND NOT EXISTS (SELECT 1
            FROM clinic_app.intake_demographicscorrection r
            WHERE r.demographics_id=v.id)""",
    "delegate_grant_two_enrollments": """
        SELECT count(*) FROM clinic_app.identity_patientdelegategrant g
        WHERE g.organization_id=%(organization)s
        AND g.represented_enrollment_id=g.delegate_enrollment_id""",
}

# Encrypted columns no reachable state of a seeded model holds, with the
# reason; every other one must carry data in at least one seeded row.
EMPTY_PROTECTED_EXCEPTIONS: Final[dict[str, str]] = {}

# Columns a later candidate migration rewrites on purpose, with the reason.
PRESERVATION_EXCEPTIONS: Final[dict[str, str]] = {
    # A replacement device seeded before identity.0050 crosses it.
    "otp_totp.TOTPDevice.key": (
        "identity.0050 moves the secret into identity.TotpSecret and clears key"
    ),
}

PROVIDERS: Final[dict[str, Seed]] = {
    "ProviderCapability": Seed(
        _rows(
            {
                "key": "upgrade_synthetic_capability",
                "record_ref": "synthetic-upgrade-record",
                "description": "Synthetic upgrade capability; no real provider.",
            }
        )
    ),
    "CapabilityApproval": Seed(
        _rows(
            {
                "approver_name": "Synthetic Approver",
                "approver_role": "synthetic-review",
                "evidence_uri": "https://example.invalid/upgrade-evidence",
            }
        )
    ),
    # Researched (no approval) and sandbox (approved) sides of the lifecycle.
    "CapabilityVersion": Seed(
        lambda _c: (
            {
                "provider": "synthetic-upgrade",
                "account": "synthetic",
                "environment": "sandbox",
                "api_version": "v1",
                "region": "br-synthetic",
                "state": "researched",
            },
            {
                "provider": "synthetic-upgrade",
                "account": "synthetic",
                "environment": "sandbox",
                "api_version": "v2",
                "region": "br-synthetic",
                "state": "researched",
            },
        ),
        # The second version walks the gated lifecycle to sandbox; the
        # approval binds on entry to approved_to_test.
        updates=tuple(
            ("providers.CapabilityVersion", _version_step(changes))
            for changes in cast(
                "tuple[Row, ...]",
                (
                    {"state": "selected_in_plan"},
                    {"state": "approved_to_test", "approval": True},
                    {"state": "sandbox"},
                ),
            )
        ),
    ),
    "ActivationRecord": Seed(
        lambda c: ({"version_id": c.ref("providers.CapabilityVersion", 1)},)
    ),
    "HealthEvent": Seed(_rows({"kind": "note", "detail": "Synthetic health note."})),
}

IDENTITY: Final[dict[str, Seed]] = {
    "UserPreference": Seed(_rows({"theme": "dark"})),
    "CareTeamMembership": Seed(
        lambda c: ({"role": "physician", "user_id": c.physician},)
    ),
    "ProfessionalRegistration": Seed(
        lambda c: (
            {
                "role": "physician",
                "council": "CRM",
                "jurisdiction": "SP",
                "number": "SYNTHETIC-CRM-0001",
                "specialty": "Synthetic general practice",
                "status": "regular",
                "user_id": c.physician,
                "valid_to": c.now + timedelta(days=365),
            },
        )
    ),
    "RoleGrant": Seed(
        _rows({"role": "receptionist", "permission": "appointment.book"})
    ),
    "ServicePrincipal": Seed(
        _rows(
            {
                "name": "upgrade-agent",
                "db_identity": "clinic_agent_appointment_read",
                "purpose": "appointment-read",
            }
        )
    ),
    "ServicePrincipalGrant": Seed(_rows({"permission": "appointment.read"})),
    "SavedView": Seed(
        _rows({"destination": "patients", "params": {"q": "synthetic"}}),
    ),
    "SignupRegistration": Seed(),
    "AuthenticatorReset": Seed(),
    # The physician retires their own device; the owner's stays enrolled.
    "AuthenticatorRetirement": Seed(
        lambda c: (
            {
                "user_id": c.physician,
                "device_id": c.ref("otp_totp.TOTPDevice", user_id=c.physician),
            },
        ),
        actor="physician",
        support=(
            (
                "otp_totp.TOTPDevice",
                Seed(
                    lambda c: (
                        {
                            "user_id": c.physician,
                            "name": "synthetic-replacement",
                            "confirmed": True,
                        },
                    ),
                    actor="physician",
                ),
            ),
        ),
    ),
    "OnboardingProgress": Seed(_rows({"step_key": "clinic-profile"})),
    "RecoveryBatch": Seed(
        lambda c: (
            {
                "user_id": c.owner,
                "device_id": c.ref("otp_totp.TOTPDevice", user_id=c.owner),
                "static_device_id": c.ref("otp_static.StaticDevice"),
            },
        ),
        support=(
            (
                "otp_static.StaticDevice",
                Seed(
                    lambda c: (
                        {
                            "user_id": c.owner,
                            "name": "synthetic-recovery",
                            "confirmed": True,
                        },
                    )
                ),
            ),
        ),
    ),
    "RecoveryCode": Seed(),
    # The physician's session: the owner's later authenticator lifecycle rows
    # legitimately revoke the owner's own sessions.
    "SessionRecord": Seed(
        lambda c: (
            {
                "ip_network": "192.0.2.0/24",
                "user_agent_family": "firefox",
                "user_id": c.physician,
            },
        )
    ),
    "StaffInvitation": Seed(
        lambda c: (
            {
                "bundle": "receptionist",
                "pending_user_id": c.physician,
                "desired_username": "synthetic-reception",
            },
        )
    ),
    "PatientDelegateGrant": Seed(
        lambda c: (
            {
                "authority_kind": "patient_authorized_adult",
                "purposes": ["care_processing"],
                "operations": ["records"],
                "represented_enrollment_id": c.enrollment,
                "delegate_enrollment_id": c.companion_enrollment,
                "requested_by_id": c.owner,
            },
        )
    ),
    "Passkey": Seed(
        _rows(
            {
                "credential_id": BYTES32,
                "public_key": BYTES32,
                "nickname": "Synthetic key",
            }
        )
    ),
    "TotpSecret": Seed(
        adopt="0050 moves every existing device secret here",
    ),
    # A grant with its mandatory high-priority review task and both notices
    # (the deferred enterprise_grant_complete constraint checks all three).
    "BreakGlassGrant": Seed(
        lambda c: (
            {
                "id": c.key("break-glass"),
                "clinician_id": c.physician,
                "reason": "Synthetic review.",
                "created_at": c.now,
                "expires_at": c.now + timedelta(hours=2),
            },
        ),
        # The physician requests emergency access from a verified session;
        # the owner's governance session is recorded alongside for the
        # stage's organization-admin writes.
        actor="physician",
        settings=CLINICIAN_SETTINGS,
        support=(
            (
                "identity.SessionRecord",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("break-glass-session"),
                            "user_id": c.physician,
                            "session_key_hash": CLINICIAN_SESSION_KEY,
                            # The physician's first device is retired; the
                            # replacement is the live authenticator.
                            "verification_device_id": c.ref(
                                "otp_totp.TOTPDevice",
                                user_id=c.physician,
                                name="synthetic-replacement",
                            ),
                            "ip_network": "192.0.2.0/24",
                            "user_agent_family": "firefox",
                            # Step-up is fresh: verified in this very transaction.
                            "verified_at": TRANSACTION_TIME,
                            "last_seen": c.now,
                            "expires_at": c.future,
                        },
                    )
                ),
            ),
            (
                "identity.SessionRecord",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("enterprise-session"),
                            "user_id": c.owner,
                            "session_key_hash": ENTERPRISE_SESSION_KEY,
                            "verification_device_id": c.ref(
                                "otp_totp.TOTPDevice", user_id=c.owner
                            ),
                            "ip_network": "192.0.2.0/24",
                            "user_agent_family": "firefox",
                            # Step-up is fresh: verified in this very transaction.
                            "verified_at": TRANSACTION_TIME,
                            "last_seen": c.now,
                            "expires_at": c.future,
                        },
                    )
                ),
            ),
        ),
    ),
    # Both notices are written by the grant's own trigger.
    # As grant_break_glass does: the clinician's factor-checked transaction
    # creates the mandatory review task, then notifies manager and patient
    # (the grant's deferred completeness constraint checks both at commit).
    "GovernanceNotice": Seed(
        lambda c: tuple(
            {"break_glass_id": c.key("break-glass"), "audience": audience}
            for audience in ("manager", "patient")
        ),
        actor="physician",
        settings=CLINICIAN_SETTINGS,
        calls=(
            lambda c: c.call(
                "enterprise_create_review",
                c.key("break-glass"),
                actor="physician",
                settings=dict(CLINICIAN_SETTINGS_MAP),
            ),
        ),
    ),
    # The owner connects the registered synthetic OIDC provider, selected as
    # the enterprise-identity capability's current version first.
    # The owner registers the synthetic OIDC provider (researched, current)
    # in the provider registry, then connects it.
    "SsoConnection": Seed(
        lambda c: ({"provider_key": "enterprise_identity", "created_by_id": c.owner},),
        settings=ENTERPRISE_SETTINGS,
        support=(
            (
                "providers.ProviderCapability",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("enterprise-capability"),
                            "key": "enterprise_identity",
                            "record_ref": "synthetic-enterprise-identity",
                            "description": "Synthetic OIDC; no real provider.",
                        },
                    )
                ),
            ),
            (
                "providers.CapabilityVersion",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("enterprise-version"),
                            "capability_id": c.key("enterprise-capability"),
                            "provider": "deterministic-oidc",
                            "account": "synthetic",
                            "environment": "synthetic",
                            "api_version": "v1",
                            "region": "br-synthetic",
                            "state": "researched",
                        },
                    ),
                    updates=(
                        (
                            "providers.ProviderCapability",
                            lambda c: (
                                c.key("enterprise-capability"),
                                {"current_version_id": c.key("enterprise-version")},
                            ),
                        ),
                    ),
                ),
            ),
        ),
    ),
    # The directory provisions an account (a password-less directory user)
    # and a group under the owner's governed connection.
    # The directory provisions an account (the resolver mints the password-less
    # directory user) and a group under the owner's governed connection.
    "ScimResource": Seed(
        lambda c: (
            {
                "kind": "Users",
                "user_id": c.call(
                    "enterprise_new_directory_user",
                    c.ref("identity.SsoConnection"),
                    actor="owner",
                    settings=dict(ENTERPRISE_SETTINGS_MAP),
                ),
                "search_hash": BYTES32,
            },
            {"kind": "Groups", "search_hash": bytes(32)},
        ),
        settings=ENTERPRISE_SETTINGS,
    ),
    "ScimMembership": Seed(
        lambda c: (
            {
                "group_id": c.ref("identity.ScimResource", 1),
                "member_id": c.ref("identity.ScimResource", 0),
            },
        ),
        settings=ENTERPRISE_SETTINGS,
    ),
    # The owner's directory sync grants the mapped bundle to the group's
    # member and binds that membership to the directory account.
    "ScimRoleBinding": Seed(
        adopt="the directory sync writes the binding",
        after=("identity.ScimMembership", "identity.ScimRoleMapping"),
        settings=ENTERPRISE_SETTINGS,
        calls=(
            lambda c: c.call(
                "enterprise_sync_directory",
                c.ref("identity.SsoConnection"),
                actor="owner",
                settings=dict(ENTERPRISE_SETTINGS_MAP),
            ),
        ),
    ),
    "ScimRoleMapping": Seed(
        lambda c: (
            {
                "group_id": c.ref("identity.ScimResource", 1),
                "unit_id": c.clinic,
                "bundle": "receptionist",
            },
        ),
        settings=ENTERPRISE_SETTINGS,
    ),
    "ScimToken": Seed(
        _rows({"secret_hash": BYTES32}),
        settings=ENTERPRISE_SETTINGS,
    ),
    "SupportAccessGrant": Seed(
        lambda c: ({"support_user_id": c.physician, "purpose": "Synthetic."},),
        settings=ENTERPRISE_SETTINGS,
    ),
    "UnitGovernanceChange": Seed(
        lambda c: (
            {
                "kind": "unit_membership",
                "access_denied": False,
                "policy_version": 1,
                "target_user_id": c.physician,
            },
        ),
        settings=ENTERPRISE_SETTINGS,
    ),
    "DelegateAccessEvent": Seed(_rows({"record_type": "result"})),
}

TELECONSULT: Final[dict[str, Seed]] = {
    "TeleconsultDeviceCheck": Seed(
        lambda c: (
            {
                "session_id": c.live_session,
                "role": "physician",
                "participant_id": c.physician,
                "camera": "ok",
                "microphone": "ok",
                "speaker": "ok",
                "network": "good",
            },
        ),
        actor="physician",
    ),
}

SCHEDULING: Final[dict[str, Seed]] = {
    "Holiday": Seed(),
    "Resource": Seed(
        _rows(
            {"kind": "room", "name": "Synthetic room", "capacity": 2},
            {"kind": "location", "name": "Synthetic front desk"},
        )
    ),
    "AppointmentResource": Seed(),
    "Absence": Seed(lambda c: ({"practitioner_id": c.physician},)),
    "AvailabilityTemplate": Seed(
        lambda c: (
            {
                "weekdays": [1, 3],
                "start_local": time(8, 0),
                "end_local": time(12, 0),
                "valid_from": c.now.date(),
                "valid_to": (c.now + timedelta(days=90)).date(),
                "timezone": "America/Sao_Paulo",
                "practitioner_id": c.physician,
            },
        )
    ),
    "ServiceType": Seed(
        _rows(
            {
                "name": "Synthetic consultation",
                "duration_min": 30,
                "required_professional_roles": ["physician"],
                "required_resource_kinds": ["room"],
            }
        )
    ),
    "AppointmentSeries": Seed(
        lambda c: (
            {
                "frequency": "weekly",
                "weekday": 2,
                "first_date": (c.now + timedelta(days=7)).date(),
                "start_local": time(9, 0),
                "end_local": time(9, 30),
                "timezone": "America/Sao_Paulo",
                "count": 4,
                "create_fingerprint": BYTES32,
            },
        )
    ),
    # Reception records the fixture visit's arrival (scheduled -> arrived);
    # the lifecycle trigger writes the receipt at the bumped revision.
    "AppointmentTransition": Seed(
        adopt="the lifecycle receipt trigger writes it for the arrival",
        prepare=tuple(
            (
                "scheduling.Appointment",
                # The visit's own practitioner moves it (move_own).
                _move(
                    _attended_visit,
                    status=status,
                    last_command_id=uuid4(),
                    _actor="physician",
                ),
            )
            for status in ("arrived", "in_progress", "completed")
        ),
    ),
    "SeriesException": Seed(
        _rows({"occurrence_index": 2, "kind": "cancelled", "affected_count": 1})
    ),
    "ClinicianDelay": Seed(
        lambda c: ({"practitioner_id": c.physician, "minutes": 15},)
    ),
    "DisplayDevice": Seed(_rows({"token_hash": DIGEST})),
    # Reception records the open visit's arrival (scheduled -> arrived,
    # receipted by the lifecycle trigger), then queues it at the front desk;
    # the guard assigns position, ticket and revision.
    "QueueEntry": Seed(
        lambda c: (
            {
                "appointment_id": _fixture_visit(c),
                "location_id": c.ref("scheduling.Resource", kind="location"),
                "state": "waiting",
            },
        ),
        prepare=(
            (
                "scheduling.Appointment",
                lambda c: (
                    _fixture_visit(c),
                    {"status": "arrived", "last_command_id": c.key("arrive-command")},
                ),
            ),
        ),
    ),
    "RoomStatus": Seed(
        lambda c: ({"room_id": c.ref("scheduling.Resource"), "unit": 1},)
    ),
}

INTAKE: Final[dict[str, Seed]] = {
    "ClinicIntakePolicy": Seed(
        _rows({"version": 1, "required_fields": ["legal_name"]})
    ),
    "EmergencyContact": Seed(
        _rows(
            {
                "name": "Synthetic Contact",
                "relationship": "Synthetic sibling",
                "phone": "+55 11 0000-0000",
            }
        )
    ),
    "InsuranceMembership": Seed(
        lambda c: (
            {
                "payer_name": "Synthetic Health",
                "plan_name": "Synthetic Plan",
                "membership_number": "SYN0000000001",
                "ans_number": "123456",
                "valid_until": (c.now + timedelta(days=365)).date(),
            },
        )
    ),
    "PatientAddress": Seed(
        _rows(
            {
                "kind": "home",
                "postal_code": "01000-000",
                "street": "Rua Sintetica",
                "street_number": "100",
                "complement": "Sala 1",
                "district": "Centro Sintetico",
                "city": "Synthetic City",
                "state_code": "SP",
            }
        )
    ),
    "PatientIdentifier": Seed(
        _rows(
            {
                "kind": "cns",
                "value": "SYNTHETIC-CNS-0001",
                "issuer": "SYNTHETIC-ISSUER",
                "blind_index": BYTES32,
            }
        )
    ),
    # Patient-reported provenance with explicitly unknown fields, then the
    # staff correction receipt for the same version.
    "PatientDemographics": Seed(
        _rows(
            {
                "version": 1,
                "legal_name": "Synthetic Upgrade Persona",
                "social_name": "Synthetic Persona",
                "preferred_name": "Persona",
                "birth_date": date(1990, 1, 1),
                "sex_at_birth": "not_informed",
                "gender_identity": "declined",
                "language": "pt-BR",
                "accessibility_needs": "Synthetic large print.",
                "pronouns": "",
                "occupation": None,
                "source": "patient_reported",
                "unknown_fields": {"occupation": "unknown", "pronouns": "declined"},
            }
        )
    ),
    "DemographicsCorrection": Seed(
        _rows(
            {
                "actor_label": "Synthetic reception",
                "reason": "Synthetic patient-reported update.",
                "changed_fields": ["legal_name", "social_name"],
            }
        )
    ),
    "DuplicateCandidate": Seed(
        lambda c: (
            {
                "signals": ["dob+name_prefix"],
                "enrollment_a_id": min(str(c.enrollment), str(c.companion_enrollment)),
                "enrollment_b_id": max(str(c.enrollment), str(c.companion_enrollment)),
            },
        ),
        settings=MERGE_SETTINGS,
        actor="physician",
    ),
    "PatientDuplicateIndex": Seed(
        lambda c: (
            {
                "patient_id": c.companion_patient,
                "version": 2,
                "index_key_version": 1,
                "name_birth_index": BYTES32,
            },
        )
    ),
    "MergeDecision": Seed(
        lambda c: (
            {
                "decision": "merge",
                "merged_version": _latest_version(c, c.companion_patient),
                "survivor_version": _latest_version(c, c.patient),
                "result_version": _latest_version(c, c.patient) + 1,
                "merged_patient_id": c.companion_patient,
                "survivor_patient_id": c.patient,
                "field_choices": {"legal_name": "survivor"},
                "reason": "Synthetic duplicate review.",
                "adjudicator_id": c.physician,
                "verified_session_id": c.key("merge-session"),
                # One adjudication instant: the candidate transition and the
                # receipt both bind to the transaction's timestamp.
                "created_at": TRANSACTION_TIME,
            },
        ),
        # The merge's receipts: the survivor's resulting demographics version
        # and its correction by the adjudicator, written in the same commit.
        settings=MERGE_SETTINGS,
        support=(
            (
                "identity.SessionRecord",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("merge-session"),
                            "user_id": c.physician,
                            "session_key_hash": MERGE_SESSION_KEY,
                            "verification_device_id": c.ref("otp_totp.TOTPDevice"),
                            "last_seen": c.now,
                            "ip_network": "192.0.2.0/24",
                            "user_agent_family": "firefox",
                            "verified_at": c.now,
                            "expires_at": c.future,
                        },
                    )
                ),
            ),
        ),
        updates=(
            (
                "intake.DuplicateCandidate",
                lambda c: (c.ref("intake.DuplicateCandidate"), {"state": "merged"}),
            ),
        ),
        actor="physician",
    ),
    "PatientAlias": Seed(
        lambda c: (
            {
                "merged_patient_id": c.companion_patient,
                "survivor_patient_id": c.patient,
                "active": True,
                "deactivation_reason": None,
            },
        ),
        actor="physician",
        settings=MERGE_SETTINGS,
        # The adjudicator then unmerges it from the same verified session.
        updates=(
            (
                "intake.PatientAlias",
                lambda c: (
                    c.ref("intake.PatientAlias"),
                    {
                        "active": False,
                        "deactivated_by_id": c.physician,
                        "deactivation_session_id": c.key("merge-session"),
                        "deactivation_reason": "Synthetic unmerge after review.",
                    },
                ),
            ),
        ),
    ),
    "PatientMergeTransfer": Seed(
        lambda c: (
            {
                "kind": "appointment",
                "original_id": str(c.companion_enrollment),
                "replacement_id": str(c.enrollment),
                "action": "recreated",
            },
        ),
        # The merge's receipts, written in its commit: the survivor's resulting
        # demographics version and the adjudicator's correction for it.
        support=(
            (
                "intake.PatientDemographics",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("merge-result-demographics"),
                            "version": _latest_version(c, c.patient) + 1,
                            "legal_name": "Synthetic Upgrade Persona",
                            # The adjudicated version answers the two fields
                            # the patient left unknown or declined.
                            "occupation": "Synthetic occupation",
                            "pronouns": "ela/dela",
                            "source": "staff_recorded",
                            "created_at": TRANSACTION_TIME,
                        },
                    ),
                    actor="physician",
                ),
            ),
            (
                "intake.DemographicsCorrection",
                Seed(
                    lambda c: (
                        {
                            "actor_id": c.physician,
                            "actor_label": "Synthetic adjudicator",
                            "reason": "Synthetic merge result.",
                            "changed_fields": ["legal_name"],
                            "demographics_id": c.key("merge-result-demographics"),
                            "previous_id": c.ref("intake.PatientDemographics"),
                        },
                    ),
                    actor="physician",
                ),
            ),
        ),
        actor="physician",
        settings=MERGE_SETTINGS,
    ),
    # Filed by the patient in their portal session; its own AFTER trigger
    # opens the review task.
    "DataSubjectRequest": Seed(
        lambda c: (
            {
                "kind": "correction",
                "details": "Synthetic correction request.",
                "patient_session_id": c.patient_session,
                "requester_enrollment_id": c.enrollment,
                "received_at": c.now,
                "due_at": c.now + timedelta(days=15),
                "task_id": c.key("data-request-task"),
            },
        ),
        actor="patient",
        settings=(("app.current_patient_session", "patient_session_id"),),
    ),
}

CONSENT: Final[dict[str, Seed]] = {
    "NoticeVersion": Seed(_rows({"topic": "ai_use", "version": 1})),
    "AIUseDisclosure": Seed(
        lambda c: (
            {
                "encounter_id": c.open_encounter,
                "informed": True,
                "refused": False,
                "recorded_by_id": c.physician,
            },
        ),
        actor="physician",
    ),
    "ParticipantAcknowledgment": Seed(
        lambda c: (
            {
                "participant_kind": "interpreter",
                "acknowledged_by_clinician_id": c.physician,
            },
        ),
        actor="physician",
    ),
    # Declined in the patient's own portal session.
    # The patient declines a newly published AI-assistance text they never
    # accepted, in their own portal session.
    "RefusalRecord": Seed(
        lambda c: (
            {
                "patient_session_id": c.patient_session,
                "text_id": c.ref("consent.ConsentText"),
            },
        ),
        actor="patient",
        settings=(("app.current_patient_session", "patient_session_id"),),
        support=(
            (
                "consent.ConsentText",
                Seed(
                    _rows(
                        {
                            "purpose": "ai_assistance",
                            "version": 1,
                            "text": "Termo sintetico de assistencia por IA.",
                            "digest": DIGEST,
                        }
                    )
                ),
            ),
        ),
    ),
}

EHR: Final[dict[str, Seed]] = {
    "Episode": Seed(_rows({"title": "Synthetic follow-up episode", "state": "open"})),
    "EpisodeEncounter": Seed(),
    # An amendment draft of the witness's finalized SOAP (the only draft the
    # product allows once a version is final), edited by its author; later
    # copies, receipts and AI drafts target it.
    "DraftEditState": Seed(
        lambda c: (
            {
                "author_id": c.physician,
                "section_edit_epochs": {
                    "assessment": 0,
                    "objective": 0,
                    "plan": 0,
                    "subjective": 0,
                },
            },
        ),
        actor="physician",
        support=(
            (
                "ehr.ClinicalDocumentVersion",
                Seed(
                    lambda c: (
                        {
                            "document_id": _finalized(c, "document_id"),
                            "template_id": _finalized(c, "template_id"),
                            "author_id": c.physician,
                            "version": 3,
                            "state": "draft",
                            "revision": 1,
                            "content_digest": "",
                            "content": _finalized(c, "content"),
                            "content_sha256": _finalized(c, "content_sha256"),
                            "amendment_of_id": _finalized(c, "id"),
                            "amendment_reason": "Synthetic amendment draft.",
                        },
                    ),
                    actor="physician",
                ),
            ),
        ),
    ),
    "DraftSaveReceipt": Seed(
        _rows({"request_sha256": DIGEST, "base_revision": 0, "revision": 1}),
        actor="physician",
    ),
    # A colleague's addendum to the still-open second visit.
    "EncounterAddendum": Seed(
        lambda c: (
            {
                "encounter_id": c.open_encounter,
                "author_id": c.owner,
                "text": None,
            },
        )
    ),
    # The author saves the draft's text (revision 1 -> 2); the save's receipt
    # acknowledges the committed revision.
    "AddendumSaveReceipt": Seed(
        _rows({"request_sha256": DIGEST, "base_revision": 1, "revision": 2}),
        prepare=(
            (
                "ehr.EncounterAddendum",
                _move(
                    lambda c: c.ref("ehr.EncounterAddendum"),
                    text=ADDENDUM_TEXT,
                    text_sha256=sha256(ADDENDUM_TEXT.encode()).hexdigest(),
                    revision=F("revision") + 1,
                ),
            ),
        ),
    ),
    # Patient-reported with unknown status, and an observed statement the
    # clinician then confirms through the reconciliation transition.
    "MedicationStatement": Seed(
        lambda c: (
            {
                "origin": "reported_by_patient",
                "provenance": "reported_by_patient",
                "status": "unknown",
                "performer_id": c.physician,
                "details": {"name": "Synthetic medication", "dose": "unknown"},
            },
            {
                "origin": "observed",
                "provenance": "observed",
                "status": "active",
                "performer_id": c.physician,
                "details": {"name": "Synthetic medication", "dose": "1 unit"},
            },
        ),
        actor="physician",
        updates=(
            (
                "ehr.MedicationStatement",
                lambda c: (
                    c.ref("ehr.MedicationStatement", 1),
                    {
                        "provenance": "clinician_confirmed",
                        "reconciled_by_id": c.physician,
                        "reconciled_at": c.now,
                        "revision": F("revision") + 1,
                    },
                ),
            ),
        ),
    ),
    "Observation": Seed(
        lambda c: (
            {
                "origin": "observed",
                "provenance": "observed",
                "kind": "heart_rate",
                "unit": "/min",
                "value": {"value": 72},
                "performer_id": c.physician,
            },
        )
    ),
    "RestrictedNote": Seed(lambda c: ({"restricted_by_id": c.physician},)),
    # The restricting physician shares the note with a physician colleague.
    "NoteAccessGrant": Seed(
        lambda c: ({"granted_by_id": c.physician, "grantee_id": c.key("colleague")},),
        actor="physician",
        support=(
            ("identity.User", Seed(_reviewer("colleague"))),
            ("identity.UserClinicRole", Seed(_reviewer_role("colleague"))),
        ),
    ),
    "CopiedNoteBlock": Seed(
        lambda c: (
            {
                "section": "plan",
                "inserted_revision": 1,
                "source_id": c.ref("ehr.ClinicalDocumentVersion", state="finalized"),
                "target_id": c.ref("ehr.ClinicalDocumentVersion"),
            },
        )
    ),
    "TimelineProjection": Seed(),
    "TimelineRefresh": Seed(adopt="0014 backfills one refresh row per patient"),
    # The physician installs the general pack: its published template and
    # questionnaire (keyed pack_general_v1) are written first.
    # The physician installs the general and pediatrics packs; each one's
    # published template and questionnaire (pack_<key>_v1) come first.
    "SpecialtyPack": Seed(
        lambda c: tuple(
            {
                "key": key,
                "version": 1,
                "license_ref": "docs/clinical/specialty-packs.md",
                "calculators": calculators,
                "clinical_owner_id": c.physician,
                "template_id": c.key(f"pack-template-{key}"),
                "questionnaire_id": c.key(f"pack-questionnaire-{key}"),
            }
            for key, calculators in PACKS
        ),
        actor="physician",
        support=tuple(
            support for key, _calculators in PACKS for support in _pack_sources(key)
        ),
    ),
    # An immunization recorded on the open visit under the pediatrics pack.
    "SpecialtyEntry": Seed(
        lambda c: (
            {
                "pack_id": c.ref("ehr.SpecialtyPack", 1),
                "encounter_id": c.open_encounter,
                "kind": "immunization",
                "details": {"vaccine": "Synthetic vaccine", "dose": "1"},
                "images": [],
                "recorded_by_id": c.physician,
            },
        ),
        actor="physician",
    ),
}

PRESCRIPTION: Final[dict[str, Seed]] = {
    # The physician opens a certificate draft on the open visit and saves it
    # once: the draft advances to version 2 and that version's text lands.
    "DocumentDraftContent": Seed(
        lambda c: (
            {
                "draft_id": c.key("certificate-draft"),
                "version": 2,
                "content": {"body": "Synthetic certificate.", "recipient": ""},
            },
        ),
        actor="physician",
        support=(
            (
                "prescription.PrescriptionDraft",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("certificate-draft"),
                            "encounter_id": c.open_encounter,
                            "patient_id": c.value(
                                "ehr.Encounter", "patient_id", id=c.open_encounter
                            ),
                            "issuer_id": c.physician,
                            "kind": "certificate",
                            "category": "synthetic_non_controlled",
                            "contract_version": "synthetic-draft-v1",
                            "state": "draft",
                            "version": 1,
                        },
                    ),
                    actor="physician",
                    updates=(
                        (
                            "prescription.PrescriptionDraft",
                            lambda c: (c.key("certificate-draft"), {"version": 2}),
                        ),
                    ),
                ),
            ),
        ),
    ),
    "MedicationCatalogImport": Seed(
        _rows({"source_url": "https://example.invalid/catalog.csv", "checksum": DIGEST})
    ),
    "MedicationCatalogEntry": Seed(
        _rows(
            {
                "code": "SYN0001",
                "description": "Synthetic medication",
                "strength_form": "10 mg tablet",
            }
        )
    ),
    "MedicationFavorite": Seed(lambda c: ({"issuer_id": c.physician},)),
    "PrescriptionClassPolicy": Seed(adopt="0013 loads the closed class catalog"),
    # The issuer records issuing the open visit's signed, unrevoked document.
    "PrescriptionIssuance": Seed(
        lambda c: (
            {
                "issuer_id": c.physician,
                "document_id": c.signed_document,
                "signature_id": c.signed_operation,
                "state": "rehearsal_complete",
            },
        ),
        actor="physician",
    ),
    "SncrNumberAllocation": Seed(
        lambda c: (
            {
                "category": "notificacao_b",
                "request_digest": DIGEST,
                "recording": {"number": "SYNTHETIC-0001"},
                "issuer_id": c.physician,
            },
        )
    ),
}

RETENTION: Final[dict[str, Seed]] = {
    # Scheduled by the clinic's retention service principal (its own
    # session, no staff user), for the brief input artifact.
    # The owner approves a zero-day AI-artifact policy and turns the dry run
    # off; the retention worker, logged in as its own principal, then runs the
    # schedule resolver, which writes a purge for each due artifact.
    "ArtifactPurge": Seed(
        adopt="the retention schedule resolver writes the purges",
        session="agent",
        support=(
            (
                "identity.ServicePrincipal",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("retention-principal"),
                            "name": "synthetic-retention",
                            # The retention worker's own login.
                            "db_identity": "clinic_agent",
                            "purpose": "retention",
                            "grant_set_version": 3,
                        },
                    )
                ),
            ),
            (
                "identity.ServicePrincipalGrant",
                Seed(
                    lambda c: (
                        {
                            "principal_id": c.key("retention-principal"),
                            "permission": "retention.execute",
                            "grant_set_version": 3,
                        },
                    )
                ),
            ),
            (
                "retention.RetentionPolicy",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("artifact-policy"),
                            "record_class": "ai_artifact",
                            "version": int(
                                str(
                                    c.value(
                                        "retention.RetentionPolicy",
                                        "max:version",
                                        record_class="ai_artifact",
                                    )
                                )
                            )
                            + 1,
                            "retention_days": 0,
                            "state": "proposed",
                            "proposed_by_id": c.owner,
                        },
                    ),
                    updates=(
                        (
                            "retention.RetentionPolicy",
                            lambda c: (
                                c.key("artifact-policy"),
                                {
                                    "state": "approved",
                                    "approved_by_id": c.owner,
                                    "approved_at": c.now,
                                },
                            ),
                        ),
                    ),
                ),
            ),
            (
                "identity.ClinicConfiguration",
                Seed(lambda c: _ai_configuration(c, retention_dry_run=False)),
            ),
        ),
        calls=(
            lambda c: c.call(
                "retention_schedule",
                c.clinic,
                "ai_artifact",
                actor="agent",
                settings={"app.current_principal": str(c.key("retention-principal"))},
            ),
        ),
    ),
}

WORKFLOWS: Final[dict[str, Seed]] = {
    # Two open tasks; the second is then assigned through the live
    # transition (revision 2, a fresh command key), as the service does.
    "Task": Seed(
        lambda c: tuple(
            {
                "kind": "follow_up",
                "subject_ref": {"kind": "enrollment", "id": str(c.enrollment)},
                "due_at": c.future,
                "fingerprint": DIGEST,
            }
            for _ in range(2)
        ),
        # The task bundle (workflow bundle 2) is opt-in per clinic role.
        prepare=tuple(
            ("identity.UserClinicRole", _bundle_two(who))
            for who in ("owner", "physician")
        ),
        updates=(
            (
                "workflows.Task",
                _command_step(
                    "workflows.Task",
                    1,
                    lambda c: {"state": "assigned", "owner_user_id": c.physician},
                ),
            ),
        ),
    ),
    "TaskComment": Seed(
        lambda c: ({"body": "Synthetic task comment.", "author_id": c.owner},)
    ),
    "WorkflowDefinitionVersion": Seed(
        after=("workflows.Task",),
        rows=_rows(
            {
                "key": "synthetic-follow-up",
                "version": 1,
                "steps": [{"handler": "timer", "seconds": 3600}],
                "fingerprint": DIGEST,
                "creation_fingerprint": DIGEST,
            }
        ),
        # Draft, validated, then published by the clinic manager.
        updates=(
            (
                "workflows.WorkflowDefinitionVersion",
                _command_step(
                    "workflows.WorkflowDefinitionVersion",
                    0,
                    lambda c: {"state": "validated", "validated_at": c.now},
                ),
            ),
            (
                "workflows.WorkflowDefinitionVersion",
                _command_step(
                    "workflows.WorkflowDefinitionVersion",
                    0,
                    lambda c: {
                        "state": "published",
                        "published_by_id": c.owner,
                        "published_at": c.now,
                    },
                ),
            ),
        ),
    ),
    "WorkflowRun": Seed(
        lambda c: (
            {
                "context_refs": [{"kind": "enrollment", "id": str(c.enrollment)}],
                "fingerprint": DIGEST,
                "started_by_id": c.owner,
            },
        ),
        after=("workflows.WorkflowDefinitionVersion",),
    ),
    "WorkflowStep": Seed(_rows({"position": 0})),
    "CarePlan": Seed(
        lambda c: (
            {
                "clinician_id": c.physician,
                "encounter_id": _final_encounter(c),
                "source_version_id": c.ref(
                    "ehr.ClinicalDocumentVersion", state="finalized"
                ),
                "fingerprint": DIGEST,
            },
        ),
        actor="physician",
    ),
    # A clinician-approved referral action on the plan, with the follow-up
    # task it owns (same idempotency key, due at the window's end).
    "CareAction": Seed(
        lambda c: (
            {
                "id": c.key("care-action"),
                "kind": "referral",
                "instruction": None,
                "window_start": c.now,
                "window_end": c.future,
                "origin_version_id": _finalized(c, "id"),
                "created_by_id": c.physician,
                "approved_by_id": c.physician,
                "task_id": c.key("care-action-task"),
                "fingerprint": DIGEST,
            },
            # A patient instruction carries its text and no staff task.
            {
                "kind": "patient_instruction",
                "instruction": "Synthetic patient instruction.",
                "window_start": c.now,
                "window_end": c.future,
                "origin_version_id": _finalized(c, "id"),
                "created_by_id": c.physician,
                "approved_by_id": c.physician,
                "task_id": None,
                "fingerprint": DIGEST,
            },
        ),
        actor="physician",
        support=(
            (
                "workflows.Task",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("care-action-task"),
                            "idempotency_key": c.key("care-action"),
                            "kind": "follow_up",
                            "subject_ref": {
                                "kind": "enrollment",
                                "id": str(c.enrollment),
                            },
                            "due_at": c.future,
                            "created_by_id": c.physician,
                            "fingerprint": DIGEST,
                        },
                    ),
                    actor="physician",
                ),
            ),
        ),
    ),
    "CareRefusal": Seed(
        lambda c: (
            {
                "kind": "recall",
                "record_id": c.ref("workflows.Task"),
                "refused_at": c.now,
                "retry_at": c.future,
            },
        )
    ),
    "PROAssignment": Seed(
        lambda c: (
            {
                "instrument_key": "phq9",
                "instrument_version": 1,
                "interval_days": 30,
                "occurrences": 2,
                "response_role": "nurse",
                "staffing_policy_ref": "synthetic-policy",
                "fingerprint": DIGEST,
                "created_by_id": c.physician,
                "state": "active",
            },
        ),
        actor="physician",
    ),
    "PROOccurrence": Seed(
        lambda c: (
            {
                "assignment_id": c.ref("workflows.PROAssignment"),
                "sequence": 1,
                "answers": None,
                "due_at": c.value(
                    "workflows.PROAssignment",
                    "first_due_at",
                    id=c.ref("workflows.PROAssignment"),
                ),
                "created_by_id": c.physician,
            },
        ),
        actor="physician",
        # The physician records the patient's response; the AFTER trigger
        # opens its review task under the response task id.
        updates=(
            (
                "workflows.PROOccurrence",
                lambda c: (
                    c.ref("workflows.PROOccurrence"),
                    {
                        "state": "responded",
                        "answers": {"q1": 0},
                        "response_task_id": c.key("pro-response-task"),
                        "recorded_at": TRANSACTION_TIME,
                        "revision": F("revision") + 1,
                        "last_command_key": c.key("pro-response-command"),
                        "last_command_digest": DIGEST,
                    },
                ),
            ),
        ),
    ),
    # Scheduled now for a year out, as the recall service anchors it.
    "Recall": Seed(
        lambda c: (
            {
                "reason": "routine_return",
                "interval_days": 365,
                "owner_role": "receptionist",
                "anchor_at": TRANSACTION_TIME,
                "due_at": RawSQL("transaction_timestamp() + interval '365 days'", ()),
                "state": "scheduled",
                "created_by_id": c.owner,
                "fingerprint": DIGEST,
            },
        )
    ),
    "Referral": Seed(
        lambda c: (
            {
                "plan_id": c.ref("workflows.CarePlan"),
                "action_id": c.key("care-action"),
                "status": "requested",
                "target_kind": "specialty",
                "target_label": "Synthetic specialist",
                "created_by_id": c.physician,
            },
        ),
        actor="physician",
        after=("workflows.CareAction",),
    ),
    "ReleaseNoticeTemplate": Seed(
        lambda c: (
            {
                "version": 1,
                "body": "Seu resultado sintetico esta disponivel.",
                "approved_by_id": c.physician,
            },
        ),
        actor="physician",
    ),
    "UrgentCoveragePolicy": Seed(
        lambda c: (
            {
                "version": 1,
                "weekdays": [1, 2, 3, 4, 5],
                "opens_at": time(8, 0),
                "closes_at": time(18, 0),
                "response_hours": 4,
                "responsible_roles": ["physician"],
                "created_by_id": c.owner,
            },
        ),
        # Coverage is clinic configuration: the owner's clinic-admin role.
        support=(("identity.UserClinicRole", Seed(_owner_role("clinic_admin"))),),
    ),
    # Raised by the patient bridge from the inbound reply (no staff user).
    "UrgentConcern": Seed(
        lambda c: (
            {
                "message_id": c.ref("comms.Message", 0),
                "conversation_id": c.ref("comms.Conversation", 1),
                "state": "open",
            },
        ),
        actor="service",
        settings=(("app.workflows_urgent_bridge", "message_id"),),
    ),
    "ReleaseDelivery": Seed(
        lambda c: (
            {
                "audience": "patient",
                "release_id": c.ref("orders.ResultRelease"),
                "version_id": c.ref("orders.ResultVersion", 0),
                "message_id": c.ref("comms.Message", 3),
                "released_by_id": c.physician,
            },
        ),
        actor="physician",
    ),
}

COMMS: Final[dict[str, Seed]] = {
    # The clinic's WhatsApp account, bound to its messaging-ingest principal.
    "ProviderAccount": Seed(
        lambda c: (
            {
                "channel": "whatsapp",
                "provider": "whatsapp-cloud-v1",
                "external_account_id": "synthetic-account",
                "signing_key_digest": sha256(INBOUND_PROOF.encode()).hexdigest(),
                "principal_id": c.key("messaging-principal"),
            },
        ),
        support=(
            (
                "identity.ServicePrincipal",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("messaging-principal"),
                            "name": "synthetic-messaging",
                            "db_identity": "clinic_agent_messaging",
                            "purpose": "messaging_ingest",
                        },
                    )
                ),
            ),
        ),
    ),
    # A portal care thread and a separate WhatsApp marketing thread.
    "Conversation": Seed(
        lambda c: (
            {
                "channel": "portal",
                "purpose": "care",
                "enrollment_id": c.enrollment,
                "contact_fingerprint": DIGEST,
                "contact_address": "synthetic.invalid",
                "last_activity_at": c.now,
            },
            {
                "channel": "whatsapp",
                "purpose": "marketing",
                "enrollment_id": c.enrollment,
                "provider_account_id": c.ref("comms.ProviderAccount"),
                "contact_fingerprint": DIGEST,
                "contact_address": "+55 11 0000-0000",
                "contact_label": "Synthetic contact",
                "last_activity_at": c.now,
            },
        ),
        after=("comms.ProviderAccount",),
    ),
    "Participant": Seed(
        lambda c: ({"role": "patient", "enrollment_id": c.enrollment},)
    ),
    "Message": Seed(
        lambda c: (
            # The patient's reply arrives through the provider webhook: the
            # ingest principal, no staff user, the provider's proof.
            {
                "direction": "in",
                "conversation_id": c.ref("comms.Conversation", 1),
                "body": "Synthetic inbound.",
                "event_id": "evt-1",
                "template_parameters": None,
                "_actor": "service",
                "_settings": _ingest(c),
            },
            {
                "direction": "out",
                "conversation_id": c.ref("comms.Conversation", 0),
                "body": "Synthetic reply.",
                "command_id": uuid4(),
                "sent_by_id": c.owner,
                "template_parameters": None,
            },
            {
                "direction": "out",
                "conversation_id": c.ref("comms.Conversation", 1),
                "body": MARKETING_BODY,
                "command_id": uuid4(),
                "sent_by_id": c.owner,
                "template_id": c.ref("comms.TemplateVersion", 1),
                "template_parameters": "{}",
            },
            # The physician's portal reply that later carries a result notice.
            {
                "direction": "out",
                "conversation_id": c.ref("comms.Conversation", 0),
                "body": "Synthetic portal reply.",
                "command_id": uuid4(),
                "sent_by_id": c.physician,
                "template_parameters": None,
                "_actor": "physician",
            },
        )
    ),
    "InboundEvent": Seed(_rows({"event_id": "evt-1", "kind": "message"})),
    "InternalNote": Seed(_rows({"body": "Synthetic internal note."})),
    "MessageMeter": Seed(_rows({"source": "provider", "category": "service"})),
    "QuickReply": Seed(
        _rows({"title": "Synthetic", "body": "Synthetic reply.", "purpose": "care"})
    ),
    "SuggestedReply": Seed(_rows({"body": "Synthetic suggestion.", "drafter": "rule"})),
    "TemplateVersion": Seed(
        _rows(
            {
                "channel": "whatsapp",
                "name": "synthetic_reminder",
                "version": 1,
                "category": "utility",
                "purpose": "transactional",
                "body": "Lembrete sintetico.",
                "content_digest": DIGEST,
            },
            {
                "channel": "whatsapp",
                "name": "synthetic_marketing",
                "version": 1,
                "category": "marketing",
                "purpose": "marketing",
                "body": MARKETING_BODY,
                "parameter_count": 0,
                "approval_status": "approved",
                "content_digest": DIGEST,
            },
        )
    ),
}

AI: Final[dict[str, Seed]] = {
    "GatewaySwitch": Seed(
        _rows({"id": 1, "enabled": True, "approved_regions": ["br-synthetic"]})
    ),
    # Draft brief and analyst configurations (the only legal initial state).
    "ModelConfiguration": Seed(
        lambda _c: tuple(
            _model_row(purpose, model, capability)
            for purpose, model, capability in (
                ("brief", "upgrade-synthetic", "llm_inference"),
                ("scribe_draft", "upgrade-synthetic-draft", "llm_inference"),
                ("extraction", "upgrade-synthetic-extract", "llm_inference"),
                ("scribe_asr", "upgrade-synthetic-asr", "asr"),
                ("analyst", "upgrade-synthetic-analyst", "llm_inference"),
            )
        ),
        updates=tuple(
            ("ai.ModelConfiguration", _model_step(index, changes))
            for index in range(5)
            for changes in MODEL_APPROVAL
        ),
    ),
    # Opened by the physician after their call timed out at the provider.
    # Opened by the physician after their call timed out at the provider;
    # meanwhile the owner's analyst call writes its output and settles.
    "AICircuit": Seed(
        lambda c: (
            {"configuration_id": c.ref("ai.ModelConfiguration", purpose="brief")},
        ),
        actor="physician",
        after=("ai.AIInvocation",),
        support=(
            (
                "ai.AIArtifact",
                Seed(
                    lambda c: (
                        {
                            "purpose": "analyst",
                            "kind": "output",
                            "method": "complete",
                            "invocation_id": c.ref("ai.AIInvocation", 1),
                            "content": "Synthetic analyst answer.",
                            "retention_class": "review_draft",
                            "expires_at": c.future,
                            "user_id": c.owner,
                        },
                    )
                ),
            ),
        ),
        updates=(
            (
                "ai.AIInvocation",
                lambda c: (
                    c.ref("ai.AIInvocation", 1),
                    {
                        "status": "succeeded",
                        "tokens_in": 4,
                        "tokens_out": 3,
                        "cost_micros": 7,
                        "cost_minor": 1,
                        "settled_at": c.now,
                    },
                ),
            ),
        ),
    ),
    "AIArtifact": Seed(
        lambda c: (
            {
                "purpose": "brief",
                "kind": "input",
                "method": "complete",
                "content": "Synthetic upgrade input; no clinical data.",
                "retention_class": "transient_input",
                "patient_enrollment_id": c.enrollment,
                "expires_at": c.future,
                "user_id": c.physician,
                "_actor": "physician",
            },
            {
                "purpose": "analyst",
                "kind": "input",
                "method": "complete",
                "content": "Synthetic analyst question; no clinical data.",
                "retention_class": "transient_input",
                "expires_at": c.future,
                "user_id": c.owner,
            },
        )
    ),
    # A reserved brief call and a settled analyst call.
    # Each call is reserved by its own caller, then settled: the physician's
    # brief times out at the provider, the owner's analyst call succeeds.
    "AIInvocation": Seed(
        lambda c: (
            {
                "purpose": "brief",
                "user_id": c.physician,
                "_actor": "physician",
                "inputs_ref_id": c.ref("ai.AIArtifact", 0),
                "configuration_id": c.ref("ai.ModelConfiguration", purpose="brief"),
                **INVOCATION_LIMITS,
            },
            {
                "purpose": "analyst",
                "user_id": c.owner,
                "_actor": "owner",
                "inputs_ref_id": c.ref("ai.AIArtifact", 1),
                "configuration_id": c.ref("ai.ModelConfiguration", purpose="analyst"),
                **INVOCATION_LIMITS,
            },
        ),
        actor="physician",
        # The owner publishes the next clinic configuration version, which
        # enables AI under the policy above (published versions are immutable).
        support=(("identity.ClinicConfiguration", Seed(_ai_configuration)),),
        updates=(
            (
                "ai.AIInvocation",
                lambda c: (
                    c.ref("ai.AIInvocation", 0),
                    {
                        "status": "failed",
                        "reason_code": "provider_timeout",
                        "cost_micros": 30,
                        "cost_minor": 1,
                        "settled_at": c.now,
                    },
                ),
            ),
        ),
    ),
    "AgentPolicyVersion": Seed(
        _rows(
            {
                "capability": "front_desk",
                "version": 1,
                "autonomy_ceiling": "A1",
                "tool_allowlist": [],
                "budget_actions": 1,
                "budget_micros": 100,
                "timeout_seconds": 30,
                "escalation_queue": "administrative",
                "compensation_policy": {"mode": "manual", "version": 1},
                "enabled": True,
            }
        )
    ),
    "AgentRun": Seed(
        _rows(
            {
                "prompt_sha256": DIGEST,
                "contract": {"synthetic": True},
                "evaluation_set": "synthetic-upgrade",
                "outcome": "proposal",
                "reason_code": "",
                "reviewer_edits": {"synthetic": True},
            }
        )
    ),
    "ActionProposal": Seed(
        _rows(
            {
                "payload": '{"synthetic":true}',
                "digest": DIGEST,
                "state": "proposal",
                "reason_code": "",
            }
        ),
        updates=(
            (
                "ai.ActionProposal",
                lambda c: (c.ref("ai.ActionProposal"), {"state": "validated"}),
            ),
        ),
    ),
    # The owner approves the validated proposal over the live authority
    # snapshot; the proposal then becomes approved.
    "Approval": Seed(
        lambda c: (
            {
                "approver_id": c.owner,
                "digest": DIGEST,
                "key_id": "agent-attestation-v1",
                "authority_snapshot": c.call(
                    "agent_snapshot", c.clinic, "automation.organization", actor="owner"
                ),
                "step_up_evidence": {"synthetic": True},
                "attestation": DIGEST,
            },
        ),
        updates=(
            (
                "ai.ActionProposal",
                lambda c: (c.ref("ai.ActionProposal"), {"state": "approved"}),
            ),
        ),
    ),
    # Executed once (fencing 1), then the provider call fails.
    "ActionExecution": Seed(
        lambda c: (
            {
                "digest": DIGEST,
                "fencing_token": 1,
                "state": "executing",
                "reserved_micros": 1,
                "intent_at": c.now,
                "deadline": c.future,
                "reason_code": "",
            },
        ),
        after=("ai.Approval",),
        updates=(
            (
                "ai.ActionExecution",
                lambda c: (
                    c.ref("ai.ActionExecution"),
                    {"state": "failure", "reason_code": "provider_unavailable"},
                ),
            ),
        ),
    ),
    "ActionReceipt": Seed(
        _rows(
            {
                "kind": "reconciliation",
                "outcome": "unknown",
                "digest": DIGEST,
                "fencing_token": 1,
                "attestation": DIGEST,
            }
        )
    ),
    "Compensation": Seed(_rows({"reason_code": "synthetic"})),
    # The physician extracts the witness attachment through a reserved call.
    "ExtractionJob": Seed(
        lambda c: (
            {
                "user_id": c.physician,
                "invocation_id": c.key("extraction-call"),
                "attachment_id": c.ref("ehr.ClinicalAttachment"),
                "sha256": c.value("ehr.ClinicalAttachment", "sha256"),
                "rotations": [0],
            },
        ),
        actor="physician",
        support=_ai_call("extraction", "extraction-call", "extract"),
    ),
    # The extraction call returns its output and settles; the physician's job
    # then holds one candidate, expiring with that output.
    "ExtractionCandidateRecord": Seed(
        lambda c: (
            {
                "job_id": c.ref("ai.ExtractionJob"),
                "position": 0,
                "payload": {
                    "field": "synthetic",
                    "value": "Synthetic candidate value.",
                },
                "status": "candidate",
                "expires_at": c.value(
                    "ai.AIArtifact",
                    "expires_at",
                    invocation_id=c.key("extraction-call"),
                    kind="output",
                ),
            },
        ),
        actor="physician",
        support=(_ai_output("extraction", "extraction-call", "extract"),),
    ),
    "EvaluationProtocol": Seed(
        lambda c: (_protocol_row(c, "AI-02"),),
        # A clinical owner is both owner and physician of the clinic.
        support=(("identity.UserClinicRole", Seed(_owner_role("physician"))),),
    ),
    # Three distinct physician reviewers; the creator rates nothing.
    "EvaluationRun": Seed(
        lambda c: (
            {
                "configuration_version": 1,
                "versions": {
                    # The configuration predates prompt/retrieval/schema
                    # digests; the run binds to their empty values.
                    "prompt": "",
                    "retrieval": "",
                    "schema": "",
                    "model": DIGEST,
                    "version": "1",
                },
                "rater_one_id": c.key("colleague"),
                "rater_two_id": c.key("rater-two"),
                "adjudicator_id": c.key("adjudicator"),
                "created_by_id": c.owner,
                "status": "pending",
            },
        ),
        support=tuple(
            support
            for name in ("rater-two", "adjudicator")
            for support in (
                ("identity.User", Seed(_reviewer(name))),
                ("identity.UserClinicRole", Seed(_reviewer_role(name))),
            )
        ),
        # The clinical owner records the automatic scoring (the guard stamps
        # scored_at); the run stays pending for its raters.
        updates=(
            ("ai.EvaluationRun", lambda c: (c.ref("ai.EvaluationRun"), _run_scores())),
        ),
    ),
    # The first assigned rater scores one registered case: every label clean.
    "EvaluationRating": Seed(
        lambda c: (
            {
                "case_id": "synthetic-case-1",
                "slot": "rater_one",
                "user_id": c.key("colleague"),
                "_actor": "key:colleague",
                "labels": {**dict.fromkeys(RATING_LABELS, 0), "correction_ms": 0},
            },
        )
    ),
    "ModelChangeRequest": Seed(
        lambda c: (
            {"status": "requested", "approval_ref": "", "requested_by_id": c.owner},
        )
    ),
    # A patient writes on a WhatsApp care thread; the webhook's ingest
    # principal enqueues it, and the bridge writes the thread and its turn
    # under the clinic's enabled A2 front-desk policy (owner-authored, bound to
    # an agent principal holding agent.execute) and the organization opt-in.
    "FrontDeskThread": Seed(
        adopt="the enqueue bridge writes the thread",
        support=(
            (
                "identity.ServicePrincipal",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("front-desk-principal"),
                            "name": "synthetic-front-desk",
                            "db_identity": "clinic_agent_front_desk",
                            "purpose": "front_desk",
                            "grant_set_version": 2,
                        },
                    )
                ),
            ),
            (
                "identity.ServicePrincipalGrant",
                Seed(
                    lambda c: (
                        {
                            "principal_id": c.key("front-desk-principal"),
                            "permission": "agent.execute",
                            "subject_scope": "clinic",
                            "grant_set_version": 2,
                        },
                    )
                ),
            ),
            (
                "ai.AgentPolicyVersion",
                Seed(
                    lambda c: (
                        {
                            "capability": "front_desk",
                            "version": 2,
                            "autonomy_ceiling": "A2",
                            "tool_allowlist": {},
                            "budget_actions": 5,
                            "budget_micros": 100,
                            "timeout_seconds": 30,
                            "escalation_queue": "administrative",
                            "compensation_policy": {"mode": "manual", "version": 1},
                            "enabled": True,
                            "principal_id": c.key("front-desk-principal"),
                            "created_by_id": c.owner,
                        },
                    )
                ),
            ),
            (
                "identity.ClinicConfiguration",
                Seed(lambda c: _ai_configuration(c, "front_desk")),
            ),
            (
                "comms.Conversation",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("front-desk-conversation"),
                            "channel": "whatsapp",
                            "purpose": "care",
                            "enrollment_id": c.enrollment,
                            "provider_account_id": c.ref("comms.ProviderAccount"),
                            "contact_fingerprint": DIGEST,
                            "contact_address": "+55 11 0000-0001",
                            "last_activity_at": c.now,
                        },
                    )
                ),
            ),
            (
                "comms.Message",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("front-desk-inbound"),
                            "direction": "in",
                            "conversation_id": c.key("front-desk-conversation"),
                            "body": "Synthetic booking question.",
                            "event_id": "evt-front-desk",
                            "template_parameters": None,
                            "created_at": TRANSACTION_TIME,
                            "_actor": "service",
                            "_settings": _ingest(c),
                        },
                    )
                ),
            ),
        ),
        calls=(
            lambda c: c.call(
                "ai_front_desk_enqueue",
                c.key("front-desk-inbound"),
                actor="service",
                settings=_ingest(c),
            ),
        ),
    ),
    "KnowledgeArticle": Seed(
        _rows(
            {
                "key": "synthetic",
                "version": 1,
                "topic": "hours",
                "title": "Synthetic",
                "body": "Synthetic hours.",
                "keywords": ["synthetic"],
            }
        )
    ),
    "FrontDeskTurn": Seed(adopt="the enqueue bridge writes the thread's first turn"),
    # The owner opts the clinic into care coordination with an approved,
    # active configuration; the physician's settled call over the current care
    # plan then proposes one clinical order.
    "CareProposal": Seed(
        lambda c: (
            {
                "invocation_id": c.key("care-call"),
                "plan_id": c.ref("workflows.CarePlan"),
                "enrollment_id": c.enrollment,
                "source_version_id": c.value(
                    "workflows.CarePlan",
                    "source_version_id",
                    id=c.ref("workflows.CarePlan"),
                ),
                "plan_revision": c.value(
                    "workflows.CarePlan", "revision", id=c.ref("workflows.CarePlan")
                ),
                "position": 0,
                "kind": "order",
                "audience": "clinical",
                "state": "pending",
                "revision": 1,
                "window_start": c.now,
                "window_end": c.future,
                "requested_by_id": c.physician,
            },
        ),
        actor="physician",
        support=(
            (
                "identity.ClinicConfiguration",
                Seed(lambda c: _ai_configuration(c, "care_coordination")),
            ),
            *_care_configuration(),
            *_ai_call("care_coordination", "care-call", "complete"),
            _ai_output("care_coordination", "care-call", "complete"),
        ),
    ),
}

SCRIBE: Final[dict[str, Seed]] = {
    # Created on the open visit, then authorized and started (epoch 1),
    # each step revisioned, under the physician's capture token.
    "RecordingSession": Seed(
        lambda c: (
            {
                "encounter_id": c.open_encounter,
                "consent_id": c.key("recording-acceptance"),
                "physician_id": c.physician,
                "token_digest": DIGEST,
                "capability_version_id": c.ref(
                    "providers.CapabilityVersion", **MEDIA_STORAGE
                ),
                "capability_snapshot": _media_snapshot(c),
                "state": "created",
                "epoch": 0,
            },
        ),
        actor="physician",
        settings=(("app.scribe_token", "token_digest"),),
        # Capture stores media only through the clinic's current registered
        # object-storage version, which the owner selects first.
        prepare=(
            (
                "providers.ProviderCapability",
                lambda c: (
                    c.ref(
                        "providers.ProviderCapability",
                        key="object_storage_media",
                        clinic_id=None,
                    ),
                    {
                        "current_version_id": c.ref(
                            "providers.CapabilityVersion", **MEDIA_STORAGE
                        )
                    },
                ),
            ),
        ),
        # The patient's own recording consent, accepted in their portal session.
        support=(
            (
                "consent.ConsentText",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("recording-text"),
                            "purpose": "consultation_recording",
                            "version": 1,
                            "text": "Termo sintetico de gravacao.",
                            "digest": DIGEST,
                        },
                    )
                ),
            ),
            (
                "consent.ConsentAcceptance",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("recording-acceptance"),
                            "text_id": c.key("recording-text"),
                            "patient_session_id": c.patient_session,
                        },
                    ),
                    actor="patient",
                    settings=(("app.current_patient_session", "patient_session_id"),),
                ),
            ),
        ),
        updates=(
            (
                "scribe.RecordingSession",
                _step("scribe.RecordingSession", 0, {"state": "authorized"}),
            ),
            (
                "scribe.RecordingSession",
                _step(
                    "scribe.RecordingSession",
                    0,
                    {"state": "recording", "epoch": F("epoch") + 1},
                ),
            ),
        ),
    ),
    "CaptureEpoch": Seed(_rows({"number": 1})),
    "RecordingCommand": Seed(
        _rows(
            {
                "action": "start",
                "payload_digest": DIGEST,
                "result_state": "recording",
                "result_epoch": 1,
            }
        )
    ),
    # Buffered, handed to the media store through its outbox operation, then
    # accepted (the sealed bytes leave the row; the wrapped key stays).
    "AudioChunk": Seed(
        lambda c: (
            {
                "id": c.key("chunk"),
                "track": "microphone",
                "seq": 0,
                "sha256": DIGEST,
                "duration_ms": 1000,
                "codec": "audio/webm;codecs=opus",
                "size_bytes": 1,
                "storage_key": "b" * 64,
                "wrapped_key": b"synthetic-wrapped-chunk-key",
                "encrypted_data": b"synthetic-sealed-audio",
                "state": "buffered",
            },
        ),
        support=(
            (
                "comms.IntegrationOperation",
                Seed(
                    lambda c: (
                        _operation(
                            c, "chunk-op", "scribe_media", "scribe.chunk", "chunk"
                        ),
                    ),
                    actor="physician",
                ),
            ),
        ),
        updates=(
            (
                "scribe.AudioChunk",
                lambda c: (c.key("chunk"), {"operation_id": c.key("chunk-op")}),
            ),
            (
                "scribe.AudioChunk",
                lambda c: (
                    c.key("chunk"),
                    {"state": "accepted", "encrypted_data": b""},
                ),
            ),
        ),
    ),
    # The accepted chunk goes to ASR: the patient's AI-assistance acceptance,
    # the physician's reserved ASR call and its gateway operation come first.
    "ChunkTranscription": Seed(
        lambda c: (
            {
                "chunk_id": c.key("chunk"),
                "consent_id": c.key("asr-acceptance"),
                "invocation_id": c.key("asr-invocation"),
            },
        ),
        support=(
            (
                "consent.ConsentAcceptance",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("asr-acceptance"),
                            "text_id": c.ref(
                                "consent.ConsentText", purpose="ai_assistance"
                            ),
                            "patient_session_id": c.patient_session,
                        },
                    ),
                    actor="patient",
                    settings=(("app.current_patient_session", "patient_session_id"),),
                ),
            ),
            (
                "ai.AIArtifact",
                Seed(
                    lambda c: (
                        {
                            "purpose": "scribe_asr",
                            "kind": "input",
                            "method": "transcribe",
                            "content": "Synthetic audio reference; no clinical data.",
                            "retention_class": "transient_input",
                            "patient_enrollment_id": c.enrollment,
                            "expires_at": c.future,
                            "user_id": c.physician,
                        },
                    ),
                    actor="physician",
                ),
            ),
            (
                "comms.IntegrationOperation",
                Seed(
                    lambda c: (
                        _operation(
                            c, "asr-op", "ai_gateway", "ai.invocation", "asr-invocation"
                        ),
                    ),
                    actor="physician",
                ),
            ),
            (
                "ai.AIInvocation",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("asr-invocation"),
                            "purpose": "scribe_asr",
                            "user_id": c.physician,
                            "inputs_ref_id": c.ref(
                                "ai.AIArtifact", purpose="scribe_asr"
                            ),
                            "configuration_id": c.ref(
                                "ai.ModelConfiguration", purpose="scribe_asr"
                            ),
                            **INVOCATION_LIMITS,
                        },
                    ),
                    actor="physician",
                    updates=(
                        (
                            "ai.AIInvocation",
                            lambda c: (
                                c.key("asr-invocation"),
                                {"operation_id": c.key("asr-op")},
                            ),
                        ),
                    ),
                ),
            ),
        ),
    ),
    "TranscriptVersion": Seed(
        lambda c: (
            {
                "revision": 1,
                "state": "partial",
                "mutation": "recognized",
                "coverage": {
                    "completed": [],
                    "sources": [str(c.key("segment-source"))],
                    "recording_closed": False,
                    "slots": c.call(
                        "scribe_transcript_coverage", c.ref("scribe.RecordingSession")
                    ),
                },
                "recognition_index": 0,
                "request_id": c.ref("scribe.ChunkTranscription"),
                "created_by_id": c.physician,
            },
        ),
        # The ASR call returns its output and settles before the recognized
        # version that cites it.
        support=(
            (
                "ai.AIArtifact",
                Seed(
                    lambda c: (
                        {
                            "purpose": "scribe_asr",
                            "kind": "output",
                            "method": "transcribe",
                            "invocation_id": c.key("asr-invocation"),
                            "content": "Synthetic recognized text; no clinical data.",
                            "retention_class": "review_draft",
                            "patient_enrollment_id": c.enrollment,
                            "expires_at": c.future,
                            "user_id": c.physician,
                        },
                    ),
                    actor="physician",
                    updates=(
                        (
                            "ai.AIInvocation",
                            lambda c: (
                                c.key("asr-invocation"),
                                {
                                    "status": "succeeded",
                                    "audio_seconds": 1,
                                    "cost_micros": 1,
                                    "cost_minor": 1,
                                    "settled_at": c.now,
                                },
                            ),
                        ),
                    ),
                ),
            ),
        ),
    ),
    "TranscriptSegment": Seed(
        lambda c: (
            {
                "version_id": c.ref("scribe.TranscriptVersion"),
                "chunk_id": c.key("chunk"),
                "source_id": c.key("segment-source"),
                "start_ms": 0,
                "end_ms": 1000,
                "speaker": "clinician",
                "text": "Synthetic transcript, no clinical content.",
                "uncertainty": ["synthetic"],
                "language": "pt-BR",
            },
        ),
        after=("scribe.TranscriptVersion",),
    ),
    "AIDraft": Seed(
        lambda c: (
            {
                "generation": 1,
                "base_content": {"synthetic": True},
                "sections": ["subjective"],
                # The draft cites the open visit itself, at its revision.
                "source_refs": [_visit_citation(c)],
                "requested_by_id": c.physician,
                "version_id": c.key("open-draft"),
                "encounter_id": c.open_encounter,
                "invocation_id": c.key("draft-call"),
                "transcript_id": c.key("final-transcript"),
                "consent_id": c.key("asr-acceptance"),
                "base_revision": 1,
                "base_epochs": {
                    "subjective": 0,
                    "objective": 0,
                    "assessment": 0,
                    "plan": 0,
                },
            },
        ),
        # Capture stops and its transcript is finalized before drafting.
        prepare=(
            (
                "scribe.RecordingSession",
                _step(
                    "scribe.RecordingSession",
                    0,
                    {"state": "stopped", "stopped_at": TRANSACTION_TIME},
                ),
            ),
        ),
        # The open visit's own SOAP draft the AI draft proposes into, the
        # final transcript and the physician's reserved drafting call.
        support=(
            (
                "scribe.TranscriptVersion",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("final-transcript"),
                            "predecessor_id": c.ref("scribe.TranscriptVersion"),
                            "revision": 2,
                            "state": "final",
                            "mutation": "finalized",
                            # The chunk's slots were never closed by capture;
                            # the physician acknowledges the gap, as the review
                            # screen requires.
                            "incomplete_acknowledged": True,
                            "coverage": {
                                "completed": [],
                                "sources": [str(c.key("segment-source"))],
                                "recording_closed": True,
                                "slots": c.call(
                                    "scribe_transcript_coverage",
                                    c.ref("scribe.RecordingSession"),
                                ),
                            },
                            "created_by_id": c.physician,
                        },
                    ),
                    actor="physician",
                ),
            ),
            *_ai_call("scribe_draft", "draft-call", "complete"),
            (
                "ehr.ClinicalDocument",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("open-document"),
                            "encounter_id": c.open_encounter,
                            "kind": "soap",
                        },
                    ),
                    actor="physician",
                ),
            ),
            (
                "ehr.ClinicalDocumentVersion",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("open-draft"),
                            "document_id": c.key("open-document"),
                            "template_id": _finalized(c, "template_id"),
                            "author_id": c.physician,
                            "version": 1,
                            "state": "draft",
                            "revision": 1,
                            "content_digest": "",
                            "content_sha256": "",
                        },
                    ),
                    actor="physician",
                ),
            ),
        ),
    ),
    "DraftSectionSuggestion": Seed(
        lambda c: (
            {
                "section": "subjective",
                "text": "Synthetic pending suggestion.",
                "citations": [_visit_citation(c)],
                "assertion_class": "reported",
                "version_id": c.key("open-draft"),
            },
        ),
        actor="physician",
        after=("scribe.DraftActionProposal",),
    ),
    # Once the draft is written the gateway returns its output and settles the
    # call; the draft's proposal and suggestion cite the open visit.
    "DraftActionProposal": Seed(
        lambda c: (
            {
                "ordinal": 0,
                "kind": "task",
                "text": "Synthetic separate proposal.",
                "citations": [_visit_citation(c)],
                "version_id": c.key("open-draft"),
            },
        ),
        actor="physician",
        support=(_ai_output("scribe_draft", "draft-call", "complete"),),
    ),
    # The note's author accepts the pending suggestion at the draft's current
    # revision and content, bound to the drafting call's configuration.
    "ReviewDecision": Seed(
        lambda c: (
            {
                "suggestion_id": c.ref("scribe.DraftSectionSuggestion"),
                "request_digest": DIGEST,
                "action": "accepted",
                "applied_text": {"subjective": "Synthetic accepted suggestion."},
                "section": "subjective",
                "expected_revision": c.value(
                    "ehr.ClinicalDocumentVersion", "revision", id=c.key("open-draft")
                ),
                "result_revision": c.value(
                    "ehr.ClinicalDocumentVersion", "revision", id=c.key("open-draft")
                ),
                "result_sha256": c.value(
                    "ehr.ClinicalDocumentVersion",
                    "content_sha256",
                    id=c.key("open-draft"),
                ),
                "capability_snapshot": {
                    "purpose": "scribe_draft",
                    "capability": c.value(
                        "ai.ModelConfiguration",
                        "capability_key",
                        purpose="scribe_draft",
                    ),
                    "configuration_id": str(
                        c.value(
                            "ai.AIInvocation",
                            "configuration_id",
                            id=c.key("draft-call"),
                        )
                    ),
                    "version": c.value(
                        "ai.AIInvocation", "config_version", id=c.key("draft-call")
                    ),
                },
                "reviewed_by_id": c.physician,
                "version_id": c.key("open-draft"),
            },
        ),
        actor="physician",
    ),
}

ORDERS: Final[dict[str, Seed]] = {
    # The synthetic lab's account, bound to its lab-ingest principal; the
    # migration owner provisions it, as the lab onboarding command does.
    "LabAccount": Seed(
        lambda c: (
            {
                "signing_key_digest": DIGEST,
                "provider": "synthetic-lab-v1",
                "principal_id": c.key("lab-principal"),
            },
        ),
        support=(
            (
                "identity.ServicePrincipal",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("lab-principal"),
                            "name": "synthetic-lab",
                            "db_identity": "clinic_agent_lab",
                            "purpose": "lab_ingest",
                        },
                    )
                ),
            ),
        ),
    ),
    "Order": Seed(
        lambda c: ({"ordered_by_id": c.physician, "fingerprint": DIGEST},),
        actor="physician",
    ),
    # A catalog item is added while the order is still unevented; its event
    # history then opens with draft and moves to placed.
    "OrderItem": Seed(_rows({"exam_code": "cbc", "catalog_version": 1})),
    "OrderEvent": Seed(
        lambda c: (
            {"sequence": 1, "state": "draft", "actor_id": c.physician},
            {"sequence": 2, "state": "placed", "actor_id": c.physician},
        ),
        after=("orders.OrderItem",),
    ),
    # Two uploaded results: the first is observed, matched to the order,
    # acknowledged and released to the patient; the second is quarantined
    # and rejected, and is never released.
    "Result": Seed(_rows({}, {})),
    "ResultVersion": Seed(
        lambda c: tuple(
            {
                "result_id": c.ref("orders.Result", index),
                "number": 1,
                "fingerprint": DIGEST,
                "identity": {"synthetic": True},
                "storage_key": ("c" if index == 0 else "d") * 64,
                # A staff upload: no lab principal, bound to the proposed order.
                "lab_account_id": None,
                "proposed_order_id": c.ref("orders.Order"),
                "sha256": DIGEST,
                "size_bytes": 1,
                "content_type": "application/pdf",
                "uploader_id": c.physician,
            }
            for index in (0, 1)
        )
    ),
    "ResultObservation": Seed(
        lambda c: (
            {
                "version_id": c.ref("orders.ResultVersion", 0),
                "position": 0,
                "source": {"analyte": "synthetic-hemoglobin", "value": "13.5 g/dL"},
            },
        )
    ),
    "ResultMatch": Seed(
        lambda c: (
            {
                "version_id": c.ref("orders.ResultVersion", 0),
                "order_id": c.ref("orders.Order"),
                "factors": {"synthetic": True},
                "operator_id": c.physician,
            },
        ),
        after=("orders.ResultObservation",),
    ),
    "ResultQuarantine": Seed(
        lambda c: (
            {
                "version_id": c.ref("orders.ResultVersion", 1),
                "reason": "identity_mismatch",
            },
        )
    ),
    # Front-desk routing rejects the quarantined upload as the wrong patient.
    "ResultRejection": Seed(
        lambda c: (
            {
                "version_id": c.ref("orders.ResultVersion", 1),
                "operator_id": c.owner,
                "evidence_code": "wrong_patient_confirmed",
                "_actor": "owner",
            },
        ),
        after=("orders.ResultQuarantine",),
        support=(("identity.UserClinicRole", Seed(_owner_role("receptionist"))),),
    ),
    "ResultAcknowledgment": Seed(
        lambda c: (
            {
                "version_id": c.ref("orders.ResultVersion", 0),
                "reviewer_id": c.physician,
                "disposition": "no_action",
            },
        ),
        after=("orders.ResultMatch",),
    ),
    "ResultRelease": Seed(
        lambda c: (
            {
                "version_id": c.ref("orders.ResultVersion", 0),
                "released_by_id": c.physician,
                "audience": "patient",
            },
        )
    ),
    "ResultWorkItem": Seed(
        lambda c: ({"version_id": c.ref("orders.ResultVersion", 0), "kind": "review"},)
    ),
}

BILLING: Final[dict[str, Seed]] = {
    # The witness owner also holds the finance and front-desk roles the
    # finance, insurance and reception guards authorize against.
    "JournalBook": Seed(
        support=tuple(
            ("identity.UserClinicRole", Seed(_owner_role(role)))
            for role in ("finance",)
        ),
    ),
    "Account": Seed(adopt="opening a journal book writes its chart of accounts"),
    "JournalEntry": Seed(
        lambda c: (
            {
                "source_kind": "invoice",
                "source_id": c.ref("billing.Invoice"),
                "content_hash": BYTES32,
                "state": "proposed",
            },
        )
    ),
    # A balanced completion: patient receivable against the physician's
    # payable share, posted once both lines exist.
    "JournalLine": Seed(
        lambda c: (
            {
                "side": "debit",
                "amount_minor": 6000,
                "account_id": c.ref("billing.Account", code="patient_ar"),
            },
            {
                "side": "credit",
                "amount_minor": 6000,
                "account_id": c.ref("billing.Account", code="practitioner_payables"),
                "practitioner_id": c.physician,
            },
        ),
        updates=(
            (
                "billing.JournalEntry",
                lambda c: (c.ref("billing.JournalEntry"), {"state": "posted"}),
            ),
        ),
    ),
    "PractitionerPayoutRule": Seed(
        lambda c: ({"kind": "percent", "value": 40, "practitioner_id": c.physician},)
    ),
    # The fixture invoice is billed to the patient's insurer.
    "InvoiceTerms": Seed(_rows({"net_minor": 15000, "payer": "insurer"})),
    "JournalSettlement": Seed(
        _rows({"amount_minor": 5000, "kind": "insurer", "account": "bank"}),
        # Finance verifies the bank receipt, then posts it; the guard stamps
        # the verifying actor and time (collections._settlement_state).
        updates=tuple(
            (
                "billing.JournalSettlement",
                _move(lambda c: c.ref("billing.JournalSettlement"), state=state),
            )
            for state in ("verified", "posted")
        ),
    ),
    # A partial payment: the invoice stays open (no legacy settlement pays it).
    "SettlementAllocation": Seed(
        _rows({"amount_minor": 5000, "repasse_minor": 0, "repasse_side": "credit"})
    ),
    # Part of the allocated payment is refunded against that allocation.
    "Refund": Seed(
        lambda c: (
            {
                "amount_minor": 1000,
                "kind": "refund",
                "allocation_id": c.ref("billing.SettlementAllocation"),
                "requested_by_id": c.owner,
            },
        ),
        after=("billing.SettlementAllocation",),
    ),
    "Payout": Seed(
        lambda c: (
            {
                "amount_minor": 6000,
                "requested_by_id": c.owner,
                "practitioner_id": c.physician,
            },
        ),
        after=("billing.JournalLine",),
    ),
    # Opened at the front desk with the (empty) petty cash counted exactly.
    "CashSession": Seed(
        lambda c: (
            {
                "command_id": c.key("cash-open"),
                "last_command_id": c.key("cash-open"),
                "counted_minor": 0,
                "state": "open",
                "reason": "",
            },
        )
    ),
    "CashSessionEvent": Seed(
        adopt="the session's AFTER trigger writes its opening event"
    ),
    "PspPolicy": Seed(),
    # The owner registers the recorded sandbox PSP fixture as the PIX
    # capability's current version (researched -> selected -> approved to test
    # -> sandbox), then requests a PIX charge under the latest policy.
    "PspIntent": Seed(
        lambda c: (
            {
                "capability_version_id": c.key("pix-version"),
                "policy_id": c.ref("billing.PspPolicy"),
                "requested_by_id": c.owner,
                "provider": "recorded-psp-v1",
                "kind": "charge",
                "target_id": c.key("pix-invoice"),
                "method": "pix",
                "installments": 1,
                "currency": "BRL",
                "amount_minor": 15000,
                "expires_at": c.future,
            },
        ),
        support=(
            # The PIX charge's own patient-paid invoice: drafted, issued
            # (draft -> open) and given its patient terms, as issuing does.
            (
                "billing.Invoice",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("pix-invoice"),
                            "patient_id": c.patient,
                            "amount_minor": 15000,
                            "currency": "BRL",
                            "idempotency_key": c.key("pix-invoice-command"),
                            "create_fingerprint": bytes.fromhex(DIGEST),
                        },
                    ),
                    updates=(
                        (
                            "billing.Invoice",
                            lambda c: (
                                c.key("pix-invoice"),
                                {"state": "open", "issued_at": c.now},
                            ),
                        ),
                    ),
                ),
            ),
            (
                "billing.InvoiceTerms",
                Seed(
                    lambda c: (
                        {
                            "invoice_id": c.key("pix-invoice"),
                            "payer": "patient",
                            "net_minor": 15000,
                        },
                    )
                ),
            ),
            (
                "providers.CapabilityApproval",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("pix-approval"),
                            "capability_id": c.ref(
                                "providers.ProviderCapability",
                                key="pix",
                                clinic_id=None,
                            ),
                            "approver_name": "Synthetic Approver",
                            "approver_role": "synthetic-review",
                            "evidence_uri": "https://example.invalid/psp-sandbox-evidence",
                        },
                    )
                ),
            ),
            (
                "providers.CapabilityVersion",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("pix-version"),
                            "capability_id": c.ref(
                                "providers.ProviderCapability",
                                key="pix",
                                clinic_id=None,
                            ),
                            "provider": "recorded-psp-v1",
                            "account": "fixture",
                            "environment": "sandbox",
                            "api_version": "v1",
                            "region": "br-synthetic",
                            "state": "researched",
                        },
                    ),
                    updates=(
                        *(
                            (
                                "providers.CapabilityVersion",
                                lambda c, state=state: (
                                    c.key("pix-version"),
                                    # The approval binds on entry to approved_to_test.
                                    {
                                        "state": state,
                                        "approval_id": c.key("pix-approval"),
                                    }
                                    if state == "approved_to_test"
                                    else {"state": state},
                                ),
                            )
                            for state in (
                                "selected_in_plan",
                                "approved_to_test",
                                "sandbox",
                            )
                        ),
                        (
                            "providers.ProviderCapability",
                            lambda c: (
                                c.ref(
                                    "providers.ProviderCapability",
                                    key="pix",
                                    clinic_id=None,
                                ),
                                {"current_version_id": c.key("pix-version")},
                            ),
                        ),
                    ),
                ),
            ),
        ),
    ),
    # The recorded sandbox PSP binds the owner's PIX charge, then reports it
    # settled; the posted PSP settlement allocated to the charge's terms is
    # receipted.
    "PspBinding": Seed(
        lambda c: (
            {
                "intent_id": c.ref("billing.PspIntent"),
                "provider": "recorded-psp-v1",
                "provider_reference": "syn-1",
            },
        ),
        after=("billing.PspIntent",),
    ),
    "PspEvidence": Seed(
        lambda c: (
            {
                "intent_id": c.ref("billing.PspIntent"),
                "provider": "recorded-psp-v1",
                "event_id": "syn-evt-1",
                "source": "callback",
                "status": "settled",
                "decision": "verified",
                "payment_code": "SYNTHETICPIXCODE",
                "payment_url": "https://psp.synthetic.invalid/pay/1",
                "version": 1,
                "amount_minor": 15000,
                "currency": "BRL",
            },
        ),
        after=("billing.PspBinding",),
    ),
    "PspReceipt": Seed(
        lambda c: (
            {
                "intent_id": c.ref("billing.PspIntent"),
                "settlement_id": c.key("psp-settlement"),
                "allocation_id": c.key("psp-allocation"),
                "amount_minor": 15000,
            },
        ),
        after=("billing.PspEvidence",),
        support=(
            (
                "billing.JournalSettlement",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("psp-settlement"),
                            "amount_minor": 15000,
                            "kind": "patient",
                            "account": "psp_clearing",
                            "evidence_reference": c.ref("billing.PspIntent"),
                        },
                    ),
                    updates=tuple(
                        (
                            "billing.JournalSettlement",
                            _move(lambda c: c.key("psp-settlement"), state=state),
                        )
                        for state in ("verified", "posted")
                    ),
                ),
            ),
            (
                "billing.SettlementAllocation",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("psp-allocation"),
                            "settlement_id": c.key("psp-settlement"),
                            "terms_id": c.ref(
                                "billing.InvoiceTerms", invoice_id=c.key("pix-invoice")
                            ),
                            "amount_minor": 15000,
                            "repasse_minor": 0,
                            "repasse_side": "credit",
                        },
                    )
                ),
            ),
        ),
    ),
    # Reviewed as a synthetic fixture (rehearsal only, never EG-9).
    "FiscalConfiguration": Seed(
        lambda c: (
            {
                "legal_entity": {"name": "Synthetic Clinic Ltda"},
                "legal_entity_reference": c.key("legal-entity"),
                "provider": "national",
                "municipality_code": "3550308",
                "service_code": "04.01",
                "tax_regime": "simples_nacional",
                "ibs_cbs_applicability": "not_applicable",
                "tax_fields": {"iss_rate_bp": 200},
                "review_kind": "synthetic_fixture",
                "review_reference": c.key("fiscal-fixture-review"),
                "reviewed_at": c.now,
                "expires_at": c.future,
                "cancellation_window_seconds": 86400,
            },
        )
    ),
    # A rehearsal document under the reviewed configuration's exact binding.
    "FiscalDocument": Seed(
        lambda c: (
            {
                "configuration_id": c.ref("billing.FiscalConfiguration"),
                "legal_entity_reference": c.key("legal-entity"),
                "provider": "national",
                "municipality_code": "3550308",
                "service_code": "04.01",
                "tax_fields": {"iss_rate_bp": 200},
                "amount_minor": 15000,
                "rehearsal": True,
                "status": "draft",
                "create_fingerprint": BYTES32,
                "dps_id": "SYNTHETIC-DPS-0001",
                "request_xml": b"<synthetic/>",
                "rejection_code": "",
                # A fresh create command is also the document's last command.
                "create_command_id": c.key("fiscal-create"),
                "last_command_id": c.key("fiscal-create"),
            },
        ),
        # The rehearsal document is submitted, authorized with its XML and PDF,
        # then cancelled inside its window; each command is new.
        updates=(
            (
                "billing.FiscalDocument",
                lambda c: (
                    c.ref("billing.FiscalDocument"),
                    {"status": "submitted", "last_command_id": c.key("fiscal-submit")},
                ),
            ),
            (
                "billing.FiscalDocument",
                lambda c: (
                    c.ref("billing.FiscalDocument"),
                    {
                        "status": "authorized",
                        "last_command_id": c.key("fiscal-authorize"),
                        "xml_payload": b"<synthetic-nfse/>",
                        "xml_reference": c.ref("billing.FiscalDocument"),
                        "pdf_payload": b"%PDF-synthetic",
                        "pdf_reference": c.ref("billing.FiscalDocument"),
                        "provider_reference": "SYNTHETIC-NFSE-0001",
                    },
                ),
            ),
            (
                "billing.FiscalDocument",
                lambda c: (
                    c.ref("billing.FiscalDocument"),
                    {
                        "status": "cancelled",
                        "last_command_id": c.key("fiscal-cancel"),
                        "cancellation_payload": b"<synthetic-cancel/>",
                    },
                ),
            ),
        ),
    ),
    "FiscalTransition": Seed(
        lambda c: (
            {"from_status": "draft", "to_status": "submitted", "actor_id": c.owner},
        )
    ),
}

INSURANCE: Final[dict[str, Seed]] = {
    "Payer": Seed(_rows({"ans_registry": "000000", "name": "Synthetic Payer"})),
    "PayerContract": Seed(
        lambda c: (
            {
                "version": 1,
                "starts_on": c.now.date(),
                "billing_identity": {"code": "SYNTHETIC"},
                "fingerprint": DIGEST,
                "eligibility_supported": True,
            },
        )
    ),
    "PriceTable": Seed(_rows({"version": 1, "prices": {"10101012": 15000}})),
    "TussTerm": Seed(
        _rows(
            {
                "component_version": "202611",
                "source_sha256": DIGEST,
                "table_code": "22",
                "code": "10101012",
                "description": "Synthetic consultation term",
            }
        )
    ),
    "InsuranceMembership": Seed(
        lambda c: (
            {
                "details": {"card": "SYNTHETIC-CARD"},
                "valid_from": c.now.date(),
                "registration_id": c.ref("intake.InsuranceMembership"),
            },
        )
    ),
    # Revenue for the completed fixture visit is recognized on the insurer's
    # terms; the consultation guide over that invoice is drafted, then
    # validated.
    "Guide": Seed(
        lambda c: (
            {
                "kind": "consulta",
                "number": "1",
                "facts": {"synthetic": True},
                "total_minor": 15000,
                "state": "draft",
                "encounter_id": _attended_encounter(c),
                "invoice_id": c.ref("billing.Invoice"),
                "membership_id": c.ref("insurance.InsuranceMembership"),
                "contract_id": c.ref("insurance.PayerContract"),
                "price_table_id": c.ref("insurance.PriceTable"),
            },
        ),
        after=("billing.InvoiceTerms",),
        calls=(
            lambda c: c.call(
                "billing_recognize_completion",
                c.clinic,
                c.ref("billing.Invoice"),
                None,
                actor="owner",
            ),
        ),
        updates=(
            (
                "insurance.Guide",
                lambda c: (c.ref("insurance.Guide"), {"state": "validated"}),
            ),
        ),
    ),
    "Batch": Seed(
        lambda c: (
            {
                "number": "1",
                "state": "draft",
                "contract_id": c.ref("insurance.PayerContract"),
                "guide_ids": [str(c.ref("insurance.Guide"))],
                "versions": [],
                "xml": "<synthetic/>",
                "content_sha256": DIGEST,
            },
        ),
        after=("insurance.Guide",),
    ),
    "BatchGuide": Seed(
        # With its guide bound, the draft batch is validated, sealed and
        # handed to submission.
        updates=tuple(
            (
                "insurance.Batch",
                _move(lambda c: c.ref("insurance.Batch"), state=state),
            )
            for state in ("validated", "sealed", "submitting")
        ),
    ),
    # The sealed batch's exact content is submitted, then acknowledged.
    "Submission": Seed(
        lambda _c: ({"channel": "portal", "content_sha256": DIGEST},),
        after=("insurance.BatchGuide",),
        updates=tuple(
            (
                "insurance.Batch",
                _move(lambda c: c.ref("insurance.Batch"), state=state),
            )
            for state in ("submitted", "acknowledged")
        ),
    ),
    # The payer's return for the acknowledged batch.
    "Return": Seed(
        lambda _c: ({"source_sha256": DIGEST, "xml": b"<synthetic-return/>"},),
        after=("insurance.Submission",),
    ),
    "Glosa": Seed(
        _rows(
            {
                "sequence": 1,
                "charged_minor": 15000,
                "accepted_minor": 10000,
                "denied_minor": 5000,
                "code": "1801",
                "detail": {"synthetic": True},
            }
        )
    ),
    "Appeal": Seed(
        _rows(
            {
                "body": "Synthetic appeal.",
                "clinical_assertions": "Synthetic clinical assertion.",
                "content_sha256": DIGEST,
            }
        )
    ),
    "ClinicalApproval": Seed(
        lambda c: ({"content_sha256": DIGEST, "approved_by_id": c.physician},),
        actor="physician",
    ),
    "PayerRequest": Seed(
        _rows(
            {
                "kind": "eligibility",
                "outcome": "eligible",
                "evidence": {"synthetic": True},
            }
        )
    ),
    # The insurer's posted payment, allocated to the guide's invoice terms.
    "PaymentMatch": Seed(
        lambda c: (
            {
                "guide_id": c.ref("insurance.Guide"),
                "settlement_id": c.ref("billing.JournalSettlement"),
                "allocation_id": c.ref("billing.SettlementAllocation"),
                "amount_minor": c.value(
                    "billing.SettlementAllocation",
                    "amount_minor",
                    id=c.ref("billing.SettlementAllocation"),
                ),
            },
        )
    ),
    # The validation command's receipt names the guide's resulting state.
    "CommandReceipt": Seed(
        lambda c: (
            {
                "kind": "guide",
                "subject_id": c.ref("insurance.Guide"),
                "next_state": "validated",
            },
        ),
        after=("insurance.Guide",),
    ),
}

INVENTORY: Final[dict[str, Seed]] = {
    "Location": Seed(_rows({"name": "Synthetic storage"})),
    "Item": Seed(_rows({"name": "Synthetic gloves"})),
    "Supplier": Seed(
        _rows({"name": "Synthetic Supplier", "contact": "sup@example.invalid"})
    ),
    "Batch": Seed(lambda c: ({"lot": "SYN-LOT-1", "expires_on": (c.future).date()},)),
    "MinimumStock": Seed(_rows({"quantity": 10})),
    "Equipment": Seed(_rows({"name": "Synthetic monitor", "serial": "SYN-SN-1"})),
    "Calibration": Seed(
        lambda c: (
            {
                "performed_on": c.now.date(),
                "due_on": (c.now + timedelta(days=365)).date(),
                "outcome": "passed",
            },
        )
    ),
    "Maintenance": Seed(
        lambda c: (
            {
                "performed_on": c.now.date(),
                "due_on": (c.now + timedelta(days=180)).date(),
                "outcome": "completed",
            },
        )
    ),
    "PurchaseBudget": Seed(lambda c: ({"amount_minor": 50000, "approved_at": c.now},)),
    "PurchaseRequest": Seed(_rows({"quantity": 5, "amount_minor": 2500})),
    "StockMovement": Seed(_rows({"kind": "receipt", "quantity": 5})),
    "CommandReceipt": Seed(
        after=("inventory.StockMovement",),
        rows=lambda c: (
            {
                "operation": "receive",
                "fingerprint": DIGEST,
                "object_type": "stockmovement",
                "recorded_by_id": c.owner,
                "object_id": c.ref("inventory.StockMovement"),
            },
        ),
    ),
}

CRM: Final[dict[str, Seed]] = {
    "ReferralSource": Seed(_rows({"code": "synthetic-web", "name": "Synthetic web"})),
    "Lead": Seed(
        _rows(
            {
                "name": "Synthetic Prospect",
                "email": "prospect@example.invalid",
                "state": "active",
            }
        )
    ),
    "PublicBookingProfile": Seed(
        _rows({"code": "synthetic-clinic", "description": "Synthetic booking."})
    ),
    # The patient records an email opt-out in their own portal session.
    "MarketingPreference": Seed(
        lambda c: (
            {
                "channel": "email",
                "opted_in": False,
                "contact_version": 0,
                "revision": 1,
                "enrollment_id": c.enrollment,
                "patient_session_id": c.patient_session,
            },
        ),
        actor="patient",
        settings=(("app.current_patient_session", "patient_session_id"),),
    ),
    # A campaign mid-send on the approved neutral marketing template.
    "Campaign": Seed(
        lambda c: (
            {
                "title": "Synthetic neutral campaign",
                "template_id": c.ref("comms.TemplateVersion", 1),
                "state": "draft",
                "created_by_id": c.owner,
            },
        ),
        # Campaigns and prospects are the clinic manager's (owner's role).
        support=(("identity.UserClinicRole", Seed(_owner_role("clinic_manager"))),),
        updates=tuple(
            (
                "crm.Campaign",
                _move(
                    lambda c: c.ref("crm.Campaign"),
                    state=state,
                    revision=F("revision") + 1,
                ),
            )
            for state in ("approved", "sending")
        ),
    ),
    "CampaignSend": Seed(
        lambda c: (
            {"message_id": c.ref("comms.Message", 2), "enrollment_id": c.enrollment},
        )
    ),
    # The arrived fixture visit is started and completed, then the
    # patient rates it from their portal session.
    "SatisfactionSurvey": Seed(
        lambda c: (
            {
                "appointment_id": _attended_visit(c),
                "patient_session_id": c.patient_session,
                "score": 5,
            },
        ),
        actor="patient",
        settings=(("app.current_patient_session", "patient_session_id"),),
    ),
}


def _metric_window(c: SeedContext) -> Row:
    """Build a valid no-show metric query over the week before the stage."""
    end = c.now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=7)
    stamp = "%Y-%m-%dT%H:%M:%S.%fZ"
    return {
        "descriptor": {
            "metric_id": "no_show",
            "version": 1,
            "dimensions": [],
            "filters": {},
            "window": {"start": start.strftime(stamp), "end": end.strftime(stamp)},
            "tz": "America/Sao_Paulo",
        },
        "window_start": start,
        "window_end": end,
    }


ANALYTICS: Final[dict[str, Seed]] = {
    "MetricDefinitionVersion": Seed(
        _rows({"metric_id": "no_show", "specification": {"synthetic": True}})
    ),
    "SavedReport": Seed(lambda c: (_metric_window(c),)),
    "ReadModelRefresh": Seed(
        lambda c: (
            {
                **_metric_window(c),
                "authorization_version": DIGEST,
                "rows": [],
                "quality": "complete",
            },
        )
    ),
    # The owner rejects the analyst's answer under their current metric
    # authorization (the version the product computes for them).
    "AnalystReview": Seed(
        lambda c: (
            {
                **_metric_window(c),
                "user_id": c.owner,
                "invocation_id": c.ref("ai.AIInvocation", 1),
                "authorization_version": c.call(
                    "analytics_authorization", c.clinic, actor="owner"
                ),
                "decision": "rejected",
            },
        )
    ),
}

SUBSCRIPTIONS: Final[dict[str, Seed]] = {
    "Plan": Seed(_rows({"code": "intelligence_usage", "version": 9})),
    "Entitlement": Seed(_rows({"meter": "messages", "quota": 1000})),
    "Subscription": Seed(adopt="0001 opens one trial subscription per organization"),
    "SubscriptionEvent": Seed(
        _rows(
            {
                "action": "trial_started",
                "from_state": "",
                "to_state": "trial",
                "revision": 1,
            }
        )
    ),
    "Seat": Seed(adopt="0001 seats every existing clinic membership"),
    "UsageEvent": Seed(_rows({"meter": "messages", "quantity": 3})),
    "UsageRollup": Seed(_rows({"meter": "messages", "quantity": 3})),
}

INTEROP: Final[dict[str, Seed]] = {
    "ImportJob": Seed(
        _rows(
            {
                "source_kind": "csv",
                "size_bytes": 128,
                "source_sha256": DIGEST,
                "fingerprint": DIGEST,
            }
        )
    ),
    "ImportMapping": Seed(
        _rows(
            {
                "code": "synthetic-patients",
                "version": 1,
                "source_kind": "csv",
                "document": {"columns": {"nome": "full_name"}},
                "document_sha256": DIGEST,
            }
        )
    ),
    "ImportFile": Seed(
        _rows(
            {
                "role": "source",
                "path": "synthetic/source.csv",
                "sha256": DIGEST,
                "size_bytes": 128,
                "media_type": "text/csv",
                "scan_verdict": "not_applicable",
                "data": b"nome\nSynthetic Upgrade Persona\n",
            }
        )
    ),
    # The owner uploads, profiles, maps, dry-runs and approves the job.
    "ImportTransition": Seed(
        lambda c: (
            _import_step(c, 1, "uploaded"),
            _import_step(c, 2, "profiled", report=True),
            _import_step(c, 3, "mapped", mapped=True),
            _import_step(c, 4, "dry_run", mapped=True, report=True, counted=True),
            {
                **_import_step(c, 5, "approved", mapped=True, counted=True),
                "rollback_window_hours": 24,
            },
        ),
        after=("interop.ImportFile", "interop.ImportMapping"),
    ),
    # The approved job's one batch, requested under the owner's step-up.
    "ImportBatch": Seed(
        lambda c: (
            {
                "sequence": 1,
                "dataset": "patients",
                "first_row": 1,
                "last_row": 1,
                "rows_sha256": DIGEST,
                "state": "pending",
                "requested_by_id": c.owner,
            },
        ),
        settings=IMPORT_SETTINGS,
        after=("interop.ImportTransition",),
        support=(_owner_session("import-session", IMPORT_SESSION_KEY),),
    ),
    # Committing the batch registers the row's patient and enrollment (keyed by
    # job and row), records it, and closes the batch with its count.
    "ImportedRecord": Seed(
        lambda c: (
            {
                "batch_id": c.ref("interop.ImportBatch"),
                "dataset": "patients",
                "row_number": 1,
                "kind": "patient",
                "source_record_id": "SYN-ROW-1",
                "content": {"nome": "Synthetic Imported Persona"},
                "original_author": "Synthetic legacy system",
                "content_sha256": DIGEST,
                "patient_id": c.key("imported-patient"),
                "enrollment_id": c.key("imported-enrollment"),
            },
        ),
        support=(
            (
                "intake.Patient",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("imported-patient"),
                            "full_name": "Synthetic Imported Persona",
                            "full_name_index": sha256(
                                b"synthetic-imported-persona"
                            ).digest(),
                            "birth_date": date(1991, 2, 3),
                        },
                    )
                ),
            ),
            (
                "intake.PatientClinicEnrollment",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("imported-enrollment"),
                            "patient_id": c.key("imported-patient"),
                            "idempotency_key": _import_key(c.ref("interop.ImportJob")),
                            "create_fingerprint": bytes.fromhex(DIGEST),
                        },
                    )
                ),
            ),
        ),
        updates=(
            (
                "interop.ImportBatch",
                lambda c: (
                    c.ref("interop.ImportBatch"),
                    {"state": "committed", "committed_count": 1},
                ),
            ),
        ),
    ),
    # The committed job is rolled back under step-up within its window; the
    # rollback resolver then hides the enrollment the job created.
    "HiddenEnrollment": Seed(
        adopt="the rollback resolver hides the job's own enrollment",
        after=("interop.ImportedRecord",),
        support=(
            (
                "interop.ImportTransition",
                Seed(
                    lambda c: (
                        _import_step(
                            c, 6, "committed", mapped=True, report=True, counted=True
                        ),
                        {
                            **_import_step(c, 7, "rolled_back"),
                            "reason": "abandoned",
                            "_settings": dict(IMPORT_SETTINGS_MAP),
                        },
                    )
                ),
            ),
        ),
        calls=(
            lambda c: c.call(
                "interop_hide_reversible", c.ref("interop.ImportJob"), actor="owner"
            ),
        ),
    ),
    "ApiClient": Seed(
        _rows(
            {
                "name": "Synthetic partner",
                "scopes": ["appointments:read", "invoices.status:read"],
                "rate_limit_per_minute": 60,
            }
        ),
    ),
    "ApiClientSecret": Seed(_rows({"version": 1, "secret_digest": DIGEST})),
    "ApiAccessToken": Seed(
        _rows({"token_digest": DIGEST, "scopes": ["appointments:read"]})
    ),
    "ApiWriteReceipt": Seed(
        _rows(
            {
                "idempotency_key": "synthetic-key",
                "operation": "webhook_create",
                "request_digest": DIGEST,
                "response_status": 201,
                "response_body": {"synthetic": True},
            }
        )
    ),
    "WebhookSubscription": Seed(
        _rows(
            {
                "url": "https://example.invalid/webhook",
                "events": ["appointment.created", "invoice.status_changed"],
            }
        )
    ),
    # A booked visit's arrival is captured for the partner's subscription:
    # the capture trigger writes the delivery and its pending action.
    # Issuing a charge (draft -> open) is captured for the partner's
    # subscription: the capture trigger writes the delivery and its action.
    "WebhookDelivery": Seed(
        adopt="the capture trigger writes the delivery",
        after=("interop.WebhookSubscription",),
        support=(
            (
                "billing.Invoice",
                Seed(
                    lambda c: (
                        {
                            "id": c.key("webhook-invoice"),
                            "patient_id": c.patient,
                            "amount_minor": 9000,
                            "currency": "BRL",
                            "idempotency_key": c.key("webhook-invoice-command"),
                            "create_fingerprint": bytes.fromhex(DIGEST),
                        },
                    ),
                    updates=(
                        (
                            "billing.Invoice",
                            lambda c: (
                                c.key("webhook-invoice"),
                                {"state": "open", "issued_at": c.now},
                            ),
                        ),
                    ),
                ),
            ),
        ),
    ),
    # The outbox worker claims the delivery's action (pending -> in progress,
    # first attempt) and records that attempt's outcome.
    "WebhookAttempt": Seed(
        lambda c: (
            {
                "delivery_id": c.ref("interop.WebhookDelivery"),
                "attempt": 1,
                "outcome": "delivered",
            },
        ),
        prepare=(
            (
                "comms.IntegrationOperation",
                lambda c: (
                    c.value(
                        "interop.WebhookDelivery",
                        "operation_id",
                        id=c.ref("interop.WebhookDelivery"),
                    ),
                    {"status": "in_progress", "attempt_count": F("attempt_count") + 1},
                ),
            ),
        ),
    ),
    # Requested, then claimed for building (its parts land while building).
    "OrganizationExport": Seed(
        updates=(
            (
                "interop.OrganizationExport",
                lambda c: (
                    c.ref("interop.OrganizationExport"),
                    {"state": "building", "revision": F("revision") + 1},
                ),
            ),
        ),
    ),
    "OrganizationExportPart": Seed(
        _rows({"part": 1, "sha256": DIGEST, "size_bytes": 1, "data": b"synthetic"})
    ),
    "RndsSubmission": Seed(
        _rows(
            {
                "request_digest": DIGEST,
                "environment": "homologation",
                "profile": "BRResultadoExameLaboratorial-1.1",
                "bundle": b'{"resourceType":"Bundle"}',
                "bundle_sha256": DIGEST,
            }
        )
    ),
}

# Capture and draft records are the recording physician's, under the session's
# capture token.
PHYSICIAN_SCRIBE: Final[dict[str, Seed]] = {
    name: replace(
        seed,
        actor="physician",
        settings=seed.settings or (("app.scribe_token", "=" + DIGEST),),
    )
    for name, seed in SCRIBE.items()
}

# Clinical order and result records are written by the ordering physician;
# only the lab account is provisioned by the owner.
PHYSICIAN_ORDERS: Final[dict[str, Seed]] = {
    name: seed if name == "LabAccount" else replace(seed, actor="physician")
    for name, seed in ORDERS.items()
}

DOMAIN_SEEDS: Final[dict[str, dict[str, Seed]]] = {
    "ai": AI,
    "analytics": ANALYTICS,
    "billing": BILLING,
    "comms": COMMS,
    "consent": CONSENT,
    "crm": CRM,
    "ehr": EHR,
    "identity": IDENTITY,
    "insurance": INSURANCE,
    "intake": INTAKE,
    "interop": INTEROP,
    "inventory": INVENTORY,
    "orders": PHYSICIAN_ORDERS,
    "prescription": PRESCRIPTION,
    "providers": PROVIDERS,
    "retention": RETENTION,
    "scheduling": SCHEDULING,
    "scribe": PHYSICIAN_SCRIBE,
    "subscriptions": SUBSCRIPTIONS,
    "teleconsult": TELECONSULT,
    "workflows": WORKFLOWS,
}
