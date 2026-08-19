import os

from .client import AnthropicLLM, LLMClient
from .heuristic import HeuristicLLM


def build_llm(backend: str = "anthropic") -> LLMClient:
    """Pick the client. Falls back to the offline heuristic with no API key set,
    so a fresh clone demos without credentials."""
    if backend == "heuristic":
        return HeuristicLLM()
    if not os.getenv("ANTHROPIC_API_KEY"):
        return HeuristicLLM()
    return AnthropicLLM()
