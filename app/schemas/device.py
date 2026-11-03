from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HikvisionDeviceBase(BaseModel):
    company_id: Optional[int] = Field(
        default=None,
        description="ID de la empresa a la que pertenece exclusivamente este reloj",
    )
    name: str = Field(..., min_length=2, max_length=120, examples=["Reloj Entrada Principal"])
    location: Optional[str] = Field(default=None, max_length=200, examples=["Planta Baja - Recepción"])
    protocol: Literal["http", "https"] = Field(default="http")
    ip_address: str = Field(..., min_length=3, max_length=120, examples=["192.168.1.64"])
    port: int = Field(default=80, ge=1, le=65535, examples=[80])
    username: str = Field(default="admin", min_length=1, max_length=64, examples=["admin"])
    verify_ssl: bool = Field(default=False)
    timeout_seconds: int = Field(default=8, ge=2, le=30)
    is_active: bool = Field(default=True)
    notes: Optional[str] = Field(default=None, max_length=500)

    @field_validator("name", "ip_address", "username")
    @classmethod
    def strip_strings(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("El campo no puede estar vacío")
        return cleaned


class HikvisionDeviceCreate(HikvisionDeviceBase):
    company_id: int = Field(..., description="ID de la empresa propietaria del reloj")
    password: str = Field(..., min_length=1, max_length=128, description="Contraseña ISAPI del reloj")
    test_on_create: bool = Field(
        default=True,
        description="Intentar consultar /ISAPI/System/deviceInfo al registrar el reloj",
    )


class HikvisionDeviceUpdate(BaseModel):
    company_id: Optional[int] = Field(default=None, description="ID de la empresa propietaria del reloj")
    name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    location: Optional[str] = Field(default=None, max_length=200)
    protocol: Optional[Literal["http", "https"]] = None
    ip_address: Optional[str] = Field(default=None, min_length=3, max_length=120)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    username: Optional[str] = Field(default=None, min_length=1, max_length=64)
    password: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Opcional: enviar solo si se desea cambiar la contraseña ISAPI",
    )
    verify_ssl: Optional[bool] = None
    timeout_seconds: Optional[int] = Field(default=None, ge=2, le=30)
    is_active: Optional[bool] = None
    notes: Optional[str] = Field(default=None, max_length=500)


class HikvisionDeviceRead(HikvisionDeviceBase):
    id: int
    company_name: Optional[str] = None
    company_code: Optional[str] = None
    base_url: str
    has_password: bool
    device_id_code: Optional[str] = None
    model: Optional[str] = None
    serial_number: Optional[str] = None
    firmware_version: Optional[str] = None
    mac_address: Optional[str] = None
    device_type: Optional[str] = None
    status: str
    last_seen_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    last_error: Optional[str] = None
    webhook_target_url: Optional[str] = None
    webhook_synced_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ISAPITestConnectionRequest(BaseModel):
    protocol: Literal["http", "https"] = "http"
    ip_address: str = Field(..., min_length=3, max_length=120)
    port: int = Field(default=80, ge=1, le=65535)
    username: str = Field(default="admin", min_length=1, max_length=64)
    password: Optional[str] = Field(default=None, max_length=128)
    device_id: Optional[int] = Field(
        default=None,
        description="Si no se envía password y se indica device_id, se usa la contraseña guardada del dispositivo",
    )
    verify_ssl: bool = False
    timeout_seconds: int = Field(default=8, ge=2, le=30)


class ISAPIProbeResponse(BaseModel):
    reachable: bool
    status: str
    message: str
    device_name: Optional[str] = None
    device_id: Optional[str] = None
    model: Optional[str] = None
    serial_number: Optional[str] = None
    mac_address: Optional[str] = None
    firmware_version: Optional[str] = None
    firmware_released_date: Optional[str] = None
    device_type: Optional[str] = None
    manufacturer: Optional[str] = None
    time_mode: Optional[str] = None
    local_time: Optional[str] = None
    time_zone: Optional[str] = None
    user_count: Optional[int] = None
    device: Optional[HikvisionDeviceRead] = None


class ISAPIActionResponse(BaseModel):
    success: bool
    message: str
    detail: Optional[dict[str, Optional[str]]] = None
    device: Optional[HikvisionDeviceRead] = None


class ISAPIDeviceUserItem(BaseModel):
    employee_no: str
    name: str
    user_type: str = "normal"
    num_of_card: int = 0
    num_of_fp: int = 0
    num_of_face: int = 0
    valid_enabled: bool = True
    begin_time: Optional[str] = None
    end_time: Optional[str] = None


