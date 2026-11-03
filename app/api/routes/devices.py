from datetime import datetime, timedelta, timezone
import json
from typing import Any, Callable, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select

from app.api.deps import CurrentUser, SessionDep, require_permissions
from app.api.routes.attendance import (
    infer_webhook_log_status_by_shift,
    process_collaborator_day_attendance_task,
    sync_collaborator_daily_attendance,
)
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.isapi import (
    ACS_MINOR_EVENT_LABELS,
    HikvisionISAPIClient,
    ISAPIError,
    IsapiGenerateXmlController,
)
from app.core.security import decrypt_secret, encrypt_secret
from app.models.attendance import AttendanceLog
from app.models.collaborator import Collaborator, CollaboratorDeviceSync, Company
from app.models.device import HikvisionDevice, ISAPINotificationConfig
from app.models.user import User
from app.schemas.device import (
    AttendanceLogRead,
    AttendanceLogStatsResponse,
    HikvisionDeviceCreate,
    HikvisionDeviceRead,
    HikvisionDeviceUpdate,
    ISAPIActionResponse,
    ISAPICaptureFaceResponse,
    ISAPIDeviceHttpHostRead,
    ISAPIDeviceUserCreateRequest,
    ISAPIDeviceUserDeleteRequest,
    ISAPIDeviceUserSearchResponse,
    ISAPIDeviceUserUpdateRequest,
    ISAPIEventSearchRequest,
    ISAPIEventSearchResponse,
    ISAPINetworkInterfaceItem,
    ISAPINetworkInterfacesResponse,
    ISAPINotificationApplyBulkResponse,
    ISAPINotificationApplyDeviceResult,
    ISAPINotificationConfigRead,
    ISAPINotificationConfigUpdate,
    ISAPIOfflineSyncRequest,
    ISAPIOfflineSyncResponse,
    ISAPIProbeResponse,
    ISAPISaveFaceRequest,
    ISAPITestConnectionRequest,
    ISAPIWebhookEventResponse,
)

router = APIRouter()


def _get_or_create_notification_config(db: SessionDep) -> ISAPINotificationConfig:
    cfg = db.scalar(select(ISAPINotificationConfig).order_by(ISAPINotificationConfig.id.asc()).limit(1))
    if not cfg:
        default_host = settings.APP_HOST
        is_ipv4 = (
            all(part.isdigit() for part in default_host.split("."))
            and len(default_host.split(".")) == 4
        )
        cfg = ISAPINotificationConfig(
            enabled=True,
            host_id=2,
            protocol_type="HTTP",
            addressing_format_type="ipaddress" if is_ipv4 else "hostname",
            host_address=default_host,
            ip_address=default_host if is_ipv4 else "192.168.1.100",
            domain=default_host if not is_ipv4 else "attendance.local",
            port_no=settings.APP_PORT,
            url_path=f"{settings.API_V1_STR}/devices/webhook/isapi",
            parameter_format_type="JSON",
            auth_method="none",
            auto_apply_on_create=True,
        )
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def _push_notification_config_to_device(
    device: HikvisionDevice,
    cfg: ISAPINotificationConfig,
) -> tuple[bool, str]:
    """Push the global ISAPI HttpHostNotification settings to a single Hikvision device."""
    client = _build_isapi_client(device)
    auth_pwd: Optional[str] = None
    if cfg.encrypted_auth_password:
        try:
            auth_pwd = decrypt_secret(cfg.encrypted_auth_password)
        except ValueError:
            auth_pwd = None

    try:
        res = client.set_http_host_notification(
            host_id=cfg.host_id,
            url_path=cfg.normalized_path,
            protocol_type=cfg.protocol_type,
            parameter_format_type=cfg.parameter_format_type,
            addressing_format_type=cfg.addressing_format_type,
            host_address=cfg.effective_host,
            ip_address=cfg.ip_address or cfg.host_address,
            domain=cfg.domain or cfg.host_address,
            port_no=cfg.port_no,
            auth_method=cfg.auth_method,
            auth_username=cfg.auth_username,
            auth_password=auth_pwd,
        )
        now = datetime.now(timezone.utc)
        device.status = "online"
        device.last_seen_at = now
        device.last_error = None
        device.webhook_target_url = str(res["target_url"])[:300]
        device.webhook_synced_at = now
        return True, f"Notificación ISAPI configurada hacia {res['target_url']} (Slot #{cfg.host_id})"
    except ISAPIError as exc:
        device.status = "offline" if exc.status_code in (502, 504) else "error"
        device.last_error = exc.message[:500]
        return False, exc.message


def _get_device_or_404(db: SessionDep, device_id: int) -> HikvisionDevice:
    device = db.get(HikvisionDevice, device_id)
    if not device:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Reloj checador Hikvision no encontrado",
        )
    return device


def _build_isapi_client(device: HikvisionDevice) -> HikvisionISAPIClient:
    try:
        plain_password = decrypt_secret(device.encrypted_password)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"No se pudo descifrar la credencial ISAPI del dispositivo: {exc}",
        ) from exc

    return HikvisionISAPIClient(
        ip_address=device.ip_address,
        port=device.port,
        username=device.username,
        password=plain_password,
        protocol=device.protocol,
        verify_ssl=device.verify_ssl,
        timeout_seconds=device.timeout_seconds,
    )


def _apply_probe_to_device(device: HikvisionDevice, probe_data: dict[str, object]) -> None:
    now = datetime.now(timezone.utc)
    device.status = "online"
    device.last_seen_at = now
    device.last_error = None

    if probe_data.get("device_id"):
        device.device_id_code = str(probe_data["device_id"])[:100]
    if probe_data.get("model"):
        device.model = str(probe_data["model"])[:100]
    if probe_data.get("serial_number"):
        device.serial_number = str(probe_data["serial_number"])[:120]
    if probe_data.get("firmware_version"):
        device.firmware_version = str(probe_data["firmware_version"])[:100]
    if probe_data.get("mac_address"):
        device.mac_address = str(probe_data["mac_address"])[:64]
    if probe_data.get("device_type"):
        device.device_type = str(probe_data["device_type"])[:80]


@router.get(
    "",
    response_model=list[HikvisionDeviceRead],
    dependencies=[Depends(require_permissions("devices:read"))],
    summary="Listar relojes checadores Hikvision",
)
def list_devices(
    db: SessionDep,
    company_id: Optional[int] = Query(default=None, description="Filtrar por empresa propietaria del reloj"),
    search: Optional[str] = Query(default=None, description="Filtrar por nombre, IP, serie o ubicación"),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    is_active: Optional[bool] = Query(default=None),
) -> list[HikvisionDevice]:
    stmt = select(HikvisionDevice)
    if company_id is not None:
        stmt = stmt.where(HikvisionDevice.company_id == company_id)
    if search and search.strip():
        term = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                HikvisionDevice.name.ilike(term),
                HikvisionDevice.ip_address.ilike(term),
                HikvisionDevice.location.ilike(term),
                HikvisionDevice.serial_number.ilike(term),
                HikvisionDevice.model.ilike(term),
            )
        )
    if status_filter and status_filter.strip():
        stmt = stmt.where(HikvisionDevice.status == status_filter.strip().lower())
    if is_active is not None:
        stmt = stmt.where(HikvisionDevice.is_active == is_active)

    stmt = stmt.order_by(HikvisionDevice.id.asc())
    return list(db.scalars(stmt).all())


