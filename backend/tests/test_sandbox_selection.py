from dataclasses import replace

import pytest

from ley_khaa.config import settings as real_settings
from ley_khaa.executor import sandbox as sandbox_module
from ley_khaa.executor.sandbox import (
    DockerSandbox,
    SandboxUnavailable,
    SubprocessSandbox,
    pick_sandbox,
)


def _pin(monkeypatch, backend: str) -> None:
    """Swap the whole settings object rather than mutating the shared global
    in place — Settings is frozen (a Phase 0 invariant), and rebinding the
    module-level name is both what frozen requires and better test hygiene:
    nothing leaks across tests even if monkeypatch's undo is ever bypassed."""
    monkeypatch.setattr(sandbox_module, "settings", replace(real_settings, sandbox_backend=backend))


def test_env_can_pin_the_subprocess_fallback(monkeypatch):
    _pin(monkeypatch, "subprocess")
    assert pick_sandbox().name == "subprocess"


def test_env_can_pin_docker_even_when_unavailable(monkeypatch):
    """An explicit pin must not be silently downgraded — that is how a reader
    ends up believing a bundle was isolated when it wasn't."""
    _pin(monkeypatch, "docker")
    monkeypatch.setattr(DockerSandbox, "available", lambda self: False)
    assert pick_sandbox().name == "docker"


def test_auto_prefers_docker_when_it_is_available(monkeypatch):
    _pin(monkeypatch, "auto")
    monkeypatch.setattr(DockerSandbox, "available", lambda self: True)
    assert pick_sandbox().name == "docker"


def test_auto_falls_back_when_docker_is_not_available(monkeypatch):
    _pin(monkeypatch, "auto")
    monkeypatch.setattr(DockerSandbox, "available", lambda self: False)
    assert pick_sandbox().name == "subprocess"


def _volume_sandbox() -> DockerSandbox:
    return DockerSandbox(
        image="img", volume="ley-khaa-task-workspaces", volume_target="/work/task-workspaces"
    )


def test_the_named_volume_is_mounted_by_name_and_scoped_to_one_task(monkeypatch):
    """Under compose the backend is itself a container, so a bind mount of its
    own path would point at a host path that does not exist — hence the volume
    by name. volume-subpath is what keeps this run to its OWN bundle: mounting
    the whole volume put every other task's manifest.json, generator/ and
    deliverable/ in reach of a script, writable."""
    args = _volume_sandbox()._mount_args("/work/task-workspaces/task-1")
    assert args == [
        "--mount",
        "type=volume,source=ley-khaa-task-workspaces,"
        "target=/work/task-workspaces/task-1,volume-subpath=task-1",
    ]


@pytest.mark.parametrize(
    "workspace", ["/work/task-workspaces", "/somewhere/else/task-1", "/work"]
)
def test_a_workspace_outside_the_volume_is_refused_not_widened(workspace):
    """The only mount that could still work is the whole volume, which is the
    exposure this replaced. Refusing is the honest move: SandboxUnavailable
    fails the task as ley-khaa's own fault rather than running it with a view of
    every other bundle."""
    with pytest.raises(SandboxUnavailable):
        _volume_sandbox()._mount_args(workspace)


def test_the_sandbox_refuses_to_run_as_root(monkeypatch, tmp_path):
    """The container inherits our uid, so a root backend means a root sandbox —
    which is not what the README, the CHANGELOG or spec §4.1 describe."""
    monkeypatch.setattr(sandbox_module.os, "getuid", lambda: 0)
    with pytest.raises(SandboxUnavailable, match="root"):
        DockerSandbox(image="img").run(
            script=tmp_path / "attempt_1.py", workspace=tmp_path, timeout_s=5
        )


def test_a_plain_directory_is_bind_mounted_at_the_same_path(tmp_path):
    sandbox = DockerSandbox(image="img")
    args = sandbox._mount_args(str(tmp_path))
    assert args == ["-v", f"{tmp_path}:{tmp_path}"]
