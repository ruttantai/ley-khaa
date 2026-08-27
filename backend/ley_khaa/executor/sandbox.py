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
import threading
import time
import uuid
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..config import settings

try:
    # Imported HERE and not inside preexec_fn. An import between fork() and
    # exec() runs in a child that inherited this process's locks — including the
    # import lock — from a process that runs a background sweeper thread, which
    # is the documented post-fork deadlock. If it ever bit, the child would hang,
    # subprocess.run would time out, and the manifest would record ley-khaa's own
    # deadlock as "the generated script ran too long".
    import resource
except ImportError:  # not POSIX; _limits() already returns None there
    resource = None

logger = logging.getLogger(__name__)

# A synthesized script decides how much it prints, and BOTH sandboxes buffer
# that output in this process — the container's --memory cap bounds the script's
# RAM, not the backend's. Keep this much of each stream and say so when more
# arrived.
_MAX_CAPTURE_BYTES = 256_000

# A grandchild that survived the kill can hold the write end of a pipe open. The
# run still has to return a result, so the drain threads are waited on, not
# waited for forever.
_DRAIN_JOIN_SECONDS = 5

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


class _Tail:
    """The last _MAX_CAPTURE_BYTES of a stream, plus how much was dropped.

    The TAIL and not the head: the traceback a repair attempt is prompted with
    is written last, so a print-flood must not be allowed to push it out.
    """

    def __init__(self) -> None:
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._dropped = 0

    def add(self, chunk: bytes) -> None:
        self._chunks.append(chunk)
        self._size += len(chunk)
        while self._size > _MAX_CAPTURE_BYTES and len(self._chunks) > 1:
            oldest = self._chunks.popleft()
            self._size -= len(oldest)
            self._dropped += len(oldest)

    def text(self) -> str:
        body = _text(b"".join(self._chunks))
        if not self._dropped:
            return body
        # Truncation is never silent: a bundle that quietly shortened its own
        # evidence is a bundle overstating what it holds.
        return f"[ley-khaa dropped the first {self._dropped} bytes of this stream]\n{body}"


class _Capture:
    """Drain a child's stdout and stderr on threads, keeping only the tail.

    Draining does not stop at the cap. Refusing to read would block the script
    on a full pipe, and the wall-clock timeout would then record ley-khaa's own
    back-pressure as "the generated script ran too long."
    """

    def __init__(self, stdout, stderr) -> None:
        self._streams = (stdout, stderr)
        self._tails = (_Tail(), _Tail())
        self._threads = [
            threading.Thread(target=self._pump, args=(stream, tail), daemon=True)
            for stream, tail in zip(self._streams, self._tails, strict=True)
        ]
        for thread in self._threads:
            thread.start()

    @staticmethod
    def _pump(stream, tail: _Tail) -> None:
        try:
            while chunk := stream.read(65536):
                tail.add(chunk)
        except (OSError, ValueError):
            return  # the pipe was closed under us; keep what we already have

    def finish(self) -> tuple[str, str]:
        for thread in self._threads:
            thread.join(timeout=_DRAIN_JOIN_SECONDS)
        for stream in self._streams:
            with suppress(OSError):
                stream.close()
        return self._tails[0].text(), self._tails[1].text()


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
        if os.name != "posix" or resource is None:
            return None
        memory_mb = self.memory_mb

        def apply() -> None:
            # Nothing is imported in here — see the module-level `import
            # resource` and why it lives there.
            #
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

        # Threads, not communicate(): communicate() keeps every byte the script
        # writes. wait() alone would deadlock on a full pipe, so the capture
        # drains continuously and throws away all but the tail.
        capture = _Capture(proc.stdout, proc.stderr)
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self._kill_group(proc)
            proc.wait()  # reap, and let the capture threads see EOF
            stdout, stderr = capture.finish()
            return SandboxResult(
                exit_code=-1,
                stdout=stdout,
                stderr=stderr + f"\nkilled after {timeout_s}s",
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
            )
        except BaseException:
            # Whatever went wrong mid-run, the process group must not outlive
            # this call — an orphaned grandchild is the same leak as a missed
            # timeout kill, just reached by a different door.
            self._kill_group(proc)
            capture.finish()
            raise

        stdout, stderr = capture.finish()
        return SandboxResult(
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
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
        if not (self.volume and self.volume_target):
            return ["-v", f"{workspace}:{workspace}"]

        # Under compose the backend is itself a container and spawns SIBLING
        # containers on the host daemon, so a bind mount of the backend's own
        # container path would resolve to a host path that does not exist. The
        # workspace therefore lives on a named volume mounted at the SAME path
        # on both sides, and paths line up without translation.
        #
        # volume-subpath narrows that to this ONE task's bundle. Mounting the
        # whole volume put every other task's manifest.json, generator/ and
        # deliverable/ inside a script's reach, writable — and the synthesis
        # prompt quotes attachment content verbatim, so what that script does is
        # partly steered by whoever sent the attachment.
        try:
            subpath = Path(workspace).relative_to(self.volume_target)
        except ValueError:
            subpath = None
        if subpath is None or subpath == Path("."):
            # Not a fall back to the whole volume: that is exactly the exposure
            # this replaced, and quietly widening a run's reach is how a bundle
            # ends up claiming isolation it never had.
            raise SandboxUnavailable(
                f"{workspace} is not a task directory inside the volume mounted "
                f"at {self.volume_target}, so this run cannot be given a view of "
                f"its own bundle alone"
            )
        return [
            "--mount",
            f"type=volume,source={self.volume},target={workspace},volume-subpath={subpath}",
        ]

    @staticmethod
    def _refuse_root() -> None:
        """The sandbox container inherits OUR uid, so a root backend means a root
        sandbox — which is not the thing the README and the manifest describe.

        Refused rather than substituted: running the container as some other uid
        would leave it unable to write into a workspace root created, and owned,
        by root. Under compose backend/docker-entrypoint.py drops privileges
        before uvicorn starts, so this fires only when someone has arranged for
        the backend to be root some other way — and it fails the task loudly
        instead of quietly producing a bundle that overstates its isolation.
        """
        if os.getuid() == 0:
            raise SandboxUnavailable(
                "the backend is running as root, so the sandbox container would "
                "run as root too; start it as an unprivileged user"
            )

    def run(self, *, script: Path, workspace: Path, timeout_s: int) -> SandboxResult:
        self._refuse_root()
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
            # back out of the workspace. _refuse_root() above is what keeps this
            # from silently becoming `--user 0:0` (spec §4.1).
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
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            raise SandboxUnavailable(f"could not invoke docker: {exc}") from exc

        capture = _Capture(proc.stdout, proc.stderr)
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # docker run --rm leaves the container going after we stop waiting;
            # killing it is also what makes the CLI we are waiting on return.
            subprocess.run(["docker", "kill", container], capture_output=True)
            try:
                proc.wait(timeout=_DRAIN_JOIN_SECONDS)
            except subprocess.TimeoutExpired:
                proc.kill()  # the kill did not take; do not wait on it forever
                proc.wait()
            stdout, stderr = capture.finish()
            return SandboxResult(
                exit_code=-1,
                stdout=stdout,
                stderr=stderr + f"\nkilled after {timeout_s}s",
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
            )

        stdout, stderr = capture.finish()
        # 125 is docker's own "could not start the container" code. That is our
        # infrastructure failing, not the script failing, and the two must not be
        # confused: one is a bug report, the other is a question for a human.
        if proc.returncode == 125:
            raise SandboxUnavailable(f"docker could not start the sandbox: {stderr}")

        return SandboxResult(
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
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
