import logging
import os
from dataclasses import dataclass, field
from functools import partial

logger = logging.getLogger(__name__)


def _tolerant_int(name: str, default: int) -> int:
    """An int setting whose typo must not stop the service.

    The file's other int settings use a bare `int(os.getenv(...))`, which
    raises `ValueError` during `import ley_khaa.config` — before logging is
    configured, so the operator gets a traceback and no service. That is the
    right posture for most of them and it is deliberately left alone.

    `dead_letter_max_rows` is the exception, for the same reason its zero is
    clamped rather than rejected (`DeadLetterRepository._prune`): it is a
    RETENTION cap read from inside the notifier's own exception handlers, and
    the ruling that put the clamp there was explicit that a misconfigured
    retention cap must never stop the service. Rejecting `lots` at import
    while clamping `0` at use would be two opposite answers to the same
    operator mistake, two lines apart. So a value that is not an integer
    falls back to the default, loudly.
    """
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        # No logging configuration exists yet at import time, so this goes to
        # the lastResort handler on stderr. Visible is the requirement; a
        # configured sink is a bonus.
        logger.warning("%s=%r is not an integer; falling back to %d", name, raw, default)
        return default


def _env_str(name: str, default: str) -> str:
    """A string setting, read at call time and falsy-safe.

    `os.getenv(name, default)` is wrong here: docker-compose.yml passes
    `${VAR:-}`, which SETS the variable to the empty string rather than leaving
    it unset, so the two-argument form returns "" and the default never fires.
    """
    return os.getenv(name) or default


def _env_int(name: str, default: int) -> int:
    """An int setting, read at call time and falsy-safe.

    Deliberately NOT tolerant of a non-integer: `int("lots")` raises during
    import, which is the right posture for a setting whose wrong value would
    silently change behaviour. `_tolerant_int` documents the one exception.
    """
    return int(os.getenv(name) or default)


