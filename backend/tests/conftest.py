import os
import tempfile

os.environ["LEY_KHAA_DISABLE_STARTUP"] = "1"
os.environ["LEY_KHAA_LLM"] = "heuristic"
os.environ["LEY_KHAA_DEBOUNCE_SECONDS"] = "0"
os.environ["LEY_KHAA_SANDBOX"] = "subprocess"
# Otherwise every test that executes a task writes a bundle into ./task-workspaces
# in whatever directory pytest was invoked from.
os.environ.setdefault("LEY_KHAA_WORKSPACE_ROOT", tempfile.mkdtemp(prefix="ley-khaa-tests-"))

import pytest
from fastapi.testclient import TestClient

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ley_khaa.db import Base
from ley_khaa.persistence import orm  # noqa: F401 — register TaskRow


@pytest.fixture
def session():
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(test_engine)
    TestingSession = sessionmaker(
        bind=test_engine, autoflush=False, expire_on_commit=False, future=True
    )
    s = TestingSession()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def stub_execution(monkeypatch, tmp_path):
    """Make _execute a no-op that succeeds.

    The approve / reject / override / edit_spec tests are about transitions.
    Without this, each of them would synthesize and run a real script, which
    both slows the suite and couples the human-in-the-loop tests to executor
    behaviour that tests/test_driver_execution.py and
    tests/test_executor_end_to_end.py already cover directly.
    """
    from ley_khaa.executor.runner import ExecutionOutcome
    from ley_khaa.executor.validator import Verdict

    def _run(self, row, spec):
        return ExecutionOutcome(
            verdict=Verdict(ok=True, reason="stubbed", checks={}),
            workspace_path=str(tmp_path / f"task-{row.id}"),
            attempts=1,
        )

    monkeypatch.setattr("ley_khaa.executor.runner.ExecutionRunner.run", _run)


@pytest.fixture
def seeded_registry(session):
    """Install the seed workflows for tests that want the registry fast path.

    Not automatic: conftest sets LEY_KHAA_DISABLE_STARTUP=1, so the lifespan
    seeding never runs under test. Opting in keeps every existing test on the
    synthesis lane it was written for, and makes a test's lane obvious from its
    own body rather than from a global.
    """
    from ley_khaa.registry.seeds import ensure_seed_workflows

    ensure_seed_workflows(session)
    return session


@pytest.fixture
def client(session):
    from ley_khaa.api.app import app, get_session

    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
