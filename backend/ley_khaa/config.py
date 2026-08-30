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


settings = Settings()
