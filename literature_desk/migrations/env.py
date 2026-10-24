from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

database_url = config.get_main_option("sqlalchemy.url", "").strip()
if not database_url:
    database_url = os.environ.get("LITLOOM_DATABASE_URL", "").strip()
if not database_url:
    raise RuntimeError("LITLOOM_DATABASE_URL must be configured for PostgreSQL migrations")
url = make_url(database_url)
if url.drivername == "postgresql":
    url = url.set(drivername="postgresql+psycopg")
database_url = url.render_as_string(hide_password=False)
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        target_metadata=target_metadata,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
