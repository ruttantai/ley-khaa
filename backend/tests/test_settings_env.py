"""The settings contract: constructing Settings() reads the environment NOW.

Two properties, and the second is a rule this project states and did not keep.

1. `Settings()` re-reads os.environ on every construction. Before v1.0.0 the
   dataclass defaults were evaluated once, at class-definition time, so the only
   way to observe a changed variable was `importlib.reload(ley_khaa.config)` —
   which rebinds the module-level `settings` and leaks that new object into every
   later test (backlog item 25).
2. Every setting is falsy-safe: an environment variable set to "" falls back to
   the default. `docker-compose.yml` passes `${VAR:-}`, which SETS the variable to
   the empty string, so the two-argument `os.getenv(NAME, default)` form returns
   "" and the default never fires.
"""

import pytest

from ley_khaa.config import Settings

# (attribute, env var, value to set, expected attribute value)
_CASES = [
    ("llm_backend", "LEY_KHAA_LLM", "ollama", "ollama"),
    ("crystallizer_debounce_seconds", "LEY_KHAA_DEBOUNCE_SECONDS", "7", 7),
    ("sweep_interval_seconds", "LEY_KHAA_SWEEP_SECONDS", "3", 3),
    ("workspace_root", "LEY_KHAA_WORKSPACE_ROOT", "/tmp/ws", "/tmp/ws"),
    ("sandbox_backend", "LEY_KHAA_SANDBOX", "subprocess", "subprocess"),
    ("sandbox_timeout_seconds", "LEY_KHAA_SANDBOX_TIMEOUT", "5", 5),
    ("dispatch_mode", "LEY_KHAA_DISPATCH", "inline", "inline"),
    ("max_concurrent_projects", "LEY_KHAA_MAX_PROJECTS", "9", 9),
    ("ollama_model", "LEY_KHAA_OLLAMA_MODEL", "llama3.1", "llama3.1"),
]


@pytest.mark.parametrize("attr,var,value,expected", _CASES)
def test_a_setting_is_read_when_settings_is_constructed(monkeypatch, attr, var, value, expected):
    monkeypatch.setenv(var, value)
    assert getattr(Settings(), attr) == expected


@pytest.mark.parametrize("attr,var,value,expected", _CASES)
def test_a_setting_set_to_empty_falls_back_to_its_default(monkeypatch, attr, var, value, expected):
    """compose passes ${VAR:-}. An empty value means "unset", never "".

    Asserted against the default the class itself declares, so this test cannot
    drift out of date when a default changes.
    """
    monkeypatch.delenv(var, raising=False)
    default = getattr(Settings(), attr)
    monkeypatch.setenv(var, "")
    assert getattr(Settings(), attr) == default


def test_constructing_settings_does_not_mutate_the_module_singleton(monkeypatch):
    """The property whose absence was backlog item 25.

    `importlib.reload` was the only way to observe a changed variable, and it
    rebound `ley_khaa.config.settings` for every module that had already imported
    it. Constructing an instance must leave that singleton untouched.
    """
    import ley_khaa.config as config_module

    before = config_module.settings
    monkeypatch.setenv("LEY_KHAA_DISPATCH", "inline")
    fresh = Settings()

    assert fresh.dispatch_mode == "inline"
    assert config_module.settings is before


def test_every_string_and_int_setting_is_falsy_safe(monkeypatch):
    """The rule stated project-wide, asserted over EVERY field rather than a list.

    A per-field list is a list someone forgets to extend. This walks the dataclass
    itself, so a new field added with the two-argument os.getenv form fails here
    the day it lands.

    Each field's default is read with the variable UNSET, never from an ambient
    Settings(): `tests/conftest.py` exports LEY_KHAA_LLM, LEY_KHAA_DEBOUNCE_SECONDS,
    LEY_KHAA_SANDBOX and LEY_KHAA_DISPATCH, and the Postgres lane exports
    DATABASE_URL, so an ambient instance carries the test harness's values rather
    than the defaults this test is about.
    """
    import dataclasses

    for f in dataclasses.fields(Settings):
        var = _ENV_FOR.get(f.name)
        if var is None:
            continue
        monkeypatch.delenv(var, raising=False)
        default = getattr(Settings(), f.name)
        monkeypatch.setenv(var, "")
        assert getattr(Settings(), f.name) == default, (
            f"{f.name} is not falsy-safe: {var}='' must fall back to its default"
        )
        monkeypatch.delenv(var, raising=False)


