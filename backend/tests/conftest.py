import os
import tempfile
import uuid

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
LANE = "postgres" if POSTGRES else "sqlite"

# The Postgres lane confines itself to a schema of its own (see _pg_engine).
TEST_SCHEMA = "ley_khaa_test"


def pytest_addoption(parser):
    """`--database=postgres` asserts, on the command line, which lane ran.

    Inferring the lane from DATABASE_URL is what keeps the SQLite lane
    zero-configuration — bare `pytest` still gets SQLite, no new variable and no
    new flag — but inference alone has a silent failure mode. Delete or misindent
    the `env:` block on CI's `pytest (postgres)` step and DATABASE_URL is simply
    gone: the step re-runs the SQLite lane, prints `1038 passed` a second time,
    and the build goes green having never touched Postgres. That is this
    project's signature defect — something that looks healthy and silently does
    nothing — sitting inside the fix for it, and the whole value of this task is
    that the second lane really is a second database.

    Any check keyed on DATABASE_URL cannot catch that case, because the
    malformation removes the very variable the check reads. So the expectation
    is stated on the COMMAND LINE instead, where it is part of the `run:` line
    and survives anything that happens to the step's environment. `--database`
    does not select a lane or supply a URL; it only refuses to let a run claim to
    be a lane it is not.
    """
    parser.addoption(
        "--database",
        action="store",
        choices=("sqlite", "postgres"),
        default=None,
        help=(
            "Assert which database this run is on and fail if it is not. "
            "Omit (the default) to infer the lane from DATABASE_URL."
        ),
    )


def pytest_configure(config):
    expected = config.getoption("--database")
    if expected is None or expected == LANE:
        return
    remedy = (
        "set DATABASE_URL to a postgresql+psycopg:// URL"
        if expected == "postgres"
        else "unset DATABASE_URL"
    )
    raise pytest.UsageError(
        f"--database={expected} was asked for, but this run is on {LANE} "
        f"(DATABASE_URL={DATABASE_URL!r}). Refusing to run one lane and report "
        f"it as the other — to run the {expected} lane, {remedy}."
    )


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

    TRUNCATE keeps commits real and keeps the two threads genuinely independent.

    RESTART IDENTITY is inert against this schema and is kept anyway: every
    mapped table's primary key is a `String` the application generates, so there
    is not one sequence or identity column for it to reset. It costs nothing,
    and it is the clause that would be missing on the day an integer key lands —
    at which point a leftover sequence would make ids differ between a fresh
    SQLite file and the shared Postgres schema, which is exactly the difference
    this fixture exists to erase.

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
def migration_url(tmp_path):
    """An empty database for one migration test, on whichever lane is running.

    Migration tests CREATE tables, so they need a namespace of their own — they
    cannot share `ley_khaa_test`, which `_clean_database` truncates but does not
    drop. On SQLite that is a tmp_path file. On Postgres it is a throwaway schema
    dropped at teardown, with search_path set through the URL's libpq `options`
    parameter so Alembic (which is handed only a URL) creates everything inside
    it — including its own `alembic_version` table.

    Before v1.0.0 these tests hardcoded SQLite URLs, so migrations never ran
    against Postgres at all (backlog item 26) — which is how `0004`'s workflows
    table shipped a `workflows_name_key` UNIQUE constraint the models never
    declared, on top of the unique index they do declare.
    """
    if not POSTGRES:
        yield f"sqlite:///{tmp_path / 'migration.db'}"
        return

    schema = f"ley_khaa_mig_{uuid.uuid4().hex[:12]}"
    bootstrap = create_engine(DATABASE_URL, future=True, isolation_level="AUTOCOMMIT")
    try:
        with bootstrap.connect() as conn:
            conn.execute(text(f"CREATE SCHEMA {schema}"))
        separator = "&" if "?" in DATABASE_URL else "?"
        # The `=` is deliberately NOT percent-encoded. SQLAlchemy parses
        # `options=-csearch_path=x` and `options=-csearch_path%3Dx` into the
        # identical psycopg connect argument, but only the first survives
        # alembic: Config.set_main_option() hands the URL to ConfigParser.set(),
        # whose BasicInterpolation rejects any raw `%` outright
        # (ValueError: invalid interpolation syntax). See the report's finding
        # on run_migrations for the same hazard in production code.
        yield f"{DATABASE_URL}{separator}options=-csearch_path={schema}"
    finally:
        with bootstrap.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        bootstrap.dispose()


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
