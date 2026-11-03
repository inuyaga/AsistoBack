from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# 1. Formato de Error Estándar del Contrato Móvil (Asisto)
# ---------------------------------------------------------------------------


class MobileErrorBody(BaseModel):
    code: str = Field(..., examples=["ACTIVATION_CODE_INVALID"])
    message: str = Field(..., examples=["El código de activación no es válido."])
    details: Optional[Any] = None
    requestId: str = Field(..., examples=["req_01JABC123"])


class MobileErrorResponse(BaseModel):
    error: MobileErrorBody


# ---------------------------------------------------------------------------
# 2. Objetos Compartidos de Empresa, Trabajador, Sucursal y Referencia Facial
# ---------------------------------------------------------------------------


class MobileCompanyInfo(BaseModel):
    id: str = Field(..., examples=["cmp_001"])
    name: str = Field(..., examples=["Kommerce S.A. de C.V."])
    timezone: str = Field(default="America/Mexico_City", examples=["America/Mexico_City"])


class MobileEmployeeInfo(BaseModel):
    id: str = Field(..., examples=["emp_00125"])
    employeeNumber: str = Field(..., examples=["00125"])
    fullName: str = Field(..., examples=["Carlos Alejandro"])
    role: Optional[str] = Field(default=None, examples=["Desarrollador"])
    area: Optional[str] = Field(default=None, examples=["Tecnología"])


class MobileBranchInfo(BaseModel):
    id: str = Field(..., examples=["br_001"])
    name: str = Field(..., examples=["Oficina Central"])
    address: Optional[str] = Field(
        default=None,
        examples=["Av. Tecnológico 1000, Villahermosa, Tabasco"],
    )


class MobileBranchBrief(BaseModel):
    id: str = Field(..., examples=["br_001"])
    name: str = Field(..., examples=["Oficina Central"])


class MobileFaceReferenceInfo(BaseModel):
    version: str = Field(..., examples=["face_1"])
    downloadUrl: str = Field(..., examples=["http://attendance.local:8000/api/v1/media/signed/..."])
    expiresAt: datetime
    sha256: str


# ---------------------------------------------------------------------------
# 3. Activación y Vinculación de Dispositivo (/activations, /auth, /me)
# ---------------------------------------------------------------------------


class ActivationDevicePayload(BaseModel):
    installationId: str = Field(..., min_length=4, max_length=120)
    platform: str = Field(default="android", max_length=32)
    operatingSystem: Optional[str] = Field(default=None, max_length=80)
    manufacturer: Optional[str] = Field(default=None, max_length=80)
    model: Optional[str] = Field(default=None, max_length=100)
    appVersion: Optional[str] = Field(default=None, max_length=40)
    appBuild: Optional[str] = Field(default=None, max_length=40)
    pushToken: Optional[str] = Field(default=None, max_length=500)

    model_config = ConfigDict(populate_by_name=True)


class ActivationValidateRequest(BaseModel):
    activationCode: Optional[str] = Field(default=None, examples=["AS-7K92-P4MX"])
    device: ActivationDevicePayload

    model_config = ConfigDict(populate_by_name=True)


class ActivationValidateResponse(BaseModel):
    validationToken: str
    expiresAt: datetime
    company: MobileCompanyInfo
    employee: MobileEmployeeInfo
    branch: MobileBranchInfo


class ActivationCompleteRequest(BaseModel):
    validationToken: str = Field(..., min_length=8)
    device: ActivationDevicePayload

    model_config = ConfigDict(populate_by_name=True)


class ActivationCompleteResponse(BaseModel):
    activationId: str
    deviceId: str
    deviceCredential: str
    activatedAt: datetime
    accessToken: str
    refreshToken: str
    expiresIn: int
    employee: MobileEmployeeInfo
    company: MobileCompanyInfo
    branch: MobileBranchInfo
    faceReference: MobileFaceReferenceInfo


class MobileRefreshRequest(BaseModel):
    refreshToken: str = Field(..., min_length=8)
    deviceId: str = Field(..., min_length=1)


class MobileRefreshResponse(BaseModel):
    accessToken: str
    refreshToken: str
    expiresIn: int


class MobileDeviceSessionRequest(BaseModel):
    activationId: str = Field(..., min_length=1)
    deviceId: str = Field(..., min_length=1)
    installationId: str = Field(..., min_length=4)
    deviceCredential: str = Field(..., min_length=8)


class MobileDeviceSessionResponse(BaseModel):
    accessToken: str
    refreshToken: str
    deviceCredential: str
    expiresIn: int


class MeActivationResponse(BaseModel):
    activationId: str
    deviceId: str
    activatedAt: datetime
    status: str = "active"
    company: MobileCompanyInfo
    employee: MobileEmployeeInfo
    branch: MobileBranchInfo


