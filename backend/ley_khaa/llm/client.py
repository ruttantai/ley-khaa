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

    # Which implementation this is, for the bundle manifest. Same contract as
    # SandboxRunner.name: the manifest must record what ACTUALLY produced the
    # script, never the model the router would have picked, because those two
    # differ every time the offline fallback stands in for a missing API key.
    name: str

    def parse(self, *, choice: ModelChoice, system: str, user: str, output_format: type[T]) -> T:
        ...

    def extract_image(
        self,
        *,
        choice: ModelChoice,
        system: str,
        user: str,
        image: bytes,
        media_type: str,
        output_format: type[T],
    ) -> T:
        """Read one image into a structured result (spec §3.4).

        Separate from parse() rather than an optional argument on it: every
        implementation must consciously answer "what do I do with an image?",
        and the offline ones answer "nothing, and I say so" — which is a
        different behaviour, not a degenerate case of text parsing.
        """
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

    name = "fake"

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

    def extract_image(
        self, *, choice: ModelChoice, system: str, user: str,
        image: bytes, media_type: str, output_format: type[T],
    ) -> T:
        return self.parse(choice=choice, system=system, user=user, output_format=output_format)


class AnthropicLLM:
    """Production client. Never instantiated from tests."""

    name = "anthropic"

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
        return self._require_parsed(response, choice, output_format)

    def extract_image(
        self, *, choice: ModelChoice, system: str, user: str,
        image: bytes, media_type: str, output_format: type[T],
    ) -> T:
        """parse(), with an image content block ahead of the text.

        The block order is Anthropic's documented shape for vision. The bytes
        go in the image block and nowhere else: pasting base64 into the text
        would double the token bill for no benefit.
        """
        import base64

        kwargs: dict[str, Any] = {
            "model": choice.model,
            "max_tokens": choice.max_tokens,
            "system": system,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64.standard_b64encode(image).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": user},
                    ],
                }
            ],
            "output_format": output_format,
        }
        # Same gate as parse(): adaptive thinking exists only on the 5-series,
        # and sending it to Haiku 4.5 is a 400.
        if choice.supports_thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        response = self._client.messages.parse(**kwargs)
        return self._require_parsed(response, choice, output_format)

    def _require_parsed(self, response: Any, choice: ModelChoice, output_format: type[T]) -> T:
        parsed = response.parsed_output
        if parsed is None:
            # A response that stops on max_tokens parses to None. Returning it
            # hands the caller a None it does not expect — the crystallizer and
            # interpreter both dereference the result immediately — and the
            # traceback then names THEIR line, not this one. Shared by parse()
            # and extract_image(): both hit the same SDK call shape.
            raise ValueError(
                f"{choice.model} returned no parsed output "
                f"(stop_reason may be max_tokens for {output_format.__name__})"
            )
        return parsed
