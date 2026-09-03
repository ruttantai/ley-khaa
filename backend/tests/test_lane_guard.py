"""The `--database` guard's own test (backlog item 28).

The guard raises pytest.UsageError from pytest_configure, so it cannot be
exercised in-process: by the time a test body runs, the guard has already
decided. These drive a real pytest in a subprocess instead, which is also the
only way to observe the exit code CI actually sees.

Why the guard exists: dropping or misindenting the `env:` block on CI's
`pytest (postgres)` step removes DATABASE_URL, so the step silently re-runs the
SQLite lane and reports a second green. Any check keyed on DATABASE_URL cannot
catch that — the malformation removes the variable the check reads — so the
expectation lives on the command line.
"""

import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent

# pytest's own exit code for a usage error (pytest.ExitCode.USAGE_ERROR).
USAGE_ERROR = 4

POSTGRES_URL = "postgresql+psycopg://ley:ley@localhost:55432/leykhaa"


def _run(args, env_overrides):
    """Run pytest in a subprocess with a controlled DATABASE_URL.

    `-x --collect-only` keeps a passing invocation cheap; neither flag can mask
    the guard, which fires in pytest_configure before either takes effect.
    """
    import os

    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
    )


def test_asking_for_postgres_on_the_sqlite_lane_is_a_usage_error():
    result = _run(["--database=postgres", "tests/test_lane_guard.py"], {})

    assert result.returncode == USAGE_ERROR, result.stdout + result.stderr
    assert "--database=postgres was asked for, but this run is on sqlite" in (
        result.stdout + result.stderr
    )
    # The remedy has to name the fix, not just the problem.
    assert "set DATABASE_URL to a postgresql+psycopg:// URL" in (result.stdout + result.stderr)


def test_asking_for_sqlite_while_a_postgres_url_is_set_is_a_usage_error():
    """The inverse leak: a DATABASE_URL escaping into the SQLite step."""
    result = _run(["--database=sqlite", "tests/test_lane_guard.py"], {"DATABASE_URL": POSTGRES_URL})

    assert result.returncode == USAGE_ERROR, result.stdout + result.stderr
    assert "--database=sqlite was asked for, but this run is on postgres" in (
        result.stdout + result.stderr
    )
    assert "unset DATABASE_URL" in (result.stdout + result.stderr)


def test_a_matching_lane_is_allowed_through():
    """The guard must not fire when the lane matches — otherwise it would be
    'always fails', which pins nothing and would break CI."""
    result = _run(["--database=sqlite", "tests/test_lane_guard.py"], {})

    assert result.returncode == 0, result.stdout + result.stderr


def test_omitting_the_flag_infers_the_lane_and_never_fails():
    """The SQLite lane stays zero-configuration: bare pytest needs no flag."""
    result = _run(["tests/test_lane_guard.py"], {})

    assert result.returncode == 0, result.stdout + result.stderr
