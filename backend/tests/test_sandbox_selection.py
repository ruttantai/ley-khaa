from ley_khaa.executor.sandbox import DockerSandbox, SubprocessSandbox, pick_sandbox


def test_env_can_pin_the_subprocess_fallback(monkeypatch):
    monkeypatch.setattr("ley_khaa.executor.sandbox.settings.sandbox_backend", "subprocess")
    assert pick_sandbox().name == "subprocess"


def test_env_can_pin_docker_even_when_unavailable(monkeypatch):
    """An explicit pin must not be silently downgraded — that is how a reader
    ends up believing a bundle was isolated when it wasn't."""
    monkeypatch.setattr("ley_khaa.executor.sandbox.settings.sandbox_backend", "docker")
    monkeypatch.setattr(DockerSandbox, "available", lambda self: False)
    assert pick_sandbox().name == "docker"


def test_auto_prefers_docker_when_it_is_available(monkeypatch):
    monkeypatch.setattr("ley_khaa.executor.sandbox.settings.sandbox_backend", "auto")
    monkeypatch.setattr(DockerSandbox, "available", lambda self: True)
    assert pick_sandbox().name == "docker"


def test_auto_falls_back_when_docker_is_not_available(monkeypatch):
    monkeypatch.setattr("ley_khaa.executor.sandbox.settings.sandbox_backend", "auto")
    monkeypatch.setattr(DockerSandbox, "available", lambda self: False)
    assert pick_sandbox().name == "subprocess"


def test_the_named_volume_is_mounted_by_name(monkeypatch):
    """Under compose the backend is itself a container, so a bind mount of its
    own path would point at a host path that does not exist."""
    sandbox = DockerSandbox(image="img", volume="ley-khaa-task-workspaces", volume_target="/work/task-workspaces")
    args = sandbox._mount_args("/work/task-workspaces/task-1")
    assert args == [
        "--mount",
        "type=volume,source=ley-khaa-task-workspaces,target=/work/task-workspaces",
    ]


def test_a_plain_directory_is_bind_mounted_at_the_same_path(tmp_path):
    sandbox = DockerSandbox(image="img")
    args = sandbox._mount_args(str(tmp_path))
    assert args == ["-v", f"{tmp_path}:{tmp_path}"]
