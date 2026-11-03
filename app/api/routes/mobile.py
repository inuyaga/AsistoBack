from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
import hmac
import json
import secrets
from typing import Any, Optional
import uuid

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
import jwt
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, SessionDep
from app.api.routes.attendance import (
    _classify_logs_by_shift,
    _deduplicate_rapid_logs,
    _ensure_utc,
    _resolve_tz,
    require_any_permission,
    sync_collaborator_daily_attendance,
)
from app.core.config import settings
from app.core.geofence import evaluate_geofence
from app.core.security import (
    create_access_token,
    decode_access_token,
    decode_signed_media_token,
    generate_opaque_token,
    hash_opaque_token,
)
from app.core.storage import (
    extract_jpeg_bytes_from_base64,
    get_media_storage,
    store_collaborator_face_image,
    store_event_photo_image,
)
from app.models.attendance import AttendanceLog, DailyAttendance, Shift
from app.models.collaborator import Collaborator, Company
from app.models.mobile import (
    Branch,
    Geofence,
    GeofenceAssignment,
    MobileActivationChallenge,
    MobileActivationCode,
    MobileDevice,
)
from app.models.user import User
from app.schemas.mobile import (
    ActivationCodeGenerateRequest,
    ActivationCodeGenerateResponse,
    ActivationCompleteRequest,
    ActivationCompleteResponse,
    ActivationValidateRequest,
    ActivationValidateResponse,
    AttendanceHistoryDetailResponse,
    AttendanceHistoryEventDetail,
    AttendanceHistoryItem,
    AttendanceHistoryListResponse,
    AttendanceHistoryPagination,
    BranchCreate,
    BranchRead,
    BranchUpdate,
    BulkGeofenceAssignRequest,
    BulkGeofenceAssignResponse,
    CheckInRequest,
    CheckInResponse,
    CheckOutRequest,
    CheckOutResponse,
    CollaboratorGeofenceTestRequest,
    CollaboratorGeofenceTestResponse,
    CurrentAttendanceResponse,
    CurrentShiftInfo,
    GeofenceAssignmentCreate,
    GeofenceAssignmentRead,
    GeofenceCoordinatePoint,
    GeofenceCreate,
    GeofenceRead,
    GeofenceResolvedGeometry,
    GeofenceResolveRequest,
    GeofenceResolveResponse,
    GeofenceRulesInfo,
    GeofenceUpdate,
    MealActionRequest,
    MealEndResponse,
    MealStartResponse,
    MeActivationResponse,
    MeDeviceUnlinkRequest,
    MeProfileResponse,
    MeScheduleInfo,
    MobileBranchBrief,
    MobileBranchInfo,
    MobileCompanyInfo,
    MobileDeviceRead,
    MobileDeviceRevokeRequest,
    MobileDeviceSessionRequest,
    MobileDeviceSessionResponse,
    MobileEmployeeInfo,
    MobileFaceReferenceInfo,
    MobileRefreshRequest,
    MobileRefreshResponse,
    PushTokenUpdateRequest,
)

router = APIRouter()


class MobileAPIError(Exception):
    """Exception formatted according to the Asisto mobile API contract."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Optional[Any] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


async def mobile_api_error_handler(_: Request, exc: MobileAPIError) -> JSONResponse:
    """Render MobileAPIError using the standard contract error envelope."""
    req_id = f"req_{uuid.uuid4().hex[:12].upper()}"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
                "requestId": req_id,
            }
        },
    )


def _parse_prefixed_id(raw: Optional[str | int]) -> Optional[int]:
    """Parse an integer ID from strings like 'emp_00125', 'dev_001', 'shift_001', '125'."""
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    cleaned = str(raw).strip()
    if not cleaned:
        return None
    if cleaned.isdigit():
        return int(cleaned)
    parts = cleaned.split("_")
    if parts[-1].isdigit():
        return int(parts[-1])
    return None


def _normalize_activation_code(raw_code: str) -> str:
    return raw_code.strip().upper().replace(" ", "")


def _build_company_info(company: Company) -> MobileCompanyInfo:
    return MobileCompanyInfo(
        id=f"cmp_{company.id:03d}",
        name=company.name,
        timezone=getattr(company, "timezone", None) or "America/Mexico_City",
    )


def _build_employee_info(collab: Collaborator) -> MobileEmployeeInfo:
    return MobileEmployeeInfo(
        id=f"emp_{collab.id:05d}",
        employeeNumber=collab.employee_no,
        fullName=collab.full_name,
        role=collab.position,
        area=collab.department,
    )


def _resolve_collaborator_branch(db: Session, collab: Collaborator) -> MobileBranchInfo:
    if collab.branch is not None:
        return MobileBranchInfo(
            id=collab.branch.public_id,
            name=collab.branch.name,
            address=collab.branch.address or (collab.company.address if collab.company else None),
        )
    if collab.branch_id is not None:
        br = db.get(Branch, collab.branch_id)
        if br:
            return MobileBranchInfo(
                id=br.public_id,
                name=br.name,
                address=br.address or (collab.company.address if collab.company else None),
            )
    default_branch = db.scalar(
        select(Branch)
        .where(Branch.company_id == collab.company_id, Branch.is_active.is_(True))
        .order_by(Branch.id.asc())
        .limit(1)
    )
    if default_branch:
        return MobileBranchInfo(
            id=default_branch.public_id,
            name=default_branch.name,
            address=default_branch.address,
        )
    company_addr = collab.company.address if collab.company else None
    return MobileBranchInfo(
        id=f"br_{collab.company_id:03d}",
        name="Oficina Principal",
        address=company_addr or "Sucursal Principal",
    )


def _ensure_collaborator_face_stored_in_media(
    db: Session,
    collab: Collaborator,
) -> None:
    """If a collaborator only has legacy face_image_base64 in DB, migrate it on-the-fly to MediaStorageService."""
    if collab.face_media_path and collab.face_sha256 and collab.face_reference_version:
        return
    if collab.face_image_base64 and collab.face_image_base64.strip():
        jpeg_bytes = extract_jpeg_bytes_from_base64(collab.face_image_base64)
        stored = store_collaborator_face_image(
            company_id=collab.company_id,
            collaborator_id=collab.id,
            jpeg_bytes=jpeg_bytes,
            previous_version=collab.face_reference_version,
            previous_media_path=collab.face_media_path,
        )
        collab.face_media_path = stored.media_path
        collab.face_reference_version = stored.version
        collab.face_sha256 = stored.sha256
        collab.face_image_base64 = None
        db.flush()


@dataclass
class MobileAuthContext:
    """Authenticated actor context for mobile endpoints (either a linked MobileDevice or an Admin User)."""

    collaborator: Optional[Collaborator]
    mobile_device: Optional[MobileDevice]
    admin_user: Optional[User]

    def ensure_can_access_employee(self, db: Session, employee_id_raw: str) -> Collaborator:
        emp_id = _parse_prefixed_id(employee_id_raw)
        target_collab: Optional[Collaborator] = None
        if emp_id is not None:
            target_collab = db.get(Collaborator, emp_id)
        if target_collab is None:
            target_collab = db.scalar(
                select(Collaborator).where(Collaborator.employee_no == employee_id_raw.strip())
            )
        if target_collab is None:
            raise MobileAPIError(
                status_code=status.HTTP_404_NOT_FOUND,
                code="EMPLOYEE_NOT_FOUND",
                message="El trabajador especificado no existe.",
            )
        if not target_collab.is_active:
            raise MobileAPIError(
                status_code=status.HTTP_423_LOCKED,
                code="EMPLOYEE_DISABLED",
                message="El trabajador se encuentra deshabilitado.",
            )

        if self.admin_user is not None:
            if self.admin_user.is_superuser or self.admin_user.has_permission("attendance:read"):
                return target_collab
            raise MobileAPIError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="DEVICE_NOT_AUTHORIZED",
                message="No cuenta con permisos para operar sobre este trabajador.",
            )

        if self.collaborator is None or self.collaborator.id != target_collab.id:
            raise MobileAPIError(
                status_code=status.HTTP_423_LOCKED,
                code="DEVICE_NOT_AUTHORIZED",
                message="El dispositivo autenticado no corresponde al trabajador solicitado.",
            )
        return target_collab


def get_mobile_auth_context(
    db: SessionDep,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_device_id: Optional[str] = Header(default=None, alias="X-Device-Id"),
) -> MobileAuthContext:
    """Validate Bearer token and X-Device-Id header for Asisto mobile endpoints."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise MobileAPIError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="UNAUTHORIZED",
            message="Se requiere un token de acceso Bearer válido.",
        )

    raw_token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_access_token(raw_token)
    except jwt.ExpiredSignatureError as exc:
        raise MobileAPIError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="ACCESS_TOKEN_EXPIRED",
            message="El token de acceso ha expirado.",
        ) from exc
    except jwt.PyJWTError as exc:
        raise MobileAPIError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="ACCESS_TOKEN_INVALID",
            message="El token de acceso no es válido.",
        ) from exc

    token_type = payload.get("token_type") or "admin_user"
    if token_type == "mobile_access":
        collab_id = _parse_prefixed_id(payload.get("sub"))
        device_id = _parse_prefixed_id(payload.get("dev"))
        if collab_id is None or device_id is None:
            raise MobileAPIError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="ACCESS_TOKEN_INVALID",
                message="El token móvil no contiene identificadores válidos.",
            )

        collab = db.get(Collaborator, collab_id)
        if not collab or not collab.is_active:
            raise MobileAPIError(
                status_code=status.HTTP_423_LOCKED,
                code="EMPLOYEE_DISABLED",
                message="El trabajador está deshabilitado.",
            )

        device = db.get(MobileDevice, device_id)
        if not device or device.collaborator_id != collab.id:
            raise MobileAPIError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="DEVICE_NOT_AUTHORIZED",
                message="El dispositivo no está autorizado para este trabajador.",
            )
        if device.status != "active":
            raise MobileAPIError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="ACTIVATION_REVOKED",
                message="La vinculación de este dispositivo fue revocada.",
            )

        if x_device_id and x_device_id.strip():
            header_dev_id = _parse_prefixed_id(x_device_id)
            if header_dev_id != device.id and x_device_id.strip() != device.installation_id:
                raise MobileAPIError(
                    status_code=status.HTTP_423_LOCKED,
                    code="DEVICE_NOT_AUTHORIZED",
                    message="El encabezado X-Device-Id no coincide con el dispositivo vinculado.",
                )

        device.last_seen_at = datetime.now(timezone.utc)
        db.commit()
        return MobileAuthContext(
            collaborator=collab,
            mobile_device=device,
            admin_user=None,
        )

    # Soporte para actor administrativo con permisos sobre el trabajador
    user_id = _parse_prefixed_id(payload.get("sub"))
    user = db.get(User, user_id) if user_id is not None else None
    if not user or not user.is_active:
        raise MobileAPIError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="UNAUTHORIZED",
            message="Usuario administrativo inválido o inactivo.",
        )
    return MobileAuthContext(
        collaborator=None,
        mobile_device=None,
        admin_user=user,
    )


def _issue_mobile_session_tokens(
    collab: Collaborator,
    device: MobileDevice,
) -> tuple[str, str, int]:
    """Generate a short-lived JWT accessToken and rotate the opaque refreshToken on MobileDevice."""
    now = datetime.now(timezone.utc)
    expires_in = settings.MOBILE_ACCESS_TOKEN_EXPIRE_SECONDS
    access_token = create_access_token(
        subject=collab.id,
        expires_delta=timedelta(seconds=expires_in),
        extra_claims={
            "token_type": "mobile_access",
            "dev": device.public_id,
            "act": device.activation_public_id,
            "emp": collab.public_id,
            "cmp": f"cmp_{collab.company_id:03d}",
        },
    )
    refresh_plain, refresh_hash = generate_opaque_token("rft")
    device.refresh_token_hash = refresh_hash
    device.refresh_token_expires_at = now + timedelta(days=settings.MOBILE_REFRESH_TOKEN_EXPIRE_DAYS)
    device.last_seen_at = now
    return access_token, refresh_plain, expires_in