class MeDeviceUnlinkRequest(BaseModel):
    reason: Optional[str] = Field(default="DEVICE_REPLACED", max_length=200)


class MeScheduleInfo(BaseModel):
    id: str = Field(..., examples=["sch_001"])
    startTime: str = Field(..., examples=["08:00:00"])
    endTime: str = Field(..., examples=["17:00:00"])
    mealMinutes: int = Field(default=60, examples=[60])
    timezone: str = Field(default="America/Mexico_City")


class MeProfileResponse(BaseModel):
    id: str
    employeeNumber: str
    fullName: str
    role: Optional[str] = None
    area: Optional[str] = None
    branch: MobileBranchInfo
    schedule: Optional[MeScheduleInfo] = None


class PushTokenUpdateRequest(BaseModel):
    pushToken: str = Field(..., min_length=1, max_length=500)
    provider: Literal["fcm", "apns"] = Field(default="fcm")


# ---------------------------------------------------------------------------
# 4. Resolución y Validación de Geocercas (/employees/{employeeId}/geofences/resolve)
# ---------------------------------------------------------------------------


class GeofenceCoordinatePoint(BaseModel):
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)


class GeofenceResolveRequest(BaseModel):
    eventType: Literal["check_in", "check_out"] = Field(default="check_in")
    occurredAt: Optional[datetime] = None
    shiftId: Optional[str] = None
    timezone: Optional[str] = Field(default="America/Mexico_City")


class GeofenceResolvedGeometry(BaseModel):
    id: str = Field(..., examples=["geo_001"])
    version: int = Field(default=1)
    type: Literal["radial", "polygonal"] = Field(default="radial")
    center: Optional[GeofenceCoordinatePoint] = None
    radiusMeters: Optional[float] = None
    vertices: Optional[list[GeofenceCoordinatePoint]] = None


class GeofenceRulesInfo(BaseModel):
    maximumLocationAccuracyMeters: float = Field(default=50.0)


class GeofenceResolveResponse(BaseModel):
    assignmentId: str
    businessDate: str
    validFrom: datetime
    validUntil: datetime
    branch: MobileBranchInfo
    geofence: GeofenceResolvedGeometry
    rules: GeofenceRulesInfo


# ---------------------------------------------------------------------------
# 5. Jornada y Asistencia Móvil (/employees/{employeeId}/attendance/...)
# ---------------------------------------------------------------------------


MobileShiftStatus = Literal["not_started", "active", "meal_active", "completed"]
MobileMealStatus = Literal["not_started", "active", "completed"]


class CurrentShiftInfo(BaseModel):
    id: str = Field(..., examples=["shift_001"])
    businessDate: str = Field(..., examples=["2026-11-03"])
    checkInAt: datetime
    checkOutAt: Optional[datetime] = None
    workedSeconds: int = 0
    mealSeconds: int = 0
    mealStatus: MobileMealStatus = "not_started"
    mealStartedAt: Optional[datetime] = None
    mealEndedAt: Optional[datetime] = None
    branch: MobileBranchBrief


class CurrentAttendanceResponse(BaseModel):
    status: MobileShiftStatus
    shift: Optional[CurrentShiftInfo] = None


class CheckInGeofencePayload(BaseModel):
    assignmentId: str = Field(..., min_length=1)
    id: str = Field(..., min_length=1)
    version: int = Field(default=1, ge=1)
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    accuracyMeters: float = Field(..., ge=0.0)
    validatedLocally: bool = True


class CheckInIdentityPayload(BaseModel):
    method: str = Field(default="local_face_verification")
    referenceVersion: str = Field(..., min_length=1)
    livenessMethod: str = Field(default="smile")
    livenessPassed: bool
    matchPassed: bool
    matchScore: float = Field(..., ge=0.0, le=1.0)


class CheckInAppPayload(BaseModel):
    version: Optional[str] = "1.0.0"
    build: Optional[str] = "1"


class CheckInRequest(BaseModel):
    clientEventId: str = Field(..., min_length=4, max_length=64)
    capturedAt: Optional[datetime] = None
    deviceId: Optional[str] = None
    branchId: Optional[str] = None
    geofence: CheckInGeofencePayload
    identity: CheckInIdentityPayload
    app: Optional[CheckInAppPayload] = None
    photoBase64: Optional[str] = Field(
        default=None,
        description="Opcional: si se envía foto de evidencia, se guarda en media_path (local/S3) y nunca en BD",
    )


class CheckInResponse(BaseModel):
    attendanceId: str
    shiftId: str
    status: Literal["active"] = "active"
    checkInAt: datetime
    serverReceivedAt: datetime
    branch: MobileBranchBrief


