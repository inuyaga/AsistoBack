import base64
from datetime import datetime, timezone
from typing import Callable, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, SessionDep
from app.api.routes.devices import _build_isapi_client
from app.core.isapi import ISAPIError
from app.core.storage import (
    extract_jpeg_bytes_from_base64,
    get_media_storage,
    read_media_as_data_url,
    store_collaborator_face_image,
)
from app.models.attendance import Shift
from app.models.collaborator import Collaborator, CollaboratorDeviceSync, Company
from app.models.device import HikvisionDevice
from app.models.mobile import Branch, Geofence, GeofenceAssignment
from app.models.user import User
from app.schemas.collaborator import (
    CollaboratorBulkSyncDevicesRequest,
    CollaboratorCreate,
    CollaboratorRead,
    CollaboratorSyncDeviceRequest,
    CollaboratorSyncItemResult,
    CollaboratorSyncResponse,
    CollaboratorUpdate,
    CompanyCreate,
    CompanyRead,
    CompanyUpdate,
)

router = APIRouter()


def require_any_permission(*codes: str) -> Callable[[CurrentUser], User]:
    """Allow access if the authenticated user is a superuser or holds at least one of the given permissions."""

    def checker(current_user: CurrentUser) -> User:
        if current_user.is_superuser:
            return current_user
        if any(current_user.has_permission(code) for code in codes):
            return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permisos insuficientes. Se requiere alguno de: {', '.join(codes)}",
        )

    return checker


def _normalize_and_validate_jpeg_base64(raw_b64: Optional[str]) -> Optional[str]:
    """Validate that a base64 string contains a valid JPEG image and return a normalized data URL."""
    if raw_b64 is None:
        return None
    cleaned = raw_b64.strip()
    if not cleaned:
        return None

    if "," in cleaned and cleaned.startswith("data:"):
        header, b64_part = cleaned.split(",", 1)
        if "image/jpeg" not in header.lower() and "image/jpg" not in header.lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La fotografía facial debe estar en formato JPEG (image/jpeg).",
            )
    else:
        b64_part = cleaned

    try:
        img_bytes = base64.b64decode(b64_part, validate=False)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La cadena base64 de la fotografía facial no es válida.",
        ) from exc

    if len(img_bytes) < 4 or not img_bytes.startswith(b"\xff\xd8\xff"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El archivo de imagen no es un JPEG válido. Usa una fotografía JPG/JPEG.",
        )

    normalized_b64 = base64.b64encode(img_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{normalized_b64}"


def _get_company_or_404(db: Session, company_id: int) -> Company:
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Empresa no encontrada",
        )
    return company


def _get_collaborator_or_404(db: Session, collaborator_id: int) -> Collaborator:
    collab = db.get(Collaborator, collaborator_id)
    if not collab:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Colaborador no encontrado",
        )
    return collab


def _get_or_create_sync_record(
    db: Session,
    collaborator_id: int,
    device_id: int,
) -> CollaboratorDeviceSync:
    now = datetime.now(timezone.utc)
    record = db.scalar(
        select(CollaboratorDeviceSync).where(
            CollaboratorDeviceSync.collaborator_id == collaborator_id,
            CollaboratorDeviceSync.device_id == device_id,
        )
    )
    if not record:
        record = CollaboratorDeviceSync(
            collaborator_id=collaborator_id,
            device_id=device_id,
            status="pending",
            face_synced=False,
            last_synced_at=now,
            last_error=None,
        )
        db.add(record)
        db.flush()
    return record


def _upsert_sync_record(
    db: Session,
    collaborator_id: int,
    device_id: int,
    status_val: str,
    face_synced: bool,
    last_error: Optional[str] = None,
    record: Optional[CollaboratorDeviceSync] = None,
) -> CollaboratorDeviceSync:
    now = datetime.now(timezone.utc)
    if record is None:
        record = _get_or_create_sync_record(db, collaborator_id, device_id)
    record.status = status_val
    record.face_synced = face_synced
    record.last_synced_at = now
    record.last_error = last_error[:500] if last_error else None
    return record