# ---------------------------------------------------------------------------
# 1. Descarga Firmada y Temporal de Archivos Media (Local / S3)
# ---------------------------------------------------------------------------


@router.get(
    "/media/signed/{signed_token}",
    summary="Descargar archivo media (referencia facial) mediante token firmado y temporal",
)
def download_signed_media(signed_token: str) -> Response:
    try:
        token_data = decode_signed_media_token(signed_token)
    except jwt.ExpiredSignatureError as exc:
        raise MobileAPIError(
            status_code=status.HTTP_410_GONE,
            code="MEDIA_URL_EXPIRED",
            message="El enlace temporal de descarga ha expirado.",
        ) from exc
    except Exception as exc:
        raise MobileAPIError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="MEDIA_TOKEN_INVALID",
            message="El enlace firmado de descarga no es válido.",
        ) from exc

    media_path = str(token_data.get("media_path") or "")
    expected_sha256 = token_data.get("sha256")
    storage = get_media_storage()
    try:
        raw_bytes = storage.get_bytes(media_path)
    except FileNotFoundError as exc:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="FACE_REFERENCE_NOT_AVAILABLE",
            message="El archivo de referencia facial no se encontró en el almacenamiento.",
        ) from exc

    headers = {
        "Cache-Control": "private, no-store, max-age=0",
    }
    if expected_sha256:
        headers["X-Content-Sha256"] = str(expected_sha256)
        headers["ETag"] = f'"{expected_sha256}"'

    return Response(
        content=raw_bytes,
        media_type="image/jpeg",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# 2. Endpoints de Activación y Vinculación de Dispositivo (/activations, /auth, /me)
# ---------------------------------------------------------------------------


@router.post(
    "/activations/validate",
    response_model=ActivationValidateResponse,
    summary="Validar código de activación manual o QR (Fase Onboarding)",
)
def validate_activation_code(
    db: SessionDep,
    payload: ActivationValidateRequest,
) -> ActivationValidateResponse:
    if not payload.activationCode or not payload.activationCode.strip():
        raise MobileAPIError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="ACTIVATION_CODE_REQUIRED",
            message="No se envió el código de activación.",
        )

    now = datetime.now(timezone.utc)
    normalized_code = _normalize_activation_code(payload.activationCode)
    code_hash = hash_opaque_token(normalized_code)

    act_code = db.scalar(
        select(MobileActivationCode).where(MobileActivationCode.code_hash == code_hash)
    )
    if not act_code:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="ACTIVATION_CODE_INVALID",
            message="El código de activación no existe.",
        )

    if act_code.status == "used":
        raise MobileAPIError(
            status_code=status.HTTP_409_CONFLICT,
            code="ACTIVATION_CODE_ALREADY_USED",
            message="El código de activación ya fue utilizado.",
        )

    exp_utc = _ensure_utc(act_code.expires_at)
    if act_code.status in ("expired", "revoked") or exp_utc <= now:
        if act_code.status == "pending":
            act_code.status = "expired"
            db.commit()
        raise MobileAPIError(
            status_code=status.HTTP_410_GONE,
            code="ACTIVATION_CODE_EXPIRED",
            message="El código de activación ha expirado.",
        )

    collab = db.get(Collaborator, act_code.collaborator_id)
    if not collab or not collab.is_active:
        raise MobileAPIError(
            status_code=status.HTTP_423_LOCKED,
            code="EMPLOYEE_DISABLED",
            message="El trabajador asociado se encuentra deshabilitado.",
        )

    company = db.get(Company, collab.company_id)
    if not company or not company.is_active:
        raise MobileAPIError(
            status_code=status.HTTP_423_LOCKED,
            code="EMPLOYEE_DISABLED",
            message="La empresa asociada se encuentra inactiva.",
        )

    val_plain, val_hash = generate_opaque_token("avt")
    expires_at = now + timedelta(minutes=settings.MOBILE_VALIDATION_TOKEN_EXPIRE_MINUTES)

    challenge = MobileActivationChallenge(
        activation_code_id=act_code.id,
        collaborator_id=collab.id,
        installation_id=payload.device.installationId.strip(),
        token_hash=val_hash,
        status="pending",
        attempts=0,
        expires_at=expires_at,
    )
    db.add(challenge)
    db.commit()

    return ActivationValidateResponse(
        validationToken=val_plain,
        expiresAt=expires_at,
        company=_build_company_info(company),
        employee=_build_employee_info(collab),
        branch=_resolve_collaborator_branch(db, collab),
    )


@router.post(
    "/activations/complete",
    response_model=ActivationCompleteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Confirmar datos y vincular dispositivo móvil al trabajador",
)
def complete_activation(
    db: SessionDep,
    payload: ActivationCompleteRequest,
) -> ActivationCompleteResponse:
    now = datetime.now(timezone.utc)
    token_hash = hash_opaque_token(payload.validationToken)

    challenge = db.scalar(
        select(MobileActivationChallenge).where(
            MobileActivationChallenge.token_hash == token_hash
        )
    )
    if not challenge:
        raise MobileAPIError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="VALIDATION_TOKEN_INVALID",
            message="El token de validación no es válido o ha expirado.",
        )

    challenge.attempts += 1
    if _ensure_utc(challenge.expires_at) <= now or challenge.status == "expired":
        challenge.status = "expired"
        db.commit()
        raise MobileAPIError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="VALIDATION_TOKEN_INVALID",
            message="El token de validación ha expirado.",
        )

    if challenge.installation_id != payload.device.installationId.strip():
        db.commit()
        raise MobileAPIError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="VALIDATION_TOKEN_INVALID",
            message="El installationId no coincide con el desafío de validación.",
        )

    act_code = db.get(MobileActivationCode, challenge.activation_code_id)
    if not act_code:
        raise MobileAPIError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="VALIDATION_TOKEN_INVALID",
            message="Código de activación asociado no encontrado.",
        )

    collab = db.get(Collaborator, challenge.collaborator_id)
    if not collab or not collab.is_active:
        raise MobileAPIError(
            status_code=status.HTTP_423_LOCKED,
            code="EMPLOYEE_DISABLED",
            message="El trabajador se encuentra deshabilitado.",
        )

    company = db.get(Company, collab.company_id)
    if not company:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="COMPANY_NOT_FOUND",
            message="Empresa no encontrada.",
        )

    # Verificar que el colaborador tenga referencia facial disponible en MediaStorageService
    _ensure_collaborator_face_stored_in_media(db, collab)
    if not collab.face_media_path or not collab.face_sha256:
        raise MobileAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="FACE_REFERENCE_NOT_AVAILABLE",
            message="El trabajador no cuenta con una fotografía de referencia facial registrada.",
        )

    # Verificar si ya existe otro dispositivo activo vinculado a este colaborador
    existing_active_device = db.scalar(
        select(MobileDevice).where(
            MobileDevice.collaborator_id == collab.id,
            MobileDevice.status == "active",
        )
    )
    if (
        existing_active_device is not None
        and existing_active_device.installation_id != payload.device.installationId.strip()
    ):
        raise MobileAPIError(
            status_code=status.HTTP_409_CONFLICT,
            code="DEVICE_ALREADY_LINKED",
            message="Ya existe otro dispositivo móvil activo vinculado a este trabajador.",
        )

    # Si la activación ya fue completada por una instalación distinta, rechazar con 409
    if challenge.status == "consumed" or act_code.status == "used":
        if (
            existing_active_device is None
            or existing_active_device.activation_code_id != act_code.id
            or existing_active_device.installation_id != payload.device.installationId.strip()
        ):
            raise MobileAPIError(
                status_code=status.HTTP_409_CONFLICT,
                code="ACTIVATION_ALREADY_COMPLETED",
                message="La activación ya fue completada previamente.",
            )

    cred_plain, cred_hash = generate_opaque_token("dc")

    if existing_active_device is not None:
        mobile_dev = existing_active_device
        mobile_dev.activation_code_id = act_code.id
        mobile_dev.platform = payload.device.platform
        mobile_dev.operating_system = payload.device.operatingSystem
        mobile_dev.manufacturer = payload.device.manufacturer
        mobile_dev.model = payload.device.model
        mobile_dev.app_version = payload.device.appVersion
        mobile_dev.app_build = payload.device.appBuild
        if payload.device.pushToken is not None:
            mobile_dev.push_token = payload.device.pushToken
        mobile_dev.device_credential_hash = cred_hash
        mobile_dev.activated_at = now
    else:
        mobile_dev = MobileDevice(
            activation_code_id=act_code.id,
            company_id=company.id,
            collaborator_id=collab.id,
            branch_id=act_code.branch_id or collab.branch_id,
            installation_id=payload.device.installationId.strip(),
            platform=payload.device.platform,
            operating_system=payload.device.operatingSystem,
            manufacturer=payload.device.manufacturer,
            model=payload.device.model,
            app_version=payload.device.appVersion,
            app_build=payload.device.appBuild,
            push_token=payload.device.pushToken,
            device_credential_hash=cred_hash,
            status="active",
            activated_at=now,
            last_seen_at=now,
        )
        db.add(mobile_dev)
        db.flush()

    access_token, refresh_token, expires_in = _issue_mobile_session_tokens(collab, mobile_dev)

    challenge.status = "consumed"
    challenge.consumed_at = now
    act_code.status = "used"
    act_code.used_at = now

    storage = get_media_storage()
    signed_url, signed_expires = storage.get_signed_download_url(
        relative_path=collab.face_media_path,
        sha256=collab.face_sha256,
        expires_in_seconds=settings.MOBILE_SIGNED_MEDIA_EXPIRE_SECONDS,
    )

    db.commit()
    db.refresh(mobile_dev)

    return ActivationCompleteResponse(
        activationId=act_code.public_id,
        deviceId=mobile_dev.public_id,
        deviceCredential=cred_plain,
        activatedAt=mobile_dev.activated_at,
        accessToken=access_token,
        refreshToken=refresh_token,
        expiresIn=expires_in,
        employee=_build_employee_info(collab),
        company=_build_company_info(company),
        branch=_resolve_collaborator_branch(db, collab),
        faceReference=MobileFaceReferenceInfo(
            version=collab.face_reference_version or "face_1",
            downloadUrl=signed_url,
            expiresAt=signed_expires,
            sha256=collab.face_sha256,
        ),
    )


@router.post(
    "/auth/refresh",
    response_model=MobileRefreshResponse,
    summary="Renovar sesión móvil mediante rotación de refreshToken",
)
def refresh_mobile_session(
    db: SessionDep,
    payload: MobileRefreshRequest,
) -> MobileRefreshResponse:
    now = datetime.now(timezone.utc)
    dev_id = _parse_prefixed_id(payload.deviceId)
    device = db.get(MobileDevice, dev_id) if dev_id is not None else None
    if not device:
        raise MobileAPIError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="DEVICE_NOT_AUTHORIZED",
            message="El dispositivo no está registrado o autorizado.",
        )

    if device.status != "active":
        raise MobileAPIError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="ACTIVATION_REVOKED",
            message="La vinculación del dispositivo fue revocada.",
        )

    collab = db.get(Collaborator, device.collaborator_id)
    if not collab or not collab.is_active:
        raise MobileAPIError(
            status_code=status.HTTP_423_LOCKED,
            code="EMPLOYEE_DISABLED",
            message="El trabajador está deshabilitado.",
        )

    provided_hash = hash_opaque_token(payload.refreshToken)
    if (
        not device.refresh_token_hash
        or not hmac.compare_digest(provided_hash, device.refresh_token_hash)
        or (
            device.refresh_token_expires_at is not None
            and _ensure_utc(device.refresh_token_expires_at) <= now
        )
    ):
        raise MobileAPIError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="REFRESH_TOKEN_INVALID",
            message="El refresh token ha expirado o no es válido.",
        )

    access_token, new_refresh_token, expires_in = _issue_mobile_session_tokens(collab, device)
    db.commit()

    return MobileRefreshResponse(
        accessToken=access_token,
        refreshToken=new_refresh_token,
        expiresIn=expires_in,
    )


