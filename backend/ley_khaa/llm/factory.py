import logging
import os

from .client import AnthropicLLM, LLMClient
from .heuristic import HeuristicLLM

logger = logging.getLogger(__name__)

# build_llm runs per request and per background sweep; the fallback notice is
# about configuration, so say it once rather than every few seconds.
_warned_about_fallback = False


def build_llm(backend: str = "anthropic") -> LLMClient:
    """Pick the client. Falls back to the offline heuristic with no API key set,
    so a fresh clone demos without credentials."""
    if backend == "heuristic":
        return HeuristicLLM()
    if not os.getenv("ANTHROPIC_API_KEY"):
        # Loud on purpose: silently degrading to a regex stub is how a reader ends
        # up believing they are looking at model output.
        global _warned_about_fallback
        if not _warned_about_fallback:
            _warned_about_fallback = True
            logger.warning(
                "ANTHROPIC_API_KEY is not set — falling back to HeuristicLLM, the offline "
                "regex stand-in. Relevance and crystallizer results will be crude: keyword "
                "matching only, no language understanding, no reasoning about which "
                "messages belong together. Export ANTHROPIC_API_KEY for the real path."
            )
        return HeuristicLLM()
    return AnthropicLLM()