@router.post(
    "",
    response_model=HikvisionDeviceRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permissions("devices:write"))],
    summary="Registrar un nuevo reloj checador Hikvision",
)
def create_device(db: SessionDep, payload: HikvisionDeviceCreate) -> HikvisionDevice:
    company = db.get(Company, payload.company_id)
    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="La empresa seleccionada para el reloj no existe",
        )

    existing_name = db.scalar(
        select(HikvisionDevice).where(HikvisionDevice.name == payload.name)
    )
    if existing_name:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe un reloj registrado con el nombre '{payload.name}'",
        )

    existing_endpoint = db.scalar(
        select(HikvisionDevice).where(
            HikvisionDevice.ip_address == payload.ip_address,
            HikvisionDevice.port == payload.port,
        )
    )
    if existing_endpoint:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe un reloj con la dirección {payload.ip_address}:{payload.port} ('{existing_endpoint.name}')",
        )

    device = HikvisionDevice(
        company_id=company.id,
        name=payload.name,
        location=payload.location,
        protocol=payload.protocol,
        ip_address=payload.ip_address,
        port=payload.port,
        username=payload.username,
        encrypted_password=encrypt_secret(payload.password),
        verify_ssl=payload.verify_ssl,
        timeout_seconds=payload.timeout_seconds,
        is_active=payload.is_active,
        notes=payload.notes,
        status="unknown",
    )

    if payload.test_on_create:
        client = HikvisionISAPIClient(
            ip_address=payload.ip_address,
            port=payload.port,
            username=payload.username,
            password=payload.password,
            protocol=payload.protocol,
            verify_ssl=payload.verify_ssl,
            timeout_seconds=payload.timeout_seconds,
        )
        try:
            probe_data = client.probe_device()
            _apply_probe_to_device(device, probe_data)
            cfg = _get_or_create_notification_config(db)
            if cfg.enabled and cfg.auto_apply_on_create:
                _push_notification_config_to_device(device, cfg)
        except ISAPIError as exc:
            device.status = "offline" if exc.status_code in (502, 504) else "error"
            device.last_error = exc.message[:500]

    db.add(device)
    db.commit()
    db.refresh(device)
    return device


@router.get(
    "/notification-config",
    response_model=ISAPINotificationConfigRead,
    dependencies=[Depends(require_permissions("devices:read"))],
    summary="Obtener la configuración global de notificación de eventos ISAPI (HttpHostNotification)",
)
def get_global_notification_config(db: SessionDep) -> ISAPINotificationConfig:
    return _get_or_create_notification_config(db)


@router.put(
    "/notification-config",
    response_model=ISAPINotificationConfigRead,
    dependencies=[Depends(require_permissions("devices:write"))],
    summary="Guardar la configuración global de API destino para eventos ISAPI",
)
def update_global_notification_config(
    db: SessionDep,
    payload: ISAPINotificationConfigUpdate,
) -> ISAPINotificationConfig:
    cfg = _get_or_create_notification_config(db)
    cfg.enabled = payload.enabled
    cfg.host_id = payload.host_id
    cfg.protocol_type = payload.protocol_type.upper()
    cfg.addressing_format_type = payload.addressing_format_type.lower()
    cfg.host_address = payload.host_address.strip()
    if payload.ip_address is not None:
        cfg.ip_address = payload.ip_address.strip() or None
    elif cfg.addressing_format_type == "ipaddress":
        cfg.ip_address = cfg.host_address
    if payload.domain is not None:
        cfg.domain = payload.domain.strip() or None
    elif cfg.addressing_format_type == "hostname":
        cfg.domain = cfg.host_address
    cfg.port_no = payload.port_no
    clean_path = payload.url_path.strip()
    cfg.url_path = clean_path if clean_path.startswith("/") else f"/{clean_path}"
    cfg.parameter_format_type = payload.parameter_format_type.upper()
    cfg.auth_method = payload.auth_method
    cfg.auth_username = payload.auth_username.strip() if payload.auth_username else None
    if payload.auth_password is not None and payload.auth_password != "":
        cfg.encrypted_auth_password = encrypt_secret(payload.auth_password)
    elif payload.auth_method == "none":
        cfg.encrypted_auth_password = None
    cfg.auto_apply_on_create = payload.auto_apply_on_create

    if payload.apply_to_all_now:
        active_devices = list(
            db.scalars(
                select(HikvisionDevice).where(HikvisionDevice.is_active.is_(True))
            ).all()
        )
        for dev in active_devices:
            _push_notification_config_to_device(dev, cfg)
        cfg.last_applied_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(cfg)
    return cfg


@router.post(
    "/notification-config/apply-all",
    response_model=ISAPINotificationApplyBulkResponse,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Configurar el webhook/API de eventos ISAPI (HttpHostNotification) en todos los relojes",
)
def apply_notification_config_to_all_devices(
    db: SessionDep,
    only_active: bool = Query(default=True, description="Aplicar únicamente a relojes activos"),
) -> ISAPINotificationApplyBulkResponse:
    cfg = _get_or_create_notification_config(db)
    stmt = select(HikvisionDevice).order_by(HikvisionDevice.id.asc())
    if only_active:
        stmt = stmt.where(HikvisionDevice.is_active.is_(True))
    target_devices = list(db.scalars(stmt).all())

    results: list[ISAPINotificationApplyDeviceResult] = []
    succeeded = 0
    failed = 0

    for dev in target_devices:
        ok, msg = _push_notification_config_to_device(dev, cfg)
        if ok:
            succeeded += 1
        else:
            failed += 1
        results.append(
            ISAPINotificationApplyDeviceResult(
                device_id=dev.id,
                device_name=dev.name,
                base_url=dev.base_url,
                success=ok,
                target_url=dev.webhook_target_url if ok else None,
                message=msg,
            )
        )

    cfg.last_applied_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(cfg)

    return ISAPINotificationApplyBulkResponse(
        total_devices=len(target_devices),
        succeeded=succeeded,
        failed=failed,
        target_url=cfg.full_target_url,
        config=ISAPINotificationConfigRead.model_validate(cfg),
        results=results,
    )


def _require_any_log_permission(*codes: str) -> Callable[[CurrentUser], User]:
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


def _parse_json_or_xml(text: str) -> Optional[dict[str, Any]]:
    """Attempt to parse a string as JSON first, then as ISAPI XML."""
    cleaned = text.strip()
    if not cleaned:
        return None
    if cleaned.startswith("{"):
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    if cleaned.startswith("<"):
        try:
            parsed_xml = IsapiGenerateXmlController.xml_to_dict(cleaned)
            if isinstance(parsed_xml, dict) and parsed_xml:
                return parsed_xml
        except Exception:
            pass
    # Fallback: buscar bloque JSON o XML embebido (ej. multipart crudo)
    first_brace = cleaned.find("{")
    last_brace = cleaned.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            parsed = json.loads(cleaned[first_brace : last_brace + 1])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


async def _extract_webhook_payload(request: Request) -> tuple[dict[str, Any], str]:
    """Extract structured event payload (JSON, XML or multipart/form-data) sent by a Hikvision device."""
    content_type = (request.headers.get("content-type") or "").lower()

    if "multipart" in content_type:
        try:
            form = await request.form()
            for key, val in form.multi_items():
                if isinstance(val, str):
                    parsed = _parse_json_or_xml(val)
                    if parsed:
                        return parsed, val.strip()
                elif hasattr(val, "read"):
                    part_bytes = await val.read()  # type: ignore[misc]
                    part_ct = (getattr(val, "content_type", "") or "").lower()
                    if (
                        key.lower()
                        in (
                            "event_log",
                            "eventnotificationalert",
                            "accesscontrollerevent",
                            "event",
                        )
                        or "json" in part_ct
                        or "xml" in part_ct
                        or "text" in part_ct
                        or part_bytes.lstrip().startswith((b"{", b"<"))
                    ):
                        part_text = part_bytes.decode("utf-8", errors="ignore").strip()
                        parsed = _parse_json_or_xml(part_text)
                        if parsed:
                            return parsed, part_text
        except Exception:
            pass

    try:
        raw_bytes = await request.body()
        raw_text = raw_bytes.decode("utf-8", errors="ignore").strip()
        if raw_text:
            parsed = _parse_json_or_xml(raw_text)
            if parsed:
                return parsed, raw_text
            return {}, raw_text[:4000]
    except Exception:
        pass

    return {}, ""


