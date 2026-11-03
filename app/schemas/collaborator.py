from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.storage import read_media_as_data_url


class CompanyBase(BaseModel):
    code: str = Field(..., min_length=1, max_length=40, examples=["EMP-001"])
    name: str = Field(..., min_length=2, max_length=150, examples=["Corporativo MEH S.A. de C.V."])
    tax_id: Optional[str] = Field(default=None, max_length=50, examples=["CME260101AAA"])
    address: Optional[str] = Field(default=None, max_length=255)
    contact_name: Optional[str] = Field(default=None, max_length=120)
    contact_email: Optional[str] = Field(default=None, max_length=150)
    contact_phone: Optional[str] = Field(default=None, max_length=50)
    is_active: bool = Field(default=True)
    notes: Optional[str] = Field(default=None, max_length=500)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned:
            raise ValueError("El código de empresa no puede estar vacío")
        return cleaned

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("El nombre de la empresa no puede estar vacío")
        return cleaned


class CompanyCreate(CompanyBase):
    pass


class CompanyUpdate(BaseModel):
    code: Optional[str] = Field(default=None, min_length=1, max_length=40)
    name: Optional[str] = Field(default=None, min_length=2, max_length=150)
    tax_id: Optional[str] = Field(default=None, max_length=50)
    address: Optional[str] = Field(default=None, max_length=255)
    contact_name: Optional[str] = Field(default=None, max_length=120)
    contact_email: Optional[str] = Field(default=None, max_length=150)
    contact_phone: Optional[str] = Field(default=None, max_length=50)
    is_active: Optional[bool] = None
    notes: Optional[str] = Field(default=None, max_length=500)


class CompanyRead(CompanyBase):
    id: int
    devices_count: int = 0
    collaborators_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CollaboratorDeviceSyncRead(BaseModel):
    id: int
    device_id: int
    device_name: Optional[str] = None
    status: str
    face_synced: bool = False
    last_synced_at: datetime
    last_error: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class CollaboratorBase(BaseModel):
    company_id: int = Field(..., description="ID de la empresa a la que pertenece el colaborador")
    shift_id: Optional[int] = Field(
        default=None,
        description="ID del turno laboral asignado al colaborador para el cálculo de asistencias",
    )
    branch_id: Optional[int] = Field(
        default=None,
        description="ID de la sucursal asignada al colaborador",
    )
    employee_no: str = Field(..., min_length=1, max_length=64, examples=["1001"])
    first_name: str = Field(..., min_length=1, max_length=100, examples=["Juan"])
    last_name: str = Field(..., min_length=1, max_length=120, examples=["Pérez López"])
    nss: Optional[str] = Field(
        default=None,
        max_length=32,
        description="Número de Seguridad Social (NSS)",
        examples=["12345678901"],
    )
    rfc: Optional[str] = Field(
        default=None,
        max_length=20,
        description="Registro Federal de Contribuyentes (RFC)",
        examples=["PELJ900101AAA"],
    )
    email: Optional[str] = Field(default=None, max_length=150)
    phone: Optional[str] = Field(default=None, max_length=50)
    department: Optional[str] = Field(default=None, max_length=120, examples=["Operaciones"])
    position: Optional[str] = Field(default=None, max_length=120, examples=["Supervisor"])
    card_no: Optional[str] = Field(default=None, max_length=64)
    user_type: Literal["normal", "visitor", "blackList"] = Field(default="normal")
    valid_begin_time: Optional[str] = Field(default=None, max_length=32, examples=["2026-01-01T00:00:00"])
    valid_end_time: Optional[str] = Field(default=None, max_length=32, examples=["2036-12-31T23:59:59"])
    is_active: bool = Field(default=True)
    notes: Optional[str] = Field(default=None, max_length=500)

    @field_validator("employee_no", "first_name", "last_name")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("El campo no puede estar vacío")
        return cleaned


class CollaboratorCreate(CollaboratorBase):
    geofence_id: Optional[int] = Field(
        default=None,
        description="ID opcional de la geocerca principal a asignar directamente al colaborador",
    )
    face_image_base64: Optional[str] = Field(
        default=None,
        description="Fotografía facial en formato JPEG base64 (opcional)",
    )
    sync_device_ids: list[int] = Field(
        default_factory=list,
        description="Lista opcional de IDs de relojes de la misma empresa donde registrar al colaborador al crearlo",
    )


