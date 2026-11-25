"""Unregistered schema-only mutations. Never import a mutant or read a row."""

import pytest

from ai.pending_boundary_inventory import inventory, violations

RAW_BINDING = "raw pending model at declarative binding"
HEADER = "from apps.ai.models import CareProposal as Pending\n"
CASES = {
    "serializer-fields": """
from rest_framework.serializers import Serializer as Fields, CharField
class Panel(Fields[Target]):
    state = CharField(source="state", read_only=True)
""",
    "serializer-instance": """
from rest_framework import serializers
class Panel(serializers.Serializer):
    state = serializers.CharField(read_only=True)
def page(row: Target):
    return Panel(instance=row).data
""",
    "model-serializer": """
from rest_framework import serializers
class Panel(serializers.ModelSerializer):
    class Meta:
        model = Target
        fields = ("id", "state")
""",
    "admin-list": """
from django.contrib import admin
@admin.register(Target)
class Panel(admin.ModelAdmin):
    list_display = ("state",)
""",
    "admin-readonly": """
from django.contrib.admin import ModelAdmin as Admin, site
class Panel(Admin):
    readonly_fields = ("state",)
site.register(Target, Panel)
""",
    "tabular-inline": """
from django.contrib import admin
class Panel(admin.TabularInline):
    model = Target
    fields = ("state",)
""",
    "stacked-inline": """
from django.contrib.admin import StackedInline
class Panel(StackedInline):
    model = Target
    readonly_fields = ("state",)
""",
    "model-form": """
from django import forms
class Panel(forms.ModelForm):
    class Meta:
        model = Target
        fields = ("state",)
""",
    "form-fields": """
from django import forms
class Panel(forms.Form):
    state = forms.CharField()
def show(row: Target):
    return Panel(initial={"state": row.state})
""",
    "form-factory": """
from django.forms import modelform_factory as from_model
Panel = from_model(Target, fields=("state",))
""",
    "context-processor": """
def add_panel(request):
    current: Target = request.review
    context = {"pending": current}
    return context
""",
}


@pytest.mark.parametrize("kind", sorted(CASES))
def test_raw_declarative_binding_is_detected(kind: str) -> None:
    spec = inventory()
    # Only the bound type changes; the framework configuration is identical.
    positive = HEADER + CASES[kind].replace("Target", "HumanView")
    mutant = positive.replace("HumanView", "Pending")
    assert not violations(positive, spec)
    assert RAW_BINDING in {reason for _, reason in violations(mutant, spec)}, kind


@pytest.mark.parametrize("framework", ["serializer", "admin"])
def test_declarative_relation_binding_is_derived(framework: str) -> None:
    spec = inventory()
    # Same ordinary root; only the configured source path changes.
    positive = (
        "from rest_framework import serializers\nclass Panel(serializers.Serializer):\n"
        "    text = serializers.CharField(source='human_summary.label')\n"
        if framework == "serializer"
        else "from django.contrib import admin\nclass Panel(admin.ModelAdmin):\n"
        "    list_display = ('human_summary__label',)\n"
    )
    mutant = positive.replace("human_summary", "draft")
    assert not violations(positive, spec)
    assert RAW_BINDING in {reason for _, reason in violations(mutant, spec)}


@pytest.mark.parametrize("source", ["payload", "*"])
def test_untyped_serializer_cannot_implicitly_bind_pending_content(source: str) -> None:
    spec = inventory()
    positive = (
        "from rest_framework import serializers\n"
        "class Preview(serializers.Serializer):\n"
        "    summary = serializers.CharField(source='label')\n"
    )
    assert not violations(positive, spec)
    assert RAW_BINDING in {
        reason
        for _, reason in violations(positive.replace("'label'", repr(source)), spec)
    }