@router.post(
    "/auth/device-session",
    response_model=MobileDeviceSessionResponse,
    summary="Recuperar sesión utilizando la credencial persistente del dispositivo vinculado",
)
def recover_mobile_device_session(
    db: SessionDep,
    payload: MobileDeviceSessionRequest,
) -> MobileDeviceSessionResponse:
    dev_id = _parse_prefixed_id(payload.deviceId)
    act_id = _parse_prefixed_id(payload.activationId)

    device = db.get(MobileDevice, dev_id) if dev_id is not None else None
    if not device:
        raise MobileAPIError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="DEVICE_NOT_AUTHORIZED",
            message="El dispositivo no está autorizado.",
        )

    if device.status != "active":
        raise MobileAPIError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="ACTIVATION_REVOKED",
            message="La activación del dispositivo ha sido revocada.",
        )

    if (
        device.installation_id != payload.installationId.strip()
        or (
            act_id is not None
            and device.activation_code_id is not None
            and device.activation_code_id != act_id
        )
    ):
        raise MobileAPIError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="DEVICE_NOT_AUTHORIZED",
            message="Los identificadores de activación e instalación no coinciden con la vinculación.",
        )

    collab = db.get(Collaborator, device.collaborator_id)
    if not collab or not collab.is_active:
        raise MobileAPIError(
            status_code=status.HTTP_423_LOCKED,
            code="EMPLOYEE_DISABLED",
            message="El trabajador está deshabilitado.",
        )

    provided_cred_hash = hash_opaque_token(payload.deviceCredential)
    if not hmac.compare_digest(provided_cred_hash, device.device_credential_hash):
        device.failed_session_attempts += 1
        db.commit()
        raise MobileAPIError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="DEVICE_CREDENTIAL_INVALID",
            message="La credencial del dispositivo no es válida.",
        )

    # Rotar deviceCredential y emitir nuevos tokens en la misma transacción
    new_cred_plain, new_cred_hash = generate_opaque_token("dc")
    device.device_credential_hash = new_cred_hash
    device.failed_session_attempts = 0
    access_token, refresh_token, expires_in = _issue_mobile_session_tokens(collab, device)
    db.commit()

    return MobileDeviceSessionResponse(
        accessToken=access_token,
        refreshToken=refresh_token,
        deviceCredential=new_cred_plain,
        expiresIn=expires_in,
    )


@router.get(
    "/me/activation",
    response_model=MeActivationResponse,
    summary="Consultar activación vigente del dispositivo autenticado",
)
def get_me_activation(
    db: SessionDep,
    ctx: MobileAuthContext = Depends(get_mobile_auth_context),
) -> MeActivationResponse:
    if ctx.collaborator is None or ctx.mobile_device is None:
        raise MobileAPIError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="DEVICE_NOT_AUTHORIZED",
            message="Este endpoint requiere una sesión de dispositivo móvil activo.",
        )
    company = db.get(Company, ctx.collaborator.company_id)
    if not company:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="COMPANY_NOT_FOUND",
            message="Empresa no encontrada.",
        )
    return MeActivationResponse(
        activationId=ctx.mobile_device.activation_public_id,
        deviceId=ctx.mobile_device.public_id,
        activatedAt=ctx.mobile_device.activated_at,
        status=ctx.mobile_device.status,
        company=_build_company_info(company),
        employee=_build_employee_info(ctx.collaborator),
        branch=_resolve_collaborator_branch(db, ctx.collaborator),
    )


@router.delete(
    "/me/device",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Desvincular el dispositivo móvil actual",
)
def unlink_me_device(
    db: SessionDep,
    payload: Optional[MeDeviceUnlinkRequest] = None,
    ctx: MobileAuthContext = Depends(get_mobile_auth_context),
) -> None:
    if ctx.mobile_device is None:
        raise MobileAPIError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="DEVICE_NOT_AUTHORIZED",
            message="No hay un dispositivo móvil vinculado en la sesión.",
        )
    now = datetime.now(timezone.utc)
    ctx.mobile_device.status = "revoked"
    ctx.mobile_device.revoke_reason = (
        payload.reason if payload and payload.reason else "DEVICE_REPLACED"
    )
    ctx.mobile_device.revoked_at = now
    ctx.mobile_device.refresh_token_hash = None
    ctx.mobile_device.refresh_token_expires_at = None
    db.commit()


@router.get(
    "/me/profile",
    response_model=MeProfileResponse,
    summary="Consultar perfil operativo y horario del trabajador autenticado",
)
def get_me_profile(
    db: SessionDep,
    ctx: MobileAuthContext = Depends(get_mobile_auth_context),
) -> MeProfileResponse:
    if ctx.collaborator is None:
        raise MobileAPIError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="DEVICE_NOT_AUTHORIZED",
            message="Se requiere sesión de trabajador.",
        )
    collab = ctx.collaborator
    schedule_info: Optional[MeScheduleInfo] = None
    if collab.shift is not None:
        sh = collab.shift
        start_str = f"{sh.check_in_time}:00" if len(sh.check_in_time) == 5 else sh.check_in_time
        end_str = f"{sh.check_out_time}:00" if len(sh.check_out_time) == 5 else sh.check_out_time
        schedule_info = MeScheduleInfo(
            id=f"sch_{sh.id:03d}",
            startTime=start_str,
            endTime=end_str,
            mealMinutes=sh.meal_duration_minutes or 0 if sh.has_meal_break else 0,
            timezone=sh.timezone or "America/Mexico_City",
        )
    return MeProfileResponse(
        id=collab.public_id,
        employeeNumber=collab.employee_no,
        fullName=collab.full_name,
        role=collab.position,
        area=collab.department,
        branch=_resolve_collaborator_branch(db, collab),
        schedule=schedule_info,
    )


@router.put(
    "/me/device/push-token",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Actualizar token push (FCM / APNS) del dispositivo móvil",
)
def update_me_push_token(
    db: SessionDep,
    payload: PushTokenUpdateRequest,
    ctx: MobileAuthContext = Depends(get_mobile_auth_context),
) -> None:
    if ctx.mobile_device is None:
        raise MobileAPIError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="DEVICE_NOT_AUTHORIZED",
            message="Se requiere sesión de dispositivo móvil.",
        )
    ctx.mobile_device.push_token = payload.pushToken.strip()
    ctx.mobile_device.push_provider = payload.provider
    db.commit()


# ---------------------------------------------------------------------------
# 3. Resolución y Validación de Geocercas (/employees/{employeeId}/geofences/resolve)
# ---------------------------------------------------------------------------


def _resolve_active_geofence_for_collaborator(
    db: Session,
    collab: Collaborator,
    business_date: date,
    event_type: str,
) -> tuple[Geofence, Optional[GeofenceAssignment], MobileBranchInfo]:
    """Find the highest-priority active Geofence assigned to the collaborator, shift, branch, or company."""
    stmt = (
        select(GeofenceAssignment)
        .where(
            GeofenceAssignment.company_id == collab.company_id,
            GeofenceAssignment.is_active.is_(True),
            GeofenceAssignment.event_type.in_(["all", event_type]),
            or_(
                GeofenceAssignment.valid_from_date.is_(None),
                GeofenceAssignment.valid_from_date <= business_date,
            ),
            or_(
                GeofenceAssignment.valid_until_date.is_(None),
                GeofenceAssignment.valid_until_date >= business_date,
            ),
            or_(
                GeofenceAssignment.collaborator_id == collab.id,
                GeofenceAssignment.collaborator_id.is_(None),
            ),
            or_(
                GeofenceAssignment.shift_id == collab.shift_id,
                GeofenceAssignment.shift_id.is_(None),
            ),
            or_(
                GeofenceAssignment.branch_id == collab.branch_id,
                GeofenceAssignment.branch_id.is_(None),
            ),
        )
        .order_by(GeofenceAssignment.priority.asc(), GeofenceAssignment.id.desc())
    )
    candidates = list(db.scalars(stmt).all())

    # Ordenar por especificidad: 1° colaborador explícito, 2° turno explícito, 3° sucursal explícita
    def _specificity_key(a: GeofenceAssignment) -> tuple[int, int, int, int]:
        return (
            0 if a.collaborator_id == collab.id else 1,
            0 if (collab.shift_id and a.shift_id == collab.shift_id) else 1,
            0 if (collab.branch_id and a.branch_id == collab.branch_id) else 1,
            a.priority,
        )

    candidates.sort(key=_specificity_key)
    for assign in candidates:
        gf = assign.geofence or db.get(Geofence, assign.geofence_id)
        if gf and gf.is_active:
            br_obj = assign.branch or gf.branch
            if br_obj:
                br_info = MobileBranchInfo(
                    id=br_obj.public_id,
                    name=br_obj.name,
                    address=br_obj.address,
                )
            else:
                br_info = _resolve_collaborator_branch(db, collab)
            return gf, assign, br_info

    # Fallback: geocerca activa de la sucursal del colaborador o de su empresa
    fallback_stmt = select(Geofence).where(
        Geofence.company_id == collab.company_id,
        Geofence.is_active.is_(True),
    )
    if collab.branch_id is not None:
        branch_gf = db.scalar(
            fallback_stmt.where(Geofence.branch_id == collab.branch_id).order_by(Geofence.id.asc())
        )
        if branch_gf:
            return branch_gf, None, _resolve_collaborator_branch(db, collab)

    company_gf = db.scalar(fallback_stmt.order_by(Geofence.id.asc()).limit(1))
    if company_gf:
        if company_gf.branch:
            br_info = MobileBranchInfo(
                id=company_gf.branch.public_id,
                name=company_gf.branch.name,
                address=company_gf.branch.address,
            )
        else:
            br_info = _resolve_collaborator_branch(db, collab)
        return company_gf, None, br_info

    raise MobileAPIError(
        status_code=status.HTTP_404_NOT_FOUND,
        code="GEOFENCE_ASSIGNMENT_NOT_FOUND",
        message="No existe una geocerca asignada vigente para este trabajador y fecha.",
    )


def _get_day_attendance_state(
    db: Session,
    collab: Collaborator,
    now_utc: datetime,
) -> tuple[
    date,
    tzinfo,
    Optional[DailyAttendance],
    Optional[AttendanceLog],
    Optional[AttendanceLog],
    Optional[AttendanceLog],
    Optional[AttendanceLog],
]:
    """Inspect today's DailyAttendance and classified AttendanceLogs for a collaborator."""
    tz_name = (
        collab.shift.timezone
        if collab.shift and collab.shift.timezone
        else (collab.company.timezone if collab.company else "America/Mexico_City")
    )
    tz = _resolve_tz(tz_name)
    business_date = now_utc.astimezone(tz).date()

    local_start = datetime.combine(business_date, time(0, 0, 0), tzinfo=tz).astimezone(timezone.utc)
    local_end = datetime.combine(business_date, time(23, 59, 59, 999999), tzinfo=tz).astimezone(
        timezone.utc
    )

    day_logs = list(
        db.scalars(
            select(AttendanceLog)
            .where(
                AttendanceLog.collaborator_id == collab.id,
                AttendanceLog.is_valid_attendance.is_(True),
                AttendanceLog.event_time >= local_start,
                AttendanceLog.event_time <= local_end,
            )
            .order_by(AttendanceLog.event_time.asc(), AttendanceLog.id.asc())
        ).all()
    )

    daily = db.scalar(
        select(DailyAttendance).where(
            DailyAttendance.collaborator_id == collab.id,
            DailyAttendance.attendance_date == business_date,
        )
    )
    if day_logs and (daily is None or daily.logs_count != len(day_logs)):
        daily = sync_collaborator_daily_attendance(db, collab.id, now_utc)

    effective_logs = _deduplicate_rapid_logs(day_logs)
    check_in_log, meal_out_log, meal_in_log, check_out_log = _classify_logs_by_shift(
        effective_logs,
        business_date,
        None,
        tz,
    )
    return (
        business_date,
        tz,
        daily,
        check_in_log,
        meal_out_log,
        meal_in_log,
        check_out_log,
    )