class MealActionRequest(BaseModel):
    clientEventId: str = Field(..., min_length=4, max_length=64)
    capturedAt: Optional[datetime] = None
    deviceId: Optional[str] = None


class MealStartResponse(BaseModel):
    shiftId: str
    mealStatus: Literal["active"] = "active"
    mealStartedAt: datetime


class MealEndResponse(BaseModel):
    shiftId: str
    mealStatus: Literal["completed"] = "completed"
    mealStartedAt: datetime
    mealEndedAt: datetime
    mealSeconds: int


class CheckOutRequest(BaseModel):
    clientEventId: str = Field(..., min_length=4, max_length=64)
    capturedAt: Optional[datetime] = None
    deviceId: Optional[str] = None
    comments: Optional[str] = Field(default=None, max_length=500)
    geofence: CheckInGeofencePayload
    photoBase64: Optional[str] = None


class CheckOutResponse(BaseModel):
    shiftId: str
    status: Literal["completed"] = "completed"
    businessDate: str
    checkInAt: datetime
    checkOutAt: datetime
    workedSeconds: int
    mealSeconds: int
    branch: MobileBranchInfo


class AttendanceHistoryItem(BaseModel):
    shiftId: str
    businessDate: str
    status: str
    checkInAt: Optional[datetime] = None
    checkOutAt: Optional[datetime] = None
    workedSeconds: int = 0
    mealSeconds: int = 0
    branchName: str


class AttendanceHistoryPagination(BaseModel):
    page: int
    pageSize: int
    totalItems: int
    totalPages: int


class AttendanceHistoryListResponse(BaseModel):
    items: list[AttendanceHistoryItem] = Field(default_factory=list)
    pagination: AttendanceHistoryPagination


class AttendanceHistoryEventDetail(BaseModel):
    eventId: str
    type: str
    occurredAt: datetime
    sourceType: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracyMeters: Optional[float] = None
    geofenceId: Optional[str] = None
    geofenceVersion: Optional[int] = None
    insideGeofence: Optional[bool] = None
    photoMediaPath: Optional[str] = None


class AttendanceHistoryDetailResponse(BaseModel):
    shiftId: str
    businessDate: str
    status: str
    checkInAt: Optional[datetime] = None
    mealStartedAt: Optional[datetime] = None
    mealEndedAt: Optional[datetime] = None
    checkOutAt: Optional[datetime] = None
    workedSeconds: int = 0
    mealSeconds: int = 0
    lateMinutes: int = 0
    comments: Optional[str] = None
    branch: MobileBranchInfo
    events: list[AttendanceHistoryEventDetail] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 6. Esquemas Administrativos (Sucursales, Geocercas, Códigos de Activación y Dispositivos)
# ---------------------------------------------------------------------------


class BranchCreate(BaseModel):
    company_id: int
    code: str = Field(..., min_length=1, max_length=40, examples=["SUC-001"])
    name: str = Field(..., min_length=2, max_length=150, examples=["Oficina Central"])
    address: Optional[str] = Field(default=None, max_length=300)
    timezone: str = Field(default="America/Mexico_City", max_length=64)
    is_active: bool = True


class BranchUpdate(BaseModel):
    code: Optional[str] = Field(default=None, min_length=1, max_length=40)
    name: Optional[str] = Field(default=None, min_length=2, max_length=150)
    address: Optional[str] = Field(default=None, max_length=300)
    timezone: Optional[str] = Field(default=None, max_length=64)
    is_active: Optional[bool] = None


class BranchRead(BaseModel):
    id: int
    public_id: str
    company_id: int
    company_name: str
    code: str
    name: str
    address: Optional[str] = None
    timezone: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GeofenceCreate(BaseModel):
    company_id: int
    branch_id: Optional[int] = None
    code: str = Field(..., min_length=1, max_length=50, examples=["GEO-CENTRAL"])
    name: str = Field(..., min_length=2, max_length=150, examples=["Geocerca Oficina Central"])
    geofence_type: Literal["radial", "polygonal"] = "radial"
    center_latitude: Optional[float] = Field(default=None, ge=-90.0, le=90.0)
    center_longitude: Optional[float] = Field(default=None, ge=-180.0, le=180.0)
    radius_meters: Optional[float] = Field(default=150.0, gt=0.0, le=50000.0)
    vertices: Optional[list[GeofenceCoordinatePoint]] = None
    max_accuracy_meters: float = Field(default=50.0, gt=0.0, le=1000.0)
    is_active: bool = True


