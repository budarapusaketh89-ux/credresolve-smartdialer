import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base


@pytest.fixture
def session_factory(tmp_path):
    database_path = tmp_path / "test.db"
    test_engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 20},
        future=True,
    )
    Base.metadata.create_all(test_engine)
    factory = sessionmaker(
        bind=test_engine, autoflush=False, autocommit=False, expire_on_commit=False
    )
    yield factory
    test_engine.dispose()
