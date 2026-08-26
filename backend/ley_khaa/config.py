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


settings = Settings()
