from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

from tripvane_collector.db import make_engine
from tripvane_collector.models import Base
from tripvane_core.config import Settings

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)


def _database_url() -> str:
    database_url = Settings.from_env().database_url
    if database_url is None:
        raise RuntimeError("DATABASE_URL is not set")
    return database_url


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=_database_url(), target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif (connection := config.attributes.get("connection")) is not None:
    # Tests pass an open connection instead of a URL.
    _run(connection)
else:
    with make_engine(_database_url()).connect() as connection:
        _run(connection)
