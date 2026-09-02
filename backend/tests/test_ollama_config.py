import importlib

from ley_khaa import config as config_module


def _reload():
    return importlib.reload(config_module).Settings()


def test_the_defaults_are_a_local_daemon_and_a_named_model():
    s = config_module.Settings()
    assert s.ollama_model == "qwen2.5"
    assert s.ollama_host == "http://localhost:11434"


def test_an_empty_model_env_var_falls_back_to_the_default(monkeypatch):
    """compose passes ${VAR:-}, which SETS the variable to "". The two-argument
    os.getenv form would return "" here and the default would never fire."""
    monkeypatch.setenv("LEY_KHAA_OLLAMA_MODEL", "")
    try:
        assert _reload().ollama_model == "qwen2.5"
    finally:
        monkeypatch.delenv("LEY_KHAA_OLLAMA_MODEL", raising=False)
        importlib.reload(config_module)


def test_an_empty_host_env_var_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("LEY_KHAA_OLLAMA_HOST", "")
    try:
        assert _reload().ollama_host == "http://localhost:11434"
    finally:
        monkeypatch.delenv("LEY_KHAA_OLLAMA_HOST", raising=False)
        importlib.reload(config_module)


def test_the_env_vars_are_actually_read(monkeypatch):
    monkeypatch.setenv("LEY_KHAA_OLLAMA_MODEL", "llama3.1")
    monkeypatch.setenv("LEY_KHAA_OLLAMA_HOST", "http://ollama:11434")
    try:
        s = _reload()
        assert s.ollama_model == "llama3.1"
        assert s.ollama_host == "http://ollama:11434"
    finally:
        monkeypatch.delenv("LEY_KHAA_OLLAMA_MODEL", raising=False)
        monkeypatch.delenv("LEY_KHAA_OLLAMA_HOST", raising=False)
        importlib.reload(config_module)
