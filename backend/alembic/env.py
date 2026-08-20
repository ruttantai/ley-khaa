from alembic import context
from sqlalchemy import create_engine, pool

from ley_khaa.config import settings
from ley_khaa.db import Base
from ley_khaa.persistence import orm  # noqa: F401 — register models on Base

# No fileConfig() call: alembic.ini deliberately carries no logging sections,
# and the app configures its own logging.
config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url", None) or settings.database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool, future=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER TABLE ADD CONSTRAINT; batch mode rewrites the
            # table instead. The no-Docker dev path runs on SQLite, so this is
            # not optional.
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
