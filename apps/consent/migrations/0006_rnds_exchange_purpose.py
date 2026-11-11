"""Add the RNDS result-sharing purpose to the closed consent taxonomy.

Widens ``consent_text_scope_check`` and the delegate-grant purpose vocabulary
(``delegate_purposes_check``, whose list is ``care_processing`` plus every
consent purpose) so a patient or an explicitly scoped representative can accept
or refuse ``rnds_exchange`` like any other purpose. The historical delegate
migration SQL is left as applied; this migration replaces the constraint.
"""

from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.tenancy.migration_operations import AddConstraint

PURPOSES = (
    "teleconsultation",
    "consultation_recording",
    "ai_assistance",
    "transactional_messaging",
    "marketing",
    "research_model_improvement",
    "rnds_exchange",
)


def _delegate_check(purposes: tuple[str, ...]) -> str:
    names = ",".join(f"'{purpose}'" for purpose in ("care_processing", *purposes))
    return (
        "ALTER TABLE clinic_app.identity_patientdelegategrant\n"
        " DROP CONSTRAINT delegate_purposes_check,\n"
        " ADD CONSTRAINT delegate_purposes_check CHECK (\n"
        f"  coalesce(cardinality(purposes) BETWEEN 1 AND {len(purposes) + 1}\n"
        "  AND array_position(purposes,NULL) IS NULL\n"
        f"  AND purposes::text[] <@ ARRAY[{names}],false));\n"
    )


class Migration(migrations.Migration):
    """Keep the Python taxonomy, the text check and delegate grants in lockstep."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("consent", "0005_delegate_purposes"),
        ("identity", "0032_patient_delegate_grants"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.RemoveConstraint(
            model_name="consenttext",
            name="consent_text_scope_check",
        ),
        migrations.AlterField(
            model_name="consenttext",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("teleconsultation", "Teleconsultation"),
                    ("consultation_recording", "Consultation recording"),
                    ("ai_assistance", "AI assistance"),
                    ("transactional_messaging", "Transactional messaging"),
                    ("marketing", "Marketing"),
                    ("research_model_improvement", "Research and model improvement"),
                    (
                        "rnds_exchange",
                        "Sharing results with the national health data network (RNDS)",
                    ),
                ],
                max_length=32,
            ),
        ),
        AddConstraint(
            model_name="consenttext",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("language", "pt-BR"),
                    ("purpose__in", list(PURPOSES)),
                    ("version__gte", 1),
                ),
                name="consent_text_scope_check",
            ),
        ),
        migrations.RunSQL(
            _delegate_check(PURPOSES), reverse_sql=_delegate_check(PURPOSES[:-1])
        ),
    ]