def _parse_event_datetime(raw_dt: Optional[str], fallback: datetime) -> datetime:
    if not raw_dt or not raw_dt.strip():
        return fallback
    cleaned = raw_dt.strip()
    try:
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return fallback


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    try:
        return int(str(value).strip())
    except Exception:
        return default


# Caché en memoria para throttling de heartbeats de dispositivos (evita saturar PostgreSQL con 500+ relojes cada 30s)
HEARTBEAT_DB_FLUSH_SECONDS = 180
OFFLINE_RECOVERY_THRESHOLD_SECONDS = 600  # 10 min sin conexión activa recuperación de checadas atrasadas
_DEVICE_HEARTBEAT_CACHE: dict[str, datetime] = {}


def _find_duplicate_attendance_log(
    db: SessionDep,
    *,
    device_id: Optional[int],
    serial_no: Optional[int],
    collaborator_id: Optional[int],
    device_employee_no: Optional[str],
    event_time: datetime,
) -> Optional[AttendanceLog]:
    """Detect duplicate check-in logs caused by intermittent network retries or offline re-syncs."""
    if device_id is not None and serial_no is not None:
        dup = db.scalar(
            select(AttendanceLog).where(
                AttendanceLog.device_id == device_id,
                AttendanceLog.serial_no == serial_no,
                AttendanceLog.event_time == event_time,
            )
        )
        if dup is not None:
            return dup

    if collaborator_id is not None:
        stmt = select(AttendanceLog).where(
            AttendanceLog.collaborator_id == collaborator_id,
            AttendanceLog.event_time == event_time,
        )
        if device_id is not None:
            stmt = stmt.where(AttendanceLog.device_id == device_id)
        return db.scalar(stmt)

    if device_id is not None and device_employee_no:
        return db.scalar(
            select(AttendanceLog).where(
                AttendanceLog.device_id == device_id,
                AttendanceLog.device_employee_no == device_employee_no,
                AttendanceLog.event_time == event_time,
            )
        )
    return None


def _sync_offline_events_from_device(
    db: SessionDep,
    device: HikvisionDevice,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    max_pages: int = 50,
) -> ISAPIOfflineSyncResponse:
    """Pull attendance events stored in the Hikvision terminal's internal memory (AcsEvent), import missing logs, and recalculate affected days."""
    now = datetime.now(timezone.utc)
    if not start_time:
        base_start = device.last_sync_at or (now - timedelta(hours=72))
        if base_start.tzinfo is None:
            base_start = base_start.replace(tzinfo=timezone.utc)
        # Retroceder 15 min adicionales por seguridad
        eff_start_dt = base_start - timedelta(minutes=15)
        start_time = eff_start_dt.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        if len(start_time) > 19 and start_time[-3] != ":":
            start_time = f"{start_time[:-2]}:{start_time[-2:]}"

    if not end_time:
        end_time = now.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        if len(end_time) > 19 and end_time[-3] != ":":
            end_time = f"{end_time[:-2]}:{end_time[-2:]}"

    client = _build_isapi_client(device)
    result = client.filter_events(
        start_time=start_time,
        end_time=end_time,
        major=5,
        minor=0,
        max_pages=max_pages,
    )
    raw_events = result.get("events", [])

    # Pre-cargar mapeos de colaboradores de este dispositivo/empresa para evitar consultas N+1
    sync_rows = list(
        db.scalars(
            select(CollaboratorDeviceSync).where(CollaboratorDeviceSync.device_id == device.id)
        ).all()
    )
    sync_by_id: dict[str, CollaboratorDeviceSync] = {str(s.id): s for s in sync_rows}
    sync_by_collab_id: dict[int, CollaboratorDeviceSync] = {
        s.collaborator_id: s for s in sync_rows
    }

    collab_rows = list(
        db.scalars(
            select(Collaborator).where(Collaborator.company_id == device.company_id)
        ).all()
    )
    collab_by_emp_no: dict[str, Collaborator] = {c.employee_no: c for c in collab_rows}
    collab_by_id: dict[int, Collaborator] = {c.id: c for c in collab_rows}

    imported_count = 0
    skipped_duplicates = 0
    affected_collab_dates: dict[tuple[int, str], datetime] = {}

    for ev in raw_events:
        emp_raw = ev.get("employee_no")
        emp_str = str(emp_raw).strip() if emp_raw is not None and str(emp_raw).strip() else None
        if not emp_str:
            continue

        ev_time = _parse_event_datetime(str(ev.get("time") or ""), now)
        serial_no = _safe_int(ev.get("serial_no"))
        minor_val = _safe_int(ev.get("minor"), default=0)
        major_val = _safe_int(ev.get("major"), default=5)

        sync_rec = sync_by_id.get(emp_str)
        collab: Optional[Collaborator] = None
        if sync_rec:
            collab = collab_by_id.get(sync_rec.collaborator_id) or db.get(
                Collaborator, sync_rec.collaborator_id
            )
        if collab is None:
            collab = collab_by_emp_no.get(emp_str)
            if collab and sync_rec is None:
                sync_rec = sync_by_collab_id.get(collab.id)

        dup = _find_duplicate_attendance_log(
            db,
            device_id=device.id,
            serial_no=serial_no,
            collaborator_id=collab.id if collab else None,
            device_employee_no=emp_str,
            event_time=ev_time,
        )
        if dup is not None:
            skipped_duplicates += 1
            continue

        log_entry = AttendanceLog(
            source_type="clock_device",
            company_id=collab.company_id if collab else device.company_id,
            collaborator_id=collab.id if collab else None,
            device_id=device.id,
            sync_id=sync_rec.id if sync_rec else None,
            device_employee_no=emp_str,
            collaborator_employee_no=collab.employee_no if collab else None,
            collaborator_name=collab.full_name if collab else (str(ev.get("name") or "").strip() or None),
            device_name=device.name,
            source_ip=device.ip_address,
            event_type="AccessControllerEvent",
            major_event=major_val,
            minor_event=minor_val,
            event_label=str(ev.get("event_label") or "Checada recuperada de memoria del reloj")[:150],
            attendance_status=str(ev.get("attendance_status"))[:64]
            if ev.get("attendance_status")
            else None,
            verify_mode=str(ev.get("verify_mode"))[:64] if ev.get("verify_mode") else None,
            card_no=str(ev.get("card_no"))[:64] if ev.get("card_no") else None,
            door_no=_safe_int(ev.get("door_no")),
            serial_no=serial_no,
            is_valid_attendance=True,
            raw_payload=json.dumps(ev, ensure_ascii=False)[:8000],
            event_time=ev_time,
        )
        db.add(log_entry)
        imported_count += 1

        if collab is not None:
            day_iso = ev_time.astimezone().date().isoformat()
            affected_collab_dates[(collab.id, day_iso)] = ev_time

    device.status = "online"
    device.last_seen_at = now
    device.last_sync_at = now
    device.last_error = None
    db.commit()

    # Recalcular únicamente 1 vez por cada (colaborador, fecha) afectado respetando su turno congelado
    for (c_id, _day_iso), rep_dt in affected_collab_dates.items():
        sync_collaborator_daily_attendance(db, c_id, rep_dt)

    return ISAPIOfflineSyncResponse(
        device_id=device.id,
        device_name=device.name,
        fetched_events_count=len(raw_events),
        imported_logs_count=imported_count,
        skipped_duplicates_count=skipped_duplicates,
        recalculated_days_count=len(affected_collab_dates),
        message=(
            f"Sincronización completada en '{device.name}': {imported_count} checada(s) atrasada(s) "
            f"importada(s), {skipped_duplicates} duplicada(s) omitida(s) y {len(affected_collab_dates)} "
            f"jornada(s) actualizada(s) en daily_attendances."
        ),
    )


