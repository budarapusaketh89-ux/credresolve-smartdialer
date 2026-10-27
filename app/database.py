from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

connect_args = {"check_same_thread": False, "timeout": 20} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def upgrade_schema() -> None:
    """Create missing tables and apply the small additive migrations used by this prototype.

    For production, replace this helper with versioned Alembic migrations.
    """
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    with engine.begin() as connection:
        if "provider_events" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("provider_events")}
            if "occurred_at" not in columns:
                connection.execute(text("ALTER TABLE provider_events ADD COLUMN occurred_at DATETIME"))
            if "applied" not in columns:
                connection.execute(text("ALTER TABLE provider_events ADD COLUMN applied BOOLEAN NOT NULL DEFAULT 0"))
        if "campaigns" in inspector.get_table_names():
            columns = {column["name"] for column in inspector.get_columns("campaigns")}
            if "provider_failure_count" not in columns:
                connection.execute(text("ALTER TABLE campaigns ADD COLUMN provider_failure_count INTEGER NOT NULL DEFAULT 0"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
