from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = "sqlite+aiosqlite:///./uptime.db"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    # Wait up to 30s for a lock rather than failing instantly with
    # "database is locked" when the checker commits several monitors at once.
    connect_args={"timeout": 30},
)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    # WAL lets readers and a single writer work concurrently (rollback-journal
    # mode blocks them); busy_timeout makes contending writers queue instead of
    # erroring under the checker's concurrent commits.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

Base = declarative_base()


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session