@router.post(
    "/employees/{employeeId}/geofences/resolve",
    response_model=GeofenceResolveResponse,
    summary="Resolver la geocerca vigente antes de registrar entrada o salida",
)
def resolve_employee_geofence(
    employeeId: str,
    payload: GeofenceResolveRequest,
    db: SessionDep,
    ctx: MobileAuthContext = Depends(get_mobile_auth_context),
) -> GeofenceResolveResponse:
    collab = ctx.ensure_can_access_employee(db, employeeId)
    now_utc = datetime.now(timezone.utc)

    business_date, tz, _daily, check_in_log, _meal_out, _meal_in, check_out_log = (
        _get_day_attendance_state(db, collab, now_utc)
    )

    if payload.eventType == "check_out":
        if check_in_log is None or check_out_log is not None:
            raise MobileAPIError(
                status_code=status.HTTP_409_CONFLICT,
                code="SHIFT_NOT_ACTIVE",
                message="Se solicitó geocerca de salida sin una jornada activa.",
            )

    geofence, assignment, branch_info = _resolve_active_geofence_for_collaborator(
        db=db,
        collab=collab,
        business_date=business_date,
        event_type=payload.eventType,
    )

    valid_from = datetime.combine(business_date, time(4, 0, 0), tzinfo=tz)
    valid_until = datetime.combine(
        business_date + timedelta(days=1), time(4, 0, 0), tzinfo=tz
    )
    assign_id_str = (
        f"gassign_{business_date.strftime('%Y_%m_%d')}_asg_{assignment.id}_emp_{collab.id:05d}"
        if assignment
        else f"gassign_{business_date.strftime('%Y_%m_%d')}_geo_{geofence.id}_emp_{collab.id:05d}"
    )

    if geofence.geofence_type == "polygonal":
        verts = [
            GeofenceCoordinatePoint(latitude=v["latitude"], longitude=v["longitude"])
            for v in geofence.vertices
        ]
        geo_obj = GeofenceResolvedGeometry(
            id=geofence.public_id,
            version=geofence.version,
            type="polygonal",
            vertices=verts,
        )
    else:
        geo_obj = GeofenceResolvedGeometry(
            id=geofence.public_id,
            version=geofence.version,
            type="radial",
            center=GeofenceCoordinatePoint(
                latitude=geofence.center_latitude or 0.0,
                longitude=geofence.center_longitude or 0.0,
            ),
            radiusMeters=geofence.radius_meters or 150.0,
        )

    return GeofenceResolveResponse(
        assignmentId=assign_id_str,
        businessDate=business_date.isoformat(),
        validFrom=valid_from,
        validUntil=valid_until,
        branch=branch_info,
        geofence=geo_obj,
        rules=GeofenceRulesInfo(
            maximumLocationAccuracyMeters=geofence.max_accuracy_meters,
        ),
    )


# ---------------------------------------------------------------------------
# 4. Endpoints de Jornada y Asistencia Móvil (/employees/{employeeId}/attendance/...)
# ---------------------------------------------------------------------------


def _compute_live_shift_metrics(
    now_utc: datetime,
    check_in_dt: datetime,
    meal_out_dt: Optional[datetime],
    meal_in_dt: Optional[datetime],
    check_out_dt: Optional[datetime],
) -> tuple[int, int, str]:
    """Compute workedSeconds, mealSeconds, and mealStatus strictly on the server."""
    c_in = _ensure_utc(check_in_dt)
    end_ref = _ensure_utc(check_out_dt) if check_out_dt else now_utc

    meal_seconds = 0
    meal_status = "not_started"
    if meal_out_dt is not None:
        m_out = _ensure_utc(meal_out_dt)
        if meal_in_dt is not None:
            m_in = _ensure_utc(meal_in_dt)
            meal_seconds = max(0, int((m_in - m_out).total_seconds()))
            meal_status = "completed"
        else:
            meal_seconds = max(0, int((end_ref - m_out).total_seconds()))
            meal_status = "active"

    gross_seconds = max(0, int((end_ref - c_in).total_seconds()))
    worked_seconds = max(0, gross_seconds - meal_seconds)
    return worked_seconds, meal_seconds, meal_status


@router.get(
    "/employees/{employeeId}/attendance/current",
    response_model=CurrentAttendanceResponse,
    summary="Consultar el estado actual de la jornada del trabajador",
)
def get_employee_current_attendance(
    employeeId: str,
    db: SessionDep,
    ctx: MobileAuthContext = Depends(get_mobile_auth_context),
) -> CurrentAttendanceResponse:
    collab = ctx.ensure_can_access_employee(db, employeeId)
    now_utc = datetime.now(timezone.utc)

    business_date, _tz, daily, check_in_log, meal_out_log, meal_in_log, check_out_log = (
        _get_day_attendance_state(db, collab, now_utc)
    )

    if check_in_log is None:
        return CurrentAttendanceResponse(status="not_started", shift=None)

    worked_sec, meal_sec, meal_status = _compute_live_shift_metrics(
        now_utc=now_utc,
        check_in_dt=check_in_log.event_time,
        meal_out_dt=meal_out_log.event_time if meal_out_log else None,
        meal_in_dt=meal_in_log.event_time if meal_in_log else None,
        check_out_dt=check_out_log.event_time if check_out_log else None,
    )

    branch_info = _resolve_collaborator_branch(db, collab)
    shift_id_str = f"shift_{daily.id:05d}" if daily else f"shift_{business_date.isoformat()}"

    if check_out_log is not None:
        top_status = "completed"
    elif meal_status == "active":
        top_status = "meal_active"
    else:
        top_status = "active"

    return CurrentAttendanceResponse(
        status=top_status,  # type: ignore[arg-type]
        shift=CurrentShiftInfo(
            id=shift_id_str,
            businessDate=business_date.isoformat(),
            checkInAt=_ensure_utc(check_in_log.event_time),
            checkOutAt=_ensure_utc(check_out_log.event_time) if check_out_log else None,
            workedSeconds=worked_sec,
            mealSeconds=meal_sec,
            mealStatus=meal_status,  # type: ignore[arg-type]
            mealStartedAt=_ensure_utc(meal_out_log.event_time) if meal_out_log else None,
            mealEndedAt=_ensure_utc(meal_in_log.event_time) if meal_in_log else None,
            branch=MobileBranchBrief(id=branch_info.id, name=branch_info.name),
        ),
    )


def _validate_event_geofence_on_server(
    db: Session,
    collab: Collaborator,
    business_date: date,
    event_type: str,
    geofence_id_raw: str,
    latitude: float,
    longitude: float,
    accuracy_meters: float,
) -> tuple[Geofence, MobileBranchInfo, float | None]:
    """Recalculate geofence containment and GPS accuracy on the backend."""
    gf_id = _parse_prefixed_id(geofence_id_raw)
    geofence: Optional[Geofence] = db.get(Geofence, gf_id) if gf_id is not None else None
    branch_info: MobileBranchInfo

    if geofence is None or geofence.company_id != collab.company_id or not geofence.is_active:
        geofence, _assign, branch_info = _resolve_active_geofence_for_collaborator(
            db=db,
            collab=collab,
            business_date=business_date,
            event_type=event_type,
        )
    else:
        if geofence.branch:
            branch_info = MobileBranchInfo(
                id=geofence.branch.public_id,
                name=geofence.branch.name,
                address=geofence.branch.address,
            )
        else:
            branch_info = _resolve_collaborator_branch(db, collab)

    eval_res = evaluate_geofence(
        latitude=latitude,
        longitude=longitude,
        accuracy_meters=accuracy_meters,
        geofence_type=geofence.geofence_type,
        max_accuracy_meters=geofence.max_accuracy_meters,
        center_lat=geofence.center_latitude,
        center_lon=geofence.center_longitude,
        radius_meters=geofence.radius_meters,
        vertices=geofence.vertices,
    )
    if not eval_res.accuracy_valid:
        raise MobileAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code=eval_res.reason_code or "LOCATION_ACCURACY_TOO_LOW",
            message=eval_res.reason_message or "La precisión del GPS es insuficiente.",
        )
    if not eval_res.inside:
        raise MobileAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code=eval_res.reason_code or "OUTSIDE_GEOFENCE",
            message=eval_res.reason_message or "Te encuentras fuera de la geocerca autorizada.",
        )

    return geofence, branch_info, eval_res.distance_meters


