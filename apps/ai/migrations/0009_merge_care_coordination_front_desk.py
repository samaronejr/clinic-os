"""Join the independent care-coordination and front-desk migration branches."""

from typing import ClassVar

from django.db import migrations


class Migration(migrations.Migration):
    """Preserve both reviewed branches without changing their operations."""

    dependencies: ClassVar = [
        ("ai", "0008_care_plan_status_private"),
        ("ai", "0008_front_desk_authority"),
    ]
    operations: ClassVar = []