def recover_device_offline_events_task(
    device_id: int,
    start_time_iso: Optional[str] = None,
    end_time_iso: Optional[str] = None,
) -> None:
    """Background task triggered when an offline device reconnects after a prolonged network outage."""
    db = SessionLocal()
    try:
        device = db.get(HikvisionDevice, device_id)
        if not device or not device.is_active:
            return
        _sync_offline_events_from_device(
            db,
            device=device,
            start_time=start_time_iso,
            end_time=end_time_iso,
            max_pages=40,
        )
    except Exception:
        db.rollback()
    finally:
        db.close()


def _resolve_webhook_device(
    db: SessionDep,
    explicit_device_id: Any,
    payload_ip: Optional[str],
    client_ip: Optional[str],
    mac_addr: Optional[str],
    device_id_code: Optional[str],
) -> Optional[HikvisionDevice]:
    device: Optional[HikvisionDevice] = None
    if explicit_device_id is not None and str(explicit_device_id).strip().isdigit():
        device = db.get(HikvisionDevice, int(str(explicit_device_id).strip()))
    if device is None and payload_ip:
        device = db.scalar(
            select(HikvisionDevice).where(HikvisionDevice.ip_address == payload_ip)
        )
    if device is None and client_ip:
        device = db.scalar(
            select(HikvisionDevice).where(HikvisionDevice.ip_address == client_ip)
        )
    if device is None and mac_addr:
        device = db.scalar(
            select(HikvisionDevice).where(HikvisionDevice.mac_address.ilike(mac_addr))
        )
    if device is None and device_id_code:
        device = db.scalar(
            select(HikvisionDevice).where(
                or_(
                    HikvisionDevice.device_id_code == device_id_code,
                    HikvisionDevice.serial_number == device_id_code,
                )
            )
        )
    return device


