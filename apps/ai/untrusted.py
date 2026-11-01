"""Content has a separate type from model proposals and registered tool arguments."""

from dataclasses import dataclass, field
from typing import Literal

from apps.ai.action_contracts import MAX_ACTION_BYTES, ActionInputError
from apps.ai.action_payload import JSON


@dataclass(frozen=True, slots=True)
class UntrustedContent:
    """A bounded data field, never a system prompt or a command parser input."""

    origin: Literal["document", "transcript", "message", "retrieval", "agent_output"]
    text: str = field(repr=False)

    def __post_init__(self) -> None:
        """Reject malformed wrappers before they enter the inference boundary."""
        if (
            self.origin
            not in {
                "document",
                "transcript",
                "message",
                "retrieval",
                "agent_output",
            }
            or not isinstance(self.text, str)
            or not 1 <= len(self.text) <= MAX_ACTION_BYTES
        ):
            raise ActionInputError

    def model_data(self) -> dict[str, JSON]:
        """Keep the trust label in the structured model data."""
        return {"origin": self.origin, "untrusted": True, "text": self.text}

    @property
    def flagged(self) -> bool:
        """Conservatively flag obvious injection; schemas remain authoritative."""
        folded = self.text.casefold()
        return any(
            fragment in folded
            for fragment in (
                "ignore instructions",
                "ignore previous",
                "ignore all",
                "system prompt",
                "<tool_call",
                "execute shell",
                "ignore as instru",
                "ignore instru",
            )
        )
