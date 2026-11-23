"""Delegation never turns one verified purpose into blanket consent authority."""

from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.consent.migrations._delegate_purposes_sql import SQL
from apps.tenancy.migration_operations import AddConstraint


class Migration(migrations.Migration):
    """Keep exact purpose checks and truthful action provenance in the database."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("consent", "0004_purpose_taxonomy"),
        ("intake", "0014_delegate_sessions"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.RemoveConstraint(
            model_name="consentacceptance", name="consent_authority_check"
        ),
        migrations.RemoveConstraint(
            model_name="refusalrecord", name="consent_refusal_authority_check"
        ),
        AddConstraint(
            model_name="consentacceptance",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    authority__in=(
                        "patient_explicit_action",
                        "delegate_explicit_action",
                    )
                ),
                name="consent_authority_check",
            ),
        ),
        AddConstraint(
            model_name="refusalrecord",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    authority__in=(
                        "patient_explicit_action",
                        "delegate_explicit_action",
                    )
                ),
                name="consent_refusal_authority_check",
            ),
        ),
        migrations.RunSQL(SQL, reverse_sql=None),
    ]
