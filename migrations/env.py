from alembic import context
from app.db import DATABASE_URL, engine, metadata

if context.is_offline_mode():
    context.configure(url=DATABASE_URL, target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