class ISAPIDeviceUserSearchResponse(BaseModel):
    total_matches: int
    response_status: str
    users: list[ISAPIDeviceUserItem] = Field(default_factory=list)


class ISAPIDeviceUserCreateRequest(BaseModel):
    employee_no: str = Field(..., min_length=1, max_length=64, examples=["1001"])
    name: str = Field(..., min_length=1, max_length=120, examples=["Juan Pérez"])
    user_type: Literal["normal", "visitor", "blackList"] = Field(default="normal")
    begin_time: Optional[str] = Field(default=None, examples=["2026-01-01T00:00:00"])
    end_time: Optional[str] = Field(default=None, examples=["2036-12-31T23:59:59"])


class ISAPIDeviceUserUpdateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, examples=["Juan P."])
    user_type: Literal["normal", "visitor", "blackList"] = Field(default="normal")
    begin_time: Optional[str] = None
    end_time: Optional[str] = None


class ISAPIDeviceUserDeleteRequest(BaseModel):
    employee_nos: list[str] = Field(..., min_length=1, examples=[["1001"]])


class ISAPISaveFaceRequest(BaseModel):
    base64_image: str = Field(
        ...,
        min_length=16,
        description="Imagen JPEG en base64 (con o sin prefijo data:image/jpeg;base64,...)",
    )


class ISAPICaptureFaceResponse(BaseModel):
    base64_image: str
    face_url: Optional[str] = None
    capture_progress: int = 100


class ISAPINetworkInterfaceItem(BaseModel):
    id: str = "1"
    ip_address: Optional[str] = None
    subnet_mask: Optional[str] = None
    default_gateway: Optional[str] = None
    mac_address: Optional[str] = None
    addressing_type: Optional[str] = None
    primary_dns: Optional[str] = None
    secondary_dns: Optional[str] = None
    mtu: Optional[str] = None
    speed: Optional[str] = None
    duplex: Optional[str] = None


class ISAPINetworkInterfacesResponse(BaseModel):
    device_id: int
    device_name: str
    interfaces: list[ISAPINetworkInterfaceItem] = Field(default_factory=list)


class ISAPIEventSearchRequest(BaseModel):
    start_time: Optional[str] = Field(
        default=None,
        description="Fecha/hora inicio ISO-8601 (ej. 2026-10-30T00:00:00-06:00)",
    )
    end_time: Optional[str] = Field(
        default=None,
        description="Fecha/hora fin ISO-8601 (ej. 2026-10-30T23:59:59-06:00)",
    )
    major: int = Field(default=5, ge=0)
    minor: int = Field(default=0, ge=0)
    search_position: int = Field(default=0, ge=0)
    max_results: int = Field(default=30, ge=1, le=100)
    employee_no: Optional[str] = None
    fetch_all_pages: bool = Field(
        default=True,
        description="Paginar en bloques de 30 hasta que responseStatusStrg deje de ser MORE",
    )


class ISAPIEventItem(BaseModel):
    serial_no: Optional[int] = None
    employee_no: Optional[str] = None
    name: Optional[str] = None
    card_no: Optional[str] = None
    time: Optional[str] = None
    major: int = 5
    minor: int = 0
    event_label: str
    attendance_status: Optional[str] = None
    verify_mode: Optional[str] = None
    door_no: Optional[int] = None


class ISAPIEventSearchResponse(BaseModel):
    total_matches: int
    response_status: str
    events: list[ISAPIEventItem] = Field(default_factory=list)


class ISAPINotificationConfigUpdate(BaseModel):
    enabled: bool = Field(default=True)
    host_id: int = Field(default=2, ge=1, le=8)
    protocol_type: Literal["HTTP", "HTTPS"] = Field(default="HTTP")
    addressing_format_type: Literal["ipaddress", "hostname"] = Field(default="ipaddress")
    host_address: str = Field(..., min_length=2, max_length=150, examples=["192.168.1.100"])
    ip_address: Optional[str] = Field(default=None, max_length=150, examples=["192.168.1.100"])
    domain: Optional[str] = Field(default=None, max_length=150, examples=["attendance.local"])
    port_no: int = Field(default=8000, ge=1, le=65535, examples=[8000])
    url_path: str = Field(
        default="/api/v1/devices/webhook/isapi",
        min_length=1,
        max_length=255,
        examples=["/api/v1/devices/webhook/isapi"],
    )
    parameter_format_type: Literal["JSON", "XML"] = Field(default="JSON")
    auth_method: Literal["none", "MD5digest", "base64"] = Field(default="none")
    auth_username: Optional[str] = Field(default=None, max_length=64)
    auth_password: Optional[str] = Field(default=None, max_length=128)
    auto_apply_on_create: bool = Field(default=True)
    apply_to_all_now: bool = Field(
        default=False,
        description="Si es True, además de guardar la configuración global, la envía por ISAPI a todos los relojes activos",
    )


