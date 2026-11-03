from datetime import datetime, timezone
from fastapi import APIRouter

from app.core.config import settings
from app.core.database import check_database_connection
from app.schemas.health import DatabaseStatus, HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse, summary="Healthcheck endpoint")
def health_check() -> HealthResponse:
    """Return API status and check PostgreSQL database connectivity."""
    db_ok, db_detail = check_database_connection()
    return HealthResponse(
        status="ok",
        service=settings.PROJECT_NAME,
        version=settings.VERSION,
        timestamp=datetime.now(timezone.utc),
        database=DatabaseStatus(
            connected=db_ok,
            engine="PostgreSQL (SQLAlchemy 2.0)",
            detail=db_detail,
        ),
    )
