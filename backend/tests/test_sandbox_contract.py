# backend/tests/test_sandbox_contract.py
"""One suite every SandboxRunner must pass.

Task 5 adds DockerSandbox to RUNNERS rather than writing a second suite: the
fallback earning a pass the real sandbox would fail is exactly the drift this
file exists to prevent.
"""
import pytest

from ley_khaa.executor.sandbox import SubprocessSandbox

RUNNERS = [pytest.param(SubprocessSandbox(), id="subprocess")]


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
