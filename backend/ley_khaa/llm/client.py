from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from .router import ModelChoice

T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    """The single seam every LLM call goes through.

    Implementations: AnthropicLLM (production), HeuristicLLM (the offline
    regex stand-in used when no API key is set) and FakeLLM (tests).
    """

    def parse(self, *, choice: ModelChoice, system: str, user: str, output_format: type[T]) -> T:
        ...


@dataclass
class RecordedCall:
    choice: ModelChoice
    system: str
    user: str
    output_format: type


@dataclass
class FakeLLM:
    """Deterministic stand-in. Returns queued responses in order and records calls."""

    responses: list[Any]
    calls: list[RecordedCall] = field(default_factory=list)

    def parse(self, *, choice: ModelChoice, system: str, user: str, output_format: type[T]) -> T:
        self.calls.append(RecordedCall(choice=choice, system=system, user=user, output_format=output_format))
        assert self.responses, "FakeLLM exhausted: more parse() calls than queued responses"
        response = self.responses.pop(0)
        # A queued exception is raised rather than returned, so tests can drive
        # the malformed-output and transport-failure branches without mocks.
        if isinstance(response, Exception):
            raise response
        return response


class AnthropicLLM:
    """Production client. Never instantiated from tests."""

    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client

    def parse(self, *, choice: ModelChoice, system: str, user: str, output_format: type[T]) -> T:
        kwargs: dict[str, Any] = {
            "model": choice.model,
            "max_tokens": choice.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_format": output_format,
        }
        # Adaptive thinking only exists on the 5-series models; sending it to
        # Haiku 4.5 is a 400.
        if choice.supports_thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        response = self._client.messages.parse(**kwargs)
        return response.parsed_output