@router.post(
    "/webhook/isapi",
    response_model=ISAPIWebhookEventResponse,
    summary="Endpoint receptor de checadas y eventos push ISAPI enviados por relojes Hikvision",
)
@router.post(
    "/webhook/isap",
    response_model=ISAPIWebhookEventResponse,
    include_in_schema=False,
)
async def receive_isapi_webhook_event(
    request: Request,
    background_tasks: BackgroundTasks,
    db: SessionDep,
) -> ISAPIWebhookEventResponse:
    """Receive real-time attendance events pushed by Hikvision devices via HttpHostNotification and record logs."""
    now = datetime.now(timezone.utc)
    client_ip = request.client.host if request.client else None

    parsed_payload, raw_payload_str = await _extract_webhook_payload(request)
    alert = (
        parsed_payload.get("EventNotificationAlert")
        if isinstance(parsed_payload.get("EventNotificationAlert"), dict)
        else parsed_payload
    )
    if not isinstance(alert, dict):
        alert = {}

    payload_ip = str(alert.get("ipAddress") or alert.get("ip_address") or "").strip() or None
    effective_ip = payload_ip or client_ip
    mac_addr = str(alert.get("macAddress") or alert.get("mac_address") or "").strip() or None
    device_id_code = str(alert.get("deviceID") or alert.get("device_id_code") or "").strip() or None
    explicit_device_id = alert.get("hikvision_device_id") or request.query_params.get("device_id")

    # Determinar si el registro proviene de dispositivo checador ('clock_device') o dispositivo móvil ('mobile_device')
    raw_source = (
        str(alert.get("source_type") or request.query_params.get("source_type") or "clock_device")
        .strip()
        .lower()
    )
    source_type = (
        "mobile_device" if raw_source in ("mobile_device", "mobile", "movil") else "clock_device"
    )
    source_label = "Dispositivo Móvil" if source_type == "mobile_device" else "Dispositivo Checador"

    event_type = str(
        alert.get("eventType") or alert.get("event_type") or "AccessControllerEvent"
    ).strip()

    if isinstance(alert.get("AccessControllerEvent"), dict):
        acs: dict[str, Any] = alert["AccessControllerEvent"]
    elif isinstance(alert.get("AcsEvent"), dict):
        acs = alert["AcsEvent"]
    else:
        acs = alert

    major_val = _safe_int(
        acs.get("majorEventType") if acs.get("majorEventType") is not None else acs.get("major"),
        default=5,
    )
    minor_val = _safe_int(
        acs.get("subEventType") if acs.get("subEventType") is not None else acs.get("minor"),
        default=0,
    )

    emp_raw = (
        acs.get("employeeNoString")
        or acs.get("employeeNo")
        or acs.get("employee_no")
        or alert.get("employeeNoString")
        or alert.get("employeeNo")
        or alert.get("employee_no")
        or request.query_params.get("employee_no")
    )
    emp_str = str(emp_raw).strip() if emp_raw is not None and str(emp_raw).strip() else None

    reported_name = str(acs.get("name") or alert.get("name") or "").strip() or None
    reported_device_name = (
        str(acs.get("deviceName") or alert.get("deviceName") or "").strip() or None
    )
    card_no = str(acs.get("cardNo") or acs.get("card_no") or "").strip() or None
    verify_mode = (
        str(acs.get("currentVerifyMode") or acs.get("verify_mode") or "").strip() or None
    )
    attendance_status = (
        str(acs.get("attendanceStatus") or acs.get("attendance_status") or "").strip() or None
    )
    door_no = _safe_int(acs.get("doorNo") if acs.get("doorNo") is not None else acs.get("door_no"))
    serial_no = _safe_int(
        acs.get("serialNo") if acs.get("serialNo") is not None else acs.get("serial_no")
    )
    date_str = (
        str(
            alert.get("dateTime")
            or acs.get("time")
            or alert.get("time")
            or alert.get("event_time")
            or ""
        ).strip()
        or None
    )
    event_time = _parse_event_datetime(date_str, now)

    is_heartbeat = event_type.lower() in ("heartbeat", "videoloss") and not emp_str and not card_no
    is_door_relay = (
        "AccessControllerEvent" in alert
        and not emp_str
        and not card_no
        and (minor_val or 0) in (21, 22, 23, 400, 1024)
    )
    hb_cache_key = str(
        explicit_device_id or payload_ip or mac_addr or device_id_code or client_ip or "unknown"
    )

    # Fast-path para Heartbeats y eventos de puerta sin empleado (protege la BD ante 500+ relojes cada 30s)
    if is_heartbeat or is_door_relay:
        last_flushed = _DEVICE_HEARTBEAT_CACHE.get(hb_cache_key)
        if (
            last_flushed is None
            or (now - last_flushed).total_seconds() >= HEARTBEAT_DB_FLUSH_SECONDS
        ):
            device = _resolve_webhook_device(
                db, explicit_device_id, payload_ip, client_ip, mac_addr, device_id_code
            )
            if device:
                prev_seen = device.last_seen_at
                if prev_seen is not None and prev_seen.tzinfo is None:
                    prev_seen = prev_seen.replace(tzinfo=timezone.utc)
                was_offline_long = (
                    device.status == "offline"
                    or (
                        prev_seen is not None
                        and (now - prev_seen).total_seconds() >= OFFLINE_RECOVERY_THRESHOLD_SECONDS
                    )
                )
                device.status = "online"
                device.last_seen_at = now
                device.last_sync_at = now
                device.last_error = None
                db.commit()

                if was_offline_long and prev_seen is not None:
                    start_iso = (prev_seen - timedelta(minutes=10)).astimezone().strftime(
                        "%Y-%m-%dT%H:%M:%S"
                    )
                    background_tasks.add_task(
                        recover_device_offline_events_task,
                        device.id,
                        start_iso,
                        None,
                    )
            _DEVICE_HEARTBEAT_CACHE[hb_cache_key] = now

        msg = (
            "Heartbeat ISAPI recibido (sin registro de checada)"
            if is_heartbeat
            else f"Evento de estado de puerta ({minor_val}) recibido sin empleado"
        )
        return ISAPIWebhookEventResponse(
            status="ok",
            logged=False,
            source_type=source_type,
            source_label=source_label,
            message=msg,
            received_at=now.isoformat(),
            source_ip=effective_ip,
            log=None,
        )

    # 1. Resolver el reloj checador Hikvision
    device = _resolve_webhook_device(
        db, explicit_device_id, payload_ip, client_ip, mac_addr, device_id_code
    )

    # 2. Resolver colaborador y registro de sincronización (usando primero collaborator_device_syncs.id)
    sync_record: Optional[CollaboratorDeviceSync] = None
    collab: Optional[Collaborator] = None

    if emp_str and emp_str.isdigit():
        candidate_sync = db.get(CollaboratorDeviceSync, int(emp_str))
        if candidate_sync and (device is None or candidate_sync.device_id == device.id):
            sync_record = candidate_sync
            collab = candidate_sync.collaborator or db.get(
                Collaborator, candidate_sync.collaborator_id
            )
            if device is None:
                device = candidate_sync.device or db.get(
                    HikvisionDevice, candidate_sync.device_id
                )

    if collab is None and emp_str:
        if device and device.company_id:
            collab = db.scalar(
                select(Collaborator).where(
                    Collaborator.company_id == device.company_id,
                    Collaborator.employee_no == emp_str,
                )
            )
        if collab is None:
            collab = db.scalar(
                select(Collaborator).where(Collaborator.employee_no == emp_str)
            )
        if collab and device and sync_record is None:
            sync_record = db.scalar(
                select(CollaboratorDeviceSync).where(
                    CollaboratorDeviceSync.collaborator_id == collab.id,
                    CollaboratorDeviceSync.device_id == device.id,
                )
            )

    if device:
        device.status = "online"
        device.last_seen_at = now
        device.last_sync_at = now
        device.last_error = None
        _DEVICE_HEARTBEAT_CACHE[hb_cache_key] = now

    company_id = (collab.company_id if collab else None) or (
        device.company_id if device else None
    )
    resolved_collab_name = collab.full_name if collab else reported_name
    resolved_device_name = device.name if device else reported_device_name

    if source_type == "mobile_device":
        event_label = str(alert.get("event_label") or "Checada en Dispositivo Móvil")
    else:
        if (minor_val or 0) in ACS_MINOR_EVENT_LABELS:
            event_label = ACS_MINOR_EVENT_LABELS[minor_val or 0]
        elif emp_str:
            event_label = "Checada registrada en reloj"
        else:
            event_label = f"Evento ISAPI ({major_val or 5}:{minor_val or 0})"

    is_valid_attendance = bool(emp_str) or ((minor_val or 0) in (1, 2, 38, 75, 105))

    # Deduplicación (idempotencia) ante reintentos del reloj por red intermitente
    existing_log = _find_duplicate_attendance_log(
        db,
        device_id=device.id if device else None,
        serial_no=serial_no,
        collaborator_id=collab.id if collab else None,
        device_employee_no=emp_str,
        event_time=event_time,
    )
    if existing_log is not None:
        if device:
            db.commit()
        if collab is not None and is_valid_attendance:
            background_tasks.add_task(
                process_collaborator_day_attendance_task,
                collab.id,
                event_time,
            )
        emp_display = resolved_collab_name or f"ID #{emp_str or 'S/N'}"
        return ISAPIWebhookEventResponse(
            status="ok",
            logged=False,
            source_type=source_type,
            source_label=source_label,
            message=f"Checada ya registrada previamente (duplicado omitido): {emp_display}",
            received_at=now.isoformat(),
            source_ip=effective_ip,
            log=AttendanceLogRead.model_validate(existing_log),
        )

    # Calcular automáticamente el Tipo de Checada (checkIn, breakOut, breakIn, checkOut)
    # de acuerdo al turno asignado del colaborador y las checadas previas del día (ignorando el botón del reloj).
    if is_valid_attendance and collab is not None:
        inferred_status, inferred_label = infer_webhook_log_status_by_shift(
            db, collab, event_time
        )
        attendance_status = inferred_status
        event_label = f"{inferred_label} • {event_label}" if event_label else inferred_label
    elif is_valid_attendance:
        attendance_status = "checkIn"

    if not raw_payload_str and parsed_payload:
        try:
            raw_payload_str = json.dumps(parsed_payload, ensure_ascii=False)
        except Exception:
            raw_payload_str = None

    log_entry = AttendanceLog(
        source_type=source_type,
        company_id=company_id,
        collaborator_id=collab.id if collab else None,
        device_id=device.id if device else None,
        sync_id=sync_record.id if sync_record else None,
        device_employee_no=emp_str,
        collaborator_employee_no=collab.employee_no if collab else None,
        collaborator_name=resolved_collab_name,
        device_name=resolved_device_name,
        source_ip=effective_ip,
        event_type=event_type[:64] if event_type else "AccessControllerEvent",
        major_event=major_val,
        minor_event=minor_val,
        event_label=event_label[:150] if event_label else None,
        attendance_status=attendance_status[:64] if attendance_status else None,
        verify_mode=verify_mode[:64] if verify_mode else None,
        card_no=card_no[:64] if card_no else None,
        door_no=door_no,
        serial_no=serial_no,
        is_valid_attendance=is_valid_attendance,
        mobile_device_id=str(alert.get("mobile_device_id"))[:120]
        if alert.get("mobile_device_id")
        else None,
        latitude=str(alert.get("latitude"))[:40] if alert.get("latitude") is not None else None,
        longitude=str(alert.get("longitude"))[:40] if alert.get("longitude") is not None else None,
        raw_payload=raw_payload_str[:8000] if raw_payload_str else None,
        event_time=event_time,
    )
    db.add(log_entry)
    db.commit()
    db.refresh(log_entry)

    # Encolar en segundo plano el cálculo/actualización de daily_attendances para este colaborador y fecha
    # sin bloquear la respuesta HTTP 200 OK hacia el reloj checador.
    if collab is not None and is_valid_attendance:
        background_tasks.add_task(
            process_collaborator_day_attendance_task,
            collab.id,
            event_time,
        )

    emp_display = resolved_collab_name or f"ID #{emp_str or 'S/N'}"
    return ISAPIWebhookEventResponse(
        status="ok",
        logged=True,
        source_type=source_type,
        source_label=source_label,
        message=f"Checada registrada ({source_label}): {emp_display} — {event_label}",
        received_at=now.isoformat(),
        source_ip=effective_ip,
        log=AttendanceLogRead.model_validate(log_entry),
    )


