from typing import Any, TypeVar

from pydantic import BaseModel

from .router import ModelChoice

T = TypeVar("T", bound=BaseModel)


class OllamaLLM:
    """A local model behind the same seam as Claude (phase 8 design §3.1).

    Generic where HeuristicLLM is hand-written: Ollama takes a JSON schema as
    its `format`, and every output type in this codebase is a pydantic model,
    so one `parse` covers every stage that exists and every one added later.
    """

    def __init__(self, model: str, host: str = "", client: Any | None = None) -> None:
        if client is None:
            import ollama

            client = ollama.Client(host=host or None)
        self._client = client
        self.model = model
        # An instance attribute, unlike the class attributes on AnthropicLLM
        # and HeuristicLLM: the manifest must name the model that actually did
        # the work, and "ollama" alone does not identify what produced a script.
        self.name = f"ollama:{model}"

    def parse(self, *, choice: ModelChoice, system: str, user: str, output_format: type[T]) -> T:
        # choice.model is deliberately unused: it names a Claude model, and a
        # local daemon has never heard of it. choice.max_tokens still applies —
        # synthesis needs room for a whole program.
        response = self._client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            format=output_format.model_json_schema(),
            options={"num_predict": choice.max_tokens},
        )
        content = response.message.content
        if not content:
            # ChatResponse.message.content is Optional[str]. Feeding None to
            # model_validate_json raises an opaque TypeError far from the cause.
            raise ValueError(f"{self.name} returned an empty response")
        return output_format.model_validate_json(content)

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
        """Text-only, as spec §11 states. `image` is deliberately unread.

        Phase 7's VisionExtractor turns this empty extraction into a stored
        carried-not-read record and names the image in the manifest, so §7's
        "image steps error out gracefully rather than guess" is satisfied here
        with no new error type and no change to the extractor.
        """
        return output_format(
            kind="text",
            content="",
            summary=(
                f"{user or 'an image'} was attached but not read: "
                f"{self.name} is a text-only backend (set ANTHROPIC_API_KEY for vision)."
            ),
        )
