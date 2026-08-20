from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from ley_khaa.db import Base
from ley_khaa.persistence import orm  # noqa: F401 — register models on Base

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _upgraded_engine(tmp_path):
    url = f"sqlite:///{tmp_path / 'drift.db'}"
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    return create_engine(url, future=True)


def test_migrations_create_the_0_2_0_tables(tmp_path):
    engine = _upgraded_engine(tmp_path)
    tables = set(inspect(engine).get_table_names())
    assert {"tasks", "messages", "task_candidates"} <= tables


def test_migrations_match_the_models(tmp_path):
    """A schema change with no migration is a failing test, not a prod crash."""
    engine = _upgraded_engine(tmp_path)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        diff = compare_metadata(context, Base.metadata)
    assert diff == [], f"models and migrations disagree: {diff}"


def test_a_pre_alembic_database_is_stamped_rather_than_recreated(tmp_path):
    """The 0.2.0 upgrade path: the tables already exist, alembic_version does not."""
    from ley_khaa.db import run_migrations

    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    legacy = create_engine(url, future=True)
    Base.metadata.create_all(legacy)  # exactly what 0.2.0 did
    assert "alembic_version" not in set(inspect(legacy).get_table_names())

    run_migrations(url)  # must not raise "table tasks already exists"

    assert "alembic_version" in set(inspect(legacy).get_table_names())