@router.get(
    "/attendance-logs",
    response_model=list[AttendanceLogRead],
    dependencies=[
        Depends(
            _require_any_log_permission(
                "attendance:read",
                "devices:read",
                "devices:isapi",
                "collaborators:read",
            )
        )
    ],
    summary="Listar registros de checadas (por Dispositivo Checador ISAPI o Dispositivo Móvil)",
)
def list_attendance_logs(
    db: SessionDep,
    company_id: Optional[int] = Query(default=None, description="Filtrar por empresa"),
    device_id: Optional[int] = Query(default=None, description="Filtrar por reloj Hikvision"),
    collaborator_id: Optional[int] = Query(default=None, description="Filtrar por colaborador"),
    source_type: Optional[Literal["clock_device", "mobile_device"]] = Query(
        default=None,
        description="Filtrar por origen: 'clock_device' (Dispositivo Checador) o 'mobile_device' (Dispositivo Móvil)",
    ),
    search: Optional[str] = Query(
        default=None,
        description="Buscar por nombre, No. empleado, ID Sync ISAPI, reloj o IP",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    skip: int = Query(default=0, ge=0),
) -> list[AttendanceLog]:
    stmt = select(AttendanceLog)
    if company_id is not None:
        stmt = stmt.where(AttendanceLog.company_id == company_id)
    if device_id is not None:
        stmt = stmt.where(AttendanceLog.device_id == device_id)
    if collaborator_id is not None:
        stmt = stmt.where(AttendanceLog.collaborator_id == collaborator_id)
    if source_type is not None:
        stmt = stmt.where(AttendanceLog.source_type == source_type)
    if search and search.strip():
        term = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                AttendanceLog.collaborator_name.ilike(term),
                AttendanceLog.collaborator_employee_no.ilike(term),
                AttendanceLog.device_employee_no.ilike(term),
                AttendanceLog.device_name.ilike(term),
                AttendanceLog.source_ip.ilike(term),
                AttendanceLog.event_label.ilike(term),
            )
        )

    stmt = (
        stmt.order_by(AttendanceLog.event_time.desc(), AttendanceLog.id.desc())
        .offset(skip)
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


@router.get(
    "/attendance-logs/stats",
    response_model=AttendanceLogStatsResponse,
    dependencies=[
        Depends(
            _require_any_log_permission(
                "attendance:read",
                "devices:read",
                "devices:isapi",
                "collaborators:read",
            )
        )
    ],
    summary="Obtener estadísticas de los logs de checadas (Dispositivo Checador vs Dispositivo Móvil)",
)
def get_attendance_logs_stats(
    db: SessionDep,
    company_id: Optional[int] = Query(default=None),
) -> AttendanceLogStatsResponse:
    base_cond = [AttendanceLog.company_id == company_id] if company_id is not None else []

    total_logs = (
        db.scalar(select(func.count(AttendanceLog.id)).where(*base_cond)) or 0
    )
    clock_device_logs = (
        db.scalar(
            select(func.count(AttendanceLog.id)).where(
                *base_cond,
                AttendanceLog.source_type == "clock_device",
            )
        )
        or 0
    )
    mobile_device_logs = (
        db.scalar(
            select(func.count(AttendanceLog.id)).where(
                *base_cond,
                AttendanceLog.source_type == "mobile_device",
            )
        )
        or 0
    )
    matched_collaborator_logs = (
        db.scalar(
            select(func.count(AttendanceLog.id)).where(
                *base_cond,
                AttendanceLog.collaborator_id.is_not(None),
            )
        )
        or 0
    )

    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    today_logs = (
        db.scalar(
            select(func.count(AttendanceLog.id)).where(
                *base_cond,
                AttendanceLog.event_time >= today_start,
            )
        )
        or 0
    )

    return AttendanceLogStatsResponse(
        total_logs=total_logs,
        clock_device_logs=clock_device_logs,
        mobile_device_logs=mobile_device_logs,
        today_logs=today_logs,
        matched_collaborator_logs=matched_collaborator_logs,
    )


@router.delete(
    "/attendance-logs/{log_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[
        Depends(
            _require_any_log_permission(
                "attendance:write",
                "devices:write",
                "devices:delete",
            )
        )
    ],
    summary="Eliminar un registro de bitácora de checada",
)
def delete_attendance_log(
    db: SessionDep,
    log_id: int,
) -> None:
    log_entry = db.get(AttendanceLog, log_id)
    if not log_entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Registro de checada no encontrado",
        )
    collab_id = log_entry.collaborator_id
    evt_time = log_entry.event_time
    db.delete(log_entry)
    db.commit()

    if collab_id is not None and evt_time is not None:
        sync_collaborator_daily_attendance(db, collab_id, evt_time)


@router.post(
    "/test-connection",
    response_model=ISAPIProbeResponse,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Probar conexión ISAPI con parámetros directos antes de guardar",
)
def test_raw_isapi_connection(
    db: SessionDep,
    payload: ISAPITestConnectionRequest,
) -> ISAPIProbeResponse:
    password = payload.password
    if not password and payload.device_id is not None:
        existing = _get_device_or_404(db, payload.device_id)
        password = decrypt_secret(existing.encrypted_password)

    if not password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes proporcionar la contraseña ISAPI para probar la conexión",
        )

    client = HikvisionISAPIClient(
        ip_address=payload.ip_address,
        port=payload.port,
        username=payload.username,
        password=password,
        protocol=payload.protocol,
        verify_ssl=payload.verify_ssl,
        timeout_seconds=payload.timeout_seconds,
    )
    try:
        probe_data = client.probe_device()
        return ISAPIProbeResponse(
            reachable=True,
            status="online",
            message=f"Conexión ISAPI exitosa con {client.base_url}",
            **probe_data,
        )
    except ISAPIError as exc:
        return ISAPIProbeResponse(
            reachable=False,
            status="offline" if exc.status_code in (502, 504) else "error",
            message=exc.message,
        )


@router.get(
    "/{device_id}",
    response_model=HikvisionDeviceRead,
    dependencies=[Depends(require_permissions("devices:read"))],
    summary="Obtener detalle de un reloj Hikvision",
)
def get_device(db: SessionDep, device_id: int) -> HikvisionDevice:
    return _get_device_or_404(db, device_id)


@router.patch(
    "/{device_id}",
    response_model=HikvisionDeviceRead,
    dependencies=[Depends(require_permissions("devices:write"))],
    summary="Actualizar configuración de un reloj Hikvision",
)
def update_device(
    db: SessionDep,
    device_id: int,
    payload: HikvisionDeviceUpdate,
) -> HikvisionDevice:
    device = _get_device_or_404(db, device_id)

    if payload.company_id is not None and payload.company_id != device.company_id:
        company = db.get(Company, payload.company_id)
        if not company:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="La empresa seleccionada para el reloj no existe",
            )
        device.company_id = company.id

    if payload.name is not None and payload.name.strip() != device.name:
        clean_name = payload.name.strip()
        conflict = db.scalar(
            select(HikvisionDevice).where(
                HikvisionDevice.name == clean_name,
                HikvisionDevice.id != device.id,
            )
        )
        if conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Ya existe otro reloj con el nombre '{clean_name}'",
            )
        device.name = clean_name

    new_ip = payload.ip_address.strip() if payload.ip_address is not None else device.ip_address
    new_port = payload.port if payload.port is not None else device.port
    if new_ip != device.ip_address or new_port != device.port:
        conflict_ep = db.scalar(
            select(HikvisionDevice).where(
                HikvisionDevice.ip_address == new_ip,
                HikvisionDevice.port == new_port,
                HikvisionDevice.id != device.id,
            )
        )
        if conflict_ep:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Ya existe otro reloj registrado en {new_ip}:{new_port} ('{conflict_ep.name}')",
            )
        device.ip_address = new_ip
        device.port = new_port

    if payload.location is not None:
        device.location = payload.location.strip() or None
    if payload.protocol is not None:
        device.protocol = payload.protocol
    if payload.username is not None:
        device.username = payload.username.strip()
    if payload.password is not None and payload.password != "":
        device.encrypted_password = encrypt_secret(payload.password)
    if payload.verify_ssl is not None:
        device.verify_ssl = payload.verify_ssl
    if payload.timeout_seconds is not None:
        device.timeout_seconds = payload.timeout_seconds
    if payload.is_active is not None:
        device.is_active = payload.is_active
    if payload.notes is not None:
        device.notes = payload.notes.strip() or None

    db.commit()
    db.refresh(device)
    return device


@router.delete(
    "/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permissions("devices:delete"))],
    summary="Eliminar un reloj checador Hikvision",
)
def delete_device(db: SessionDep, device_id: int) -> None:
    device = _get_device_or_404(db, device_id)
    db.delete(device)
    db.commit()


