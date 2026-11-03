from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import os
from pathlib import Path
import subprocess
import sys

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# Si se ejecuta con el Python global sin activar .venv, redirigir automáticamente a .venv
if sys.prefix == sys.base_prefix:
    _venv_python = _project_root / ".venv" / "Scripts" / "python.exe"
    if _venv_python.exists():
        _env = {**os.environ, "PYTHONPATH": str(_project_root)}
        raise SystemExit(subprocess.call([str(_venv_python), *sys.argv], env=_env))

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402

from app.api import api_router  # noqa: E402
from app.cli import ensure_tables_and_defaults  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.database import SessionLocal  # noqa: E402
import app.models  # noqa: E402, F401 - register ORM models with Base.metadata


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    """Attempt to initialize database tables and base permissions on startup if PostgreSQL is reachable."""
    try:
        with SessionLocal() as db:
            ensure_tables_and_defaults(db)
    except Exception:
        pass
    yield


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=settings.ALLOWED_HOSTS,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/", tags=["root"])
def root() -> dict[str, str]:
    return {
        "message": f"Welcome to {settings.PROJECT_NAME}",
        "host": settings.APP_HOST,
        "docs": f"http://{settings.APP_HOST}:{settings.APP_PORT}/docs",
        "health": f"http://{settings.APP_HOST}:{settings.APP_PORT}{settings.API_V1_STR}/health",
    }


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.APP_HOST,
        port=settings.APP_PORT,
        reload=True,
    )
