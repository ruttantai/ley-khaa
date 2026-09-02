from typing import Any, TypeVar

from pydantic import BaseModel

from .router import ModelChoice

T = TypeVar("T", bound=BaseModel)

# Ollama's default context window is 4096 tokens — smaller than Stage.
# SYNTHESIS's own num_predict budget (16000) alone, so with no num_ctx set
# the model is asked for up to 16000 output tokens inside a 4096-token
# window, and Ollama truncates the OLDEST tokens to make room. On the repair
# path what gets truncated is exactly the rules the model must follow: the
# SYSTEM block ("pandas and numpy are NOT installed", "there is no network",
# "never hardcode a filename"), the previous script, and up to 4000 chars of
# stderr (synthesizer.py's _MAX_STDERR). num_ctx has to cover BOTH the prompt
# (worst case: that whole repair prompt) and the requested output
# (choice.max_tokens) — it is the same window, shared by input and output.
# 32768 comfortably covers the largest max_tokens this codebase asks for
# (16000, synthesis) plus a generous multiple of the largest realistic
# prompt, without pinning this to synthesis's number exactly — a token
# budget added for a future stage does not require re-deriving this.
_NUM_CTX = 32768


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
            options={"num_predict": choice.max_tokens, "num_ctx": _NUM_CTX},
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
