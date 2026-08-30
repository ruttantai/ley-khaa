import os
import tempfile

os.environ["LEY_KHAA_DISABLE_STARTUP"] = "1"
os.environ["LEY_KHAA_LLM"] = "heuristic"
os.environ["LEY_KHAA_DEBOUNCE_SECONDS"] = "0"
os.environ["LEY_KHAA_SANDBOX"] = "subprocess"
# The existing suite asserts on tasks that have already run. Workers mode is
# covered on its own terms in test_dispatcher.py and test_concurrency.py.
os.environ["LEY_KHAA_DISPATCH"] = "inline"
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
    # The dispatcher opens a session per unit of work and closes it; this one is
    # shared with the test that yields it, so it must survive.
    s._ley_khaa_shared = True
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def session_factory(tmp_path):
    """Hand out genuinely independent sessions over a file-backed sqlite
    database.

    The `session` fixture above shares ONE physical connection (via
    StaticPool over an in-memory database) across every caller, which is
    fine for tests that touch the database from a single thread. The
    dispatcher's own tests need the opposite: two dispatcher workers
    genuinely running in two OS threads at once (`asyncio.to_thread`) must
    each get their own real connection, the way `SessionLocal` does against
    Postgres in production — a shared in-memory connection is not safe for
    that (concurrent cursor use on one sqlite3 connection corrupts, even
    with check_same_thread=False). A file-backed database gives every
    `session_factory()` call its own connection to the same file, so
    SQLite's own locking — not test-only Python machinery — is what
    arbitrates concurrent access.

    Deliberately named `session_factory` and placed here rather than local
    to test_dispatcher.py: Task 15's lost-update test needs exactly this
    fixture, and should reuse it rather than grow a second file-backed
    factory beside it.
    """
    db_path = tmp_path / "dispatcher-test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        # A busy writer (two dispatchers racing a claim) should wait briefly
        # for the lock rather than raise "database is locked" immediately.
        connect_args={"timeout": 30},
        future=True,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    try:
        yield Session
    finally:
        engine.dispose()


@pytest.fixture
def seed_workflow(session_factory):
    """Seed the registry over `session_factory`'s database and hand back the
    name of a workflow that's now in it, for Task 15's lost-update tests.

    `session_factory` yields a sessionmaker (see above), so a session has to
    be opened, used, and closed here rather than passed straight to
    ensure_seed_workflows.
    """
    from ley_khaa.registry.seeds import ensure_seed_workflows

    session = session_factory()
    try:
        ensure_seed_workflows(session)
        return "set_difference"
    finally:
        session.close()


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