@router.post(
    "/employees/{employeeId}/attendance/check-ins",
    response_model=CheckInResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar entrada de jornada validando geocerca e identidad en backend",
)
def register_employee_check_in(
    employeeId: str,
    payload: CheckInRequest,
    request: Request,
    db: SessionDep,
    ctx: MobileAuthContext = Depends(get_mobile_auth_context),
) -> CheckInResponse:
    collab = ctx.ensure_can_access_employee(db, employeeId)
    now_utc = datetime.now(timezone.utc)
    clean_event_id = payload.clientEventId.strip()

    # 1. Idempotencia por clientEventId
    existing_log = db.scalar(
        select(AttendanceLog).where(AttendanceLog.client_event_id == clean_event_id)
    )
    if existing_log is not None and existing_log.collaborator_id == collab.id:
        daily = sync_collaborator_daily_attendance(db, collab.id, existing_log.event_time)
        br_info = _resolve_collaborator_branch(db, collab)
        return CheckInResponse(
            attendanceId=f"att_{existing_log.id:05d}",
            shiftId=f"shift_{daily.id:05d}" if daily else f"shift_{existing_log.id:05d}",
            status="active",
            checkInAt=_ensure_utc(existing_log.event_time),
            serverReceivedAt=_ensure_utc(existing_log.created_at),
            branch=MobileBranchBrief(id=br_info.id, name=br_info.name),
        )

    business_date, _tz, _daily, check_in_log, _m_out, _m_in, _c_out = _get_day_attendance_state(
        db, collab, now_utc
    )
    if check_in_log is not None:
        raise MobileAPIError(
            status_code=status.HTTP_409_CONFLICT,
            code="SHIFT_ALREADY_ACTIVE",
            message="Ya existe una jornada iniciada para la fecha de negocio actual.",
        )

    # 2. Validar evidencia de verificación facial y prueba de vida
    if (
        not payload.identity.livenessPassed
        or not payload.identity.matchPassed
        or payload.identity.matchScore < settings.MOBILE_MIN_FACE_MATCH_SCORE
    ):
        raise MobileAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="FACE_VERIFICATION_REQUIRED",
            message="La verificación biométrica facial o prueba de vida no fue superada.",
        )

    # 3. Recalcular y validar geocerca en el servidor
    geofence, branch_info, dist_m = _validate_event_geofence_on_server(
        db=db,
        collab=collab,
        business_date=business_date,
        event_type="check_in",
        geofence_id_raw=payload.geofence.id,
        latitude=payload.geofence.latitude,
        longitude=payload.geofence.longitude,
        accuracy_meters=payload.geofence.accuracyMeters,
    )

    # 4. Si se adjuntó foto de evidencia opcional, guardar archivo mediante MediaStorageService y persistir solo el path
    photo_media_path: Optional[str] = None
    if payload.photoBase64 and payload.photoBase64.strip():
        try:
            photo_bytes = extract_jpeg_bytes_from_base64(payload.photoBase64)
            stored_photo = store_event_photo_image(
                company_id=collab.company_id,
                collaborator_id=collab.id,
                event_id_or_token=clean_event_id,
                jpeg_bytes=photo_bytes,
            )
            photo_media_path = stored_photo.media_path
        except ValueError:
            photo_media_path = None

    dev_obj = ctx.mobile_device
    dev_public_id = dev_obj.public_id if dev_obj else (payload.deviceId or "mobile_app")
    dev_label = (
        f"{dev_obj.manufacturer or ''} {dev_obj.model or ''}".strip()
        if dev_obj and (dev_obj.manufacturer or dev_obj.model)
        else "App Móvil Asisto"
    )
    resolved_branch_id = geofence.branch_id or collab.branch_id or _parse_prefixed_id(branch_info.id)

    log_entry = AttendanceLog(
        source_type="mobile_device",
        company_id=collab.company_id,
        collaborator_id=collab.id,
        device_id=None,
        sync_id=None,
        device_employee_no=collab.employee_no,
        collaborator_employee_no=collab.employee_no,
        collaborator_name=collab.full_name,
        device_name=dev_label,
        source_ip=request.client.host if request.client else None,
        event_type="MobileAttendanceEvent",
        major_event=5,
        minor_event=75,
        event_label="Entrada registrada en App Móvil (Geocerca + Rostro)",
        attendance_status="checkIn",
        verify_mode=payload.identity.method,
        is_valid_attendance=True,
        client_event_id=clean_event_id,
        mobile_device_id=dev_public_id,
        mobile_device_fk=dev_obj.id if dev_obj else None,
        branch_id=resolved_branch_id,
        captured_at=payload.capturedAt,
        latitude=f"{payload.geofence.latitude:.8f}",
        longitude=f"{payload.geofence.longitude:.8f}",
        accuracy_meters=payload.geofence.accuracyMeters,
        geofence_id=geofence.id,
        geofence_version=geofence.version,
        geofence_assignment_id=payload.geofence.assignmentId,
        inside_geofence=True,
        distance_meters=dist_m,
        identity_method=payload.identity.method,
        face_reference_version=payload.identity.referenceVersion,
        liveness_passed=payload.identity.livenessPassed,
        match_passed=payload.identity.matchPassed,
        match_score=payload.identity.matchScore,
        photo_media_path=photo_media_path,
        raw_payload=json.dumps(payload.model_dump(mode="json", exclude={"photoBase64"}), ensure_ascii=False),
        event_time=now_utc,
        created_at=now_utc,
    )
    db.add(log_entry)
    db.commit()
    db.refresh(log_entry)

    daily_record = sync_collaborator_daily_attendance(db, collab.id, now_utc)
    shift_id_str = f"shift_{daily_record.id:05d}" if daily_record else f"shift_{log_entry.id:05d}"

    return CheckInResponse(
        attendanceId=f"att_{log_entry.id:05d}",
        shiftId=shift_id_str,
        status="active",
        checkInAt=now_utc,
        serverReceivedAt=now_utc,
        branch=MobileBranchBrief(id=branch_info.id, name=branch_info.name),
    )


@router.post(
    "/employees/{employeeId}/attendance/shifts/{shiftId}/meal/start",
    response_model=MealStartResponse,
    summary="Registrar inicio de comida en la jornada activa",
)
def start_employee_meal_break(
    employeeId: str,
    shiftId: str,
    payload: MealActionRequest,
    request: Request,
    db: SessionDep,
    ctx: MobileAuthContext = Depends(get_mobile_auth_context),
) -> MealStartResponse:
    collab = ctx.ensure_can_access_employee(db, employeeId)
    now_utc = datetime.now(timezone.utc)
    clean_event_id = payload.clientEventId.strip()

    # Idempotencia por clientEventId
    existing_log = db.scalar(
        select(AttendanceLog).where(AttendanceLog.client_event_id == clean_event_id)
    )
    if existing_log is not None and existing_log.collaborator_id == collab.id:
        daily = sync_collaborator_daily_attendance(db, collab.id, existing_log.event_time)
        return MealStartResponse(
            shiftId=f"shift_{daily.id:05d}" if daily else shiftId,
            mealStatus="active",
            mealStartedAt=_ensure_utc(existing_log.event_time),
        )

    _b_date, _tz, daily, check_in_log, meal_out_log, _meal_in_log, check_out_log = (
        _get_day_attendance_state(db, collab, now_utc)
    )
    if check_in_log is None:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="ACTIVE_SHIFT_NOT_FOUND",
            message="No existe una jornada activa para iniciar comida.",
        )
    if check_out_log is not None:
        raise MobileAPIError(
            status_code=status.HTTP_409_CONFLICT,
            code="SHIFT_ALREADY_COMPLETED",
            message="La jornada ya fue finalizada.",
        )
    if collab.shift is not None and not collab.shift.has_meal_break:
        raise MobileAPIError(
            status_code=status.HTTP_409_CONFLICT,
            code="MEAL_BREAK_NOT_ALLOWED",
            message="El turno asignado no contempla horario de comida.",
        )
    if meal_out_log is not None:
        raise MobileAPIError(
            status_code=status.HTTP_409_CONFLICT,
            code="MEAL_BREAK_ALREADY_STARTED",
            message="El registro de comida ya fue iniciado en esta jornada.",
        )

    dev_obj = ctx.mobile_device
    dev_public_id = dev_obj.public_id if dev_obj else (payload.deviceId or "mobile_app")
    dev_label = (
        f"{dev_obj.manufacturer or ''} {dev_obj.model or ''}".strip()
        if dev_obj and (dev_obj.manufacturer or dev_obj.model)
        else "App Móvil Asisto"
    )

    log_entry = AttendanceLog(
        source_type="mobile_device",
        company_id=collab.company_id,
        collaborator_id=collab.id,
        device_employee_no=collab.employee_no,
        collaborator_employee_no=collab.employee_no,
        collaborator_name=collab.full_name,
        device_name=dev_label,
        source_ip=request.client.host if request.client else None,
        event_type="MobileAttendanceEvent",
        major_event=5,
        minor_event=75,
        event_label="Salida a comida registrada en App Móvil",
        attendance_status="breakOut",
        verify_mode="mobile_session",
        is_valid_attendance=True,
        client_event_id=clean_event_id,
        mobile_device_id=dev_public_id,
        mobile_device_fk=dev_obj.id if dev_obj else None,
        branch_id=collab.branch_id,
        captured_at=payload.capturedAt,
        raw_payload=json.dumps(payload.model_dump(mode="json"), ensure_ascii=False),
        event_time=now_utc,
        created_at=now_utc,
    )
    db.add(log_entry)
    db.commit()
    db.refresh(log_entry)

    daily = sync_collaborator_daily_attendance(db, collab.id, now_utc)
    return MealStartResponse(
        shiftId=f"shift_{daily.id:05d}" if daily else shiftId,
        mealStatus="active",
        mealStartedAt=now_utc,
    )


@router.post(
    "/employees/{employeeId}/attendance/shifts/{shiftId}/meal/end",
    response_model=MealEndResponse,
    summary="Registrar regreso de comida en la jornada activa",
)
def end_employee_meal_break(
    employeeId: str,
    shiftId: str,
    payload: MealActionRequest,
    request: Request,
    db: SessionDep,
    ctx: MobileAuthContext = Depends(get_mobile_auth_context),
) -> MealEndResponse:
    collab = ctx.ensure_can_access_employee(db, employeeId)
    now_utc = datetime.now(timezone.utc)
    clean_event_id = payload.clientEventId.strip()

    _b_date, _tz, daily, check_in_log, meal_out_log, meal_in_log, check_out_log = (
        _get_day_attendance_state(db, collab, now_utc)
    )

    # Idempotencia por clientEventId
    existing_log = db.scalar(
        select(AttendanceLog).where(AttendanceLog.client_event_id == clean_event_id)
    )
    if existing_log is not None and existing_log.collaborator_id == collab.id:
        daily = sync_collaborator_daily_attendance(db, collab.id, existing_log.event_time)
        m_start = _ensure_utc(meal_out_log.event_time) if meal_out_log else _ensure_utc(existing_log.event_time)
        m_end = _ensure_utc(existing_log.event_time)
        return MealEndResponse(
            shiftId=f"shift_{daily.id:05d}" if daily else shiftId,
            mealStatus="completed",
            mealStartedAt=m_start,
            mealEndedAt=m_end,
            mealSeconds=max(0, int((m_end - m_start).total_seconds())),
        )

    if check_in_log is None:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="ACTIVE_SHIFT_NOT_FOUND",
            message="No existe una jornada activa.",
        )
    if check_out_log is not None:
        raise MobileAPIError(
            status_code=status.HTTP_409_CONFLICT,
            code="SHIFT_ALREADY_COMPLETED",
            message="La jornada ya fue finalizada.",
        )
    if meal_out_log is None:
        raise MobileAPIError(
            status_code=status.HTTP_409_CONFLICT,
            code="MEAL_BREAK_NOT_ACTIVE",
            message="No hay una comida en curso para finalizar.",
        )
    if meal_in_log is not None:
        raise MobileAPIError(
            status_code=status.HTTP_409_CONFLICT,
            code="MEAL_BREAK_ALREADY_COMPLETED",
            message="El regreso de comida ya fue registrado previamente.",
        )

    dev_obj = ctx.mobile_device
    dev_public_id = dev_obj.public_id if dev_obj else (payload.deviceId or "mobile_app")
    dev_label = (
        f"{dev_obj.manufacturer or ''} {dev_obj.model or ''}".strip()
        if dev_obj and (dev_obj.manufacturer or dev_obj.model)
        else "App Móvil Asisto"
    )

    log_entry = AttendanceLog(
        source_type="mobile_device",
        company_id=collab.company_id,
        collaborator_id=collab.id,
        device_employee_no=collab.employee_no,
        collaborator_employee_no=collab.employee_no,
        collaborator_name=collab.full_name,
        device_name=dev_label,
        source_ip=request.client.host if request.client else None,
        event_type="MobileAttendanceEvent",
        major_event=5,
        minor_event=75,
        event_label="Regreso de comida registrado en App Móvil",
        attendance_status="breakIn",
        verify_mode="mobile_session",
        is_valid_attendance=True,
        client_event_id=clean_event_id,
        mobile_device_id=dev_public_id,
        mobile_device_fk=dev_obj.id if dev_obj else None,
        branch_id=collab.branch_id,
        captured_at=payload.capturedAt,
        raw_payload=json.dumps(payload.model_dump(mode="json"), ensure_ascii=False),
        event_time=now_utc,
        created_at=now_utc,
    )
    db.add(log_entry)
    db.commit()
    db.refresh(log_entry)

    daily = sync_collaborator_daily_attendance(db, collab.id, now_utc)
    m_start = _ensure_utc(meal_out_log.event_time)
    meal_seconds = max(0, int((now_utc - m_start).total_seconds()))

    return MealEndResponse(
        shiftId=f"shift_{daily.id:05d}" if daily else shiftId,
        mealStatus="completed",
        mealStartedAt=m_start,
        mealEndedAt=now_utc,
        mealSeconds=meal_seconds,
    )


