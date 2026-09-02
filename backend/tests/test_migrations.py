import builtins
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from ley_khaa.db import ALEMBIC_DIR, Base
from ley_khaa.persistence import orm  # noqa: F401 — register models on Base

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "ley_khaa" / "alembic.ini"


def _config(url: str) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", url)
    # alembic.ini's script_location is relative and alembic resolves that
    # against the process CWD, not the ini's own directory — see db.py's
    # run_migrations for the full story. Override it the same way here so
    # these tests pass regardless of where pytest is invoked from.
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    return config


def _upgraded_engine(tmp_path):
    url = f"sqlite:///{tmp_path / 'drift.db'}"
    command.upgrade(_config(url), "head")
    return create_engine(url, future=True)


def test_migrations_create_the_0_2_0_tables(tmp_path):
    engine = _upgraded_engine(tmp_path)
    tables = set(inspect(engine).get_table_names())
    assert {"tasks", "messages", "task_candidates"} <= tables


def test_migrations_match_the_models(tmp_path):
    """A schema change with no migration is a failing test, not a prod crash."""
    engine = _upgraded_engine(tmp_path)
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        diff = compare_metadata(context, Base.metadata)
    assert diff == [], f"models and migrations disagree: {diff}"


def test_a_pre_alembic_database_is_stamped_rather_than_recreated(tmp_path):
    """The 0.2.0 upgrade path: the tables already exist, alembic_version does not."""
    from ley_khaa.db import run_migrations

    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    legacy = create_engine(url, future=True)
    # Build exactly the 0.2.0 schema. Base.metadata.create_all() would use
    # whatever columns orm.py declares *today* — which, now that phase-2 columns
    # exist, is no longer the 0.2.0 schema. Running only the baseline migration
    # and then dropping alembic_version reproduces a genuine pre-alembic
    # database: the tables exist, but nothing ever stamped a version.
    command.upgrade(_config(url), "0001_baseline")
    with legacy.begin() as connection:
        connection.execute(sa.text("DROP TABLE alembic_version"))
    assert "alembic_version" not in set(inspect(legacy).get_table_names())

    run_migrations(url)  # must not raise "table tasks already exists"

    assert "alembic_version" in set(inspect(legacy).get_table_names())
    assert "spec" in {c["name"] for c in inspect(legacy).get_columns("tasks")}


def test_every_migration_downgrades_back_to_an_empty_database(tmp_path):
    """Backlog item 7: no downgrade was exercised by anything, at any revision.

    `alembic downgrade base` is the operator's rollback path, and half of these
    revisions do their work through `batch_alter_table`, which on SQLite is a
    table rebuild rather than a real `ALTER` — a downgrade that names a column
    the rebuild forgot fails only when someone runs it. Now it runs here.
    """
    url = f"sqlite:///{tmp_path / 'down.db'}"
    config = _config(url)
    command.upgrade(config, "head")
    engine = create_engine(url, future=True)
    assert "tasks" in set(inspect(engine).get_table_names())

    command.downgrade(config, "base")

    remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert remaining == set(), f"downgrade to base left tables behind: {sorted(remaining)}"


def _revisions() -> builtins.list[str]:
    """Every revision id, oldest first, read from the script directory.

    Enumerated rather than hardcoded so a new migration is covered by the round
    trip below the day it is added, instead of the day someone remembers to
    extend a list.
    """
    scripts = ScriptDirectory.from_config(_config("sqlite://")).walk_revisions()
    return [script.revision for script in scripts][::-1]


@pytest.mark.parametrize("stop_at", _revisions()[:-1] + ["base"])
def test_a_downgrade_and_re_upgrade_lands_on_the_same_schema(tmp_path, stop_at):
    """The stronger half: a downgrade that runs is not the same as one that is
    correct. Going head -> `stop_at` -> head must reproduce the schema
    `Base.metadata` declares; a `drop_column` whose upgrade partner adds a
    different one shows up here as drift and nowhere else.

    **Parametrised over every stopping point on purpose, not for thoroughness.**
    A single stopping point only discriminates revisions whose leftovers survive
    down to it: with `stop_at="0001_baseline"`, a no-op `0009` downgrade fails
    loudly (`tasks` survives, so `last_notified_question` is still there and the
    re-upgrade hits a duplicate column) while a no-op `0010` downgrade passes —
    `0008`'s own downgrade drops `image_extractions` outright, taking the
    leftover column with it. Stopping at each revision in turn is what puts
    every downgrade in a position where its own leftovers can be observed.

    **What this cannot discriminate: `0006_alias_jsonb`.** Its downgrade is an
    `alter_column` between `sa.JSON()` and `JSON().with_variant(JSONB(),
    "postgresql")`, and on SQLite those two render identically — mutating that
    downgrade to `pass` leaves this whole file green. It is unexercised here and
    everywhere else, because migrations run on SQLite only (backlog item 26).
    Saying so is the point: a guard that appears to cover a type change it
    cannot see is worse than one that admits the gap.
    """
    url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    config = _config(url)
    command.upgrade(config, "head")
    command.downgrade(config, stop_at)
    command.upgrade(config, "head")

    engine = create_engine(url, future=True)
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        diff = compare_metadata(context, Base.metadata)
    assert diff == [], f"head -> {stop_at} -> head did not restore the schema: {diff}"
