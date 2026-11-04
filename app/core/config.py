from pathlib import Path
from typing import Any, Literal, Optional
from pydantic import computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACK_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    PROJECT_NAME: str = "Attendance System API"
    VERSION: str = "0.1.0"
    API_V1_STR: str = "/api/v1"

    APP_HOST: str = "attendance.local"
    APP_PORT: int = 8000

    SECRET_KEY: str = "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    # Mobile App (Asisto) Security & Business Rules Configuration
    MOBILE_ACCESS_TOKEN_EXPIRE_SECONDS: int = 3600
    MOBILE_REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    MOBILE_VALIDATION_TOKEN_EXPIRE_MINUTES: int = 10
    MOBILE_SIGNED_MEDIA_EXPIRE_SECONDS: int = 600
    MOBILE_MIN_FACE_MATCH_SCORE: float = 0.75
    MOBILE_MAX_CLOCK_DRIFT_SECONDS: int = 900

    # Media Storage Configuration ('local' filesystem folder or 's3' bucket)
    MEDIA_STORAGE_BACKEND: Literal["local", "s3"] = "local"
    MEDIA_ROOT_DIR: str = "media"
    MEDIA_PUBLIC_BASE_URL: Optional[str] = None
    S3_BUCKET_NAME: Optional[str] = None
    S3_REGION: str = "us-east-1"
    S3_ENDPOINT_URL: Optional[str] = None
    S3_ACCESS_KEY_ID: Optional[str] = None
    S3_SECRET_ACCESS_KEY: Optional[str] = None

    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "attendance_db"
    DATABASE_URL: Optional[str] = None

    ALLOWED_HOSTS: list[str] = [
        "*",
    ]
    CORS_ORIGINS: list[str] = [
        "http://attendance.local:5173",
        "http://attendance.local",
        "https://attendance.local",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    model_config = SettingsConfigDict(
        env_file=(str(_BACK_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    @field_validator("*", mode="before")
    @classmethod
    def _strip_surrounding_quotes(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1].strip()
        return v

    @field_validator("API_V1_STR", mode="after")
    @classmethod
    def _normalize_api_v1_str(cls, v: str) -> str:
        cleaned = v.strip().strip("\"'").strip()
        if not cleaned:
            return "/api/v1"
        if not cleaned.startswith("/"):
            cleaned = f"/{cleaned}"
        return cleaned.rstrip("/") or "/api/v1"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def media_root_path(self) -> Path:
        raw_path = Path(self.MEDIA_ROOT_DIR)
        if raw_path.is_absolute():
            return raw_path
        return (_BACK_ROOT / raw_path).resolve()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def public_api_base_url(self) -> str:
        if self.MEDIA_PUBLIC_BASE_URL and self.MEDIA_PUBLIC_BASE_URL.strip():
            return self.MEDIA_PUBLIC_BASE_URL.strip().rstrip("/")
        return f"http://{self.APP_HOST}:{self.APP_PORT}{self.API_V1_STR}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_database_uri(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


settings = Settings()