def _execute_collaborators_device_sync(
    db: Session,
    company: Company,
    collaborators: list[Collaborator],
    devices: list[HikvisionDevice],
    action: Literal["upsert", "delete"] = "upsert",
    include_face: bool = True,
) -> CollaboratorSyncResponse:
    """Execute individual or bulk registration/deletion of collaborators across company devices via ISAPI."""
    # Validar pertenencia exclusiva a la misma empresa
    invalid_collabs = [c for c in collaborators if c.company_id != company.id]
    if invalid_collabs:
        names = ", ".join(f"{c.full_name} ({c.employee_no})" for c in invalid_collabs[:5])
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Los siguientes colaboradores no pertenecen a la empresa '{company.name}': {names}",
        )

    invalid_devices = [d for d in devices if d.company_id != company.id]
    if invalid_devices:
        dev_names = ", ".join(d.name for d in invalid_devices)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Cada reloj es exclusivo de una sola empresa. Los relojes [{dev_names}] "
                f"no pertenecen a '{company.name}'."
            ),
        )

    results: list[CollaboratorSyncItemResult] = []
    succeeded = 0
    failed = 0
    now = datetime.now(timezone.utc)

    for device in devices:
        client = _build_isapi_client(device)
        device_unreachable_error: Optional[str] = None

        for collab in collaborators:
            sync_record = _get_or_create_sync_record(
                db=db,
                collaborator_id=collab.id,
                device_id=device.id,
            )
            sync_emp_no = str(sync_record.id)

            if device_unreachable_error is not None:
                failed += 1
                _upsert_sync_record(
                    db=db,
                    collaborator_id=collab.id,
                    device_id=device.id,
                    status_val="error",
                    face_synced=False,
                    last_error=device_unreachable_error,
                    record=sync_record,
                )
                results.append(
                    CollaboratorSyncItemResult(
                        collaborator_id=collab.id,
                        sync_id=sync_record.id,
                        employee_no=sync_emp_no,
                        collaborator_name=collab.full_name,
                        device_id=device.id,
                        device_name=device.name,
                        action=action,
                        success=False,
                        user_synced=False,
                        face_synced=False,
                        message=device_unreachable_error,
                    )
                )
                continue

            if action == "delete":
                try:
                    client.baja_employee([sync_emp_no])
                    device.status = "online"
                    device.last_seen_at = now
                    device.last_sync_at = now
                    device.last_error = None
                    _upsert_sync_record(
                        db=db,
                        collaborator_id=collab.id,
                        device_id=device.id,
                        status_val="removed",
                        face_synced=False,
                        last_error=None,
                        record=sync_record,
                    )
                    succeeded += 1
                    results.append(
                        CollaboratorSyncItemResult(
                            collaborator_id=collab.id,
                            sync_id=sync_record.id,
                            employee_no=sync_emp_no,
                            collaborator_name=collab.full_name,
                            device_id=device.id,
                            device_name=device.name,
                            action="delete",
                            success=True,
                            user_synced=False,
                            face_synced=False,
                            message=f"Baja aplicada en '{device.name}' para ID Sync #{sync_emp_no}",
                        )
                    )
                except ISAPIError as exc:
                    if exc.status_code in (401, 502, 504):
                        device_unreachable_error = exc.message
                        device.status = "offline" if exc.status_code in (502, 504) else "error"
                    device.last_error = exc.message[:500]
                    failed += 1
                    _upsert_sync_record(
                        db=db,
                        collaborator_id=collab.id,
                        device_id=device.id,
                        status_val="error",
                        face_synced=False,
                        last_error=exc.message,
                        record=sync_record,
                    )
                    results.append(
                        CollaboratorSyncItemResult(
                            collaborator_id=collab.id,
                            sync_id=sync_record.id,
                            employee_no=sync_emp_no,
                            collaborator_name=collab.full_name,
                            device_id=device.id,
                            device_name=device.name,
                            action="delete",
                            success=False,
                            user_synced=False,
                            face_synced=False,
                            message=exc.message,
                        )
                    )
            else:
                # action == "upsert"
                try:
                    upsert_res = client.upsert_employee(
                        employee_no=sync_emp_no,
                        name=collab.full_name[:120],
                        user_type=collab.user_type,
                        begin_time=collab.valid_begin_time,
                        end_time=collab.valid_end_time,
                    )
                    op_label = (
                        "actualizado"
                        if upsert_res.get("operation") == "updated"
                        else "registrado"
                    )
                    face_ok = False
                    face_warn: Optional[str] = None

                    if include_face and collab.has_face_image:
                        face_data_url = (
                            read_media_as_data_url(collab.face_media_path)
                            or collab.face_image_base64
                        )
                        if face_data_url:
                            try:
                                client.save_face_image(face_data_url, sync_emp_no)
                                face_ok = True
                            except ISAPIError as face_exc:
                                face_warn = face_exc.message

                    device.status = "online"
                    device.last_seen_at = now
                    device.last_sync_at = now
                    device.last_error = None
                    collab.last_synced_at = now

                    _upsert_sync_record(
                        db=db,
                        collaborator_id=collab.id,
                        device_id=device.id,
                        status_val="synced",
                        face_synced=face_ok,
                        last_error=face_warn,
                        record=sync_record,
                    )
                    succeeded += 1

                    if face_ok:
                        msg = f"Colaborador {op_label} (ID Sync #{sync_emp_no}) + fotografía facial JPEG sincronizada en '{device.name}'"
                    elif face_warn:
                        msg = f"Colaborador {op_label} (ID Sync #{sync_emp_no}) en '{device.name}', pero el rostro fue rechazado: {face_warn}"
                    else:
                        msg = f"Colaborador {op_label} (ID Sync #{sync_emp_no}) correctamente en '{device.name}' (sin foto facial)"

                    results.append(
                        CollaboratorSyncItemResult(
                            collaborator_id=collab.id,
                            sync_id=sync_record.id,
                            employee_no=sync_emp_no,
                            collaborator_name=collab.full_name,
                            device_id=device.id,
                            device_name=device.name,
                            action="upsert",
                            success=True,
                            user_synced=True,
                            face_synced=face_ok,
                            message=msg,
                        )
                    )
                except ISAPIError as exc:
                    if exc.status_code in (401, 502, 504):
                        device_unreachable_error = exc.message
                        device.status = "offline" if exc.status_code in (502, 504) else "error"
                    device.last_error = exc.message[:500]
                    failed += 1
                    _upsert_sync_record(
                        db=db,
                        collaborator_id=collab.id,
                        device_id=device.id,
                        status_val="error",
                        face_synced=False,
                        last_error=exc.message,
                        record=sync_record,
                    )
                    results.append(
                        CollaboratorSyncItemResult(
                            collaborator_id=collab.id,
                            sync_id=sync_record.id,
                            employee_no=sync_emp_no,
                            collaborator_name=collab.full_name,
                            device_id=device.id,
                            device_name=device.name,
                            action="upsert",
                            success=False,
                            user_synced=False,
                            face_synced=False,
                            message=exc.message,
                        )
                    )

    db.commit()
    return CollaboratorSyncResponse(
        company_id=company.id,
        company_name=company.name,
        action=action,
        total_operations=len(results),
        total_collaborators=len(collaborators),
        total_devices=len(devices),
        succeeded=succeeded,
        failed=failed,
        results=results,
    )