@router.post(
    "/employees/{employeeId}/attendance/shifts/{shiftId}/check-out",
    response_model=CheckOutResponse,
    summary="Registrar salida de jornada validando geocerca y estado de comida en backend",
)
def register_employee_check_out(
    employeeId: str,
    shiftId: str,
    payload: CheckOutRequest,
    request: Request,
    db: SessionDep,
    ctx: MobileAuthContext = Depends(get_mobile_auth_context),
) -> CheckOutResponse:
    collab = ctx.ensure_can_access_employee(db, employeeId)
    now_utc = datetime.now(timezone.utc)
    clean_event_id = payload.clientEventId.strip()

    business_date, _tz, daily, check_in_log, meal_out_log, meal_in_log, check_out_log = (
        _get_day_attendance_state(db, collab, now_utc)
    )

    # 1. Idempotencia por clientEventId
    existing_log = db.scalar(
        select(AttendanceLog).where(AttendanceLog.client_event_id == clean_event_id)
    )
    if existing_log is not None and existing_log.collaborator_id == collab.id:
        daily = sync_collaborator_daily_attendance(db, collab.id, existing_log.event_time)
        c_in_dt = _ensure_utc(check_in_log.event_time) if check_in_log else _ensure_utc(existing_log.event_time)
        c_out_dt = _ensure_utc(existing_log.event_time)
        worked_sec, meal_sec, _ = _compute_live_shift_metrics(
            now_utc=c_out_dt,
            check_in_dt=c_in_dt,
            meal_out_dt=meal_out_log.event_time if meal_out_log else None,
            meal_in_dt=meal_in_log.event_time if meal_in_log else None,
            check_out_dt=c_out_dt,
        )
        return CheckOutResponse(
            shiftId=f"shift_{daily.id:05d}" if daily else shiftId,
            status="completed",
            businessDate=business_date.isoformat(),
            checkInAt=c_in_dt,
            checkOutAt=c_out_dt,
            workedSeconds=worked_sec,
            mealSeconds=meal_sec,
            branch=_resolve_collaborator_branch(db, collab),
        )

    if check_in_log is None:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="ACTIVE_SHIFT_NOT_FOUND",
            message="No existe una jornada activa para registrar salida.",
        )
    if check_out_log is not None:
        raise MobileAPIError(
            status_code=status.HTTP_409_CONFLICT,
            code="SHIFT_ALREADY_COMPLETED",
            message="La salida de esta jornada ya fue registrada.",
        )
    if meal_out_log is not None and meal_in_log is None:
        raise MobileAPIError(
            status_code=status.HTTP_409_CONFLICT,
            code="MEAL_BREAK_ACTIVE",
            message="Debes finalizar tu horario de comida antes de registrar la salida.",
        )

    # 2. Recalcular geocerca de salida en el servidor
    geofence, branch_info, dist_m = _validate_event_geofence_on_server(
        db=db,
        collab=collab,
        business_date=business_date,
        event_type="check_out",
        geofence_id_raw=payload.geofence.id,
        latitude=payload.geofence.latitude,
        longitude=payload.geofence.longitude,
        accuracy_meters=payload.geofence.accuracyMeters,
    )

    photo_media_path: Optional[str] = None
    if payload.photoBase64 and payload.photoBase64.strip():
        try:
            photo_bytes = extract_jpeg_bytes_from_base64(payload.photoBase64)
            stored_photo = store_event_photo_image(
                company_id=collab.company_id,
                collaborator_id=collab.id,
                event_id_or_token=clean_event_id,
                jpeg_bytes=photo_bytes,
            )
            photo_media_path = stored_photo.media_path
        except ValueError:
            photo_media_path = None

    dev_obj = ctx.mobile_device
    dev_public_id = dev_obj.public_id if dev_obj else (payload.deviceId or "mobile_app")
    dev_label = (
        f"{dev_obj.manufacturer or ''} {dev_obj.model or ''}".strip()
        if dev_obj and (dev_obj.manufacturer or dev_obj.model)
        else "App Móvil Asisto"
    )
    resolved_branch_id = geofence.branch_id or collab.branch_id or _parse_prefixed_id(branch_info.id)

    log_entry = AttendanceLog(
        source_type="mobile_device",
        company_id=collab.company_id,
        collaborator_id=collab.id,
        device_employee_no=collab.employee_no,
        collaborator_employee_no=collab.employee_no,
        collaborator_name=collab.full_name,
        device_name=dev_label,
        source_ip=request.client.host if request.client else None,
        event_type="MobileAttendanceEvent",
        major_event=5,
        minor_event=75,
        event_label="Salida registrada en App Móvil (Geocerca validada)",
        attendance_status="checkOut",
        verify_mode="mobile_geofence",
        is_valid_attendance=True,
        client_event_id=clean_event_id,
        mobile_device_id=dev_public_id,
        mobile_device_fk=dev_obj.id if dev_obj else None,
        branch_id=resolved_branch_id,
        captured_at=payload.capturedAt,
        latitude=f"{payload.geofence.latitude:.8f}",
        longitude=f"{payload.geofence.longitude:.8f}",
        accuracy_meters=payload.geofence.accuracyMeters,
        geofence_id=geofence.id,
        geofence_version=geofence.version,
        geofence_assignment_id=payload.geofence.assignmentId,
        inside_geofence=True,
        distance_meters=dist_m,
        comments=payload.comments.strip() if payload.comments else None,
        photo_media_path=photo_media_path,
        raw_payload=json.dumps(payload.model_dump(mode="json", exclude={"photoBase64"}), ensure_ascii=False),
        event_time=now_utc,
        created_at=now_utc,
    )
    db.add(log_entry)
    db.commit()
    db.refresh(log_entry)

    daily = sync_collaborator_daily_attendance(db, collab.id, now_utc)
    worked_sec, meal_sec, _ = _compute_live_shift_metrics(
        now_utc=now_utc,
        check_in_dt=check_in_log.event_time,
        meal_out_dt=meal_out_log.event_time if meal_out_log else None,
        meal_in_dt=meal_in_log.event_time if meal_in_log else None,
        check_out_dt=now_utc,
    )

    return CheckOutResponse(
        shiftId=f"shift_{daily.id:05d}" if daily else shiftId,
        status="completed",
        businessDate=business_date.isoformat(),
        checkInAt=_ensure_utc(check_in_log.event_time),
        checkOutAt=now_utc,
        workedSeconds=worked_sec,
        mealSeconds=meal_sec,
        branch=branch_info,
    )


@router.get(
    "/employees/{employeeId}/attendance/history",
    response_model=AttendanceHistoryListResponse,
    summary="Listar historial paginado de jornadas del trabajador",
)
def list_employee_attendance_history(
    employeeId: str,
    db: SessionDep,
    from_date: Optional[date] = Query(default=None, alias="from"),
    to_date: Optional[date] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    pageSize: int = Query(default=20, ge=1, le=100),
    ctx: MobileAuthContext = Depends(get_mobile_auth_context),
) -> AttendanceHistoryListResponse:
    collab = ctx.ensure_can_access_employee(db, employeeId)
    default_branch = _resolve_collaborator_branch(db, collab)

    conds = [
        DailyAttendance.collaborator_id == collab.id,
        DailyAttendance.has_attendance.is_(True),
    ]
    if from_date is not None:
        conds.append(DailyAttendance.attendance_date >= from_date)
    if to_date is not None:
        conds.append(DailyAttendance.attendance_date <= to_date)

    total_items = db.scalar(select(func.count(DailyAttendance.id)).where(*conds)) or 0
    total_pages = max(1, (total_items + pageSize - 1) // pageSize)
    offset = (page - 1) * pageSize

    records = list(
        db.scalars(
            select(DailyAttendance)
            .where(*conds)
            .order_by(DailyAttendance.attendance_date.desc(), DailyAttendance.id.desc())
            .offset(offset)
            .limit(pageSize)
        ).all()
    )

    now_utc = datetime.now(timezone.utc)
    items: list[AttendanceHistoryItem] = []
    for rec in records:
        if rec.actual_check_in:
            w_sec, m_sec, m_stat = _compute_live_shift_metrics(
                now_utc=now_utc,
                check_in_dt=rec.actual_check_in,
                meal_out_dt=rec.actual_meal_out,
                meal_in_dt=rec.actual_meal_in,
                check_out_dt=rec.actual_check_out,
            )
            st = "completed" if rec.actual_check_out else ("meal_active" if m_stat == "active" else "active")
        else:
            w_sec = rec.net_worked_minutes * 60
            m_sec = (rec.meal_taken_minutes or 0) * 60
            st = rec.status

        items.append(
            AttendanceHistoryItem(
                shiftId=f"shift_{rec.id:05d}",
                businessDate=rec.attendance_date.isoformat(),
                status=st,
                checkInAt=_ensure_utc(rec.actual_check_in) if rec.actual_check_in else None,
                checkOutAt=_ensure_utc(rec.actual_check_out) if rec.actual_check_out else None,
                workedSeconds=w_sec,
                mealSeconds=m_sec,
                branchName=rec.branch_name or default_branch.name,
            )
        )

    return AttendanceHistoryListResponse(
        items=items,
        pagination=AttendanceHistoryPagination(
            page=page,
            pageSize=pageSize,
            totalItems=total_items,
            totalPages=total_pages,
        ),
    )


@router.get(
    "/employees/{employeeId}/attendance/history/{shiftId}",
    response_model=AttendanceHistoryDetailResponse,
    summary="Consultar detalle de una jornada del historial y sus eventos validados",
)
def get_employee_attendance_history_detail(
    employeeId: str,
    shiftId: str,
    db: SessionDep,
    ctx: MobileAuthContext = Depends(get_mobile_auth_context),
) -> AttendanceHistoryDetailResponse:
    collab = ctx.ensure_can_access_employee(db, employeeId)
    parsed_id = _parse_prefixed_id(shiftId)

    rec: Optional[DailyAttendance] = None
    if parsed_id is not None:
        rec = db.get(DailyAttendance, parsed_id)
    if rec is None:
        # Intentar buscar por fecha YYYY-MM-DD si se envió shift_2026-11-03
        raw_date = shiftId.replace("shift_", "").strip()
        try:
            d_val = date.fromisoformat(raw_date)
            rec = db.scalar(
                select(DailyAttendance).where(
                    DailyAttendance.collaborator_id == collab.id,
                    DailyAttendance.attendance_date == d_val,
                )
            )
        except ValueError:
            rec = None

    if rec is None or rec.collaborator_id != collab.id:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="SHIFT_NOT_FOUND",
            message="No se encontró la jornada solicitada.",
        )

    tz = _resolve_tz(rec.timezone)
    local_start = datetime.combine(rec.attendance_date, time(0, 0, 0), tzinfo=tz).astimezone(
        timezone.utc
    )
    local_end = datetime.combine(rec.attendance_date, time(23, 59, 59, 999999), tzinfo=tz).astimezone(
        timezone.utc
    )
    day_logs = list(
        db.scalars(
            select(AttendanceLog)
            .where(
                AttendanceLog.collaborator_id == collab.id,
                AttendanceLog.is_valid_attendance.is_(True),
                AttendanceLog.event_time >= local_start,
                AttendanceLog.event_time <= local_end,
            )
            .order_by(AttendanceLog.event_time.asc(), AttendanceLog.id.asc())
        ).all()
    )

    now_utc = datetime.now(timezone.utc)
    if rec.actual_check_in:
        w_sec, m_sec, m_stat = _compute_live_shift_metrics(
            now_utc=now_utc,
            check_in_dt=rec.actual_check_in,
            meal_out_dt=rec.actual_meal_out,
            meal_in_dt=rec.actual_meal_in,
            check_out_dt=rec.actual_check_out,
        )
        st = "completed" if rec.actual_check_out else ("meal_active" if m_stat == "active" else "active")
    else:
        w_sec = rec.net_worked_minutes * 60
        m_sec = (rec.meal_taken_minutes or 0) * 60
        st = rec.status

    branch_info = _resolve_collaborator_branch(db, collab)
    if rec.branch_id:
        br_obj = db.get(Branch, rec.branch_id)
        if br_obj:
            branch_info = MobileBranchInfo(
                id=br_obj.public_id,
                name=br_obj.name,
                address=br_obj.address,
            )

    events_detail: list[AttendanceHistoryEventDetail] = []
    for lg in day_logs:
        events_detail.append(
            AttendanceHistoryEventDetail(
                eventId=f"att_{lg.id:05d}",
                type=lg.attendance_status or "checkIn",
                occurredAt=_ensure_utc(lg.event_time),
                sourceType=lg.source_type,
                latitude=float(lg.latitude) if lg.latitude else None,
                longitude=float(lg.longitude) if lg.longitude else None,
                accuracyMeters=lg.accuracy_meters,
                geofenceId=f"geo_{lg.geofence_id:03d}" if lg.geofence_id else None,
                geofenceVersion=lg.geofence_version,
                insideGeofence=lg.inside_geofence,
                photoMediaPath=lg.photo_media_path,
            )
        )

    return AttendanceHistoryDetailResponse(
        shiftId=f"shift_{rec.id:05d}",
        businessDate=rec.attendance_date.isoformat(),
        status=st,
        checkInAt=_ensure_utc(rec.actual_check_in) if rec.actual_check_in else None,
        mealStartedAt=_ensure_utc(rec.actual_meal_out) if rec.actual_meal_out else None,
        mealEndedAt=_ensure_utc(rec.actual_meal_in) if rec.actual_meal_in else None,
        checkOutAt=_ensure_utc(rec.actual_check_out) if rec.actual_check_out else None,
        workedSeconds=w_sec,
        mealSeconds=m_sec,
        lateMinutes=rec.late_minutes,
        comments=rec.check_out_comments,
        branch=branch_info,
        events=events_detail,
    )


