"""Optional offline fixture generator; never imported by application or CI paths."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    """Render authored scripts only when explicitly invoked on a prepared host."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    manifest = json.loads(
        (ROOT / "apps/ai/evaluation/corpora/engineering-v1.json").read_text()
    )
    fixtures = {}
    for case in manifest["cases"]:
        voice = case["accent"].removeprefix("flite-")
        source = f"flite=text='{case['script']}':voice={voice}"
        transformation = "anull"
        if case["adversarial"] == "noise":
            transformation = (
                "anoisesrc=color=white:seed=47:amplitude=0.01[noise];"
                "[0:a][noise]amix=inputs=2:duration=shortest"
            )
        elif case["adversarial"] == "interruptions":
            transformation = "volume='if(between(t,0.8,1.5),0,1)':eval=frame"
        elif case["adversarial"] == "mic_failure":
            transformation = "volume=0"
        generated = subprocess.run(  # noqa: S603 - fixed binary and offline authored corpus.
            [
                "/usr/bin/ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-fflags",
                "+bitexact",
                "-f",
                "lavfi",
                "-i",
                source,
                "-filter_complex",
                transformation,
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "libopus",
                "-b:a",
                "12k",
                "-vbr",
                "off",
                "-flags:a",
                "+bitexact",
                "-map_metadata",
                "-1",
                "-f",
                "ogg",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
        fixtures[case["id"]] = {
            "voice": voice,
            "transform": case["adversarial"],
            "script_sha256": sha256(case["script"].encode()).hexdigest(),
            "audio_sha256": sha256(generated.stdout).hexdigest(),
            "encoding": "ogg-opus",
            "audio_base64": base64.b64encode(generated.stdout).decode(),
        }
    with options.output.open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "schema_version": 1,
                "provenance": (
                    "Self-authored CC0 synthetic scripts, rendered offline with "
                    "the host's FFmpeg Flite filter; no patient audio or network. "
                    "Voice/device/noise tags are engineering simulations only."
                ),
                "fixtures": fixtures,
            },
            stream,
            indent=2,
            sort_keys=True,
        )
        stream.write("\n")


if __name__ == "__main__":
    main()
