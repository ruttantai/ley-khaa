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

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ley_khaa.db import Base
from ley_khaa.persistence import orm  # noqa: F401 — register TaskRow

# --- Which database the suite runs against (spec §4.2) -----------------------
#
# Two lanes, one suite. With DATABASE_URL unset — the default, and the
# documented dev loop — every test runs on SQLite exactly as it always has.
# Point DATABASE_URL at a Postgres server and the same tests run against the
# database the project actually ships on. That is the CI lane, and the only way
# a Postgres-only behaviour difference can ever fail a build.
DATABASE_URL = os.getenv("DATABASE_URL", "")
POSTGRES = DATABASE_URL.startswith("postgresql")

# The Postgres lane confines itself to a schema of its own (see _pg_engine).
TEST_SCHEMA = "ley_khaa_test"


@pytest.fixture(scope="session")
def _pg_engine():
    """The one engine the Postgres lane runs on, or None on the SQLite lane.

    Built once per session in a DEDICATED SCHEMA, for two reasons:

    * it is the safety rail. A developer whose DATABASE_URL points at their own
      `docker compose` database must not have the suite truncate `public`.
      Everything the tests create lives in `ley_khaa_test`, which is dropped and
      recreated at the start of every run.
    * every connection this engine hands out resolves unqualified table names
      inside that schema — including the ones `session_factory` gives to two
      racing dispatcher threads, which is why the search_path is set in
      connect_args rather than per-session.

    lock_timeout is set so that a test which leaks an open transaction fails
    loudly at the next TRUNCATE instead of hanging the whole suite on a lock.

    The schema is built with Base.metadata.create_all, the same as the SQLite
    lane — deliberately NOT with Alembic. Putting migration correctness on the
    critical path of a lane whose purpose is finding Postgres/SQLite behaviour
    differences would make every failure ambiguous. Running the suite against a
    migrated schema is its own piece of work.
    """
    if not POSTGRES:
        yield None
        return

    bootstrap = create_engine(DATABASE_URL, future=True, isolation_level="AUTOCOMMIT")
    try:
        with bootstrap.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
            conn.execute(text(f"CREATE SCHEMA {TEST_SCHEMA}"))
    finally:
        bootstrap.dispose()

    engine = create_engine(
        DATABASE_URL,
        future=True,
        connect_args={"options": f"-csearch_path={TEST_SCHEMA} -clock_timeout=30s"},
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _clean_database(_pg_engine):
    """Give every test an empty schema, the way the SQLite fixtures do for free.

    `session` builds a brand-new in-memory database per test and
    `session_factory` a brand-new tmp_path file per test, so on SQLite isolation
    costs nothing. One shared Postgres server has neither property, so the
    Postgres lane buys the same guarantee with TRUNCATE ... RESTART IDENTITY
    CASCADE over every mapped table before each test.

    Chosen over the two alternatives:

    * a per-test transaction rolled back at teardown cannot work here. The code
      under test commits constantly, and `session_factory` deliberately hands
      two OS threads two *independent* connections — a single outer transaction
      cannot span them without turning the concurrency tests into a fiction.
    * a schema (or database) per test would re-run the full DDL a thousand
      times over.

    TRUNCATE keeps commits real, keeps the two threads genuinely independent,
    and RESTART IDENTITY resets the sequences so generated ids start from 1 in
    each test exactly as they do on a fresh SQLite file.

    autouse, so a test that builds its own session off `_pg_engine` still starts
    clean; `session` and `session_factory` also depend on it explicitly so the
    ordering against seeding fixtures cannot drift.
    """
    if _pg_engine is None:
        yield
        return
    tables = ", ".join(
        f'{TEST_SCHEMA}."{table.name}"' for table in Base.metadata.sorted_tables
    )
    with _pg_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def session(_pg_engine, _clean_database):
    if _pg_engine is not None:
        test_engine = _pg_engine
    else:
        # check_same_thread / StaticPool are SQLite-only idioms: neither is a
        # psycopg connect argument, and StaticPool would collapse the Postgres
        # lane onto one connection.
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
def session_factory(tmp_path, _pg_engine, _clean_database):
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
    if _pg_engine is not None:
        # On the Postgres lane this fixture reaches its stated intent exactly:
        # a normally pooled engine gives every session its own real connection,
        # "the way SessionLocal does against Postgres in production". No
        # StaticPool shortcut here — that would quietly retire the concurrency
        # coverage the dispatcher tests exist for.
        maker = sessionmaker(
            bind=_pg_engine, autoflush=False, expire_on_commit=False, future=True
        )
        # The SQLite branch below ends in engine.dispose(), so there the fixture
        # already owns cleanup of every connection it handed out. This is the
        # Postgres equivalent, and it is not optional: a session left open holds
        # ACCESS SHARE on the tables it read, which would block the NEXT test's
        # TRUNCATE and report the leak against an innocent test. Closing here
        # keeps the two lanes' contract identical and keeps a leak attributable.
        handed_out = []

        def factory():
            s = maker()
            handed_out.append(s)
            return s

        try:
            yield factory
        finally:
            for s in handed_out:
                s.close()
        return

    db_path = tmp_path / "dispatcher-test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        # A busy writer (two dispatchers racing a claim) should wait briefly
        # for the lock rather than raise "database is locked" immediately.
        # (psycopg spells this connect_timeout and means something else; the
        # Postgres lane returns above, before this ever applies.)
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
