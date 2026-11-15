"""Immutable release bundles; selecting a bundle never activates a provider."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

PROFILE_ENV: Final = "CLINIC_RELEASE_PROFILE"
OWNER_PUBLIC_KEY_ENV: Final = "CLINIC_RELEASE_OWNER_PUBLIC_KEY"
COMMIT_ENV: Final = "CLINIC_RELEASE_COMMIT_SHA"
TREE_ENV: Final = "CLINIC_RELEASE_TREE_SHA"
DEFAULT_LIVE_PROFILE: Final = "core-live"

CORE_FEATURES: Final = ("identity", "patients", "scheduling", "ehr", "ordinary_billing")
CORE_PROVIDERS: Final = (
    "hosted_pitr",
    "data_at_rest",
    "tenant_key_management",
    "managed_secrets",
    "tls_transport",
    "attachment_storage",
    "attachment_scanning",
    "pdf_rendering",
)
LIVE_GATE_CAPABILITIES: Final = (
    "hosted_ci",
    "monitoring_delivery",
    "legal_approval",
    "privacy_approval",
    "incident_response",
    "credential_rotation",
    "retention_policy",
    "dpa",
    "ropa",
    "dpo_designation",
    "dpo_public_contact",
    "incident_record_retention",
)
CORE_REVERIFICATIONS: Final = ("RV-04", "RV-05", "RV-06", "RV-07")
COMMON_RECORDS: Final = (
    "release-candidate",
    "security-assessment",
    "disaster-recovery",
)


@dataclass(frozen=True, slots=True)
class ReleaseProfile:
    """A closed feature bundle and its independently required records."""

    name: str
    mode: str
    features: tuple[str, ...]
    providers: tuple[str, ...] = ()
    gates: tuple[str, ...] = ()
    evaluations: tuple[str, ...] = ()
    reverifications: tuple[str, ...] = ()

    @property
    def required_records(self) -> tuple[str, ...]:
        """Return stable refusal order, independent of filesystem order."""
        if self.mode == "synthetic":
            return ()
        return (
            *self.gates,
            *COMMON_RECORDS,
            *(f"provider-{key}" for key in self.providers),
            *(f"evaluation-{key}" for key in self.evaluations),
            *(f"reverification-{key}" for key in self.reverifications),
        )

    @property
    def evidence_capabilities(self) -> tuple[str, ...]:
        """Require the live-data gate and only this bundle's provider records."""
        return (*LIVE_GATE_CAPABILITIES, *self.providers)


PROFILES: Final = MappingProxyType(
    {
        profile.name: profile
        for profile in (
            ReleaseProfile(
                "synthetic-demo",
                "synthetic",
                (
                    *CORE_FEATURES,
                    *(f"AI-{number:02d}" for number in range(1, 9)),
                    "psp",
                    "nfse",
                    "tiss",
                    "regulated_rx",
                    "sso",
                    "scim",
                    "rnds",
                    "video",
                    "messaging",
                ),
            ),
            ReleaseProfile(
                "core-live",
                "live",
                CORE_FEATURES,
                CORE_PROVIDERS,
                ("EG-1", "EG-2", "EG-12"),
                reverifications=CORE_REVERIFICATIONS,
            ),
            ReleaseProfile(
                "intelligence-live",
                "live",
                (*CORE_FEATURES, "AI-01", "AI-02", "AI-03"),
                (*CORE_PROVIDERS, "asr", "llm_inference", "object_storage_media"),
                ("EG-1", "EG-2", "EG-12", "EG-3", "EG-5", "EG-11"),
                ("AI-01", "AI-02", "AI-03"),
                (*CORE_REVERIFICATIONS, "RV-01"),
            ),
            ReleaseProfile(
                "revenue-live",
                "live",
                (*CORE_FEATURES, "psp", "nfse", "tiss"),
                (*CORE_PROVIDERS, "pix", "psp_card", "nfse", "tiss"),
                ("EG-1", "EG-2", "EG-12", "EG-5", "EG-8", "EG-9"),
                reverifications=(*CORE_REVERIFICATIONS, "RV-11", "RV-12", "RV-13"),
            ),
            ReleaseProfile(
                "regulated-rx-live",
                "live",
                (*CORE_FEATURES, "regulated_rx"),
                (
                    *CORE_PROVIDERS,
                    "physician_registration",
                    "qualified_signing",
                    "signature_verification",
                    "sncr",
                ),
                ("EG-1", "EG-2", "EG-12", "EG-6"),
                reverifications=(*CORE_REVERIFICATIONS, "RV-08"),
            ),
            ReleaseProfile(
                "network-live",
                "live",
                (*CORE_FEATURES, "sso", "scim", "rnds"),
                (*CORE_PROVIDERS, "rnds"),
                ("EG-1", "EG-2", "EG-12", "EG-7"),
                reverifications=(*CORE_REVERIFICATIONS, "RV-09"),
            ),
        )
    }
)
FEATURES: Final = tuple(
    dict.fromkeys(
        feature for profile in PROFILES.values() for feature in profile.features
    )
)


def availability(profile: ReleaseProfile, *, ready: bool) -> dict[str, str]:
    """Describe disabled features honestly, including a refused live bundle."""
    return {
        feature: (
            ("synthetic" if profile.mode == "synthetic" else "available")
            if ready and feature in profile.features
            else "unavailable"
        )
        for feature in FEATURES
    }