# Every field that reads an environment variable, and which one. Kept beside the
# class it describes; the test above fails if a field here stops being falsy-safe.
_ENV_FOR = {
    "database_url": "DATABASE_URL",
    "llm_backend": "LEY_KHAA_LLM",
    "crystallizer_debounce_seconds": "LEY_KHAA_DEBOUNCE_SECONDS",
    "sweep_interval_seconds": "LEY_KHAA_SWEEP_SECONDS",
    "workspace_root": "LEY_KHAA_WORKSPACE_ROOT",
    "sandbox_backend": "LEY_KHAA_SANDBOX",
    "sandbox_image": "LEY_KHAA_SANDBOX_IMAGE",
    "sandbox_timeout_seconds": "LEY_KHAA_SANDBOX_TIMEOUT",
    "sandbox_memory_mb": "LEY_KHAA_SANDBOX_MEMORY_MB",
    "dispatch_mode": "LEY_KHAA_DISPATCH",
    "lease_ttl_seconds": "LEY_KHAA_LEASE_TTL",
    "lease_heartbeat_seconds": "LEY_KHAA_LEASE_HEARTBEAT",
    "max_concurrent_projects": "LEY_KHAA_MAX_PROJECTS",
    "max_lease_attempts": "LEY_KHAA_MAX_LEASE_ATTEMPTS",
    "slack_bot_token": "LEY_KHAA_SLACK_BOT_TOKEN",
    "slack_app_token": "LEY_KHAA_SLACK_APP_TOKEN",
    "slack_channels": "LEY_KHAA_SLACK_CHANNELS",
    "discord_bot_token": "LEY_KHAA_DISCORD_BOT_TOKEN",
    "discord_channels": "LEY_KHAA_DISCORD_CHANNELS",
    "image_hosts": "LEY_KHAA_IMAGE_HOSTS",
    "image_max_bytes": "LEY_KHAA_IMAGE_MAX_BYTES",
    "ollama_model": "LEY_KHAA_OLLAMA_MODEL",
    "ollama_host": "LEY_KHAA_OLLAMA_HOST",
    "dead_letter_max_rows": "LEY_KHAA_DEAD_LETTER_MAX_ROWS",
}


def test_disable_startup_is_true_only_for_exactly_one(monkeypatch):
    """Preserved verbatim from the pre-1.0.0 expression `os.getenv(...) == "1"`.

    A looser truthy check would make LEY_KHAA_DISABLE_STARTUP=0 disable startup,
    which is the opposite of what an operator typing 0 means.
    """
    monkeypatch.setenv("LEY_KHAA_DISABLE_STARTUP", "1")
    assert Settings().disable_startup is True
    for value in ("0", "", "true", "yes"):
        monkeypatch.setenv("LEY_KHAA_DISABLE_STARTUP", value)
        assert Settings().disable_startup is False, f"{value!r} must not disable startup"


def test_vision_is_on_unless_explicitly_off(monkeypatch):
    for value in ("", "on", "anything"):
        monkeypatch.setenv("LEY_KHAA_VISION", value)
        assert Settings().vision_enabled is True, f"{value!r} must leave vision on"
    monkeypatch.setenv("LEY_KHAA_VISION", "off")
    assert Settings().vision_enabled is False


def test_workspace_volume_is_none_rather_than_empty(monkeypatch):
    """DockerSandbox branches on `is None`; "" would take the wrong branch."""
    monkeypatch.setenv("LEY_KHAA_WORKSPACE_VOLUME", "")
    assert Settings().workspace_volume is None
    monkeypatch.setenv("LEY_KHAA_WORKSPACE_VOLUME", "vol")
    assert Settings().workspace_volume == "vol"
