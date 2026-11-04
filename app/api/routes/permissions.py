from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import SessionDep, require_permissions
from app.cli import DEFAULT_PERMISSIONS, sync_base_permissions_and_roles
from app.models.user import Permission, Role
from app.schemas.user import (
    PermissionCreate,
    PermissionRead,
    PermissionUpdate,
    RoleCreate,
    RoleRead,
    RoleUpdate,
)

router = APIRouter()


@router.get(
    "/permissions",
    response_model=list[PermissionRead],
    dependencies=[Depends(require_permissions("permissions:read"))],
    summary="Listar todos los permisos",
)
def list_permissions(db: SessionDep) -> list[Permission]:
    existing = list(db.scalars(select(Permission).order_by(Permission.code)).all())
    existing_codes = {p.code for p in existing}
    if any(code not in existing_codes for code, _, _ in DEFAULT_PERMISSIONS):
        sync_base_permissions_and_roles(db)
        existing = list(db.scalars(select(Permission).order_by(Permission.code)).all())
    return existing


@router.post(
    "/permissions",
    response_model=PermissionRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permissions("permissions:write"))],
    summary="Crear un nuevo permiso",
)
def create_permission(db: SessionDep, payload: PermissionCreate) -> Permission:
    code = payload.code.strip()
    existing = db.scalar(select(Permission).where(Permission.code == code))
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"El permiso '{code}' ya existe",
        )
    permission = Permission(
        code=code,
        name=payload.name.strip(),
        description=payload.description.strip() if payload.description else None,
    )
    db.add(permission)
    db.commit()
    db.refresh(permission)
    return permission


@router.patch(
    "/permissions/{permission_id}",
    response_model=PermissionRead,
    dependencies=[Depends(require_permissions("permissions:write"))],
    summary="Actualizar un permiso",
)
def update_permission(
    db: SessionDep,
    permission_id: int,
    payload: PermissionUpdate,
) -> Permission:
    permission = db.get(Permission, permission_id)
    if not permission:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Permiso no encontrado",
        )

    if payload.code is not None and payload.code.strip() != permission.code:
        cleaned_code = payload.code.strip()
        conflict = db.scalar(select(Permission).where(Permission.code == cleaned_code))
        if conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"El permiso '{cleaned_code}' ya existe",
            )
        permission.code = cleaned_code

    if payload.name is not None:
        permission.name = payload.name.strip()

    if payload.description is not None:
        permission.description = payload.description.strip() or None

    db.commit()
    db.refresh(permission)
    return permission


@router.post(
    "/permissions/sync-defaults",
    response_model=list[PermissionRead],
    dependencies=[Depends(require_permissions("permissions:write"))],
    summary="Sincronizar permisos y rol admin base del sistema",
)
def sync_default_permissions(db: SessionDep) -> list[Permission]:
    sync_base_permissions_and_roles(db)
    return list(db.scalars(select(Permission).order_by(Permission.code)).all())


@router.delete(
    "/permissions/{permission_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permissions("permissions:write"))],
    summary="Eliminar un permiso",
)
def delete_permission(db: SessionDep, permission_id: int) -> None:
    permission = db.get(Permission, permission_id)
    if not permission:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Permiso no encontrado",
        )
    db.delete(permission)
    db.commit()


@router.get(
    "/roles",
    response_model=list[RoleRead],
    dependencies=[Depends(require_permissions("roles:read"))],
    summary="Listar roles y sus permisos",
)
def list_roles(db: SessionDep) -> list[Role]:
    return list(db.scalars(select(Role).order_by(Role.name)).all())


@router.post(
    "/roles",
    response_model=RoleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permissions("roles:write"))],
    summary="Crear un rol con permisos asignados",
)
def create_role(db: SessionDep, payload: RoleCreate) -> Role:
    existing = db.scalar(select(Role).where(Role.name == payload.name))
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"El rol '{payload.name}' ya existe",
        )

    permissions: list[Permission] = []
    if payload.permission_codes:
        permissions = list(
            db.scalars(
                select(Permission).where(Permission.code.in_(payload.permission_codes))
            ).all()
        )
        found = {p.code for p in permissions}
        missing = [code for code in payload.permission_codes if code not in found]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Permisos no encontrados: {', '.join(missing)}",
            )

    role = Role(
        name=payload.name,
        description=payload.description,
        permissions=permissions,
    )
    db.add(role)
    db.commit()
    db.refresh(role)
    return role


@router.patch(
    "/roles/{role_id}",
    response_model=RoleRead,
    dependencies=[Depends(require_permissions("roles:write"))],
    summary="Actualizar un rol y sus permisos",
)
def update_role(db: SessionDep, role_id: int, payload: RoleUpdate) -> Role:
    role = db.get(Role, role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rol no encontrado",
        )

    if payload.name is not None and payload.name != role.name:
        conflict = db.scalar(select(Role).where(Role.name == payload.name))
        if conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"El rol '{payload.name}' ya existe",
            )
        role.name = payload.name

    if payload.description is not None:
        role.description = payload.description

    if payload.permission_codes is not None:
        permissions = list(
            db.scalars(
                select(Permission).where(Permission.code.in_(payload.permission_codes))
            ).all()
        )
        found = {p.code for p in permissions}
        missing = [code for code in payload.permission_codes if code not in found]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Permisos no encontrados: {', '.join(missing)}",
            )
        role.permissions = permissions

    db.commit()
    db.refresh(role)
    return role


@router.delete(
    "/roles/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permissions("roles:write"))],
    summary="Eliminar un rol",
)
def delete_role(db: SessionDep, role_id: int) -> None:
    role = db.get(Role, role_id)
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rol no encontrado",
        )
    db.delete(role)
    db.commit()