# ---------------------------------------------------------------------------
# Endpoints de Empresas (Multiempresa)
# ---------------------------------------------------------------------------


@router.get(
    "/companies",
    response_model=list[CompanyRead],
    dependencies=[
        Depends(
            require_any_permission(
                "companies:read",
                "collaborators:read",
                "devices:read",
            )
        )
    ],
    summary="Listar empresas registradas",
)
def list_companies(
    db: SessionDep,
    search: Optional[str] = Query(default=None, description="Filtrar por código, nombre o RFC"),
    is_active: Optional[bool] = Query(default=None),
) -> list[Company]:
    stmt = select(Company)
    if search and search.strip():
        term = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                Company.code.ilike(term),
                Company.name.ilike(term),
                Company.tax_id.ilike(term),
                Company.contact_name.ilike(term),
            )
        )
    if is_active is not None:
        stmt = stmt.where(Company.is_active == is_active)

    stmt = stmt.order_by(Company.id.asc())
    return list(db.scalars(stmt).all())


@router.post(
    "/companies",
    response_model=CompanyRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_permission("companies:write", "collaborators:write"))],
    summary="Crear una nueva empresa",
)
def create_company(db: SessionDep, payload: CompanyCreate) -> Company:
    existing_code = db.scalar(select(Company).where(Company.code == payload.code))
    if existing_code:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe una empresa con el código '{payload.code}'",
        )
    existing_name = db.scalar(select(Company).where(Company.name == payload.name))
    if existing_name:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe una empresa con el nombre '{payload.name}'",
        )

    company = Company(
        code=payload.code,
        name=payload.name,
        tax_id=payload.tax_id.strip() if payload.tax_id else None,
        address=payload.address.strip() if payload.address else None,
        contact_name=payload.contact_name.strip() if payload.contact_name else None,
        contact_email=payload.contact_email.strip() if payload.contact_email else None,
        contact_phone=payload.contact_phone.strip() if payload.contact_phone else None,
        is_active=payload.is_active,
        notes=payload.notes.strip() if payload.notes else None,
    )
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