# ---------------------------------------------------------------------------
# 5. Endpoints Administrativos para Gestión de Sucursales, Geocercas y Activaciones
# ---------------------------------------------------------------------------


@router.get(
    "/branches",
    response_model=list[BranchRead],
    dependencies=[Depends(require_any_permission("companies:read", "collaborators:read", "attendance:read"))],
    summary="Listar sucursales configuradas",
)
def list_branches(
    db: SessionDep,
    company_id: Optional[int] = Query(default=None),
    is_active: Optional[bool] = Query(default=None),
) -> list[Branch]:
    stmt = select(Branch)
    if company_id is not None:
        stmt = stmt.where(Branch.company_id == company_id)
    if is_active is not None:
        stmt = stmt.where(Branch.is_active == is_active)
    return list(db.scalars(stmt.order_by(Branch.company_id.asc(), Branch.id.asc())).all())


@router.post(
    "/branches",
    response_model=BranchRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_permission("companies:write", "collaborators:write"))],
    summary="Crear una sucursal para una empresa",
)
def create_branch(db: SessionDep, payload: BranchCreate) -> Branch:
    company = db.get(Company, payload.company_id)
    if not company:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="COMPANY_NOT_FOUND",
            message="La empresa seleccionada no existe.",
        )
    clean_code = payload.code.strip().upper()
    conflict = db.scalar(
        select(Branch).where(Branch.company_id == company.id, Branch.code == clean_code)
    )
    if conflict:
        raise MobileAPIError(
            status_code=status.HTTP_409_CONFLICT,
            code="BRANCH_CODE_ALREADY_EXISTS",
            message=f"Ya existe una sucursal con el código '{clean_code}' en esta empresa.",
        )
    branch = Branch(
        company_id=company.id,
        code=clean_code,
        name=payload.name.strip(),
        address=payload.address.strip() if payload.address else None,
        timezone=payload.timezone.strip() or "America/Mexico_City",
        is_active=payload.is_active,
    )
    db.add(branch)
    db.commit()
    db.refresh(branch)
    return branch


@router.patch(
    "/branches/{branch_id}",
    response_model=BranchRead,
    dependencies=[Depends(require_any_permission("companies:write", "collaborators:write"))],
    summary="Actualizar una sucursal",
)
def update_branch(db: SessionDep, branch_id: int, payload: BranchUpdate) -> Branch:
    branch = db.get(Branch, branch_id)
    if not branch:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="BRANCH_NOT_FOUND",
            message="Sucursal no encontrada.",
        )
    if payload.code is not None:
        branch.code = payload.code.strip().upper()
    if payload.name is not None:
        branch.name = payload.name.strip()
    if payload.address is not None:
        branch.address = payload.address.strip() or None
    if payload.timezone is not None:
        branch.timezone = payload.timezone.strip() or "America/Mexico_City"
    if payload.is_active is not None:
        branch.is_active = payload.is_active
    db.commit()
    db.refresh(branch)
    return branch


@router.delete(
    "/branches/{branch_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_any_permission("companies:write", "collaborators:write"))],
    summary="Eliminar una sucursal",
)
def delete_branch(db: SessionDep, branch_id: int) -> None:
    branch = db.get(Branch, branch_id)
    if not branch:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="BRANCH_NOT_FOUND",
            message="Sucursal no encontrada.",
        )
    db.delete(branch)
    db.commit()


@router.get(
    "/geofences",
    response_model=list[GeofenceRead],
    dependencies=[Depends(require_any_permission("companies:read", "collaborators:read", "attendance:read"))],
    summary="Listar geocercas radiales y poligonales",
)
def list_geofences(
    db: SessionDep,
    company_id: Optional[int] = Query(default=None),
    branch_id: Optional[int] = Query(default=None),
    is_active: Optional[bool] = Query(default=None),
) -> list[Geofence]:
    stmt = select(Geofence)
    if company_id is not None:
        stmt = stmt.where(Geofence.company_id == company_id)
    if branch_id is not None:
        stmt = stmt.where(Geofence.branch_id == branch_id)
    if is_active is not None:
        stmt = stmt.where(Geofence.is_active == is_active)
    return list(db.scalars(stmt.order_by(Geofence.company_id.asc(), Geofence.id.asc())).all())


@router.post(
    "/geofences",
    response_model=GeofenceRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_permission("companies:write", "collaborators:write", "shifts:write"))],
    summary="Crear una geocerca (radial o poligonal)",
)
def create_geofence(db: SessionDep, payload: GeofenceCreate) -> Geofence:
    company = db.get(Company, payload.company_id)
    if not company:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="COMPANY_NOT_FOUND",
            message="Empresa no encontrada.",
        )
    if payload.geofence_type == "radial":
        if payload.center_latitude is None or payload.center_longitude is None:
            raise MobileAPIError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="GEOFENCE_CENTER_REQUIRED",
                message="Una geocerca radial requiere center_latitude y center_longitude.",
            )
    else:
        if not payload.vertices or len(payload.vertices) < 3:
            raise MobileAPIError(
                status_code=status.HTTP_400_BAD_REQUEST,
                code="GEOFENCE_VERTICES_REQUIRED",
                message="Una geocerca poligonal requiere al menos 3 vértices.",
            )

    clean_code = payload.code.strip().upper()
    verts_json = (
        json.dumps([v.model_dump() for v in payload.vertices], ensure_ascii=False)
        if payload.vertices
        else None
    )
    gf = Geofence(
        company_id=company.id,
        branch_id=payload.branch_id,
        code=clean_code,
        name=payload.name.strip(),
        version=1,
        geofence_type=payload.geofence_type,
        center_latitude=payload.center_latitude,
        center_longitude=payload.center_longitude,
        radius_meters=payload.radius_meters,
        vertices_json=verts_json,
        max_accuracy_meters=payload.max_accuracy_meters,
        is_active=payload.is_active,
    )
    db.add(gf)
    db.commit()
    db.refresh(gf)
    return gf


@router.patch(
    "/geofences/{geofence_id}",
    response_model=GeofenceRead,
    dependencies=[Depends(require_any_permission("companies:write", "collaborators:write", "shifts:write"))],
    summary="Actualizar una geocerca (incrementa automáticamente su versión)",
)
def update_geofence(db: SessionDep, geofence_id: int, payload: GeofenceUpdate) -> Geofence:
    gf = db.get(Geofence, geofence_id)
    if not gf:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="GEOFENCE_NOT_FOUND",
            message="Geocerca no encontrada.",
        )
    geometry_changed = False
    if payload.branch_id is not None:
        gf.branch_id = payload.branch_id or None
    if payload.code is not None:
        gf.code = payload.code.strip().upper()
    if payload.name is not None:
        gf.name = payload.name.strip()
    if payload.geofence_type is not None and payload.geofence_type != gf.geofence_type:
        gf.geofence_type = payload.geofence_type
        geometry_changed = True
    if payload.center_latitude is not None:
        gf.center_latitude = payload.center_latitude
        geometry_changed = True
    if payload.center_longitude is not None:
        gf.center_longitude = payload.center_longitude
        geometry_changed = True
    if payload.radius_meters is not None:
        gf.radius_meters = payload.radius_meters
        geometry_changed = True
    if payload.vertices is not None:
        gf.vertices_json = json.dumps([v.model_dump() for v in payload.vertices], ensure_ascii=False)
        geometry_changed = True
    if payload.max_accuracy_meters is not None:
        gf.max_accuracy_meters = payload.max_accuracy_meters
    if payload.is_active is not None:
        gf.is_active = payload.is_active

    if geometry_changed:
        gf.version += 1

    db.commit()
    db.refresh(gf)
    return gf


@router.delete(
    "/geofences/{geofence_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_any_permission("companies:write", "collaborators:write", "shifts:write"))],
    summary="Eliminar una geocerca",
)
def delete_geofence(db: SessionDep, geofence_id: int) -> None:
    gf = db.get(Geofence, geofence_id)
    if not gf:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="GEOFENCE_NOT_FOUND",
            message="Geocerca no encontrada.",
        )
    db.delete(gf)
    db.commit()


@router.get(
    "/geofence-assignments",
    response_model=list[GeofenceAssignmentRead],
    dependencies=[Depends(require_any_permission("companies:read", "collaborators:read", "attendance:read"))],
    summary="Listar asignaciones de geocercas",
)
def list_geofence_assignments(
    db: SessionDep,
    company_id: Optional[int] = Query(default=None),
    collaborator_id: Optional[int] = Query(default=None),
) -> list[GeofenceAssignment]:
    stmt = select(GeofenceAssignment)
    if company_id is not None:
        stmt = stmt.where(GeofenceAssignment.company_id == company_id)
    if collaborator_id is not None:
        stmt = stmt.where(GeofenceAssignment.collaborator_id == collaborator_id)
    return list(
        db.scalars(
            stmt.order_by(GeofenceAssignment.priority.asc(), GeofenceAssignment.id.desc())
        ).all()
    )


@router.post(
    "/geofence-assignments",
    response_model=GeofenceAssignmentRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_permission("companies:write", "collaborators:write", "shifts:write"))],
    summary="Asignar una geocerca a un colaborador, turno o sucursal",
)
def create_geofence_assignment(
    db: SessionDep,
    payload: GeofenceAssignmentCreate,
) -> GeofenceAssignment:
    gf = db.get(Geofence, payload.geofence_id)
    if not gf or gf.company_id != payload.company_id:
        raise MobileAPIError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="GEOFENCE_NOT_FOUND",
            message="La geocerca seleccionada no existe o no pertenece a la empresa.",
        )
    assign = GeofenceAssignment(
        company_id=payload.company_id,
        collaborator_id=payload.collaborator_id,
        shift_id=payload.shift_id,
        branch_id=payload.branch_id,
        geofence_id=payload.geofence_id,
        event_type=payload.event_type,
        valid_from_date=payload.valid_from_date,
        valid_until_date=payload.valid_until_date,
        priority=payload.priority,
        is_active=payload.is_active,
    )
    db.add(assign)
    db.commit()
    db.refresh(assign)
    return assign