def _env_bool(name: str, default: bool, *, false_value: str) -> bool:
    """A tri-state flag: unset or "" means the default, `false_value` means False.

    Kept explicit rather than parsing a general truthy set, because the two flags
    that use it have opposite defaults and opposite spellings — LEY_KHAA_VISION
    defaults ON and is disabled by "off"; LEY_KHAA_DISABLE_STARTUP defaults OFF
    and is enabled by "1".
    """
    raw = os.getenv(name)
    if not raw:
        return default
    return raw != false_value


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=partial(
            _env_str, "DATABASE_URL", "postgresql+psycopg://ley:ley@localhost:5432/leykhaa"
        )
    )
    # True for exactly "1" and nothing else. A looser truthy check would make
    # LEY_KHAA_DISABLE_STARTUP=0 *disable* startup, the opposite of what an
    # operator typing 0 means — so this keeps its own factory rather than being
    # forced into `_env_bool`.
    disable_startup: bool = field(
        default_factory=lambda: os.getenv("LEY_KHAA_DISABLE_STARTUP") == "1"
    )
    llm_backend: str = field(default_factory=partial(_env_str, "LEY_KHAA_LLM", "anthropic"))
    crystallizer_debounce_seconds: int = field(
        default_factory=partial(_env_int, "LEY_KHAA_DEBOUNCE_SECONDS", 45)
    )
    # How often the background sweeper re-checks ready candidates. Wants to be
    # comfortably shorter than the debounce so a settled candidate promotes soon
    # after its quiet period elapses.
    sweep_interval_seconds: int = field(
        default_factory=partial(_env_int, "LEY_KHAA_SWEEP_SECONDS", 15)
    )
    # Where Output Bundles live (spec §5.11). Under compose this is a named
    # volume mounted at the SAME path in the backend and the sandbox container,
    # so a path is valid on both sides — see docker-compose.yml.
    workspace_root: str = field(
        default_factory=partial(_env_str, "LEY_KHAA_WORKSPACE_ROOT", "./task-workspaces")
    )
    # "auto" picks Docker when a daemon answers and falls back otherwise.
    # "docker" / "subprocess" pin one explicitly. Tests pin subprocess.
    sandbox_backend: str = field(default_factory=partial(_env_str, "LEY_KHAA_SANDBOX", "auto"))
    sandbox_image: str = field(
        default_factory=partial(_env_str, "LEY_KHAA_SANDBOX_IMAGE", "ley-khaa-sandbox")
    )
    sandbox_timeout_seconds: int = field(
        default_factory=partial(_env_int, "LEY_KHAA_SANDBOX_TIMEOUT", 60)
    )
    sandbox_memory_mb: int = field(
        default_factory=partial(_env_int, "LEY_KHAA_SANDBOX_MEMORY_MB", 512)
    )
    # Set under compose so DockerSandbox mounts the named volume by name; a
    # sibling container cannot bind-mount the backend's own container paths.
    # None rather than "": both call sites (sandbox.py:304, :440) branch on
    # falsiness, so "" and None behave identically today. None is kept because
    # it is the honest value for "unset" under the declared `str | None`, and
    # does not rely on every future caller staying with a falsy check.
    workspace_volume: str | None = field(
        default_factory=lambda: os.getenv("LEY_KHAA_WORKSPACE_VOLUME") or None
    )
    # "workers" runs tasks on the background dispatcher; "inline" drives them on
    # the calling thread, which is what every test and a single-operator CLI run
    # wants. See spec §3.4 — inline is a real supported mode, not a test shim.
    dispatch_mode: str = field(default_factory=partial(_env_str, "LEY_KHAA_DISPATCH", "workers"))
    # How long a worker's claim on a task stays valid without a heartbeat. Long
    # enough that a slow sandbox run is not mistaken for a dead worker.
    lease_ttl_seconds: int = field(default_factory=partial(_env_int, "LEY_KHAA_LEASE_TTL", 120))
    lease_heartbeat_seconds: int = field(
        default_factory=partial(_env_int, "LEY_KHAA_LEASE_HEARTBEAT", 30)
    )
    # How many projects may run at once. Each one is a full lane: two Opus calls
    # and a sandbox run.
    max_concurrent_projects: int = field(
        default_factory=partial(_env_int, "LEY_KHAA_MAX_PROJECTS", 4)
    )
    # Past this many reclaims of an expired lease, a task is poison and fails
    # visibly rather than being re-run forever.
    max_lease_attempts: int = field(
        default_factory=partial(_env_int, "LEY_KHAA_MAX_LEASE_ATTEMPTS", 3)
    )

    # --- channel adapters (spec §5). Tokens come from the environment only:
    # never committed, never logged, never written into a bundle, never
    # returned by an API. No token -> that adapter does not start, which is
    # what keeps `docker compose up` a zero-account demo.
    slack_bot_token: str = field(default_factory=partial(_env_str, "LEY_KHAA_SLACK_BOT_TOKEN", ""))
    slack_app_token: str = field(default_factory=partial(_env_str, "LEY_KHAA_SLACK_APP_TOKEN", ""))
    # Comma-separated channel ids. EMPTY MEANS INGEST NOTHING, never "ingest
    # everything": being invited to a channel is not consent to read it
    # (decision #4), and an adapter with a token and an empty allowlist starts,
    # ingests nothing, and says so — the safe reading of an incomplete
    # configuration.
    slack_channels: str = field(default_factory=partial(_env_str, "LEY_KHAA_SLACK_CHANNELS", ""))
    discord_bot_token: str = field(
        default_factory=partial(_env_str, "LEY_KHAA_DISCORD_BOT_TOKEN", "")
    )
    discord_channels: str = field(default_factory=partial(_env_str, "LEY_KHAA_DISCORD_CHANNELS", ""))

    # Vision intake (spec §5 of the phase 7 design). Off means an image is
    # carried but not read, which is the same shape as having no API key.
    # Tri-state: "off" disables; anything else — including "" — leaves it on.
    vision_enabled: bool = field(
        default_factory=partial(_env_bool, "LEY_KHAA_VISION", True, false_value="off")
    )
    # Exact hostnames an image may be fetched from. Parsed with
    # adapters.base.channel_set, the same helper the channel allowlists use —
    # one parser, so "empty means empty" cannot drift between them.
    image_hosts: str = field(
        default_factory=partial(
            _env_str,
            "LEY_KHAA_IMAGE_HOSTS",
            "files.slack.com,cdn.discordapp.com,media.discordapp.net",
        )
    )
    image_max_bytes: int = field(
        default_factory=partial(_env_int, "LEY_KHAA_IMAGE_MAX_BYTES", 5 * 1024 * 1024)
    )

    # Ollama offline fallback (phase 8 design §4). The local model is used for
    # EVERY stage: the router's Claude model id is ignored, because someone
    # running Ollama typically has exactly one model pulled and requiring two
    # is friction on the very path this exists to serve.
    ollama_model: str = field(default_factory=partial(_env_str, "LEY_KHAA_OLLAMA_MODEL", "qwen2.5"))
    ollama_host: str = field(
        default_factory=partial(_env_str, "LEY_KHAA_OLLAMA_HOST", "http://localhost:11434")
    )

    # Bounds `dead_letters` row COUNT (backlog item 18) — MAX_PAYLOAD_CHARS
    # already bounds a row's SIZE. A permanently bad token writes one
    # `connection` row per minute at the supervisor's 60s backoff cap,
    # forever; this is what stops that from being unbounded growth. Count-
    # based, not time-based: a quiet system with a handful of old dead
    # letters keeps every one of them. 1000 is ~10x the dashboard's default
    # page size (see DeadLetterRepository.list), generous enough to
    # investigate an incident without holding history forever. An operator
    # can raise it.
    dead_letter_max_rows: int = field(
        default_factory=partial(_tolerant_int, "LEY_KHAA_DEAD_LETTER_MAX_ROWS", 1000)
    )


settings = Settings()