@router.get(
    "/companies/{company_id}",
    response_model=CompanyRead,
    dependencies=[Depends(require_any_permission("companies:read", "collaborators:read", "devices:read"))],
    summary="Obtener detalle de una empresa",
)
def get_company(db: SessionDep, company_id: int) -> Company:
    return _get_company_or_404(db, company_id)


@router.patch(
    "/companies/{company_id}",
    response_model=CompanyRead,
    dependencies=[Depends(require_any_permission("companies:write", "collaborators:write"))],
    summary="Actualizar una empresa",
)
def update_company(
    db: SessionDep,
    company_id: int,
    payload: CompanyUpdate,
) -> Company:
    company = _get_company_or_404(db, company_id)

    if payload.code is not None and payload.code.strip().upper() != company.code:
        clean_code = payload.code.strip().upper()
        conflict = db.scalar(
            select(Company).where(Company.code == clean_code, Company.id != company.id)
        )
        if conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Ya existe otra empresa con el código '{clean_code}'",
            )
        company.code = clean_code

    if payload.name is not None and payload.name.strip() != company.name:
        clean_name = payload.name.strip()
        conflict = db.scalar(
            select(Company).where(Company.name == clean_name, Company.id != company.id)
        )
        if conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Ya existe otra empresa con el nombre '{clean_name}'",
            )
        company.name = clean_name

    if payload.tax_id is not None:
        company.tax_id = payload.tax_id.strip() or None
    if payload.address is not None:
        company.address = payload.address.strip() or None
    if payload.contact_name is not None:
        company.contact_name = payload.contact_name.strip() or None
    if payload.contact_email is not None:
        company.contact_email = payload.contact_email.strip() or None
    if payload.contact_phone is not None:
        company.contact_phone = payload.contact_phone.strip() or None
    if payload.is_active is not None:
        company.is_active = payload.is_active
    if payload.notes is not None:
        company.notes = payload.notes.strip() or None

    db.commit()
    db.refresh(company)
    return company


@router.delete(
    "/companies/{company_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_any_permission("companies:delete", "collaborators:delete"))],
    summary="Eliminar una empresa",
)
def delete_company(db: SessionDep, company_id: int) -> None:
    company = _get_company_or_404(db, company_id)
    if company.devices_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"No se puede eliminar la empresa '{company.name}' porque tiene "
                f"{company.devices_count} reloj(es) asignado(s). Reasigna o elimina primero los relojes."
            ),
        )
    if company.collaborators_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"No se puede eliminar la empresa '{company.name}' porque tiene "
                f"{company.collaborators_count} colaborador(es) registrado(s)."
            ),
        )
    db.delete(company)
    db.commit()


# ---------------------------------------------------------------------------
# Endpoints de Colaboradores y Sincronización ISAPI (Individual y Masiva)
# ---------------------------------------------------------------------------


