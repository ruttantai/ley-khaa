# backend/tests/test_sandbox_contract.py
"""One suite every SandboxRunner must pass.

Task 5 adds DockerSandbox to RUNNERS rather than writing a second suite: the
fallback earning a pass the real sandbox would fail is exactly the drift this
file exists to prevent.
"""
import time

import pytest

from ley_khaa.executor.sandbox import DockerSandbox, SubprocessSandbox

_docker = DockerSandbox(image="ley-khaa-sandbox")
# Skipped unless a daemon answers AND the image is built. CI builds it, so the
# real sandbox is genuinely exercised there rather than only in theory.
_no_docker = not _docker.available()

RUNNERS = [
    pytest.param(SubprocessSandbox(), id="subprocess"),
    pytest.param(
        _docker,
        id="docker",
        marks=[
            pytest.mark.docker,
            pytest.mark.skipif(_no_docker, reason="no docker daemon or sandbox image"),
        ],
    ),
]


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "deliverable").mkdir()
    (tmp_path / "generator").mkdir()
    (tmp_path / "inputs" / "data.csv").write_text("ticker\nAAA\n")
    return tmp_path


def _script(workspace, source: str):
    path = workspace / "generator" / "attempt_1.py"
    path.write_text(source)
    return path


@pytest.mark.parametrize("runner", RUNNERS)
def test_a_clean_run_reports_success_and_captures_stdout(runner, workspace):
    script = _script(workspace, "print('hello from the sandbox')")
    result = runner.run(script=script, workspace=workspace, timeout_s=30)
    assert result.exit_code == 0
    assert result.ok
    assert "hello from the sandbox" in result.stdout
    assert result.duration_ms >= 0


@pytest.mark.parametrize("runner", RUNNERS)
def test_a_crash_reports_the_traceback(runner, workspace):
    """The traceback is what the repair attempt is prompted with."""
    script = _script(workspace, "raise ValueError('boom')")
    result = runner.run(script=script, workspace=workspace, timeout_s=30)
    assert result.exit_code != 0
    assert not result.ok
    assert "ValueError" in result.stderr
    assert "boom" in result.stderr


@pytest.mark.parametrize("runner", RUNNERS)
def test_a_runaway_script_is_killed(runner, workspace):
    script = _script(workspace, "while True:\n    pass\n")
    result = runner.run(script=script, workspace=workspace, timeout_s=2)
    assert result.timed_out
    assert not result.ok


@pytest.mark.parametrize("runner", RUNNERS)
def test_the_script_can_read_inputs_and_write_a_deliverable(runner, workspace):
    script = _script(
        workspace,
        "rows = open('inputs/data.csv').read()\n"
        "open('deliverable/out.csv', 'w').write(rows)\n",
    )
    result = runner.run(script=script, workspace=workspace, timeout_s=30)
    assert result.ok, result.stderr
    assert (workspace / "deliverable" / "out.csv").read_text() == "ticker\nAAA\n"


@pytest.mark.parametrize("runner", RUNNERS)
def test_secrets_are_not_visible_to_the_script(runner, workspace, monkeypatch):
    """Synthesized code must never be able to read our API key out of the env."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    script = _script(
        workspace,
        "import os\nprint('KEY=' + repr(os.environ.get('ANTHROPIC_API_KEY')))\n",
    )
    result = runner.run(script=script, workspace=workspace, timeout_s=30)
    assert result.ok, result.stderr
    assert "KEY=None" in result.stdout


@pytest.mark.parametrize("runner", RUNNERS)
def test_invalid_utf8_on_stdout_does_not_crash_the_runner(runner, workspace):
    """An LLM-generated data script can write arbitrary bytes; a decode error
    in the sandbox itself must come back as a SandboxResult, not an exception
    that skips the manifest entirely."""
    script = _script(
        workspace,
        "import sys\nsys.stdout.buffer.write(b'\\xff\\xfeinvalid utf-8')\n",
    )
    result = runner.run(script=script, workspace=workspace, timeout_s=30)
    assert result.exit_code == 0
    assert result.ok


@pytest.mark.parametrize("runner", RUNNERS)
def test_killing_a_runaway_script_also_kills_its_children(runner, workspace):
    """A grandchild that outlives the timeout keeps the network access this
    fallback sandbox can never take away — the kill has to reach the whole
    process tree, not just the one pid the runner started."""
    (workspace / "child.py").write_text(
        "import time\n"
        "time.sleep(2)\n"
        "open('sentinel.txt', 'w').write('leaked')\n"
    )
    script = _script(
        workspace,
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, 'child.py'])\n"
        "while True:\n"
        "    time.sleep(0.05)\n",
    )
    result = runner.run(script=script, workspace=workspace, timeout_s=1)
    assert result.timed_out

    time.sleep(1.5)  # past the child's 2s delay, were it still alive
    assert not (workspace / "sentinel.txt").exists()
