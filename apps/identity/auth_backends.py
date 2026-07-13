"""Database-function-only Django authentication backend."""

from uuid import UUID

from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.hashers import check_password
from django.db import connection
from django.http import HttpRequest

from apps.identity.models import User


class ClinicBackend(BaseBackend):
    """Authenticate without direct runtime access to the user table."""

    def authenticate(
        self,
        request: HttpRequest | None,
        **credentials: str | None,
    ) -> User | None:
        """Load credentials through the hardened pre-session resolver."""
        del request
        username = credentials.get("username")
        password = credentials.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            return None

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM clinic_app.auth_lookup(%s)",
                [username],
            )
            row = cursor.fetchone()
        if row is None:
            return None

        user_id, stored_username, encoded_password, is_active = row
        if not is_active or not check_password(password, encoded_password):
            return None
        return User(
            id=user_id,
            username=stored_username,
            password=encoded_password,
            is_active=is_active,
        )

    def get_user(self, user_id: UUID | str) -> User | None:
        """Reconstruct only the GUC-bound user matching the signed session id."""
        try:
            expected_id = UUID(str(user_id))
        except ValueError:
            return None

        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM clinic_app.load_current_user()")
            row = cursor.fetchone()
            description = cursor.description
        if row is None or description is None:
            return None

        field_names = [column.name for column in description]
        user = User.from_db(connection.alias, field_names, row)
        if user.pk != expected_id:
            return None
        return user