@router.get(
    "/collaborators",
    response_model=list[CollaboratorRead],
    dependencies=[Depends(require_any_permission("collaborators:read", "devices:isapi", "attendance:read", "shifts:read"))],
    summary="Listar colaboradores (filtrable por empresa, turno, sucursal, geocerca, búsqueda y estado)",
)
def list_collaborators(
    db: SessionDep,
    company_id: Optional[int] = Query(default=None, description="Filtrar colaboradores por empresa"),
    shift_id: Optional[int] = Query(default=None, description="Filtrar colaboradores por turno asignado"),
    branch_id: Optional[int] = Query(default=None, description="Filtrar colaboradores por sucursal"),
    geofence_id: Optional[int] = Query(default=None, description="Filtrar colaboradores por geocerca asignada"),
    search: Optional[str] = Query(
        default=None,
        description="Filtrar por No. empleado, nombre, apellidos, puesto o departamento",
    ),
    is_active: Optional[bool] = Query(default=None),
    has_face: Optional[bool] = Query(default=None, description="Filtrar por si cuenta con foto facial JPEG"),
) -> list[Collaborator]:
    stmt = select(Collaborator)
    if company_id is not None:
        stmt = stmt.where(Collaborator.company_id == company_id)
    if shift_id is not None:
        if shift_id == 0:
            stmt = stmt.where(Collaborator.shift_id.is_(None))
        else:
            stmt = stmt.where(Collaborator.shift_id == shift_id)
    if branch_id is not None:
        if branch_id == 0:
            stmt = stmt.where(Collaborator.branch_id.is_(None))
        else:
            stmt = stmt.where(Collaborator.branch_id == branch_id)
    if geofence_id is not None:
        assigned_collab_ids = select(GeofenceAssignment.collaborator_id).where(
            GeofenceAssignment.geofence_id == geofence_id,
            GeofenceAssignment.is_active.is_(True),
            GeofenceAssignment.collaborator_id.is_not(None),
        )
        stmt = stmt.where(Collaborator.id.in_(assigned_collab_ids))
    if search and search.strip():
        term = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                Collaborator.employee_no.ilike(term),
                Collaborator.first_name.ilike(term),
                Collaborator.last_name.ilike(term),
                Collaborator.nss.ilike(term),
                Collaborator.rfc.ilike(term),
                Collaborator.department.ilike(term),
                Collaborator.position.ilike(term),
                Collaborator.email.ilike(term),
            )
        )
    if is_active is not None:
        stmt = stmt.where(Collaborator.is_active == is_active)
    if has_face is True:
        stmt = stmt.where(
            or_(
                Collaborator.face_media_path.is_not(None),
                Collaborator.face_image_base64.is_not(None),
            )
        )
    elif has_face is False:
        stmt = stmt.where(
            Collaborator.face_media_path.is_(None),
            Collaborator.face_image_base64.is_(None),
        )

    stmt = stmt.order_by(Collaborator.company_id.asc(), Collaborator.employee_no.asc())
    return list(db.scalars(stmt).all())


