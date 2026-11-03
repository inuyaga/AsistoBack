from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, SessionDep, require_permissions
from app.core.security import get_password_hash
from app.models.user import Permission, Role, User
from app.schemas.user import UserCreate, UserRead, UserUpdate

router = APIRouter()


def _resolve_roles(db: Session, role_names: list[str]) -> list[Role]:
    if not role_names:
        return []
    roles = list(db.scalars(select(Role).where(Role.name.in_(role_names))).all())
    found = {r.name for r in roles}
    missing = [name for name in role_names if name not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Roles no encontrados: {', '.join(missing)}",
        )
    return roles


def _resolve_permissions(db: Session, permission_codes: list[str]) -> list[Permission]:
    if not permission_codes:
        return []
    perms = list(db.scalars(select(Permission).where(Permission.code.in_(permission_codes))).all())
    found = {p.code for p in perms}
    missing = [code for code in permission_codes if code not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Permisos no encontrados: {', '.join(missing)}",
        )
    return perms


@router.get(
    "",
    response_model=list[UserRead],
    dependencies=[Depends(require_permissions("users:read"))],
    summary="Listar usuarios",
)
def list_users(
    db: SessionDep,
    skip: int = 0,
    limit: int = 100,
    search: Optional[str] = Query(default=None, description="Filtrar por usuario, correo o nombre"),
    is_active: Optional[bool] = Query(default=None, description="Filtrar por estado activo/inactivo"),
    is_superuser: Optional[bool] = Query(default=None, description="Filtrar por superusuario"),
    role: Optional[str] = Query(default=None, description="Filtrar por nombre de rol"),
) -> list[User]:
    stmt = select(User)
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                User.username.ilike(pattern),
                User.email.ilike(pattern),
                User.full_name.ilike(pattern),
            )
        )
    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)
    if is_superuser is not None:
        stmt = stmt.where(User.is_superuser == is_superuser)
    if role and role.strip():
        stmt = stmt.join(User.roles).where(Role.name == role.strip())

    stmt = stmt.offset(skip).limit(limit).order_by(User.id)
    return list(db.scalars(stmt).unique().all())


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permissions("users:write"))],
    summary="Crear usuario",
)
def create_user(db: SessionDep, payload: UserCreate) -> User:
    existing = db.scalar(
        select(User).where(
            or_(User.username == payload.username, User.email == payload.email)
        )
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El nombre de usuario o correo electrónico ya está registrado",
        )

    roles = _resolve_roles(db, payload.role_names)
    permissions = _resolve_permissions(db, payload.permission_codes)

    user = User(
        username=payload.username,
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=get_password_hash(payload.password),
        is_active=payload.is_active,
        is_superuser=payload.is_superuser,
        roles=roles,
        direct_permissions=permissions,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get(
    "/{user_id}",
    response_model=UserRead,
    dependencies=[Depends(require_permissions("users:read"))],
    summary="Obtener usuario por ID",
)
def get_user(db: SessionDep, user_id: int) -> User:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado",
        )
    return user


@router.patch(
    "/{user_id}",
    response_model=UserRead,
    dependencies=[Depends(require_permissions("users:write"))],
    summary="Actualizar usuario, roles o permisos",
)
def update_user(db: SessionDep, user_id: int, payload: UserUpdate) -> User:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado",
        )

    if payload.username is not None and payload.username.strip() != user.username:
        cleaned_username = payload.username.strip()
        conflict = db.scalar(select(User).where(User.username == cleaned_username))
        if conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El nombre de usuario ya está en uso",
            )
        user.username = cleaned_username

    if payload.email is not None and payload.email != user.email:
        conflict = db.scalar(select(User).where(User.email == payload.email))
        if conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El correo electrónico ya está en uso",
            )
        user.email = payload.email

    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.is_superuser is not None:
        user.is_superuser = payload.is_superuser
    if payload.password is not None:
        user.hashed_password = get_password_hash(payload.password)
    if payload.role_names is not None:
        user.roles = _resolve_roles(db, payload.role_names)
    if payload.permission_codes is not None:
        user.direct_permissions = _resolve_permissions(db, payload.permission_codes)

    db.commit()
    db.refresh(user)
    return user


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permissions("users:delete"))],
    summary="Eliminar usuario",
)
def delete_user(db: SessionDep, user_id: int, current_user: CurrentUser) -> None:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Usuario no encontrado",
        )
    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No puedes eliminar tu propio usuario con sesión activa",
        )
    db.delete(user)
    db.commit()
