from logging.config import fileConfig
import os
from pathlib import Path
import sys

from sqlalchemy import engine_from_config, pool

from alembic import context

# ─── Make src importable ─────────────────────────────────────────
# When running `alembic` from the repo root, we need to add
# services/api-gateway to sys.path so `from src.db.models import Base` works.
_here = Path(__file__).parent.parent  # repo root
_api_gw = _here / "services" / "api-gateway"
for p in [str(_here), str(_api_gw)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from src.db.models import Base  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Override sqlalchemy.url from environment if set
postgres_dsn = os.environ.get("POSTGRES_DSN", "")
if postgres_dsn:
    # asyncpg DSN → sync psycopg2 DSN (Alembic uses sync SQLAlchemy for migrations)
    sync_dsn = postgres_dsn.replace("postgresql+asyncpg://", "postgresql://")
    config.set_main_option("sqlalchemy.url", sync_dsn)

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Add your model's MetaData object here for 'autogenerate' support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
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