@router.post(
    "/collaborators",
    response_model=CollaboratorRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_permission("collaborators:write"))],
    summary="Registrar un nuevo colaborador en una empresa (con sincronización opcional en relojes)",
)
def create_collaborator(
    db: SessionDep,
    payload: CollaboratorCreate,
) -> Collaborator:
    company = _get_company_or_404(db, payload.company_id)

    conflict = db.scalar(
        select(Collaborator).where(
            Collaborator.company_id == company.id,
            Collaborator.employee_no == payload.employee_no,
        )
    )
    if conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Ya existe un colaborador con el No. de empleado '{payload.employee_no}' "
                f"en la empresa '{company.name}' ({conflict.full_name})"
            ),
        )

    jpeg_bytes: Optional[bytes] = None
    if payload.face_image_base64 and payload.face_image_base64.strip():
        try:
            jpeg_bytes = extract_jpeg_bytes_from_base64(payload.face_image_base64)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    resolved_shift_id: Optional[int] = None
    if payload.shift_id:
        shift_obj = db.get(Shift, payload.shift_id)
        if not shift_obj:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="El turno laboral seleccionado no existe",
            )
        if shift_obj.company_id is not None and shift_obj.company_id != company.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"El turno '{shift_obj.name}' pertenece a otra empresa y no puede asignarse a '{company.name}'.",
            )
        resolved_shift_id = shift_obj.id

    resolved_branch_id: Optional[int] = None
    if payload.branch_id:
        branch_obj = db.get(Branch, payload.branch_id)
        if not branch_obj or branch_obj.company_id != company.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La sucursal seleccionada no existe o no pertenece a la empresa.",
            )
        resolved_branch_id = branch_obj.id

    resolved_geofence: Optional[Geofence] = None
    if payload.geofence_id:
        gf_obj = db.get(Geofence, payload.geofence_id)
        if not gf_obj or gf_obj.company_id != company.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="La geocerca seleccionada no existe o no pertenece a la empresa.",
            )
        resolved_geofence = gf_obj
        if resolved_branch_id is None and gf_obj.branch_id:
            resolved_branch_id = gf_obj.branch_id

    collab = Collaborator(
        company_id=company.id,
        shift_id=resolved_shift_id,
        branch_id=resolved_branch_id,
        employee_no=payload.employee_no,
        first_name=payload.first_name,
        last_name=payload.last_name,
        nss=payload.nss.strip() if payload.nss and payload.nss.strip() else None,
        rfc=payload.rfc.strip().upper() if payload.rfc and payload.rfc.strip() else None,
        email=payload.email.strip() if payload.email else None,
        phone=payload.phone.strip() if payload.phone else None,
        department=payload.department.strip() if payload.department else None,
        position=payload.position.strip() if payload.position else None,
        card_no=payload.card_no.strip() if payload.card_no else None,
        user_type=payload.user_type,
        valid_begin_time=payload.valid_begin_time.strip() if payload.valid_begin_time else None,
        valid_end_time=payload.valid_end_time.strip() if payload.valid_end_time else None,
        face_image_base64=None,
        is_active=payload.is_active,
        notes=payload.notes.strip() if payload.notes else None,
    )
    db.add(collab)
    db.flush()

    if resolved_geofence is not None:
        db.add(
            GeofenceAssignment(
                company_id=company.id,
                collaborator_id=collab.id,
                branch_id=resolved_branch_id,
                geofence_id=resolved_geofence.id,
                event_type="all",
                priority=10,
                is_active=True,
            )
        )

    if jpeg_bytes is not None:
        stored_face = store_collaborator_face_image(
            company_id=company.id,
            collaborator_id=collab.id,
            jpeg_bytes=jpeg_bytes,
            previous_version=None,
            previous_media_path=None,
        )
        collab.face_media_path = stored_face.media_path
        collab.face_reference_version = stored_face.version
        collab.face_sha256 = stored_face.sha256

    db.commit()
    db.refresh(collab)

    if payload.sync_device_ids:
        target_devices = list(
            db.scalars(
                select(HikvisionDevice).where(
                    HikvisionDevice.id.in_(payload.sync_device_ids),
                    HikvisionDevice.company_id == company.id,
                )
            ).all()
        )
        if target_devices:
            _execute_collaborators_device_sync(
                db=db,
                company=company,
                collaborators=[collab],
                devices=target_devices,
                action="upsert",
                include_face=True,
            )
            db.refresh(collab)

    return collab


@router.post(
    "/collaborators/bulk-sync-devices",
    response_model=CollaboratorSyncResponse,
    dependencies=[Depends(require_any_permission("collaborators:sync", "devices:isapi"))],
    summary="Registro o baja masiva de colaboradores de una empresa en los relojes de dicha empresa",
)
def bulk_sync_collaborators_to_devices(
    db: SessionDep,
    payload: CollaboratorBulkSyncDevicesRequest,
) -> CollaboratorSyncResponse:
    company = _get_company_or_404(db, payload.company_id)

    if payload.sync_all_company_collaborators:
        stmt = (
            select(Collaborator)
            .where(
                Collaborator.company_id == company.id,
                Collaborator.is_active.is_(True),
            )
            .order_by(Collaborator.employee_no.asc())
        )
        collaborators = list(db.scalars(stmt).all())
    else:
        if not payload.collaborator_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Debes seleccionar al menos un colaborador o activar la opción de sincronizar toda la empresa",
            )
        stmt = (
            select(Collaborator)
            .where(Collaborator.id.in_(payload.collaborator_ids))
            .order_by(Collaborator.employee_no.asc())
        )
        collaborators = list(db.scalars(stmt).all())

    if not collaborators:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No se encontraron colaboradores para sincronizar en la empresa '{company.name}'",
        )

    devices = list(
        db.scalars(
            select(HikvisionDevice)
            .where(HikvisionDevice.id.in_(payload.device_ids))
            .order_by(HikvisionDevice.id.asc())
        ).all()
    )
    if not devices:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No se encontraron los relojes Hikvision seleccionados",
        )

    return _execute_collaborators_device_sync(
        db=db,
        company=company,
        collaborators=collaborators,
        devices=devices,
        action=payload.action,
        include_face=payload.include_face,
    )


