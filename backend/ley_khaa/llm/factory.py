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

# The Ollama probe is a live network round-trip (`client.list()`) to decide
# which backend to use. build_llm runs on the hot path of every request and
# every background sweep, so without caching that round-trip happens on every
# one of them — a latency cost nobody chose, and worse, it would let the app
# silently step down to HeuristicLLM mid-session if the daemon later dies,
# which contradicts the "no runtime step-down" guarantee this phase promises.
# Caching the resolved client (success or fallback) makes the decision once,
# at first use, and every later call reuses it — matching "probe once, at
# startup" as documented, not "probe once per request".
_ollama_client_cache: LLMClient | None = None


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

    build_llm runs per request and per background sweep, so "once" only holds
    if the resolved client is cached: the probe itself runs at most once per
    process, and every later call reuses that decision rather than repeating
    the network round-trip (or re-deciding to fall back) on the hot path.

    Two failures with different fixes are reported differently: a daemon that
    is not running, and a model that was never pulled.
    """
    global _ollama_client_cache
    if _ollama_client_cache is not None:
        return _ollama_client_cache

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
        _ollama_client_cache = HeuristicLLM()
        return _ollama_client_cache

    if not any(name == model or name.startswith(f"{model}:") for name in pulled):
        _fall_back(
            f"LEY_KHAA_LLM=ollama and the daemon is reachable at {host}, but the model "
            f"{model!r} is not pulled — falling back to HeuristicLLM, the offline regex "
            f"stand-in. Fix with: ollama pull {model}"
        )
        _ollama_client_cache = HeuristicLLM()
        return _ollama_client_cache

    _ollama_client_cache = OllamaLLM(model=model, host=host)
    return _ollama_client_cache


def _fall_back(message: str) -> None:
    """Say it once. build_llm runs per request and per background sweep."""
    global _warned_about_fallback
    if not _warned_about_fallback:
        _warned_about_fallback = True
        logger.warning(message)
