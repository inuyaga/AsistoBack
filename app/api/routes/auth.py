from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, SessionDep
from app.core.config import settings
from app.core.security import create_access_token, verify_password
from app.models.user import User
from app.schemas.auth import LoginRequest, Token
from app.schemas.user import UserRead

router = APIRouter()


def _authenticate_user(db: Session, identifier: str, password: str) -> User:
    stmt = select(User).where(
        or_(User.username == identifier, User.email == identifier)
    )
    user = db.scalar(stmt)
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuario inactivo",
        )
    return user


def _build_token_response(user: User) -> Token:
    expires_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        subject=user.id,
        expires_delta=expires_delta,
        extra_claims={
            "username": user.username,
            "is_superuser": user.is_superuser,
            "permissions": user.all_permission_codes,
        },
    )
    return Token(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post("/token", response_model=Token, summary="OAuth2 Form Login (Swagger UI)")
def login_access_token(
    db: SessionDep,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> Token:
    """OAuth2 compatible token login, get an access token for future requests."""
    user = _authenticate_user(db, form_data.username, form_data.password)
    return _build_token_response(user)


@router.post("/login", response_model=Token, summary="JSON Login")
def login_json(db: SessionDep, credentials: LoginRequest) -> Token:
    """Authenticate with JSON payload (username or email + password) and return a JWT."""
    user = _authenticate_user(db, credentials.username, credentials.password)
    return _build_token_response(user)


@router.get("/me", response_model=UserRead, summary="Perfil del usuario autenticado")
def read_current_user(current_user: CurrentUser) -> User:
    """Return the currently authenticated user's profile, roles, and permissions."""
    return current_user