@router.get(
    "/collaborators/{collaborator_id}",
    response_model=CollaboratorRead,
    dependencies=[Depends(require_any_permission("collaborators:read", "devices:isapi"))],
    summary="Obtener detalle de un colaborador",
)
def get_collaborator(db: SessionDep, collaborator_id: int) -> Collaborator:
    return _get_collaborator_or_404(db, collaborator_id)


@router.patch(
    "/collaborators/{collaborator_id}",
    response_model=CollaboratorRead,
    dependencies=[Depends(require_any_permission("collaborators:write"))],
    summary="Actualizar datos o biometría facial de un colaborador",
)
def update_collaborator(
    db: SessionDep,
    collaborator_id: int,
    payload: CollaboratorUpdate,
) -> Collaborator:
    collab = _get_collaborator_or_404(db, collaborator_id)

    target_company_id = payload.company_id if payload.company_id is not None else collab.company_id
    target_emp_no = (
        payload.employee_no.strip()
        if payload.employee_no is not None
        else collab.employee_no
    )

    if target_company_id != collab.company_id:
        _get_company_or_404(db, target_company_id)

    if target_company_id != collab.company_id or target_emp_no != collab.employee_no:
        conflict = db.scalar(
            select(Collaborator).where(
                Collaborator.company_id == target_company_id,
                Collaborator.employee_no == target_emp_no,
                Collaborator.id != collab.id,
            )
        )
        if conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Ya existe otro colaborador con el No. de empleado '{target_emp_no}' "
                    f"en esa empresa ({conflict.full_name})"
                ),
            )
        collab.company_id = target_company_id
        collab.employee_no = target_emp_no

    if "shift_id" in payload.model_fields_set:
        if not payload.shift_id:
            collab.shift_id = None
        else:
            shift_obj = db.get(Shift, payload.shift_id)
            if not shift_obj:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="El turno laboral seleccionado no existe",
                )
            if shift_obj.company_id is not None and shift_obj.company_id != collab.company_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"El turno '{shift_obj.name}' pertenece a otra empresa.",
                )
            collab.shift_id = shift_obj.id

    if "branch_id" in payload.model_fields_set:
        if not payload.branch_id:
            collab.branch_id = None
        else:
            branch_obj = db.get(Branch, payload.branch_id)
            if not branch_obj or branch_obj.company_id != collab.company_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="La sucursal seleccionada no existe o pertenece a otra empresa.",
                )
            collab.branch_id = branch_obj.id

    if "geofence_id" in payload.model_fields_set:
        existing_direct_assigns = list(
            db.scalars(
                select(GeofenceAssignment).where(
                    GeofenceAssignment.collaborator_id == collab.id,
                    GeofenceAssignment.event_type == "all",
                )
            ).all()
        )
        if not payload.geofence_id:
            for item in existing_direct_assigns:
                item.is_active = False
        else:
            gf_obj = db.get(Geofence, payload.geofence_id)
            if not gf_obj or gf_obj.company_id != collab.company_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="La geocerca seleccionada no existe o pertenece a otra empresa.",
                )
            if collab.branch_id is None and gf_obj.branch_id:
                collab.branch_id = gf_obj.branch_id
            if existing_direct_assigns:
                primary_assign = existing_direct_assigns[0]
                primary_assign.geofence_id = gf_obj.id
                primary_assign.branch_id = collab.branch_id
                primary_assign.is_active = True
                for extra in existing_direct_assigns[1:]:
                    extra.is_active = False
            else:
                db.add(
                    GeofenceAssignment(
                        company_id=collab.company_id,
                        collaborator_id=collab.id,
                        branch_id=collab.branch_id,
                        geofence_id=gf_obj.id,
                        event_type="all",
                        priority=10,
                        is_active=True,
                    )
                )

    if payload.first_name is not None:
        collab.first_name = payload.first_name.strip()
    if payload.last_name is not None:
        collab.last_name = payload.last_name.strip()
    if payload.nss is not None:
        collab.nss = payload.nss.strip() or None
    if payload.rfc is not None:
        collab.rfc = payload.rfc.strip().upper() or None
    if payload.email is not None:
        collab.email = payload.email.strip() or None
    if payload.phone is not None:
        collab.phone = payload.phone.strip() or None
    if payload.department is not None:
        collab.department = payload.department.strip() or None
    if payload.position is not None:
        collab.position = payload.position.strip() or None
    if payload.card_no is not None:
        collab.card_no = payload.card_no.strip() or None
    if payload.user_type is not None:
        collab.user_type = payload.user_type
    if payload.valid_begin_time is not None:
        collab.valid_begin_time = payload.valid_begin_time.strip() or None
    if payload.valid_end_time is not None:
        collab.valid_end_time = payload.valid_end_time.strip() or None
    if payload.face_image_base64 is not None:
        if payload.face_image_base64.strip() == "":
            if collab.face_media_path:
                get_media_storage().delete(collab.face_media_path)
            collab.face_media_path = None
            collab.face_reference_version = None
            collab.face_sha256 = None
            collab.face_image_base64 = None
        else:
            try:
                jpeg_bytes = extract_jpeg_bytes_from_base64(payload.face_image_base64)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=str(exc),
                ) from exc
            stored_face = store_collaborator_face_image(
                company_id=collab.company_id,
                collaborator_id=collab.id,
                jpeg_bytes=jpeg_bytes,
                previous_version=collab.face_reference_version,
                previous_media_path=collab.face_media_path,
            )
            collab.face_media_path = stored_face.media_path
            collab.face_reference_version = stored_face.version
            collab.face_sha256 = stored_face.sha256
            collab.face_image_base64 = None
    if payload.is_active is not None:
        collab.is_active = payload.is_active
    if payload.notes is not None:
        collab.notes = payload.notes.strip() or None

    db.commit()
    db.refresh(collab)
    return collab


