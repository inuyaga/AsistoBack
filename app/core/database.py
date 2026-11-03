from collections.abc import Generator
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.sqlalchemy_database_uri,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=30,
    pool_recycle=1800,
    connect_args={"connect_timeout": 3},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Base class for SQLAlchemy ORM models."""

    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that provides a database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_database_connection() -> tuple[bool, str]:
    """Verify connectivity to the configured PostgreSQL database."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True, "Connected to PostgreSQL"
    except Exception as exc:
        return False, str(exc).splitlines()[0] if str(exc) else "Database connection failed"
