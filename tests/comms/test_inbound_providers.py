"""Raw-byte webhook authentication and recorded-shape parsing, no database."""

from __future__ import annotations

import json

import pytest
from apps.comms.inbound_providers import (
    InboundMessage,
    StatusUpdate,
    TemplateStatus,
    WebhookRejectedError,
    normalize_address,
    parse_signed_envelope,
    parse_whatsapp,
    sign,
    verify_signature,
)
from apps.comms.whatsapp_payloads import (
    template_payload,
    text_payload,
    verification_response,
)

SECRET = bytes.fromhex("11" * 32)
PHONE_ID = "100000000000001"


def _whatsapp(
    *,
    messages: list[dict[str, object]] | None = None,
    statuses: list[dict[str, object]] | None = None,
) -> bytes:
    value: dict[str, object] = {
        "messaging_product": "whatsapp",
        "metadata": {
            "display_phone_number": "551100000000",
            "phone_number_id": PHONE_ID,
        },
        "contacts": [
            {"profile": {"name": "Paciente Sintetico"}, "wa_id": "5511999990001"}
        ],
    }
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {"id": "waba-1", "changes": [{"field": "messages", "value": value}]}
            ],
        }
    ).encode()


def test_signature_is_verified_over_the_exact_raw_bytes() -> None:
    body = _whatsapp(messages=[])
    header = sign(secret=SECRET, body=body)
    verify_signature(secret=SECRET, body=body, header=header)
    # Re-serialising the same JSON changes the bytes, so the signature must fail.
    reserialised = json.dumps(json.loads(body), indent=1).encode()
    for secret, candidate, value in (
        (SECRET, reserialised, header),
        (bytes.fromhex("22" * 32), body, header),
        (SECRET, body, ""),
        (SECRET, body, "sha256=" + "0" * 64),
        (SECRET, body, header.upper()),
        (b"", body, header),
    ):
        with pytest.raises(WebhookRejectedError):
            verify_signature(secret=secret, body=candidate, header=value)


def test_oversize_bodies_are_refused_before_hashing() -> None:
    body = b"x" * (1024 * 1024 + 1)
    with pytest.raises(WebhookRejectedError):
        verify_signature(
            secret=SECRET, body=body, header=sign(secret=SECRET, body=body)
        )


def test_whatsapp_text_message_is_normalized() -> None:
    body = _whatsapp(
        messages=[
            {
                "from": "5511999990001",
                "id": "wamid.IN1",
                "timestamp": "1790000000",
                "type": "text",
                "text": {"body": "Olá, preciso remarcar"},
            }
        ]
    )
    (event,) = parse_whatsapp(body)
    assert event == InboundMessage(
        account_ref=PHONE_ID,
        event_id="wamid.IN1",
        address="+5511999990001",
        profile_name="Paciente Sintetico",
        text="Olá, preciso remarcar",
        supported=True,
        sent_at=1790000000,
    )


def test_whatsapp_media_is_flagged_unsupported_without_a_body() -> None:
    body = _whatsapp(
        messages=[
            {
                "from": "5511999990001",
                "id": "wamid.IN2",
                "timestamp": "1790000001",
                "type": "image",
            }
        ]
    )
    (event,) = parse_whatsapp(body)
    assert isinstance(event, InboundMessage)
    assert (event.supported, event.text) == (False, "")


def test_whatsapp_status_carries_the_providers_pricing_statement() -> None:
    body = _whatsapp(
        statuses=[
            {
                "id": "wamid.OUT1",
                "status": "delivered",
                "timestamp": "1790000100",
                "recipient_id": "5511999990001",
                "pricing": {
                    "billable": True,
                    "pricing_model": "PMP",
                    "type": "regular",
                    "category": "utility",
                },
            }
        ]
    )
    (event,) = parse_whatsapp(body)
    assert isinstance(event, StatusUpdate)
    assert (
        event.status,
        event.pricing_category,
        event.billable,
        event.pricing_type,
    ) == (
        "delivered",
        "utility",
        True,
        "regular",
    )
    assert event.event_id == "wamid.OUT1:delivered:1790000100"


def test_whatsapp_template_status_update() -> None:
    body = json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba-1",
                    "changes": [
                        {
                            "field": "message_template_status_update",
                            "value": {
                                "event": "APPROVED",
                                "message_template_id": 42,
                                "message_template_name": "lembrete_consulta",
                                "message_template_language": "pt_BR",
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()
    (event,) = parse_whatsapp(body)
    assert event == TemplateStatus(
        account_ref="waba-1",
        event_id="template:42:APPROVED",
        template_name="lembrete_consulta",
        language="pt_BR",
        status="approved",
    )


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"object": "page", "entry": []}',
        _whatsapp(statuses=[{"id": "x", "status": "exploded", "timestamp": "1"}]),
        _whatsapp(
            messages=[
                {
                    "from": "abc",
                    "id": "m",
                    "timestamp": "1",
                    "type": "text",
                    "text": {"body": "x"},
                }
            ]
        ),
    ],
)
def test_malformed_payloads_are_refused(body: bytes) -> None:
    with pytest.raises(WebhookRejectedError):
        parse_whatsapp(body)


def test_signed_envelope_for_email_and_sms() -> None:
    body = json.dumps(
        {
            "account": "caixa-1",
            "events": [
                {
                    "kind": "message",
                    "event_id": "e1",
                    "from": " Paciente@Example.INVALID ",
                    "text": "Bom dia",
                    "sent_at": 1790000000,
                },
                {
                    "kind": "status",
                    "event_id": "e2",
                    "provider_message_id": "m-1",
                    "status": "delivered",
                    "sent_at": 1790000010,
                },
            ],
        }
    ).encode()
    message, status = parse_signed_envelope("email", body)
    assert isinstance(message, InboundMessage)
    assert message.address == "paciente@example.invalid"
    assert isinstance(status, StatusUpdate)
    assert status.provider_message_id == "m-1"
    with pytest.raises(WebhookRejectedError):
        parse_signed_envelope("sms", body)  # an email address is not a phone number


def test_address_normalization() -> None:
    assert normalize_address("sms", "+55 (11) 99999-0001") == "+5511999990001"
    assert normalize_address("email", " A@B.INVALID ") == "a@b.invalid"
    with pytest.raises(WebhookRejectedError):
        normalize_address("sms", "12")


def test_outbound_payload_shapes() -> None:
    assert text_payload(to="+5511999990001", body="Oi")["text"] == {
        "preview_url": False,
        "body": "Oi",
    }
    template = template_payload(
        to="+5511999990001", name="lembrete", language="pt_BR", parameters=("10:00",)
    )
    assert template["template"]["components"][0]["parameters"] == [
        {"type": "text", "text": "10:00"}
    ]
    assert template["to"] == "5511999990001"
    with pytest.raises(ValueError, match="too many"):
        template_payload(to="+55", name="n", language="pt_BR", parameters=("x",) * 11)


def test_subscription_handshake() -> None:
    kwargs = {
        "mode": "subscribe",
        "token": "tok",
        "challenge": "12345",
        "expected_token": "tok",
    }
    assert verification_response(**kwargs) == "12345"
    assert verification_response(**{**kwargs, "token": "bad"}) is None
    assert verification_response(**{**kwargs, "mode": "unsubscribe"}) is None
    assert verification_response(**{**kwargs, "challenge": "<script>"}) is None
    assert verification_response(**{**kwargs, "expected_token": ""}) is None