@router.delete(
    "/collaborators/{collaborator_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_any_permission("collaborators:delete"))],
    summary="Eliminar un colaborador (con baja opcional en los relojes de su empresa)",
)
def delete_collaborator(
    db: SessionDep,
    collaborator_id: int,
    remove_from_devices: bool = Query(
        default=False,
        description="Si es True, intenta dar de baja el employeeNo en los relojes donde estaba sincronizado antes de eliminar",
    ),
) -> None:
    collab = _get_collaborator_or_404(db, collaborator_id)
    if remove_from_devices and collab.device_syncs:
        for sync_rec in collab.device_syncs:
            if sync_rec.status == "synced" and sync_rec.device:
                try:
                    client = _build_isapi_client(sync_rec.device)
                    client.baja_employee([str(sync_rec.id)])
                except Exception:
                    pass
    if collab.face_media_path:
        get_media_storage().delete(collab.face_media_path)
    db.delete(collab)
    db.commit()


@router.post(
    "/collaborators/{collaborator_id}/sync-devices",
    response_model=CollaboratorSyncResponse,
    dependencies=[Depends(require_any_permission("collaborators:sync", "devices:isapi"))],
    summary="Registro o baja individual de un colaborador en uno o varios relojes de su empresa",
)
def sync_single_collaborator_to_devices(
    db: SessionDep,
    collaborator_id: int,
    payload: CollaboratorSyncDeviceRequest,
) -> CollaboratorSyncResponse:
    collab = _get_collaborator_or_404(db, collaborator_id)
    company = _get_company_or_404(db, collab.company_id)

    devices = list(
        db.scalars(
            select(HikvisionDevice)
            .where(HikvisionDevice.id.in_(payload.device_ids))
            .order_by(HikvisionDevice.id.asc())
        ).all()
    )
    if not devices:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No se encontraron los relojes Hikvision seleccionados",
        )

    return _execute_collaborators_device_sync(
        db=db,
        company=company,
        collaborators=[collab],
        devices=devices,
        action=payload.action,
        include_face=payload.include_face,
    )