@router.post(
    "/geofence-assignments/bulk",
    response_model=BulkGeofenceAssignResponse,
    dependencies=[Depends(require_any_permission("companies:write", "collaborators:write", "shifts:write"))],
    summary="Asignación masiva de una geocerca a múltiples colaboradores, turno o sucursal",
)
def bulk_assign_geofence(
    db: SessionDep,
    payload: BulkGeofenceAssignRequest,
) -> BulkGeofenceAssignResponse:
    company = db.get(Company, payload.company_id)
    if not company:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="COMPANY_NOT_FOUND",
            message="Empresa no encontrada.",
        )
    gf = db.get(Geofence, payload.geofence_id)
    if not gf or gf.company_id != company.id:
        raise MobileAPIError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="GEOFENCE_NOT_FOUND",
            message="La geocerca seleccionada no existe o no pertenece a la empresa.",
        )

    target_collaborators: list[Collaborator] = []
    if payload.assign_all_company_collaborators:
        stmt = select(Collaborator).where(
            Collaborator.company_id == company.id,
            Collaborator.is_active.is_(True),
        )
        if payload.shift_id:
            stmt = stmt.where(Collaborator.shift_id == payload.shift_id)
        if payload.branch_id:
            stmt = stmt.where(Collaborator.branch_id == payload.branch_id)
        target_collaborators = list(db.scalars(stmt).all())
    elif payload.collaborator_ids:
        target_collaborators = list(
            db.scalars(
                select(Collaborator).where(
                    Collaborator.company_id == company.id,
                    Collaborator.id.in_(payload.collaborator_ids),
                )
            ).all()
        )
    elif payload.shift_id or payload.branch_id:
        # Asignación por regla de turno o sucursal sin expandir colaborador por colaborador
        existing_rule = db.scalar(
            select(GeofenceAssignment).where(
                GeofenceAssignment.company_id == company.id,
                GeofenceAssignment.collaborator_id.is_(None),
                GeofenceAssignment.shift_id == payload.shift_id,
                GeofenceAssignment.branch_id == payload.branch_id,
                GeofenceAssignment.event_type == payload.event_type,
            )
        )
        if existing_rule and payload.replace_existing:
            existing_rule.geofence_id = gf.id
            existing_rule.valid_from_date = payload.valid_from_date
            existing_rule.valid_until_date = payload.valid_until_date
            existing_rule.priority = payload.priority
            existing_rule.is_active = True
            db.commit()
            return BulkGeofenceAssignResponse(
                company_id=company.id,
                geofence_id=gf.id,
                geofence_name=gf.name,
                total_assigned=1,
                updated_existing=1,
                created_new=0,
            )
        db.add(
            GeofenceAssignment(
                company_id=company.id,
                collaborator_id=None,
                shift_id=payload.shift_id,
                branch_id=payload.branch_id,
                geofence_id=gf.id,
                event_type=payload.event_type,
                valid_from_date=payload.valid_from_date,
                valid_until_date=payload.valid_until_date,
                priority=payload.priority,
                is_active=True,
            )
        )
        db.commit()
        return BulkGeofenceAssignResponse(
            company_id=company.id,
            geofence_id=gf.id,
            geofence_name=gf.name,
            total_assigned=1,
            updated_existing=0,
            created_new=1,
        )

    if not target_collaborators:
        raise MobileAPIError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code="NO_COLLABORATORS_SELECTED",
            message="No se encontraron colaboradores para asignar la geocerca.",
        )

    updated_count = 0
    created_count = 0
    for collab in target_collaborators:
        if gf.branch_id and collab.branch_id is None:
            collab.branch_id = gf.branch_id

        existing = None
        if payload.replace_existing:
            existing = db.scalar(
                select(GeofenceAssignment).where(
                    GeofenceAssignment.collaborator_id == collab.id,
                    GeofenceAssignment.event_type == payload.event_type,
                )
            )
        if existing:
            existing.geofence_id = gf.id
            existing.branch_id = payload.branch_id or collab.branch_id or gf.branch_id
            existing.valid_from_date = payload.valid_from_date
            existing.valid_until_date = payload.valid_until_date
            existing.priority = payload.priority
            existing.is_active = True
            updated_count += 1
        else:
            db.add(
                GeofenceAssignment(
                    company_id=company.id,
                    collaborator_id=collab.id,
                    shift_id=payload.shift_id,
                    branch_id=payload.branch_id or collab.branch_id or gf.branch_id,
                    geofence_id=gf.id,
                    event_type=payload.event_type,
                    valid_from_date=payload.valid_from_date,
                    valid_until_date=payload.valid_until_date,
                    priority=payload.priority,
                    is_active=True,
                )
            )
            created_count += 1

    db.commit()
    return BulkGeofenceAssignResponse(
        company_id=company.id,
        geofence_id=gf.id,
        geofence_name=gf.name,
        total_assigned=updated_count + created_count,
        updated_existing=updated_count,
        created_new=created_count,
    )


@router.delete(
    "/geofence-assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_any_permission("companies:write", "collaborators:write", "shifts:write"))],
    summary="Eliminar una asignación de geocerca",
)
def delete_geofence_assignment(db: SessionDep, assignment_id: int) -> None:
    assign = db.get(GeofenceAssignment, assignment_id)
    if not assign:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="ASSIGNMENT_NOT_FOUND",
            message="Asignación de geocerca no encontrada.",
        )
    db.delete(assign)
    db.commit()


@router.post(
    "/collaborators/{collaborator_id}/geofence-test",
    response_model=CollaboratorGeofenceTestResponse,
    dependencies=[Depends(require_any_permission("collaborators:read", "companies:read", "attendance:read"))],
    summary="Simular resolución de geocerca y validar una coordenada para un colaborador",
)
def test_collaborator_geofence(
    collaborator_id: int,
    payload: CollaboratorGeofenceTestRequest,
    db: SessionDep,
) -> CollaboratorGeofenceTestResponse:
    collab = db.get(Collaborator, collaborator_id)
    if not collab:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="EMPLOYEE_NOT_FOUND",
            message="Colaborador no encontrado.",
        )
    now_utc = datetime.now(timezone.utc)
    tz = _resolve_tz(collab.company.timezone if collab.company else "America/Mexico_City")
    business_date = now_utc.astimezone(tz).date()

    geofence, assignment, _branch = _resolve_active_geofence_for_collaborator(
        db=db,
        collab=collab,
        business_date=business_date,
        event_type=payload.event_type,
    )
    eval_res = evaluate_geofence(
        latitude=payload.latitude,
        longitude=payload.longitude,
        accuracy_meters=payload.accuracy_meters,
        geofence_type=geofence.geofence_type,
        max_accuracy_meters=geofence.max_accuracy_meters,
        center_lat=geofence.center_latitude,
        center_lon=geofence.center_longitude,
        radius_meters=geofence.radius_meters,
        vertices=geofence.vertices,
    )
    assign_id_str = (
        f"asg_{assignment.id:05d}" if assignment is not None else f"asg_default_{geofence.id:03d}"
    )
    return CollaboratorGeofenceTestResponse(
        collaborator_id=collab.id,
        collaborator_name=collab.full_name,
        employee_no=collab.employee_no,
        assignment_id=assign_id_str,
        geofence_id=geofence.public_id,
        geofence_name=geofence.name,
        geofence_type=geofence.geofence_type,
        geofence_version=geofence.version,
        max_accuracy_meters=geofence.max_accuracy_meters,
        inside=eval_res.inside and eval_res.accuracy_valid,
        accuracy_valid=eval_res.accuracy_valid,
        distance_meters=eval_res.distance_meters,
        radius_meters=geofence.radius_meters,
        reason_code=eval_res.reason_code,
        reason_message=eval_res.reason_message,
    )


@router.post(
    "/collaborators/{collaborator_id}/activation-codes",
    response_model=ActivationCodeGenerateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_permission("collaborators:write", "devices:write"))],
    summary="Generar un código de activación de un solo uso para vincular el dispositivo móvil de un colaborador",
)
def generate_collaborator_activation_code(
    collaborator_id: int,
    db: SessionDep,
    current_user: CurrentUser,
    payload: Optional[ActivationCodeGenerateRequest] = None,
) -> ActivationCodeGenerateResponse:
    collab = db.get(Collaborator, collaborator_id)
    if not collab:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="EMPLOYEE_NOT_FOUND",
            message="Colaborador no encontrado.",
        )
    if not collab.is_active:
        raise MobileAPIError(
            status_code=status.HTTP_423_LOCKED,
            code="EMPLOYEE_DISABLED",
            message="El colaborador está deshabilitado.",
        )

    _ensure_collaborator_face_stored_in_media(db, collab)
    if not collab.face_media_path:
        raise MobileAPIError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="FACE_REFERENCE_NOT_AVAILABLE",
            message="Registra primero la fotografía facial del colaborador antes de generar su código de activación móvil.",
        )

    # Revocar códigos pendientes anteriores del mismo colaborador
    pending_codes = list(
        db.scalars(
            select(MobileActivationCode).where(
                MobileActivationCode.collaborator_id == collab.id,
                MobileActivationCode.status == "pending",
            )
        ).all()
    )
    for old_code in pending_codes:
        old_code.status = "revoked"

    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    part1 = "".join(secrets.choice(alphabet) for _ in range(4))
    part2 = "".join(secrets.choice(alphabet) for _ in range(4))
    plain_code = f"AS-{part1}-{part2}"
    code_hash = hash_opaque_token(plain_code)
    code_hint = f"AS-{part1[:2]}**-*^{part2[-2:]}"

    ttl_hours = payload.expires_in_hours if payload else 72
    branch_id = (payload.branch_id if payload and payload.branch_id else None) or collab.branch_id
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=ttl_hours)

    act_record = MobileActivationCode(
        company_id=collab.company_id,
        collaborator_id=collab.id,
        branch_id=branch_id,
        code_hash=code_hash,
        code_hint=code_hint,
        status="pending",
        expires_at=expires_at,
        created_by_user_id=current_user.id,
    )
    db.add(act_record)
    db.commit()
    db.refresh(act_record)

    branch_info = _resolve_collaborator_branch(db, collab)
    return ActivationCodeGenerateResponse(
        id=act_record.id,
        activationId=act_record.public_id,
        collaboratorId=collab.id,
        collaboratorName=collab.full_name,
        employeeNo=collab.employee_no,
        companyId=collab.company_id,
        companyName=collab.company_name,
        branchId=branch_id,
        branchName=branch_info.name,
        activationCode=plain_code,
        codeHint=code_hint,
        expiresAt=expires_at,
    )


@router.get(
    "/mobile-devices",
    response_model=list[MobileDeviceRead],
    dependencies=[Depends(require_any_permission("collaborators:read", "devices:read"))],
    summary="Listar dispositivos móviles vinculados",
)
def list_mobile_devices(
    db: SessionDep,
    company_id: Optional[int] = Query(default=None),
    collaborator_id: Optional[int] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
) -> list[MobileDevice]:
    stmt = select(MobileDevice)
    if company_id is not None:
        stmt = stmt.where(MobileDevice.company_id == company_id)
    if collaborator_id is not None:
        stmt = stmt.where(MobileDevice.collaborator_id == collaborator_id)
    if status_filter:
        stmt = stmt.where(MobileDevice.status == status_filter)
    return list(db.scalars(stmt.order_by(MobileDevice.id.desc())).all())


@router.post(
    "/mobile-devices/{mobile_device_id}/revoke",
    response_model=MobileDeviceRead,
    dependencies=[Depends(require_any_permission("collaborators:write", "devices:write"))],
    summary="Revocar administrativamente un dispositivo móvil vinculado",
)
def revoke_mobile_device(
    mobile_device_id: int,
    db: SessionDep,
    payload: Optional[MobileDeviceRevokeRequest] = None,
) -> MobileDevice:
    device = db.get(MobileDevice, mobile_device_id)
    if not device:
        raise MobileAPIError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="DEVICE_NOT_FOUND",
            message="Dispositivo móvil no encontrado.",
        )
    now = datetime.now(timezone.utc)
    device.status = "revoked"
    device.revoke_reason = payload.reason if payload else "ADMIN_REVOKED"
    device.revoked_at = now
    device.refresh_token_hash = None
    device.refresh_token_expires_at = None
    db.commit()
    db.refresh(device)
    return device
