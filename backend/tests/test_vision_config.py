from dataclasses import replace

from ley_khaa.adapters.base import channel_set
from ley_khaa.config import Settings


def test_vision_is_on_by_default():
    assert Settings().vision_enabled is True


def test_vision_can_be_turned_off():
    assert replace(Settings(), vision_enabled=False).vision_enabled is False


def test_the_default_host_allowlist_covers_both_platforms():
    hosts = channel_set(Settings().image_hosts)
    assert "files.slack.com" in hosts
    assert "cdn.discordapp.com" in hosts
    assert "media.discordapp.net" in hosts


def test_the_allowlist_parses_with_the_same_helper_channels_use():
    """One parser for every comma-separated allowlist in the project, so the
    empty-means-empty rule cannot drift between them."""
    assert channel_set("a.example, b.example") == frozenset({"a.example", "b.example"})
    assert channel_set("") == frozenset()


def test_the_byte_cap_has_a_sane_default():
    assert Settings().image_max_bytes == 5 * 1024 * 1024


def test_an_empty_env_var_does_not_silently_empty_the_allowlist(monkeypatch):
    """docker-compose passes ${VAR:-} for an unset variable, which arrives as
    "". If that beat the default, every image fetch would be refused and the
    only symptom would be dead letters."""
    monkeypatch.setenv("LEY_KHAA_IMAGE_HOSTS", "")
    from importlib import reload

    import ley_khaa.config as config_module

    reload(config_module)
    try:
        assert channel_set(config_module.Settings().image_hosts) != frozenset(), (
            "an empty LEY_KHAA_IMAGE_HOSTS must fall back to the default, "
            "or compose's ${VAR:-} silently disables all image fetching"
        )
    finally:
        monkeypatch.delenv("LEY_KHAA_IMAGE_HOSTS", raising=False)
        reload(config_module)


def test_an_empty_max_bytes_env_var_falls_back_to_the_default(monkeypatch):
    """docker-compose passes ${VAR:-} for an unset variable, which arrives as
    "". A two-argument os.getenv default never fires for a SET-but-empty
    variable, so a naive read would crash the whole process at import time
    with ValueError: invalid literal for int() with base 10: ''."""
    monkeypatch.setenv("LEY_KHAA_IMAGE_MAX_BYTES", "")
    from importlib import reload

    import ley_khaa.config as config_module

    try:
        reload(config_module)
        assert config_module.Settings().image_max_bytes == 5 * 1024 * 1024
    finally:
        monkeypatch.delenv("LEY_KHAA_IMAGE_MAX_BYTES", raising=False)
        reload(config_module)


def test_a_set_max_bytes_env_var_is_honored(monkeypatch):
    monkeypatch.setenv("LEY_KHAA_IMAGE_MAX_BYTES", "1024")
    from importlib import reload

    import ley_khaa.config as config_module

    try:
        reload(config_module)
        assert config_module.Settings().image_max_bytes == 1024
    finally:
        monkeypatch.delenv("LEY_KHAA_IMAGE_MAX_BYTES", raising=False)
        reload(config_module)


def test_vision_can_be_turned_off_via_the_environment(monkeypatch):
    """test_vision_can_be_turned_off only proves the field is a settable
    dataclass attribute via `replace` — it never exercises the
    os.getenv(...) != "off" line at all. This drives it through the env var
    the way an operator actually would."""
    monkeypatch.setenv("LEY_KHAA_VISION", "off")
    from importlib import reload

    import ley_khaa.config as config_module

    try:
        reload(config_module)
        assert config_module.Settings().vision_enabled is False
    finally:
        monkeypatch.delenv("LEY_KHAA_VISION", raising=False)
        reload(config_module)
