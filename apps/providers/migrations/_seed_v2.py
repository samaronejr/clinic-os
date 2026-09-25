"""Seed data for the provider capability registry (record set 2026-09-24-v2).

Frozen with migration ``providers.0001``: the seed must reproduce the
record set as reviewed on 2026-09-24 even if the live models evolve.
Tests may call ``seed_v2_capabilities(django.apps.apps)`` to rebuild the
registry after a transactional flush wipes the tables.

Every capability starts at ``researched`` with an unselected placeholder
version. Capabilities whose PV-table row names a proposed primary or
alternatives additionally record those candidates as ``selected_in_plan``
versions (D-12); the primary becomes the capability's current version.
Nothing here is an approval: ``approval_id`` stays NULL and only the
owner CLI can move a version to ``approved_to_test``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from django.apps.registry import Apps

RECORD_SET: Final = "2026-09-24-v2"
UNVERIFIED: Final = "unverified"

# key -> (record file stem, description, [(provider, environment, region), ...])
# The candidate tuple order is the PV table order: first entry is the
# proposed primary and becomes the current version.
CAPABILITY_SEED: Final[dict[str, tuple[str, str, tuple[tuple[str, str, str], ...]]]] = {
    "asr": (
        "asr",
        "Speech recognition (pt-BR) for the ambient scribe.",
        (
            ("AWS Transcribe", "production", "sa-east-1"),
            ("Azure Speech", "production", "Brazil South"),
            ("Google Speech-to-Text v2", "production", "southamerica-east1"),
            ("Deepgram", "production", ""),
            ("Speechmatics", "production", ""),
        ),
    ),
    "attachment_scanning": (
        "attachment_scanning",
        "Malware scanning for uploaded attachments.",
        (
            ("GuardDuty Malware Protection for S3", "production", "sa-east-1"),
            ("ClamAV self-hosted", "production", ""),
        ),
    ),
    "attachment_storage": (
        "attachment_storage",
        "Private attachment and document storage.",
        (("Amazon S3", "production", "sa-east-1"),),
    ),
    "data_at_rest": (
        "data_at_rest",
        "AES-256 storage encryption for database and objects.",
        (
            ("Amazon RDS storage encryption", "production", "sa-east-1"),
            ("Amazon S3 SSE-KMS", "production", "sa-east-1"),
        ),
    ),
    "email": (
        "email",
        "Transactional email delivery.",
        (
            ("Amazon SES", "production", "sa-east-1"),
            ("Zenvia", "production", ""),
            ("Twilio", "production", ""),
        ),
    ),
    "hosted_pitr": (
        "hosted_pitr",
        "Hosted encrypted backup and point-in-time recovery.",
        (("Amazon RDS PITR", "production", "sa-east-1"),),
    ),
    "llm_inference": (
        "llm_inference",
        "LLM inference for drafting and assistance.",
        (
            ("Amazon Bedrock (in-region models only)", "production", "sa-east-1"),
            ("Azure OpenAI (regional deployment)", "production", "Brazil South"),
            ("Vertex AI", "production", "southamerica-east1"),
        ),
    ),
    "managed_secrets": (
        "managed_secrets",
        "Managed secret loading and rotation.",
        (("AWS Secrets Manager", "production", "sa-east-1"),),
    ),
    "nfse": (
        "nfse",
        "NFS-e fiscal documents.",
        (
            ("NFS-e Padrao Nacional API (gov.br)", "production", ""),
            ("Focus NFe", "production", ""),
            ("eNotas", "production", ""),
            ("PlugNotas", "production", ""),
            ("NFE.io", "production", ""),
        ),
    ),
    "object_storage_media": (
        "object_storage_media",
        "Encrypted object storage for media and AI artifacts.",
        (
            ("Amazon S3 with KMS", "production", "sa-east-1"),
            ("self-hosted object storage", "production", ""),
        ),
    ),
    "pdf_rendering": (
        "pdf_rendering",
        "HTML/CSS to PDF rendering for documents.",
        (),
    ),
    "physician_registration": (
        "physician_registration",
        "Professional registration verification (CFM and other councils).",
        (),
    ),
    "pix": (
        "pix",
        "PIX charging and reconciliation.",
        (
            ("Asaas", "production", ""),
            ("Pagar.me", "production", ""),
            ("Stone", "production", ""),
            ("Iugu", "production", ""),
            ("Efi", "production", ""),
        ),
    ),
    "psp_card": (
        "psp_card",
        "Card payments, installments and split.",
        (
            ("Asaas", "production", ""),
            ("Pagar.me", "production", ""),
            ("Stone", "production", ""),
            ("Iugu", "production", ""),
            ("Efi", "production", ""),
        ),
    ),
    "qualified_signing": (
        "qualified_signing",
        "ICP-Brasil qualified document signing.",
        (
            ("BirdID (Soluti)", "production", ""),
            ("VIDaaS (Valid)", "production", ""),
            ("SafeID", "production", ""),
            ("RemoteID", "production", ""),
            ("Memed", "production", ""),
            ("Nexodata", "production", ""),
            ("Mevo", "production", ""),
        ),
    ),
    "rnds": (
        "rnds",
        "RNDS national health data exchange.",
        (("DATASUS RNDS credentialing", "production", ""),),
    ),
    "signature_verification": (
        "signature_verification",
        "Independent signature verification.",
        (),
    ),
    "sms": (
        "sms",
        "SMS delivery.",
        (
            ("Amazon SNS", "production", "sa-east-1"),
            ("Zenvia", "production", ""),
            ("Twilio", "production", ""),
        ),
    ),
    "sncr": (
        "sncr",
        "SNCR controlled-prescription integration.",
        (
            ("Anvisa SNCR API", "production", ""),
            ("prescription partner integration", "production", ""),
        ),
    ),
    "tenant_key_management": (
        "tenant_key_management",
        "Tenant-scoped envelope encryption key management.",
        (("AWS KMS", "production", "sa-east-1"),),
    ),
    "tiss": (
        "tiss",
        "TISS insurance exchange.",
        (
            ("direct XML webservice per operadora", "production", ""),
            ("clearinghouse", "production", ""),
        ),
    ),
    "tls_transport": (
        "tls_transport",
        "TLS transport policy for external endpoints.",
        (),
    ),
    "video": (
        "video",
        "Video consultation rooms.",
        (
            ("LiveKit Cloud", "production", ""),
            ("Twilio Video", "production", ""),
            ("Daily", "production", ""),
            ("Vonage", "production", ""),
        ),
    ),
    "whatsapp": (
        "whatsapp",
        "WhatsApp Business messaging.",
        (
            ("Meta WhatsApp Cloud API", "production", ""),
            ("approved BSP", "production", ""),
        ),
    ),
}


def seed_v2_capabilities(apps: Apps) -> None:
    """Insert the 2026-09-24-v2 capability registry idempotently.

    Works with both the migration-time historical app registry and the
    live ``django.apps.apps`` registry used by tests.
    """
    capability_model = apps.get_model("providers", "ProviderCapability")
    version_model = apps.get_model("providers", "CapabilityVersion")
    for key, (record_ref, description, candidates) in CAPABILITY_SEED.items():
        capability, _ = capability_model.objects.get_or_create(
            key=key,
            clinic_id=None,
            defaults={
                "record_ref": f"{RECORD_SET}/{record_ref}",
                "description": description,
            },
        )
        researched, _ = version_model.objects.get_or_create(
            capability=capability,
            provider="unselected",
            account="",
            environment="",
            api_version="",
            region="",
            defaults={"state": "researched"},
        )
        current = researched
        for provider, environment, region in candidates:
            version, _ = version_model.objects.get_or_create(
                capability=capability,
                provider=provider,
                account="",
                environment=environment,
                api_version=UNVERIFIED,
                region=region,
                defaults={
                    "state": "selected_in_plan",
                    "retention_terms": UNVERIFIED,
                },
            )
            if current.state == "researched":
                current = version
        if capability.current_version_id is None:
            capability.current_version = current
            capability.save(update_fields=["current_version", "updated_at"])
