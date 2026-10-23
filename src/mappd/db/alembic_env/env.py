"""Alembic environment for mappd: online migrations only, the URL comes from the config.

mappd builds the Alembic config in code (mappd.db.migrate); there is no alembic.ini.
"""

from alembic import context
from sqlalchemy import create_engine, pool

url = context.config.get_main_option("sqlalchemy.url")
if not url:
    raise RuntimeError("sqlalchemy.url is not set; run migrations through mappd.db.migrate")
engine = create_engine(url, poolclass=pool.NullPool)
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