class CollaboratorUpdate(BaseModel):
    company_id: Optional[int] = None
    shift_id: Optional[int] = Field(
        default=None,
        description="ID del turno laboral a asignar (enviar 0 o null explícito para desasignar)",
    )
    branch_id: Optional[int] = Field(
        default=None,
        description="ID de la sucursal a asignar (enviar 0 o null explícito para desasignar)",
    )
    geofence_id: Optional[int] = Field(
        default=None,
        description="ID de la geocerca principal a asignar (enviar 0 o null explícito para desasignar)",
    )
    employee_no: Optional[str] = Field(default=None, min_length=1, max_length=64)
    first_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    last_name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    nss: Optional[str] = Field(default=None, max_length=32)
    rfc: Optional[str] = Field(default=None, max_length=20)
    email: Optional[str] = Field(default=None, max_length=150)
    phone: Optional[str] = Field(default=None, max_length=50)
    department: Optional[str] = Field(default=None, max_length=120)
    position: Optional[str] = Field(default=None, max_length=120)
    card_no: Optional[str] = Field(default=None, max_length=64)
    user_type: Optional[Literal["normal", "visitor", "blackList"]] = None
    valid_begin_time: Optional[str] = Field(default=None, max_length=32)
    valid_end_time: Optional[str] = Field(default=None, max_length=32)
    face_image_base64: Optional[str] = Field(
        default=None,
        description="Enviar nueva imagen JPEG en base64, o cadena vacía '' para eliminar la foto actual",
    )
    is_active: Optional[bool] = None
    notes: Optional[str] = Field(default=None, max_length=500)


class CollaboratorRead(CollaboratorBase):
    id: int
    full_name: str
    company_name: str
    company_code: str
    shift_name: Optional[str] = None
    shift_code: Optional[str] = None
    branch_name: Optional[str] = None
    primary_geofence_id: Optional[int] = None
    primary_geofence_name: Optional[str] = None
    primary_geofence_type: Optional[str] = None
    geofence_assignments_count: int = 0
    has_active_mobile_device: bool = False
    mobile_device_model: Optional[str] = None
    has_face_image: bool
    face_media_path: Optional[str] = None
    face_reference_version: Optional[str] = None
    face_sha256: Optional[str] = None
    face_image_base64: Optional[str] = None
    last_synced_at: Optional[datetime] = None
    device_syncs: list[CollaboratorDeviceSyncRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def populate_face_preview_from_storage(self) -> "CollaboratorRead":
        if self.face_media_path and not self.face_image_base64:
            self.face_image_base64 = read_media_as_data_url(self.face_media_path)
        return self

    model_config = ConfigDict(from_attributes=True)


class CollaboratorSyncDeviceRequest(BaseModel):
    device_ids: list[int] = Field(
        ...,
        min_length=1,
        description="IDs de los relojes Hikvision (de la misma empresa) donde registrar/sincronizar al colaborador",
    )
    action: Literal["upsert", "delete"] = Field(
        default="upsert",
        description="'upsert' para dar de alta/actualizar en el reloj; 'delete' para dar de baja del reloj",
    )
    include_face: bool = Field(
        default=True,
        description="Si es True y el colaborador tiene fotografía JPEG, también se sube al reloj vía FDSetUp",
    )


class CollaboratorBulkSyncDevicesRequest(BaseModel):
    company_id: int = Field(
        ...,
        description="ID de la empresa cuyos colaboradores se sincronizarán en los relojes de dicha empresa",
    )
    collaborator_ids: list[int] = Field(
        default_factory=list,
        description="IDs de los colaboradores seleccionados (ignorado si sync_all_company_collaborators=True)",
    )
    sync_all_company_collaborators: bool = Field(
        default=False,
        description="Si es True, sincroniza todos los colaboradores activos de la empresa indicada",
    )
    device_ids: list[int] = Field(
        ...,
        min_length=1,
        description="IDs de los relojes Hikvision de la empresa donde se ejecutará el registro masivo",
    )
    action: Literal["upsert", "delete"] = Field(
        default="upsert",
        description="'upsert' para alta/modificación masiva; 'delete' para baja masiva en los relojes",
    )
    include_face: bool = Field(
        default=True,
        description="Si es True, envía también el rostro JPEG (FDSetUp) de cada colaborador que tenga foto",
    )


class CollaboratorSyncItemResult(BaseModel):
    collaborator_id: int
    sync_id: Optional[int] = None
    employee_no: str
    collaborator_name: str
    device_id: int
    device_name: str
    action: str
    success: bool
    user_synced: bool = False
    face_synced: bool = False
    message: str


class CollaboratorSyncResponse(BaseModel):
    company_id: int
    company_name: str
    action: str
    total_operations: int
    total_collaborators: int
    total_devices: int
    succeeded: int
    failed: int
    results: list[CollaboratorSyncItemResult] = Field(default_factory=list)
