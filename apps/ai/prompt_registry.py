"""Load only code-versioned prompts; callers never supply template paths."""

from hashlib import sha256
from pathlib import Path
from typing import Final

from apps.ai.contracts import Purpose

PROMPT_VERSIONS: Final = {**dict.fromkeys(Purpose, 1), Purpose.EXTRACTION: 2}


def load_prompt(purpose: Purpose) -> tuple[str, str]:
    """Return the exact shipped bytes as UTF-8 plus their SHA-256 provenance."""
    version = PROMPT_VERSIONS[purpose]
    content = (
        Path(__file__).parent / "prompts" / purpose.value / f"v{version}.md"
    ).read_bytes()
    return content.decode("utf-8"), sha256(content).hexdigest()
