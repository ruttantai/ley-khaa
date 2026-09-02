import logging
import os

from ..config import settings
from .client import AnthropicLLM, LLMClient
from .heuristic import HeuristicLLM
from .ollama import OllamaLLM

logger = logging.getLogger(__name__)

# build_llm runs per request and per background sweep; the fallback notice is
# about configuration, so say it once rather than every few seconds.
_warned_about_fallback = False


def _ollama_client(host: str):
    """The one place the real client is constructed, so tests can replace it
    without ever opening a socket."""
    import ollama

    return ollama.Client(host=host or None)


def build_llm(backend: str = "anthropic") -> LLMClient:
    """Pick the client. Falls back to the offline heuristic with no API key set,
    so a fresh clone demos without credentials."""
    if backend == "heuristic":
        return HeuristicLLM()
    if backend == "ollama":
        return _build_ollama()
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


def _build_ollama() -> LLMClient:
    """Probe once, at startup, and degrade loudly (phase 8 design §3.4).

    Two failures with different fixes are reported differently: a daemon that
    is not running, and a model that was never pulled.
    """
    model, host = settings.ollama_model, settings.ollama_host
    try:
        listing = _ollama_client(host).list()
        pulled = {m.model for m in listing.models}
    except Exception as exc:
        # Deliberately broad: this is a startup probe whose only job is "decide
        # which backend to use, never crash deciding". Every failure here means
        # the same thing — the daemon is not usable right now — so every
        # failure gets the same response: warn (naming the real exception type,
        # so diagnosis isn't harmed) and hand back the heuristic. A dead daemon
        # raises builtins.ConnectionError, NOT an ollama.* type, and ollama's own
        # RequestError/ResponseError inherit straight from Exception with no
        # shared base to catch instead — so any enumeration here is only ever a
        # guess about a third-party library's exception surface. Catching too
        # much costs nothing (there is no failure this probe should propagate);
        # catching too little means the app crash-loops on `docker compose up`
        # for the exact user this phase exists to serve.
        _fall_back(
            f"LEY_KHAA_LLM=ollama but the Ollama daemon is not reachable at {host} "
            f"({type(exc).__name__}) — falling back to HeuristicLLM, the offline regex "
            "stand-in. Start Ollama, or set LEY_KHAA_OLLAMA_HOST."
        )
        return HeuristicLLM()

    if not any(name == model or name.startswith(f"{model}:") for name in pulled):
        _fall_back(
            f"LEY_KHAA_LLM=ollama and the daemon is reachable at {host}, but the model "
            f"{model!r} is not pulled — falling back to HeuristicLLM, the offline regex "
            f"stand-in. Fix with: ollama pull {model}"
        )
        return HeuristicLLM()

    return OllamaLLM(model=model, host=host)


def _fall_back(message: str) -> None:
    """Say it once. build_llm runs per request and per background sweep."""
    global _warned_about_fallback
    if not _warned_about_fallback:
        _warned_about_fallback = True
        logger.warning(message)
