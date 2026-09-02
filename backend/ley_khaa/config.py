import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql+psycopg://ley:ley@localhost:5432/leykhaa"
    )
    disable_startup: bool = os.getenv("LEY_KHAA_DISABLE_STARTUP") == "1"
    llm_backend: str = os.getenv("LEY_KHAA_LLM", "anthropic")
    crystallizer_debounce_seconds: int = int(os.getenv("LEY_KHAA_DEBOUNCE_SECONDS", "45"))
    # How often the background sweeper re-checks ready candidates. Wants to be
    # comfortably shorter than the debounce so a settled candidate promotes soon
    # after its quiet period elapses.
    sweep_interval_seconds: int = int(os.getenv("LEY_KHAA_SWEEP_SECONDS", "15"))
    # Where Output Bundles live (spec §5.11). Under compose this is a named
    # volume mounted at the SAME path in the backend and the sandbox container,
    # so a path is valid on both sides — see docker-compose.yml.
    workspace_root: str = os.getenv("LEY_KHAA_WORKSPACE_ROOT", "./task-workspaces")
    # "auto" picks Docker when a daemon answers and falls back otherwise.
    # "docker" / "subprocess" pin one explicitly. Tests pin subprocess.
    sandbox_backend: str = os.getenv("LEY_KHAA_SANDBOX", "auto")
    sandbox_image: str = os.getenv("LEY_KHAA_SANDBOX_IMAGE", "ley-khaa-sandbox")
    sandbox_timeout_seconds: int = int(os.getenv("LEY_KHAA_SANDBOX_TIMEOUT", "60"))
    sandbox_memory_mb: int = int(os.getenv("LEY_KHAA_SANDBOX_MEMORY_MB", "512"))
    # Set under compose so DockerSandbox mounts the named volume by name; a
    # sibling container cannot bind-mount the backend's own container paths.
    workspace_volume: str | None = os.getenv("LEY_KHAA_WORKSPACE_VOLUME") or None
    # "workers" runs tasks on the background dispatcher; "inline" drives them on
    # the calling thread, which is what every test and a single-operator CLI run
    # wants. See spec §3.4 — inline is a real supported mode, not a test shim.
    dispatch_mode: str = os.getenv("LEY_KHAA_DISPATCH", "workers")
    # How long a worker's claim on a task stays valid without a heartbeat. Long
    # enough that a slow sandbox run is not mistaken for a dead worker.
    lease_ttl_seconds: int = int(os.getenv("LEY_KHAA_LEASE_TTL", "120"))
    lease_heartbeat_seconds: int = int(os.getenv("LEY_KHAA_LEASE_HEARTBEAT", "30"))
    # How many projects may run at once. Each one is a full lane: two Opus calls
    # and a sandbox run.
    max_concurrent_projects: int = int(os.getenv("LEY_KHAA_MAX_PROJECTS", "4"))
    # Past this many reclaims of an expired lease, a task is poison and fails
    # visibly rather than being re-run forever.
    max_lease_attempts: int = int(os.getenv("LEY_KHAA_MAX_LEASE_ATTEMPTS", "3"))

    # --- channel adapters (spec §5). Tokens come from the environment only:
    # never committed, never logged, never written into a bundle, never
    # returned by an API. No token -> that adapter does not start, which is
    # what keeps `docker compose up` a zero-account demo.
    slack_bot_token: str = os.getenv("LEY_KHAA_SLACK_BOT_TOKEN", "")
    slack_app_token: str = os.getenv("LEY_KHAA_SLACK_APP_TOKEN", "")
    # Comma-separated channel ids. EMPTY MEANS INGEST NOTHING, never "ingest
    # everything": being invited to a channel is not consent to read it
    # (decision #4), and an adapter with a token and an empty allowlist starts,
    # ingests nothing, and says so — the safe reading of an incomplete
    # configuration.
    slack_channels: str = os.getenv("LEY_KHAA_SLACK_CHANNELS", "")
    discord_bot_token: str = os.getenv("LEY_KHAA_DISCORD_BOT_TOKEN", "")
    discord_channels: str = os.getenv("LEY_KHAA_DISCORD_CHANNELS", "")

    # Vision intake (spec §5 of the phase 7 design). Off means an image is
    # carried but not read, which is the same shape as having no API key.
    vision_enabled: bool = os.getenv("LEY_KHAA_VISION", "on") != "off"
    # Exact hostnames an image may be fetched from. Parsed with
    # adapters.base.channel_set, the same helper the channel allowlists use —
    # one parser, so "empty means empty" cannot drift between them.
    image_hosts: str = os.getenv("LEY_KHAA_IMAGE_HOSTS") or (
        "files.slack.com,cdn.discordapp.com,media.discordapp.net"
    )
    image_max_bytes: int = int(os.getenv("LEY_KHAA_IMAGE_MAX_BYTES") or str(5 * 1024 * 1024))

    # Ollama offline fallback (phase 8 design §4). The local model is used for
    # EVERY stage: the router's Claude model id is ignored, because someone
    # running Ollama typically has exactly one model pulled and requiring two
    # is friction on the very path this exists to serve.
    ollama_model: str = os.getenv("LEY_KHAA_OLLAMA_MODEL") or "qwen2.5"
    ollama_host: str = os.getenv("LEY_KHAA_OLLAMA_HOST") or "http://localhost:11434"


settings = Settings()
