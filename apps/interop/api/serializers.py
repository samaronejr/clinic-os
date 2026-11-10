"""Wire shapes of the external API; every scalar is bounded before the resolver."""

from __future__ import annotations

from typing import Final

from rest_framework import serializers

from apps.interop.models import EVENT_TYPES

APPOINTMENT_STATUSES: Final = (
    "requested",
    "held",
    "booked",
    "arrived",
    "in_progress",
    "completed",
    "cancelled",
    "expired",
    "no_show",
)
INVOICE_STATUSES: Final = ("open", "paid", "cancelled")
MAX_PAGE: Final = 100
MAX_URL_LENGTH: Final = 2048


class ErrorSerializer(serializers.Serializer[object]):
    """The ``{code, message_key}`` refusal body."""

    code = serializers.CharField()
    message_key = serializers.CharField()


class OAuthErrorSerializer(serializers.Serializer[object]):
    """RFC 6749 section 5.2 token error body."""

    error = serializers.ChoiceField(
        choices=(
            "invalid_request",
            "invalid_client",
            "invalid_scope",
            "unsupported_grant_type",
            "temporarily_unavailable",
        )
    )


class TokenRequestSerializer(serializers.Serializer[object]):
    """Client credentials grant; credentials in Basic auth or in the form."""

    grant_type = serializers.ChoiceField(choices=("client_credentials",))
    scope = serializers.CharField(required=False, max_length=200)
    client_id = serializers.UUIDField(required=False)
    client_secret = serializers.CharField(required=False, max_length=67)


class TokenResponseSerializer(serializers.Serializer[object]):
    """RFC 6749 section 5.1 token response."""

    access_token = serializers.CharField()
    token_type = serializers.ChoiceField(choices=("Bearer",))
    expires_in = serializers.IntegerField()
    scope = serializers.CharField()


class AppointmentSerializer(serializers.Serializer[object]):
    """One appointment with opaque ids; no clinical content."""

    id = serializers.CharField()
    status = serializers.ChoiceField(choices=APPOINTMENT_STATUSES)
    start_at = serializers.DateTimeField()
    end_at = serializers.DateTimeField()
    patient = serializers.CharField()
    practitioner = serializers.CharField()
    service_type = serializers.CharField(allow_null=True)
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()


class PatientSerializer(serializers.Serializer[object]):
    """Minimal demographics: the display name and birth date only."""

    id = serializers.CharField()
    display_name = serializers.CharField()
    birth_date = serializers.DateField(allow_null=True)


class AvailabilityEntrySerializer(serializers.Serializer[object]):
    """A working window, a busy interval or a clinic closure."""

    kind = serializers.ChoiceField(choices=("window", "busy", "closed"))
    practitioner = serializers.CharField(allow_null=True)
    start_at = serializers.DateTimeField()
    end_at = serializers.DateTimeField()


class InvoiceSerializer(serializers.Serializer[object]):
    """Issued invoice status in integer BRL centavos."""

    id = serializers.CharField()
    status = serializers.ChoiceField(choices=INVOICE_STATUSES)
    amount_minor = serializers.IntegerField()
    currency = serializers.CharField()
    appointment = serializers.CharField(allow_null=True)
    patient = serializers.CharField()
    issued_at = serializers.DateTimeField()


class WindowQuerySerializer(serializers.Serializer[object]):
    """A bounded time window and keyset page."""

    start = serializers.CharField(max_length=40)
    end = serializers.CharField(max_length=40)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=MAX_PAGE)
    cursor = serializers.CharField(required=False, max_length=200)


class WebhookCreateSerializer(serializers.Serializer[object]):
    """An HTTPS endpoint without query, fragment or credentials."""

    url = serializers.CharField(max_length=MAX_URL_LENGTH)
    events = serializers.ListField(
        child=serializers.ChoiceField(choices=EVENT_TYPES),
        min_length=1,
        max_length=len(EVENT_TYPES),
    )


class WebhookSubscriptionSerializer(serializers.Serializer[object]):
    """One endpoint registration of the calling client."""

    id = serializers.UUIDField()
    url = serializers.CharField()
    events = serializers.ListField(child=serializers.ChoiceField(choices=EVENT_TYPES))
    state = serializers.ChoiceField(choices=("active", "revoked"))
    secret_version = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    revoked_at = serializers.DateTimeField(allow_null=True)


class WebhookSecretSerializer(WebhookSubscriptionSerializer):
    """A registration plus its signing secret, returned on create and rotate."""

    secret = serializers.CharField()
