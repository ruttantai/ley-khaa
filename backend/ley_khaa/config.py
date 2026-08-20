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


settings = Settings()
