from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

# Shipped as package data (see pyproject.toml's [tool.setuptools.package-data])
# so migrations are found under a non-editable `pip install .` too, where
# ley_khaa lands in site-packages but a source checkout's alembic.ini would
# not. Both paths live inside the package now, not one level up from it.
ALEMBIC_INI = Path(__file__).resolve().parent / "alembic.ini"
ALEMBIC_DIR = Path(__file__).resolve().parent / "alembic"


def run_migrations(url: str | None = None) -> None:
    """Bring a database up to head. Defaults to the configured one.

    Replaces the old Base.metadata.create_all(): create_all silently ignores a
    table that already exists but lacks a newly added column, which is exactly
    how 0.2.0 ended up telling everyone to drop their database.

    `url` is a parameter rather than read-only config because Settings is a
    frozen dataclass — this is the seam the upgrade-path test needs.
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect

    from .persistence import orm  # noqa: F401 — register models on Base

    url = url or settings.database_url
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", url)
    # alembic.ini's script_location ("alembic") is relative, and alembic
    # resolves a relative script_location against the process CWD, not against
    # the ini file's own directory. That is harmless today only because
    # uvicorn's default --app-dir inserts the CWD on sys.path and the app
    # happens to be run from /app, the same directory alembic.ini lives in.
    # Any other CWD (gunicorn, a different WORKDIR, a downstream install)
    # breaks it. Overriding it here with an absolute path removes the CWD from
    # the equation entirely.
    config.set_main_option("script_location", str(ALEMBIC_DIR))

    # A database created by 0.2.0's create_all() already has the tables but no
    # alembic_version row, so `upgrade head` would try to create them again and
    # crash on first start. Stamp it at the baseline instead, then upgrade
    # normally — this is what lets 0.3.0 be the release that stops asking people
    # to drop their database.
    target = create_engine(url, future=True)
    try:
        tables = set(inspect(target).get_table_names())
    finally:
        target.dispose()
    if "tasks" in tables and "alembic_version" not in tables:
        command.stamp(config, "0001_baseline")

    command.upgrade(config, "head")