class ISAPINotificationConfigRead(BaseModel):
    id: int
    enabled: bool
    host_id: int
    protocol_type: str
    addressing_format_type: str
    host_address: str
    ip_address: Optional[str] = None
    domain: Optional[str] = None
    port_no: int
    url_path: str
    parameter_format_type: str
    auth_method: str
    auth_username: Optional[str] = None
    has_auth_password: bool
    auto_apply_on_create: bool
    full_target_url: str
    last_applied_at: Optional[datetime] = None
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ISAPINotificationApplyDeviceResult(BaseModel):
    device_id: int
    device_name: str
    base_url: str
    success: bool
    target_url: Optional[str] = None
    message: str


class ISAPINotificationApplyBulkResponse(BaseModel):
    total_devices: int
    succeeded: int
    failed: int
    target_url: str
    config: ISAPINotificationConfigRead
    results: list[ISAPINotificationApplyDeviceResult] = Field(default_factory=list)


class ISAPIDeviceHttpHostRead(BaseModel):
    device_id: int
    device_name: str
    reachable: bool
    host_id: int = 1
    url_path: Optional[str] = None
    protocol_type: Optional[str] = None
    parameter_format_type: Optional[str] = None
    addressing_format_type: Optional[str] = None
    host_address: Optional[str] = None
    port_no: Optional[int] = None
    auth_method: Optional[str] = None
    auth_username: Optional[str] = None
    message: str


class AttendanceLogRead(BaseModel):
    id: int
    source_type: str = Field(
        default="clock_device",
        description="Origen de la checada: 'clock_device' (Dispositivo Checador) o 'mobile_device' (Dispositivo Móvil)",
    )
    source_label: str = "Dispositivo Checador"
    company_id: Optional[int] = None
    company_name: Optional[str] = None
    company_code: Optional[str] = None
    collaborator_id: Optional[int] = None
    collaborator_employee_no: Optional[str] = None
    collaborator_name: Optional[str] = None
    collaborator_nss: Optional[str] = None
    collaborator_rfc: Optional[str] = None
    device_id: Optional[int] = None
    device_name: Optional[str] = None
    sync_id: Optional[int] = None
    device_employee_no: Optional[str] = None
    source_ip: Optional[str] = None
    event_type: str = "AccessControllerEvent"
    major_event: Optional[int] = None
    minor_event: Optional[int] = None
    event_label: Optional[str] = None
    attendance_status: Optional[str] = None
    verify_mode: Optional[str] = None
    card_no: Optional[str] = None
    door_no: Optional[int] = None
    serial_no: Optional[int] = None
    is_valid_attendance: bool = True
    mobile_device_id: Optional[str] = None
    latitude: Optional[str] = None
    longitude: Optional[str] = None
    raw_payload: Optional[str] = None
    event_time: datetime
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AttendanceLogStatsResponse(BaseModel):
    total_logs: int = 0
    clock_device_logs: int = 0
    mobile_device_logs: int = 0
    today_logs: int = 0
    matched_collaborator_logs: int = 0


class ISAPIWebhookEventResponse(BaseModel):
    status: str = "ok"
    logged: bool = False
    source_type: str = "clock_device"
    source_label: str = "Dispositivo Checador"
    message: str
    received_at: str
    source_ip: Optional[str] = None
    log: Optional[AttendanceLogRead] = None


class ISAPIOfflineSyncRequest(BaseModel):
    start_time: Optional[str] = Field(
        default=None,
        description="Fecha/hora inicio ISO-8601 (por defecto las últimas 72 horas o desde last_sync_at)",
    )
    end_time: Optional[str] = Field(
        default=None,
        description="Fecha/hora fin ISO-8601 (por defecto ahora)",
    )
    max_pages: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Máximo de bloques de 30 eventos a descargar del reloj (50 bloques = hasta 1,500 eventos)",
    )


class ISAPIOfflineSyncResponse(BaseModel):
    device_id: int
    device_name: str
    fetched_events_count: int = 0
    imported_logs_count: int = 0
    skipped_duplicates_count: int = 0
    recalculated_days_count: int = 0
    message: str
