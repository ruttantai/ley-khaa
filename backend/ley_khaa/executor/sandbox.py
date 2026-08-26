"""Where synthesized code actually runs (spec §5.10, decision 3).

Two implementations behind one protocol. DockerSandbox is the real thing and
the default. SubprocessSandbox keeps the Docker-free dev loop working and is
weaker in a way it is loud about: it cannot take the network away. The manifest
records which one ran, so a bundle never overstates its own isolation.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import settings

logger = logging.getLogger(__name__)

# Everything else is stripped. A synthesized script has no business reading our
# API key, and an allowlist means a credential added later is excluded by
# default rather than leaking until someone notices.
_ALLOWED_ENV = ("PATH", "LANG", "LC_ALL", "TZ")


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class SandboxUnavailable(Exception):
    """The sandbox itself could not run.

    Infrastructure, not the script — the caller must treat this as a failure of
    ley-khaa, never as a question to put to a human.
    """


class SandboxRunner(Protocol):
    name: str

    def run(self, *, script: Path, workspace: Path, timeout_s: int) -> SandboxResult:
        ...


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _clean_env(workspace: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in _ALLOWED_ENV if key in os.environ}
    env["HOME"] = str(workspace)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


class SubprocessSandbox:
    """Fallback for when no Docker daemon answers.

    Weaker than DockerSandbox: it limits CPU and memory and scrubs the
    environment, but it CANNOT remove network access. Callers announce that.
    """

    name = "subprocess"

    def __init__(self, *, memory_mb: int = 512) -> None:
        self.memory_mb = memory_mb

    def _limits(self, timeout_s: int):
        if os.name != "posix":
            return None
        memory_mb = self.memory_mb

        def apply() -> None:
            import resource

            # A generous headroom over the wall-clock timeout: RLIMIT_CPU is a
            # backstop for a script that burns CPU while blocked on something
            # subprocess.run's own timeout won't catch as fast (e.g. heavy
            # non-interruptible C extension work); it must not race the wall
            # clock and win, or a run that subprocess.run would report as
            # `timed_out` instead exits via SIGXCPU and gets misreported as an
            # ordinary crash.
            cpu_limit = timeout_s + 5
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
            # RLIMIT_AS is deliberately skipped on macOS: numpy and pandas
            # reserve large VIRTUAL address ranges at import time, so an
            # address-space cap spuriously MemoryErrors there while saying
            # nothing about real memory use. The wall-clock timeout and
            # RLIMIT_CPU still apply.
            if sys.platform != "darwin":
                limit = memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))

        return apply

    def run(self, *, script: Path, workspace: Path, timeout_s: int) -> SandboxResult:
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                [sys.executable, str(script)],
                cwd=str(workspace),
                env=_clean_env(workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=self._limits(timeout_s),
                # A session of its own means its pid is also its process
                # group id, so killpg on timeout reaches every grandchild the
                # script spawned too — a lone process.kill() would leave them
                # running with the network access this fallback can never
                # take away.
                start_new_session=True,
            )
        except OSError as exc:  # no interpreter, no permission: our problem
            raise SandboxUnavailable(f"could not start the sandbox: {exc}") from exc

        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self._kill_group(proc)
            # Draining after the kill both reaps the process and collects
            # whatever it had already written before being killed.
            stdout, stderr = proc.communicate()
            return SandboxResult(
                exit_code=-1,
                stdout=_text(stdout),
                stderr=_text(stderr) + f"\nkilled after {timeout_s}s",
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
            )
        except BaseException:
            # Whatever went wrong mid-run, the process group must not outlive
            # this call — an orphaned grandchild is the same leak as a missed
            # timeout kill, just reached by a different door.
            self._kill_group(proc)
            raise

        return SandboxResult(
            exit_code=proc.returncode,
            stdout=_text(stdout),
            stderr=_text(stderr),
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=False,
        )

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass  # the group already exited on its own


class DockerSandbox:
    """The real thing (spec §5.10): no network, read-only rootfs, capped."""

    name = "docker"

    def __init__(
        self,
        *,
        image: str,
        memory_mb: int = 512,
        volume: str | None = None,
        volume_target: str | None = None,
    ) -> None:
        self.image = image
        self.memory_mb = memory_mb
        self.volume = volume
        self.volume_target = volume_target

    def available(self) -> bool:
        """True only if a daemon answers AND our image exists.

        Checking the image too is what lets "auto" fall back cleanly on a
        machine that has Docker but has never built the sandbox, instead of
        failing every task with an obscure `docker run` error.
        """
        for command in (["docker", "info"], ["docker", "image", "inspect", self.image]):
            try:
                completed = subprocess.run(command, capture_output=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                return False
            if completed.returncode != 0:
                return False
        return True

    def _mount_args(self, workspace: str) -> list[str]:
        if self.volume and self.volume_target:
            # Under compose the backend is itself a container and spawns SIBLING
            # containers on the host daemon, so a bind mount of the backend's own
            # container path would resolve to a host path that does not exist.
            # The workspace therefore lives on a named volume mounted at the SAME
            # path on both sides, and paths line up without translation.
            return [
                "--mount",
                f"type=volume,source={self.volume},target={self.volume_target}",
            ]
        return ["-v", f"{workspace}:{workspace}"]

    def run(self, *, script: Path, workspace: Path, timeout_s: int) -> SandboxResult:
        workspace = workspace.resolve()
        container = f"ley-khaa-{uuid.uuid4().hex[:12]}"
        command = [
            "docker", "run", "--rm",
            "--name", container,
            # The §5.10 guarantee: synthesized code reaches nothing.
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:size=64m,exec",
            "--memory", f"{self.memory_mb}m",
            "--cpus", "1",
            "--pids-limit", "64",
            # Run as the caller so the deliverable is owned by us and readable
            # back out of a bind-mounted workspace.
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/tmp",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            *self._mount_args(str(workspace)),
            "-w", str(workspace),
            self.image,
            "python", str(script.resolve()),
        ]
        started = time.monotonic()
        try:
            # No text=True: docker's stdout/stderr are captured as bytes and
            # decoded through _text(), same as SubprocessSandbox above. A
            # synthesized script can write arbitrary bytes to stdout, and a
            # strict decode there must not raise UnicodeDecodeError out of
            # run() — that would turn a script's output into a sandbox crash.
            completed = subprocess.run(command, capture_output=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            # docker run --rm leaves the container going after we stop waiting.
            subprocess.run(["docker", "kill", container], capture_output=True)
            return SandboxResult(
                exit_code=-1,
                stdout=_text(exc.stdout),
                stderr=_text(exc.stderr) + f"\nkilled after {timeout_s}s",
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
            )
        except OSError as exc:
            raise SandboxUnavailable(f"could not invoke docker: {exc}") from exc

        # 125 is docker's own "could not start the container" code. That is our
        # infrastructure failing, not the script failing, and the two must not be
        # confused: one is a bug report, the other is a question for a human.
        if completed.returncode == 125:
            raise SandboxUnavailable(
                f"docker could not start the sandbox: {_text(completed.stderr)}"
            )

        return SandboxResult(
            exit_code=completed.returncode,
            stdout=_text(completed.stdout),
            stderr=_text(completed.stderr),
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=False,
        )


_warned_about_fallback = False


def pick_sandbox() -> SandboxRunner:
    """Docker unless we cannot, subprocess when we must, never a silent swap."""
    subprocess_sandbox = SubprocessSandbox(memory_mb=settings.sandbox_memory_mb)
    if settings.sandbox_backend == "subprocess":
        return subprocess_sandbox

    docker = DockerSandbox(
        image=settings.sandbox_image,
        memory_mb=settings.sandbox_memory_mb,
        volume=settings.workspace_volume,
        volume_target=settings.workspace_root if settings.workspace_volume else None,
    )
    if settings.sandbox_backend == "docker":
        # An explicit pin is never downgraded: quietly running somewhere weaker
        # than the operator asked for is how a reader ends up trusting a bundle
        # that was never isolated.
        return docker
    if docker.available():
        return docker

    global _warned_about_fallback
    if not _warned_about_fallback:
        _warned_about_fallback = True
        logger.warning(
            "No usable Docker sandbox (daemon or image %s missing) — falling back to "
            "SubprocessSandbox. Synthesized scripts will run on this machine's "
            "interpreter with CPU and memory caps and a scrubbed environment, but "
            "WITHOUT network isolation. The manifest records this.",
            settings.sandbox_image,
        )
    return subprocess_sandbox
