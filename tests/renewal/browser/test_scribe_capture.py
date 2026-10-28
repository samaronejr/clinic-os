"""Real MediaRecorder and private HTTP ingest; no recording-video dependency in CI."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from base64 import b64encode
from hashlib import sha256
from typing import TYPE_CHECKING, Literal
from urllib.parse import urljoin
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from renewal.browser._fixture_secrets import fixture_dsn
from renewal.browser._navigation import (
    click_to_navigate,
    expect_document,
    goto_settled,
    reload_settled,
)
from renewal.browser._page_wait import evaluate_js
from renewal.browser.a11y_support import check_page
from renewal.browser.engines import full_page_screenshot, grant_capture_audio
from renewal.browser.test_availability import _sign_in_physician
from renewal.browser.test_encounter import seed

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from playwright.sync_api import Page, Response


def fixture_command(
    staff: dict[str, str],
    values: Mapping[str, str],
) -> bytes:
    """Use the existing private owner channel, never a URL or artifact secret."""
    result = subprocess.run(
        [sys.executable, "-m", "ops.testing.scribe_fixture"],
        env={
            **os.environ,
            "APP_DATABASE_URL": fixture_dsn(),
            "DJANGO_SETTINGS_MODULE": "config.settings.base",
            "CLINIC_DATA_MODE": "synthetic",
            "SCRIBE_FIXTURE_ORGANIZATION": staff["organization"],
            "SCRIBE_FIXTURE_CLINIC": staff["clinic_a"],
            "SCRIBE_FIXTURE_PHYSICIAN": staff["physician_a_id"],
            **values,
        },
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0
    return result.stdout.strip()


def open_capture(
    page: Page,
    base: str,
    staff: dict[str, str],
    day: str,
    *,
    admission: Literal["allowed", "recording_refused", "ai_refused"] = "allowed",
) -> dict[str, str]:
    data = seed(staff, day)
    assert (
        fixture_command(
            staff,
            {
                "SCRIBE_FIXTURE_ENROLLMENT": data["enrollment"],
                "SCRIBE_FIXTURE_ACCEPTED": (
                    "0" if admission == "recording_refused" else "1"
                ),
            },
        )
        == b"SCRIBE_FIXTURE_READY"
    )
    _sign_in_physician(page, base, staff)
    goto_settled(
        page, f"{base}/scheduling/clinics/{staff['clinic_a']}/agenda/day/{day}/1/"
    )
    click_to_navigate(page.locator('button[value="open"]'))
    page.locator("#template-id").select_option(data["specialty"])
    click_to_navigate(page.locator('button[value="template"]'))
    expect(page.locator("#soap-form")).to_be_visible()
    assert (
        fixture_command(
            staff,
            {
                "SCRIBE_FIXTURE_ACTION": "disclosure",
                "SCRIBE_FIXTURE_APPOINTMENT": data["appointment"],
                "SCRIBE_FIXTURE_AI_REFUSED": "1" if admission == "ai_refused" else "0",
            },
        )
        == b"SCRIBE_FIXTURE_DISCLOSED"
    )
    reload_settled(page)
    return data


@pytest.fixture
def capture_page(renewal_page: Page) -> Iterator[Page]:
    """Keep init scripts and capture custody isolated between journeys."""
    page = renewal_page.context.new_page()
    yield page
    page.close()


@pytest.fixture
def outage_page(
    renewal_page: Page,
    renewal_artifact_root: Path,
    renewal_base_url: str,
) -> Iterator[Page]:
    """Record the requested local demo without imposing a CI video dependency."""
    browser = renewal_page.context.browser
    assert browser is not None
    video = os.environ.get("SCRIBE_QA_VIDEO") == "1"
    context = browser.new_context(
        viewport={"width": 375, "height": 1000},
        record_video_dir=str(renewal_artifact_root / "video") if video else None,
    )
    grant_capture_audio(context, renewal_base_url)
    page = context.new_page()
    page.set_default_timeout(20_000)
    yield page
    recording = page.video
    context.close()
    if video:
        assert recording is not None
        recording.save_as(str(renewal_artifact_root / "offline.webm"))


def supported(page: Page) -> bool:
    """Check the actual encoder rather than claiming every engine can record."""
    return bool(
        page.evaluate(
            """() => Boolean(window.MediaRecorder &&
          ['audio/webm;codecs=opus','audio/ogg;codecs=opus']
            .some(codec => MediaRecorder.isTypeSupported(codec)))"""
        )
    )


def inspect_capture(page: Page, staff: dict[str, str]) -> dict[str, object]:
    identifier = page.locator("[data-capture-form] [name=session_id]").input_value()
    result: object = json.loads(
        fixture_command(
            staff,
            {
                "SCRIBE_FIXTURE_ACTION": "inspect",
                "SCRIBE_FIXTURE_SESSION": identifier,
            },
        )
    )
    assert isinstance(result, dict)
    return result


def save_manual_note(page: Page) -> None:
    """Prove declined capture still leaves a real acknowledged clinical save."""
    note = "SINTETICO-40: atendimento registrado manualmente."
    with page.expect_response(
        lambda response: (
            "autosave" in response.url and response.request.method == "POST"
        ),
    ) as saved:
        page.locator("#id_subjective").fill(note)
    assert saved.value.status == 200
    reload_settled(page)
    expect(page.locator("#id_subjective")).to_have_value(note)


def fake_microphone(page: Page, *, declined: bool = False) -> None:
    """Replace hardware only; the real browser encoder and HTTP stack still run."""
    assert os.environ.get("CLINIC_BROWSER_FAKE_MEDIA") == "1"
    browser = page.context.browser
    assert browser is not None
    native = browser.browser_type.name in {"chromium", "firefox"}
    page.add_init_script(
        """(() => {
          window.__scribeOpens = 0;
          window.__scribeStops = 0;
          const nativeGetUserMedia = navigator.mediaDevices.getUserMedia
            .bind(navigator.mediaDevices);
          Object.defineProperty(navigator, 'mediaDevices', {configurable: true,value: {
            enumerateDevices: async () => [{
              kind:'audioinput', deviceId:'synthetic-input',
              label:'Synthetic microphone'
            }],
            getUserMedia: async () => {
              window.__scribeOpens += 1;
              if (DECLINED || !NATIVE) {
                throw new DOMException('Synthetic refusal','NotAllowedError');
              }
              const stream = await nativeGetUserMedia({audio:true});
              const track = stream.getAudioTracks()[0];
              const stop = track.stop.bind(track);
              track.stop = () => {
                window.__scribeStops += 1;
                stop();
              };
              return stream;
            }
          }});
        })();""".replace("DECLINED", "true" if declined else "false").replace(
            "NATIVE", "true" if native else "false"
        )
    )


def test_capture_upload_pause_resume_and_stop(
    capture_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    page = capture_page
    fake_microphone(page)
    open_capture(page, renewal_base_url, availability_staff, "2036-04-20")
    panel = page.locator("[data-scribe-capture]")
    expect(panel).to_have_attribute("data-phase", "idle")
    page.locator('[data-capture-action="start"]').click()
    if supported(page):
        expect(panel).to_have_attribute("data-phase", "recording")
        expect(panel).to_have_attribute(
            "data-received", re.compile(r"^[1-9][0-9]*$"), timeout=15000
        )
        page.locator('[data-capture-action="pause"]').click()
        expect(panel).to_have_attribute("data-phase", "paused")
        page.locator('[data-capture-action="resume"]').click()
        expect(panel).to_have_attribute("data-phase", "recording")
        page.locator('[data-capture-action="stop"]').click()
        expect(panel).to_have_attribute("data-phase", "done")
    else:
        expect(panel).to_have_attribute("data-state", "disabled")
        expect(page.locator("#soap-form")).to_be_visible()
    for width, theme in (
        (1280, "light"),
        (375, "light"),
        (1280, "dark"),
        (375, "dark"),
    ):
        page.set_viewport_size({"width": width, "height": 1000})
        page.evaluate(
            """async theme => {
              document.documentElement.dataset.theme = theme;
              const running = () => document.getAnimations().filter(animation =>
                animation.playState === 'running' &&
                animation.effect.getComputedTiming().endTime !== Infinity);
              let animations = running();
              while (animations.length) {
                await Promise.allSettled(
                  animations.map(animation => animation.finished));
                animations = running();
              }
            }""",
            theme,
        )
        panel.scroll_into_view_if_needed()
        page.screenshot(path=str(renewal_artifact_root / f"scribe-{width}-{theme}.png"))
        check_page(
            page,
            base_url=renewal_base_url,
            artifact_root=renewal_artifact_root,
            state=f"scribe-{width}-{theme}",
        )


def test_three_minute_capture_is_contiguous_and_meets_ingest_target(
    capture_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    page = capture_page
    fake_microphone(page)
    open_capture(page, renewal_base_url, availability_staff, "2036-04-21")
    if not supported(page):
        page.locator('[data-capture-action="start"]').click()
        expect(page.locator("[data-scribe-capture]")).to_have_attribute(
            "data-state", "disabled"
        )
        save_manual_note(page)
        return
    responses: list[Response] = []
    page.on(
        "response",
        lambda response: (
            responses.append(response)
            if response.url.endswith("/scribe/chunk/")
            else None
        ),
    )
    page.evaluate("""() => {
      const panel = document.querySelector('[data-scribe-capture]');
      window.__scribeNinety = new Promise((resolve, reject) => {
        const signal = AbortSignal.timeout(240000);
        signal.addEventListener('abort',
          () => reject(new Error('capture_count_timeout')), {once:true});
        panel.addEventListener('scribe:state', event => {
          if (event.detail.received >= 90) resolve();
        });
      });
    }""")
    page.locator('[data-capture-action="start"]').click()
    expect(page.locator("[data-scribe-capture]")).to_have_attribute(
        "data-phase", "recording"
    )
    with page.expect_response(
        lambda response: (
            "autosave" in response.url and response.request.method == "POST"
        ),
    ) as manual:
        page.locator("#id_objective").fill(
            "SINTETICO-40: registro manual durante a captura."
        )
    assert manual.value.status == 200
    page.evaluate("() => window.__scribeNinety")
    assert [response.json()["ack"] for response in responses[:90]] == ["received"] * 90
    sequences = []
    for response in responses[:90]:
        payload = response.request.post_data_json
        assert isinstance(payload, dict)
        sequences.append(payload["seq"])
    assert sequences == list(range(90))
    durations = sorted(
        response.request.timing["responseEnd"] - response.request.timing["requestStart"]
        for response in responses[:90]
    )
    p95 = durations[math.ceil(0.95 * len(durations)) - 1]
    assert 0 <= p95 <= 3000
    page.locator('[data-capture-action="stop"]').click()
    expect(page.locator("[data-scribe-capture]")).to_have_attribute(
        "data-phase", "done"
    )
    metadata = inspect_capture(page, availability_staff)
    assert metadata["original_expiry_verified"] is True
    assert metadata["epochs"] == [
        {"number": 1, "closed": True, "gaps": {"microphone": []}}
    ]
    (renewal_artifact_root / "three-minute.json").write_text(
        json.dumps(
            {
                "level": "L1",
                "hardware": "synthetic",
                "confirmed_slices": 90,
                "ingest_p95_ms": p95,
                "metadata": metadata,
            },
            sort_keys=True,
        )
        + "\n"
    )


def test_seventy_second_outage_pauses_drops_expired_audio_and_records_gaps(
    outage_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    page = outage_page
    page.clock.install()
    fake_microphone(page)
    open_capture(page, renewal_base_url, availability_staff, "2036-04-22")
    page.locator('[data-capture-action="start"]').click()
    panel = page.locator("[data-scribe-capture]")
    if not supported(page):
        expect(panel).to_have_attribute("data-state", "disabled")
        save_manual_note(page)
        return
    expect(panel).to_have_attribute(
        "data-received", re.compile(r"^[1-9][0-9]*$"), timeout=15000
    )
    page.evaluate("""() => {
      const panel = document.querySelector('[data-scribe-capture]');
      window.__scribeMaximumBuffer = 0;
      panel.addEventListener('scribe:state', event => {
          window.__scribeMaximumBuffer = Math.max(window.__scribeMaximumBuffer,
            Number(event.target.dataset.bufferedMs));
        });
      window.__scribeOfflineBuffer = new Promise(resolve => {
        const buffered = event => {
          if (!navigator.onLine && event.detail.pending > 0) {
            panel.removeEventListener('scribe:state', buffered);
            resolve();
          }
        };
        panel.addEventListener('scribe:state', buffered);
      });
      window.__scribeOfflinePause = new Promise(resolve => {
        const paused = event => {
          if (event.detail.phase === 'offline' && event.detail.lost > 0) {
            panel.removeEventListener('scribe:state', paused);
            resolve();
          }
        };
        panel.addEventListener('scribe:state', paused);
      });
    }""")
    page.context.set_offline(True)
    evaluate_js(page, "() => window.__scribeOfflineBuffer", timeout=20_000)
    # Advance the application's actual deadline clock, not an auxiliary encoder
    # whose elapsed wall time is quantized differently by each browser.
    page.clock.fast_forward(70_000)
    evaluate_js(page, "() => window.__scribeOfflinePause", timeout=20_000)
    expect(panel).to_have_attribute("data-phase", "offline")
    assert 0 < page.evaluate("() => window.__scribeMaximumBuffer") <= 60000
    assert page.evaluate("() => window.__scribeStops") >= 1
    page.screenshot(path=str(renewal_artifact_root / "offline-paused.png"))

    def pause_ack(response: Response) -> bool:
        if not response.url.endswith("/scribe/control/"):
            return False
        payload = response.request.post_data_json
        return (
            response.request.method == "POST"
            and isinstance(payload, dict)
            and payload.get("action") == "pause"
        )

    with page.expect_response(pause_ack) as paused:
        page.context.set_offline(False)
    assert paused.value.status == 200
    pause_payload = paused.value.request.post_data_json
    assert isinstance(pause_payload, dict)
    last_seq = pause_payload["last_seq"]
    assert isinstance(last_seq, int)
    expect(page.locator('[data-capture-action="resume"]')).to_be_enabled(timeout=20000)
    metadata = inspect_capture(page, availability_staff)
    assert metadata["original_expiry_verified"] is True
    epochs = metadata["epochs"]
    assert isinstance(epochs, list)
    sequences = metadata["sequences"]
    assert isinstance(sequences, list)
    assert sequences
    acknowledged = [
        seq for number, seq, state in sequences if state in {"buffered", "accepted"}
    ]
    assert acknowledged
    assert acknowledged == list(range(acknowledged[-1] + 1))
    first_missing = acknowledged[-1] + 1
    assert last_seq >= first_missing
    assert epochs == [
        {
            "number": 1,
            "closed": True,
            "gaps": {"microphone": [[first_missing, last_seq]]},
        }
    ]
    assert int(panel.get_attribute("data-lost") or "0") > 0
    page.locator('[data-capture-action="stop"]').click()
    expect(panel).to_have_attribute("data-phase", "done")
    (renewal_artifact_root / "offline.json").write_text(
        json.dumps(
            {
                "level": "L1",
                "hardware": "synthetic",
                "clock_advance_ms": 70_000,
                "gap_first_seq": first_missing,
                "gap_last_seq": last_seq,
                "metadata": metadata,
            },
            sort_keys=True,
        )
        + "\n"
    )


def test_declined_microphone_keeps_manual_clinical_save(
    capture_page: Page,
    renewal_base_url: str,
    availability_staff: dict[str, str],
) -> None:
    page = capture_page
    fake_microphone(page, declined=True)
    open_capture(page, renewal_base_url, availability_staff, "2036-04-23")
    page.locator('[data-capture-action="start"]').click()
    panel = page.locator("[data-scribe-capture]")
    expect(panel).to_have_attribute(
        "data-state", "permission-denied" if supported(page) else "disabled"
    )
    if supported(page):
        assert inspect_capture(page, availability_staff)["chunks"] == 0
    save_manual_note(page)


def test_suspended_meter_does_not_block_capture_or_manual_care(
    capture_page: Page,
    renewal_base_url: str,
    availability_staff: dict[str, str],
    renewal_artifact_root: Path,
) -> None:
    page = capture_page
    fake_microphone(page)
    page.add_init_script("""(() => {
      const Native = window.AudioContext;
      window.AudioContext = new Proxy(Native,{construct(target,args) {
        const context = Reflect.construct(target,args);
        context.suspend();
        context.resume = () => new Promise(() => {});
        return context;
      }});
    })();""")
    open_capture(page, renewal_base_url, availability_staff, "2036-04-29")
    page.locator('[data-capture-action="start"]').click()
    if not supported(page):
        save_manual_note(page)
        return
    panel = page.locator("[data-scribe-capture]")
    expect(panel).to_have_attribute("data-phase", "recording")
    expect(panel).to_have_attribute(
        "data-received", re.compile(r"^[1-9][0-9]*$"), timeout=15000
    )
    expect(page.locator("[data-capture-meter-unavailable]")).to_be_visible()
    page.screenshot(path=str(renewal_artifact_root / "meter-unavailable.png"))
    with page.expect_response(
        lambda response: (
            "autosave" in response.url and response.request.method == "POST"
        ),
    ) as manual:
        page.locator("#id_objective").fill("SINTETICO-40: cuidado sem medidor.")
    assert manual.value.status == 200
    page.locator('[data-capture-action="stop"]').click()
    expect(panel).to_have_attribute("data-phase", "done")


def test_recording_refusal_hides_capture_without_blocking_care(
    capture_page: Page,
    renewal_base_url: str,
    availability_staff: dict[str, str],
) -> None:
    page = capture_page
    fake_microphone(page)
    open_capture(
        page,
        renewal_base_url,
        availability_staff,
        "2036-04-24",
        admission="recording_refused",
    )
    expect(page.locator("[data-scribe-capture]")).to_have_count(0)
    assert page.evaluate("() => window.__scribeOpens") == 0
    save_manual_note(page)


def test_ai_refusal_hides_capture_without_blocking_manual_care(
    capture_page: Page,
    renewal_base_url: str,
    availability_staff: dict[str, str],
) -> None:
    # Given: recording was accepted, but this encounter records AI-use refusal.
    page = capture_page
    fake_microphone(page)
    # When: the clinician opens the disclosed encounter.
    open_capture(
        page,
        renewal_base_url,
        availability_staff,
        "2036-05-10",
        admission="ai_refused",
    )
    # Then: capture never opens the microphone, while manual care still saves.
    expect(page.locator("[data-scribe-capture]")).to_have_count(0)
    assert page.evaluate("() => window.__scribeOpens") == 0
    save_manual_note(page)


def test_patient_switch_requires_an_explicit_bound_audio_decision(
    capture_page: Page,
    renewal_base_url: str,
    availability_staff: dict[str, str],
) -> None:
    page = capture_page
    fake_microphone(page)
    open_capture(page, renewal_base_url, availability_staff, "2036-04-25")
    page.locator('[data-capture-action="start"]').click()
    if not supported(page):
        save_manual_note(page)
        return
    panel = page.locator("[data-scribe-capture]")
    expect(panel).to_have_attribute(
        "data-received", re.compile(r"^[1-9][0-9]*$"), timeout=15000
    )
    identifier = page.locator("[data-capture-form] [name=session_id]").input_value()
    close = page.locator(".patient-banner-close")
    close.click()
    expect(page.locator("[data-capture-dialog]")).to_be_visible()
    page.locator('[data-capture-choice="keep"]').click()
    expect(panel).to_have_attribute("data-phase", "recording")
    close.click()
    expect(page.locator("[data-capture-dialog]")).to_be_visible()
    with expect_document(page):
        page.locator('[data-capture-choice="cancel"]').click()
    metadata: object = json.loads(
        fixture_command(
            availability_staff,
            {
                "SCRIBE_FIXTURE_ACTION": "inspect",
                "SCRIBE_FIXTURE_SESSION": identifier,
            },
        )
    )
    assert isinstance(metadata, dict)
    assert metadata["discarded"] is True
    assert metadata["retained_keys"] == 0
    data = seed(availability_staff, "2036-04-26")
    goto_settled(
        page,
        f"{renewal_base_url}/scheduling/clinics/"
        f"{availability_staff['clinic_a']}/agenda/day/2036-04-26/1/",
    )
    click_to_navigate(page.locator('button[value="open"]'))
    page.locator("#template-id").select_option(data["specialty"])
    click_to_navigate(page.locator('button[value="template"]'))
    expect(page.locator("[data-scribe-capture]")).to_have_count(0)
    save_manual_note(page)


def test_consent_withdrawal_turns_the_microphone_off_and_retains_manual_care(
    capture_page: Page,
    renewal_base_url: str,
    availability_staff: dict[str, str],
) -> None:
    page = capture_page
    fake_microphone(page)
    open_capture(page, renewal_base_url, availability_staff, "2036-04-27")
    page.locator('[data-capture-action="start"]').click()
    if not supported(page):
        save_manual_note(page)
        return
    panel = page.locator("[data-scribe-capture]")
    expect(panel).to_have_attribute(
        "data-received", re.compile(r"^[1-9][0-9]*$"), timeout=15000
    )
    identifier = page.locator("[data-capture-form] [name=session_id]").input_value()
    assert (
        fixture_command(
            availability_staff,
            {
                "SCRIBE_FIXTURE_ACTION": "revoke",
                "SCRIBE_FIXTURE_SESSION": identifier,
            },
        )
        == b"SCRIBE_FIXTURE_REVOKED"
    )
    expect(panel).to_have_attribute("data-state", "error", timeout=15000)
    assert page.evaluate("() => window.__scribeStops") >= 1
    save_manual_note(page)


def test_native_stop_and_manual_save_need_no_javascript(
    capture_page: Page,
    renewal_base_url: str,
    availability_staff: dict[str, str],
) -> None:
    page = capture_page
    fake_microphone(page)
    open_capture(page, renewal_base_url, availability_staff, "2036-04-28")
    page.locator('[data-capture-action="start"]').click()
    if not supported(page):
        save_manual_note(page)
        return
    expect(page.locator("[data-scribe-capture]")).to_have_attribute(
        "data-received", re.compile(r"^[1-9][0-9]*$"), timeout=15000
    )
    browser = page.context.browser
    assert browser is not None
    context = browser.new_context(
        java_script_enabled=False, storage_state=page.context.storage_state()
    )
    try:
        native = context.new_page()
        goto_settled(native, page.url)
        click_to_navigate(native.locator('[data-capture-action="stop"]'))
        note = "SINTETICO-40: cuidado manual sem JavaScript."
        native.locator("#id_subjective").fill(note)
        click_to_navigate(native.locator('button[value="save"]'))
        expect(native.locator("#id_subjective")).to_have_value(note)
        expect(page.locator("[data-scribe-capture]")).to_have_attribute(
            "data-phase", "recovery", timeout=15000
        )
        assert page.evaluate("() => window.__scribeStops") >= 1
    finally:
        context.close()


def test_transcript_pending_incomplete_final_and_manual_care(
    capture_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    page = capture_page
    page.route(
        "**/static/vendor/axe/axe.min.js",
        lambda route: route.continue_(
            headers={
                key: value
                for key, value in route.request.headers.items()
                if key.lower() not in {"if-modified-since", "if-none-match"}
            }
        ),
    )
    open_capture(page, renewal_base_url, availability_staff, "2036-05-08")
    panel = page.locator("[data-scribe-capture]")
    form = panel.locator("[data-capture-form]")
    token = form.locator('[name="encounter_token"]').input_value()
    headers = {
        "X-CSRFToken": form.locator('[name="csrfmiddlewaretoken"]').input_value(),
        "Referer": page.url,
    }
    control = panel.get_attribute("data-control-url")
    upload = panel.get_attribute("data-chunk-url")
    assert control is not None
    assert upload is not None
    authorized = page.request.post(
        urljoin(renewal_base_url, control),
        data={
            "action": "authorize",
            "encounter_token": token,
            "command_id": str(uuid4()),
        },
        headers=headers,
    )
    assert authorized.status == 200
    session = authorized.json()["session_id"]
    started = page.request.post(
        urljoin(renewal_base_url, control),
        data={
            "action": "start",
            "encounter_token": token,
            "command_id": str(uuid4()),
            "session_id": session,
        },
        headers=headers,
    )
    assert started.status == 200
    epoch = started.json()["epoch"]
    audio = b"SINTETICO-41-ASR-AUDIO"
    received = page.request.post(
        urljoin(renewal_base_url, upload),
        data={
            "session_id": session,
            "epoch": epoch,
            "track": "microphone",
            "seq": 0,
            "sha256": sha256(audio).hexdigest(),
            "duration_ms": 2000,
            "codec": "audio/webm;codecs=opus",
            "bytes": b64encode(audio).decode("ascii"),
        },
        headers={**headers, "X-Encounter-Token": token},
    )
    assert received.status == 200
    assert received.json()["ack"] == "received"
    stopped = page.request.post(
        urljoin(renewal_base_url, control),
        data={
            "action": "stop",
            "encounter_token": token,
            "command_id": str(uuid4()),
            "session_id": session,
            "expected_epoch": epoch,
            "last_seq": 1,
        },
        headers=headers,
    )
    assert stopped.status == 200
    assert stopped.json()["state"] == "stopped"
    click_to_navigate(page.locator('form[action$="/scribe/transcript/"] button'))
    expect(page.locator("[data-transcript-state]")).to_have_attribute(
        "data-transcript-state", "transcription_pending"
    )
    expect(page.locator("[data-transcription-disabled]")).to_be_visible()
    expect(page.locator("#transcript-coverage-title")).to_be_visible()
    for width in (1280, 375):
        page.set_viewport_size({"width": width, "height": 1000})
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth"
        )
        full_page_screenshot(
            page, renewal_artifact_root / f"transcript-pending-{width}.png"
        )
    check_page(page, renewal_base_url, renewal_artifact_root, "transcript-pending")
    click_to_navigate(page.locator('button[value="finalize"]'))
    expect(page.locator("[data-focus-error]")).to_be_visible()
    expect(page.locator("[data-transcript-state]")).to_have_attribute(
        "data-transcript-state", "transcription_pending"
    )
    page.locator('[name="acknowledge_incomplete"]').check()
    click_to_navigate(page.locator('button[value="finalize"]'))
    expect(page.locator("[data-transcript-state]")).to_have_attribute(
        "data-transcript-state", "final"
    )
    expect(page.locator("#transcript-coverage-title")).to_be_visible()
    click_to_navigate(page.locator('button[value="refresh"]'))
    expect(page.locator("[data-transcript-state]")).to_have_attribute(
        "data-transcript-state", "final"
    )
    check_page(page, renewal_base_url, renewal_artifact_root, "transcript-final")
    full_page_screenshot(page, renewal_artifact_root / "transcript-final.png")
    click_to_navigate(page.locator('a[href$="/encounter/"]'))
    expect(page.locator("#soap-form")).to_be_visible()
    save_manual_note(page)
