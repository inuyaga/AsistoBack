from datetime import datetime
from pydantic import BaseModel


class DatabaseStatus(BaseModel):
    connected: bool
    engine: str = "PostgreSQL (SQLAlchemy)"
    detail: str


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    timestamp: datetime
    database: DatabaseStatus