class GeofenceUpdate(BaseModel):
    branch_id: Optional[int] = None
    code: Optional[str] = Field(default=None, min_length=1, max_length=50)
    name: Optional[str] = Field(default=None, min_length=2, max_length=150)
    geofence_type: Optional[Literal["radial", "polygonal"]] = None
    center_latitude: Optional[float] = Field(default=None, ge=-90.0, le=90.0)
    center_longitude: Optional[float] = Field(default=None, ge=-180.0, le=180.0)
    radius_meters: Optional[float] = Field(default=None, gt=0.0, le=50000.0)
    vertices: Optional[list[GeofenceCoordinatePoint]] = None
    max_accuracy_meters: Optional[float] = Field(default=None, gt=0.0, le=1000.0)
    is_active: Optional[bool] = None


class GeofenceRead(BaseModel):
    id: int
    public_id: str
    company_id: int
    branch_id: Optional[int] = None
    branch_name: Optional[str] = None
    code: str
    name: str
    version: int
    geofence_type: str
    center_latitude: Optional[float] = None
    center_longitude: Optional[float] = None
    radius_meters: Optional[float] = None
    vertices: list[GeofenceCoordinatePoint] = Field(default_factory=list)
    max_accuracy_meters: float
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GeofenceAssignmentCreate(BaseModel):
    company_id: int
    collaborator_id: Optional[int] = None
    shift_id: Optional[int] = None
    branch_id: Optional[int] = None
    geofence_id: int
    event_type: Literal["all", "check_in", "check_out"] = "all"
    valid_from_date: Optional[date] = None
    valid_until_date: Optional[date] = None
    priority: int = Field(default=10, ge=1, le=1000)
    is_active: bool = True


class GeofenceAssignmentRead(BaseModel):
    id: int
    company_id: int
    company_name: str = ""
    collaborator_id: Optional[int] = None
    collaborator_name: Optional[str] = None
    employee_no: Optional[str] = None
    shift_id: Optional[int] = None
    shift_name: Optional[str] = None
    branch_id: Optional[int] = None
    branch_name: Optional[str] = None
    geofence_id: int
    geofence_name: Optional[str] = None
    geofence_code: Optional[str] = None
    geofence_type: Optional[str] = None
    geofence_version: int = 1
    event_type: str
    valid_from_date: Optional[date] = None
    valid_until_date: Optional[date] = None
    priority: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BulkGeofenceAssignRequest(BaseModel):
    company_id: int
    geofence_id: int
    collaborator_ids: list[int] = Field(default_factory=list)
    assign_all_company_collaborators: bool = False
    shift_id: Optional[int] = None
    branch_id: Optional[int] = None
    event_type: Literal["all", "check_in", "check_out"] = "all"
    valid_from_date: Optional[date] = None
    valid_until_date: Optional[date] = None
    priority: int = Field(default=10, ge=1, le=1000)
    replace_existing: bool = True


class BulkGeofenceAssignResponse(BaseModel):
    company_id: int
    geofence_id: int
    geofence_name: str
    total_assigned: int
    updated_existing: int
    created_new: int


class CollaboratorGeofenceTestRequest(BaseModel):
    event_type: Literal["check_in", "check_out"] = "check_in"
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)
    accuracy_meters: float = Field(default=15.0, ge=0.0)


class CollaboratorGeofenceTestResponse(BaseModel):
    collaborator_id: int
    collaborator_name: str
    employee_no: str
    assignment_id: str
    geofence_id: str
    geofence_name: str
    geofence_type: str
    geofence_version: int
    max_accuracy_meters: float
    inside: bool
    accuracy_valid: bool
    distance_meters: Optional[float] = None
    radius_meters: Optional[float] = None
    reason_code: Optional[str] = None
    reason_message: Optional[str] = None


class ActivationCodeGenerateRequest(BaseModel):
    collaborator_id: Optional[int] = None
    branch_id: Optional[int] = None
    expires_in_hours: int = Field(default=72, ge=1, le=720)


class ActivationCodeGenerateResponse(BaseModel):
    id: int
    activationId: str
    collaboratorId: int
    collaboratorName: str
    employeeNo: str
    companyId: int
    companyName: str
    branchId: Optional[int] = None
    branchName: Optional[str] = None
    activationCode: str = Field(
        ...,
        description="Código en texto plano devuelto una sola vez para mostrar o generar QR",
    )
    codeHint: str
    expiresAt: datetime


class MobileDeviceRead(BaseModel):
    id: int
    public_id: str
    activation_public_id: str
    company_id: int
    company_name: str = ""
    collaborator_id: int
    collaborator_name: str = ""
    employee_no: str = ""
    branch_id: Optional[int] = None
    branch_name: Optional[str] = None
    installation_id: str
    platform: str
    operating_system: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    app_version: Optional[str] = None
    app_build: Optional[str] = None
    status: str
    revoke_reason: Optional[str] = None
    activated_at: datetime
    revoked_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class MobileDeviceRevokeRequest(BaseModel):
    reason: str = Field(default="ADMIN_REVOKED", max_length=200)
