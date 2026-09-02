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

# Spec §3.4: "a cheap client.list() with a short timeout". The `ollama`
# package (0.6.2) defaults to timeout=None on the underlying httpx client, so
# with nothing set here a firewall that DROPs packets to 11434 (ufw's default
# on Linux) blocks the fresh-clone startup path for ~127s with no /health,
# and indefinitely behind a stalling proxy. This probe's only job is "decide
# which backend to use, never crash or hang deciding" — its failure mode is
# "use the heuristic", so waiting is strictly worse than deciding quickly.
_PROBE_TIMEOUT_SECONDS = 5.0


def _ollama_client(host: str):
    """The one place the real client is constructed, so tests can replace it
    without ever opening a socket."""
    import ollama

    return ollama.Client(host=host or None, timeout=_PROBE_TIMEOUT_SECONDS)


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
        # ollama._types.ListResponse.Model.model is Optional[str] — a listing
        # entry with no name filtered out here rather than left to reach the
        # membership check below, which happens OUTSIDE this try: a bare
        # `None` in the set would make `None.startswith(...)` raise
        # AttributeError past the very guard meant to prevent build_llm from
        # ever crashing.
        pulled = {m.model for m in listing.models if m.model}
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
            "stand-in. Start Ollama, or set LEY_KHAA_OLLAMA_HOST, then restart the backend "
            "— the resolved client is cached for the life of the process, so fixing this "
            "does not take effect until then."
        )
        _ollama_client_cache = HeuristicLLM()
        return _ollama_client_cache

    # A prefix match is real: Ollama tags a pull by version (`qwen2.5:7b`),
    # and that is what most model cards tell you to pull, not the bare name.
    # But OllamaLLM must be built with the tag that was ACTUALLY pulled, not
    # the bare config name — the bare name resolves to `:latest`, which may
    # not be on disk, and every request would then fail forever with "model
    # not found" even though the probe passed and nothing was ever logged.
    # `pulled` is a set, and CPython randomizes string hashing per process, so
    # iterating it directly (`next(...)` over a generator expression) would
    # pick an arbitrary tag when several match the same configured prefix —
    # e.g. both qwen2.5:7b and qwen2.5:14b pulled under
    # LEY_KHAA_OLLAMA_MODEL=qwen2.5. That would make the manifest, and
    # therefore the provenance record this project sells as reproducible,
    # differ between two runs of an identically-configured system (and
    # invalidate Phase 7's vision cache on every flip, since it re-extracts
    # whenever the stored `model` differs from the current client's name).
    # Prefer an exact match, then the lexicographically first tag, so the
    # choice is the same every time.
    matched = model if model in pulled else next(
        iter(sorted(name for name in pulled if name.startswith(f"{model}:"))), None
    )
    if matched is None:
        _fall_back(
            f"LEY_KHAA_LLM=ollama and the daemon is reachable at {host}, but the model "
            f"{model!r} is not pulled — falling back to HeuristicLLM, the offline regex "
            f"stand-in. Fix with: ollama pull {model}, then restart the backend — the "
            "resolved client is cached for the life of the process, so fixing this does "
            "not take effect until then."
        )
        _ollama_client_cache = HeuristicLLM()
        return _ollama_client_cache

    _ollama_client_cache = OllamaLLM(model=matched, host=host)
    return _ollama_client_cache


def _fall_back(message: str) -> None:
    """Say it once. build_llm runs per request and per background sweep."""
    global _warned_about_fallback
    if not _warned_about_fallback:
        _warned_about_fallback = True
        logger.warning(message)