@router.post(
    "/{device_id}/probe",
    response_model=ISAPIProbeResponse,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Probar conexión ISAPI y sincronizar metadatos del reloj",
)
def probe_saved_device(db: SessionDep, device_id: int) -> ISAPIProbeResponse:
    device = _get_device_or_404(db, device_id)
    client = _build_isapi_client(device)

    try:
        probe_data = client.probe_device()
        _apply_probe_to_device(device, probe_data)
        db.commit()
        db.refresh(device)
        return ISAPIProbeResponse(
            reachable=True,
            status=device.status,
            message=f"Dispositivo '{device.name}' en línea vía ISAPI",
            device=HikvisionDeviceRead.model_validate(device),
            **probe_data,
        )
    except ISAPIError as exc:
        device.status = "offline" if exc.status_code in (502, 504) else "error"
        device.last_error = exc.message[:500]
        db.commit()
        db.refresh(device)
        return ISAPIProbeResponse(
            reachable=False,
            status=device.status,
            message=exc.message,
            device=HikvisionDeviceRead.model_validate(device),
        )


@router.post(
    "/{device_id}/sync-time",
    response_model=ISAPIActionResponse,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Sincronizar la hora del reloj Hikvision con el servidor vía ISAPI",
)
def sync_device_time(db: SessionDep, device_id: int) -> ISAPIActionResponse:
    device = _get_device_or_404(db, device_id)
    client = _build_isapi_client(device)

    try:
        time_result = client.sync_time()
        now = datetime.now(timezone.utc)
        device.status = "online"
        device.last_seen_at = now
        device.last_sync_at = now
        device.last_error = None
        db.commit()
        db.refresh(device)
        return ISAPIActionResponse(
            success=True,
            message=f"Hora sincronizada en '{device.name}' ({time_result.get('local_time')})",
            detail=time_result,
            device=HikvisionDeviceRead.model_validate(device),
        )
    except ISAPIError as exc:
        device.last_error = exc.message[:500]
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.post(
    "/{device_id}/reboot",
    response_model=ISAPIActionResponse,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Reiniciar remotamente el reloj Hikvision vía ISAPI",
)
def reboot_device(db: SessionDep, device_id: int) -> ISAPIActionResponse:
    device = _get_device_or_404(db, device_id)
    client = _build_isapi_client(device)

    try:
        status_str = client.reboot()
        device.last_seen_at = datetime.now(timezone.utc)
        device.last_error = None
        db.commit()
        db.refresh(device)
        return ISAPIActionResponse(
            success=True,
            message=f"Orden de reinicio enviada a '{device.name}' (ISAPI: {status_str})",
            device=HikvisionDeviceRead.model_validate(device),
        )
    except ISAPIError as exc:
        device.last_error = exc.message[:500]
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.post(
    "/{device_id}/door/{door_id}",
    response_model=ISAPIActionResponse,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Controlar puerta/relevador del reloj Hikvision vía ISAPI",
)
def control_device_door(
    db: SessionDep,
    device_id: int,
    door_id: int = 1,
    cmd: Literal["open", "close", "alwaysOpen", "alwaysClose"] = Query(default="open"),
) -> ISAPIActionResponse:
    device = _get_device_or_404(db, device_id)
    client = _build_isapi_client(device)

    try:
        msg = client.control_door(door_id=door_id, cmd=cmd)
        now = datetime.now(timezone.utc)
        device.status = "online"
        device.last_seen_at = now
        device.last_error = None
        db.commit()
        db.refresh(device)
        return ISAPIActionResponse(
            success=True,
            message=msg,
            device=HikvisionDeviceRead.model_validate(device),
        )
    except ISAPIError as exc:
        device.last_error = exc.message[:500]
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.get(
    "/{device_id}/isapi/users",
    response_model=ISAPIDeviceUserSearchResponse,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Consultar usuarios almacenados en el reloj Hikvision vía ISAPI",
)
def search_device_users(
    db: SessionDep,
    device_id: int,
    search_position: int = Query(default=0, ge=0),
    max_results: int = Query(default=30, ge=1, le=100),
    employee_no: Optional[str] = Query(default=None),
) -> ISAPIDeviceUserSearchResponse:
    device = _get_device_or_404(db, device_id)
    client = _build_isapi_client(device)

    try:
        result = client.search_users(
            search_position=search_position,
            max_results=max_results,
            employee_no=employee_no,
        )
        device.status = "online"
        device.last_seen_at = datetime.now(timezone.utc)
        device.last_error = None
        db.commit()
        return ISAPIDeviceUserSearchResponse(**result)
    except ISAPIError as exc:
        device.last_error = exc.message[:500]
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.post(
    "/{device_id}/isapi/events",
    response_model=ISAPIEventSearchResponse,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Consultar eventos de asistencia (AcsEvent) del reloj Hikvision vía ISAPI",
)
def search_device_events(
    db: SessionDep,
    device_id: int,
    payload: ISAPIEventSearchRequest,
) -> ISAPIEventSearchResponse:
    device = _get_device_or_404(db, device_id)
    client = _build_isapi_client(device)

    try:
        result = client.search_events(
            start_time=payload.start_time,
            end_time=payload.end_time,
            major=payload.major,
            minor=payload.minor,
            search_position=payload.search_position,
            max_results=payload.max_results,
            employee_no=payload.employee_no,
            fetch_all_pages=payload.fetch_all_pages,
        )
        now = datetime.now(timezone.utc)
        device.status = "online"
        device.last_seen_at = now
        device.last_sync_at = now
        device.last_error = None
        db.commit()
        return ISAPIEventSearchResponse(**result)
    except ISAPIError as exc:
        device.last_error = exc.message[:500]
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.post(
    "/{device_id}/sync-offline-events",
    response_model=ISAPIOfflineSyncResponse,
    dependencies=[
        Depends(
            _require_any_log_permission(
                "devices:isapi",
                "attendance:write",
                "devices:write",
            )
        )
    ],
    summary="Recuperar e importar checadas atrasadas almacenadas en el reloj Hikvision tras corte de red",
)
def sync_device_offline_events(
    db: SessionDep,
    device_id: int,
    payload: Optional[ISAPIOfflineSyncRequest] = None,
) -> ISAPIOfflineSyncResponse:
    device = _get_device_or_404(db, device_id)
    req = payload or ISAPIOfflineSyncRequest()
    try:
        return _sync_offline_events_from_device(
            db,
            device=device,
            start_time=req.start_time,
            end_time=req.end_time,
            max_pages=req.max_pages,
        )
    except ISAPIError as exc:
        device.last_error = exc.message[:500]
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.get(
    "/{device_id}/isapi/health",
    response_model=ISAPIActionResponse,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Verificar salud ISAPI del reloj mediante GET /ISAPI/Event/notification/httpHosts",
)
def check_device_isapi_health(db: SessionDep, device_id: int) -> ISAPIActionResponse:
    device = _get_device_or_404(db, device_id)
    client = _build_isapi_client(device)

    try:
        status_msg = client.health_check()
        now = datetime.now(timezone.utc)
        device.status = "online"
        device.last_seen_at = now
        device.last_error = None
        db.commit()
        db.refresh(device)
        return ISAPIActionResponse(
            success=True,
            message=f"Health check ISAPI exitoso ({status_msg})",
            detail={"health": status_msg},
            device=HikvisionDeviceRead.model_validate(device),
        )
    except ISAPIError as exc:
        device.status = "offline" if exc.status_code in (502, 504) else "error"
        device.last_error = exc.message[:500]
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.get(
    "/{device_id}/isapi/network",
    response_model=ISAPINetworkInterfacesResponse,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Consultar interfaces de red del reloj mediante GET /ISAPI/System/Network/interfaces",
)
def get_device_network_interfaces(
    db: SessionDep,
    device_id: int,
) -> ISAPINetworkInterfacesResponse:
    device = _get_device_or_404(db, device_id)
    client = _build_isapi_client(device)

    try:
        net_data = client.get_networks_interfaces_info()
        now = datetime.now(timezone.utc)
        device.status = "online"
        device.last_seen_at = now
        device.last_error = None
        if net_data["interfaces"] and net_data["interfaces"][0].get("mac_address"):
            device.mac_address = str(net_data["interfaces"][0]["mac_address"])[:64]
        db.commit()
        return ISAPINetworkInterfacesResponse(
            device_id=device.id,
            device_name=device.name,
            interfaces=[ISAPINetworkInterfaceItem(**iface) for iface in net_data["interfaces"]],
        )
    except ISAPIError as exc:
        device.last_error = exc.message[:500]
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.post(
    "/{device_id}/isapi/users",
    response_model=ISAPIActionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Dar de alta un empleado en el reloj mediante POST /ISAPI/AccessControl/UserInfo/Record?format=json",
)
def add_device_employee(
    db: SessionDep,
    device_id: int,
    payload: ISAPIDeviceUserCreateRequest,
) -> ISAPIActionResponse:
    device = _get_device_or_404(db, device_id)
    client = _build_isapi_client(device)

    try:
        res = client.add_employee(
            employee_no=payload.employee_no,
            name=payload.name,
            user_type=payload.user_type,
            begin_time=payload.begin_time,
            end_time=payload.end_time,
        )
        device.status = "online"
        device.last_seen_at = datetime.now(timezone.utc)
        device.last_error = None
        db.commit()
        db.refresh(device)
        return ISAPIActionResponse(
            success=True,
            message=f"Empleado '{payload.name}' ({payload.employee_no}) registrado en '{device.name}'",
            detail=res,
            device=HikvisionDeviceRead.model_validate(device),
        )
    except ISAPIError as exc:
        device.last_error = exc.message[:500]
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.put(
    "/{device_id}/isapi/users/{employee_no}",
    response_model=ISAPIActionResponse,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Actualizar nombre/vigencia de un empleado en el reloj mediante PUT /ISAPI/AccessControl/UserInfo/Modify?format=json",
)
def update_device_employee(
    db: SessionDep,
    device_id: int,
    employee_no: str,
    payload: ISAPIDeviceUserUpdateRequest,
) -> ISAPIActionResponse:
    device = _get_device_or_404(db, device_id)
    client = _build_isapi_client(device)

    try:
        res = client.update_employee(
            employee_no=employee_no,
            name=payload.name,
            user_type=payload.user_type,
            begin_time=payload.begin_time,
            end_time=payload.end_time,
        )
        device.status = "online"
        device.last_seen_at = datetime.now(timezone.utc)
        device.last_error = None
        db.commit()
        db.refresh(device)
        return ISAPIActionResponse(
            success=True,
            message=f"Empleado '{employee_no}' actualizado a '{payload.name}' en '{device.name}'",
            detail=res,
            device=HikvisionDeviceRead.model_validate(device),
        )
    except ISAPIError as exc:
        device.last_error = exc.message[:500]
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.post(
    "/{device_id}/isapi/users/delete",
    response_model=ISAPIActionResponse,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Dar de baja empleados del reloj mediante PUT /ISAPI/AccessControl/UserInfo/Delete?format=json",
)
def delete_device_employees(
    db: SessionDep,
    device_id: int,
    payload: ISAPIDeviceUserDeleteRequest,
) -> ISAPIActionResponse:
    device = _get_device_or_404(db, device_id)
    client = _build_isapi_client(device)

    try:
        res = client.baja_employee(payload.employee_nos)
        device.status = "online"
        device.last_seen_at = datetime.now(timezone.utc)
        device.last_error = None
        db.commit()
        db.refresh(device)
        return ISAPIActionResponse(
            success=True,
            message=f"Baja aplicada en '{device.name}' para empleado(s): {', '.join(res['deleted_employee_nos'])}",
            detail={"deleted": ", ".join(res["deleted_employee_nos"])},
            device=HikvisionDeviceRead.model_validate(device),
        )
    except ISAPIError as exc:
        device.last_error = exc.message[:500]
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.put(
    "/{device_id}/isapi/users/{employee_no}/face",
    response_model=ISAPIActionResponse,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Registrar fotografía facial JPEG para un empleado mediante PUT /ISAPI/Intelligent/FDLib/FDSetUp?format=json",
)
def save_device_employee_face(
    db: SessionDep,
    device_id: int,
    employee_no: str,
    payload: ISAPISaveFaceRequest,
) -> ISAPIActionResponse:
    device = _get_device_or_404(db, device_id)
    client = _build_isapi_client(device)

    try:
        res = client.save_face_image(payload.base64_image, employee_no)
        device.status = "online"
        device.last_seen_at = datetime.now(timezone.utc)
        device.last_error = None
        db.commit()
        db.refresh(device)
        return ISAPIActionResponse(
            success=True,
            message=res["message"],
            detail={"employee_no": employee_no, "status": "OK"},
            device=HikvisionDeviceRead.model_validate(device),
        )
    except ISAPIError as exc:
        device.last_error = exc.message[:500]
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST
            if exc.status_code is None
            else status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.post(
    "/{device_id}/isapi/capture-face",
    response_model=ISAPICaptureFaceResponse,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Capturar rostro en vivo desde la cámara del reloj mediante POST /ISAPI/AccessControl/CaptureFaceData",
)
def capture_face_from_device(
    db: SessionDep,
    device_id: int,
) -> ISAPICaptureFaceResponse:
    device = _get_device_or_404(db, device_id)
    client = _build_isapi_client(device)

    try:
        res = client.capture_face_data()
        device.status = "online"
        device.last_seen_at = datetime.now(timezone.utc)
        device.last_error = None
        db.commit()
        return ISAPICaptureFaceResponse(**res)
    except ISAPIError as exc:
        device.last_error = exc.message[:500]
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.get(
    "/{device_id}/notification-config",
    response_model=ISAPIDeviceHttpHostRead,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Leer la configuración de HttpHostNotification actual directamente desde el reloj Hikvision",
)
def read_device_notification_config(
    db: SessionDep,
    device_id: int,
    host_id: int = Query(default=1, ge=1, le=8),
) -> ISAPIDeviceHttpHostRead:
    device = _get_device_or_404(db, device_id)
    client = _build_isapi_client(device)

    try:
        info = client.get_http_host_notification(host_id=host_id)
        now = datetime.now(timezone.utc)
        device.status = "online"
        device.last_seen_at = now
        device.last_error = None
        if info.get("host_address") and info.get("port_no") and info.get("url_path"):
            proto = str(info.get("protocol_type") or "HTTP").lower()
            path = str(info.get("url_path"))
            norm_path = path if path.startswith("/") else f"/{path}"
            device.webhook_target_url = f"{proto}://{info['host_address']}:{info['port_no']}{norm_path}"[:300]
        db.commit()
        return ISAPIDeviceHttpHostRead(
            device_id=device.id,
            device_name=device.name,
            reachable=True,
            message="Configuración HttpHostNotification leída desde el dispositivo",
            **info,
        )
    except ISAPIError as exc:
        device.last_error = exc.message[:500]
        db.commit()
        return ISAPIDeviceHttpHostRead(
            device_id=device.id,
            device_name=device.name,
            reachable=False,
            host_id=host_id,
            message=exc.message,
        )


@router.post(
    "/{device_id}/notification-config/apply",
    response_model=ISAPIActionResponse,
    dependencies=[Depends(require_permissions("devices:isapi"))],
    summary="Aplicar la configuración global de notificación de eventos ISAPI a este reloj",
)
def apply_notification_config_to_single_device(
    db: SessionDep,
    device_id: int,
) -> ISAPIActionResponse:
    device = _get_device_or_404(db, device_id)
    cfg = _get_or_create_notification_config(db)

    ok, msg = _push_notification_config_to_device(device, cfg)
    db.commit()
    db.refresh(device)

    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=msg,
        )

    return ISAPIActionResponse(
        success=True,
        message=msg,
        detail={"target_url": device.webhook_target_url},
        device=HikvisionDeviceRead.model_validate(device),
    )
