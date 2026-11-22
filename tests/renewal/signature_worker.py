"""Drive one committed signature in a browser fixture (not a broker proof)."""

import sys

from apps.prescription.tasks import execute_signature
from django.db import connection


def main() -> None:
    """Execute the actual Celery entrypoint as the runtime database login."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_app",)
    result = execute_signature.apply(kwargs={"operation_id": sys.argv[1]}, throw=True)
    sys.stdout.write(f"{result.result}\n")
