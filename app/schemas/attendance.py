from datetime import datetime
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.device import AttendanceLogRead

TIME_24H_REGEX = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _validate_hhmm(value: str, field_label: str) -> str:
    cleaned = value.strip()
    if not TIME_24H_REGEX.match(cleaned):
        raise ValueError(f"{field_label} debe tener formato 24h 'HH:MM' (ej. '09:00' o '18:00')")
    return cleaned


class ShiftBase(BaseModel):
    company_id: Optional[int] = Field(
        default=None,
        description="ID de la empresa propietaria del turno (None = turno global disponible para todas las empresas)",
    )
    code: str = Field(..., min_length=1, max_length=40, examples=["TUR-OFI"])
    name: str = Field(..., min_length=2, max_length=150, examples=["Turno Oficina L-V (09:00 - 18:00)"])
    description: Optional[str] = Field(default=None, max_length=500)
    working_days: list[int] = Field(
        default_factory=lambda: [0, 1, 2, 3, 4],
        description="Días laborales de la semana: 0=Lunes, 1=Martes, 2=Miércoles, 3=Jueves, 4=Viernes, 5=Sábado, 6=Domingo",
    )
    check_in_time: str = Field(default="09:00", examples=["09:00"], description="Hora de entrada (HH:MM)")
    check_out_time: str = Field(default="18:00", examples=["18:00"], description="Hora de salida (HH:MM)")
    tolerance_minutes: int = Field(
        default=10,
        ge=0,
        le=240,
        description="Minutos de tolerancia en la entrada antes de marcar retardo",
    )
    early_leave_tolerance_minutes: int = Field(
        default=5,
        ge=0,
        le=240,
        description="Minutos de tolerancia antes de marcar salida anticipada",
    )
    has_meal_break: bool = Field(default=True, description="Si el turno contempla horario de comida")
    meal_start_time: Optional[str] = Field(default="14:00", examples=["14:00"])
    meal_end_time: Optional[str] = Field(default="15:00", examples=["15:00"])
    meal_duration_minutes: Optional[int] = Field(
        default=60,
        ge=0,
        le=300,
        description="Duración permitida de comida en minutos",
    )
    timezone: str = Field(default="America/Mexico_City", max_length=64)
    is_active: bool = Field(default=True)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned:
            raise ValueError("El código del turno no puede estar vacío")
        return cleaned

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("El nombre del turno no puede estar vacío")
        return cleaned

    @field_validator("working_days", mode="before")
    @classmethod
    def parse_working_days(cls, value: object) -> list[int]:
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(",") if p.strip()]
            days = [int(p) for p in parts if p.isdigit() and 0 <= int(p) <= 6]
            return sorted(set(days))
        if isinstance(value, (list, tuple, set)):
            days = []
            for item in value:
                val = int(item)
                if val < 0 or val > 6:
                    raise ValueError("Los días de la semana deben estar entre 0 (Lunes) y 6 (Domingo)")
                days.append(val)
            return sorted(set(days))
        raise ValueError("working_days debe ser una lista de enteros de 0 (Lunes) a 6 (Domingo)")

    @field_validator("check_in_time")
    @classmethod
    def validate_check_in(cls, value: str) -> str:
        return _validate_hhmm(value, "Hora de entrada")

    @field_validator("check_out_time")
    @classmethod
    def validate_check_out(cls, value: str) -> str:
        return _validate_hhmm(value, "Hora de salida")

    @field_validator("meal_start_time", "meal_end_time")
    @classmethod
    def validate_meal_time(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        return _validate_hhmm(value, "Hora de comida")


class ShiftCreate(ShiftBase):
    pass


class ShiftUpdate(BaseModel):
    company_id: Optional[int] = None
    code: Optional[str] = Field(default=None, min_length=1, max_length=40)
    name: Optional[str] = Field(default=None, min_length=2, max_length=150)
    description: Optional[str] = Field(default=None, max_length=500)
    working_days: Optional[list[int]] = None
    check_in_time: Optional[str] = None
    check_out_time: Optional[str] = None
    tolerance_minutes: Optional[int] = Field(default=None, ge=0, le=240)
    early_leave_tolerance_minutes: Optional[int] = Field(default=None, ge=0, le=240)
    has_meal_break: Optional[bool] = None
    meal_start_time: Optional[str] = None
    meal_end_time: Optional[str] = None
    meal_duration_minutes: Optional[int] = Field(default=None, ge=0, le=300)
    timezone: Optional[str] = Field(default=None, max_length=64)
    is_active: Optional[bool] = None

    @field_validator("working_days", mode="before")
    @classmethod
    def parse_working_days_opt(cls, value: object) -> Optional[list[int]]:
        if value is None:
            return None
        return ShiftBase.parse_working_days(value)

    @field_validator("check_in_time", "check_out_time", "meal_start_time", "meal_end_time")
    @classmethod
    def validate_opt_time(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        return _validate_hhmm(value, "Horario")


class ShiftRead(BaseModel):
    id: int
    company_id: Optional[int] = None
    company_name: Optional[str] = None
    company_code: Optional[str] = None
    code: str
    name: str
    description: Optional[str] = None
    working_days: list[int] = Field(default_factory=list)
    rest_days: list[int] = Field(default_factory=list)
    check_in_time: str
    check_out_time: str
    tolerance_minutes: int
    early_leave_tolerance_minutes: int
    has_meal_break: bool
    meal_start_time: Optional[str] = None
    meal_end_time: Optional[str] = None
    meal_duration_minutes: Optional[int] = None
    timezone: str
    is_active: bool
    collaborators_count: int = 0
    created_at: datetime
    updated_at: datetime

    @field_validator("working_days", mode="before")
    @classmethod
    def parse_read_working_days(cls, value: object) -> list[int]:
        return ShiftBase.parse_working_days(value)

    model_config = ConfigDict(from_attributes=True)


class ShiftAssignRequest(BaseModel):
    collaborator_ids: list[int] = Field(
        default_factory=list,
        description="Lista de IDs de colaboradores a los que se asignará este turno",
    )
    assign_all_company_id: Optional[int] = Field(
        default=None,
        description="Si se especifica, asigna el turno a todos los colaboradores activos de esa empresa",
    )


class ShiftAssignResponse(BaseModel):
    shift_id: Optional[int] = None
    shift_name: Optional[str] = None
    updated_count: int
    collaborator_ids: list[int] = Field(default_factory=list)
    message: str


AttendanceStatusCode = Literal[
    "present",
    "late",
    "absent",
    "incomplete",
    "rest_day",
    "worked_rest_day",
    "scheduled",
    "no_shift",
]


class CalculatedDailyAttendanceItem(BaseModel):
    id: Optional[int] = Field(
        default=None,
        description="ID del registro persistido en la tabla histórica daily_attendances",
    )
    is_persisted: bool = Field(
        default=False,
        description="True si el registro está guardado en daily_attendances con su turno congelado",
    )
    is_locked: bool = Field(
        default=False,
        description="True si el registro histórico fue bloqueado/cerrado",
    )
    calculated_at: Optional[datetime] = None
    date: str = Field(..., description="Fecha evaluada en formato YYYY-MM-DD")
    weekday: int = Field(..., ge=0, le=6, description="0=Lunes ... 6=Domingo")
    weekday_name: str
    collaborator_id: int
    employee_no: str
    collaborator_name: str
    department: Optional[str] = None
    position: Optional[str] = None
    company_id: int
    company_name: str
    company_code: str
    shift_id: Optional[int] = None
    shift_code: Optional[str] = None
    shift_name: Optional[str] = None
    working_days_snapshot: list[int] = Field(default_factory=list)
    current_shift_id: Optional[int] = None
    current_shift_code: Optional[str] = None
    current_shift_name: Optional[str] = None
    shift_changed_since_calculation: bool = False
    is_working_day: bool
    day_type_label: str
    scheduled_check_in: Optional[str] = None
    scheduled_check_out: Optional[str] = None
    tolerance_minutes: int = 0
    early_leave_tolerance_minutes: int = 0
    has_meal_break: bool = False
    scheduled_meal_start: Optional[str] = None
    scheduled_meal_end: Optional[str] = None
    scheduled_meal_duration_minutes: Optional[int] = None
    timezone: str = "America/Mexico_City"
    actual_check_in: Optional[datetime] = None
    actual_check_in_time: Optional[str] = None
    actual_meal_out: Optional[datetime] = None
    actual_meal_out_time: Optional[str] = None
    actual_meal_in: Optional[datetime] = None
    actual_meal_in_time: Optional[str] = None
    actual_check_out: Optional[datetime] = None
    actual_check_out_time: Optional[str] = None
    status: AttendanceStatusCode
    status_label: str
    has_attendance: bool = False
    late_minutes: int = 0
    early_leave_minutes: int = 0
    meal_taken_minutes: Optional[int] = None
    meal_excess_minutes: int = 0
    gross_worked_minutes: int = 0
    net_worked_minutes: int = 0
    net_worked_hours: float = 0.0
    scheduled_worked_minutes: int = 0
    scheduled_worked_hours: float = 0.0
    missing_worked_minutes: int = 0
    logs_count: int = 0
    logs: list[AttendanceLogRead] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class DailyAttendanceLockRequest(BaseModel):
    record_ids: list[int] = Field(
        default_factory=list,
        description="Lista de IDs de daily_attendances a bloquear o desbloquear",
    )
    is_locked: bool = Field(default=True, description="True para bloquear, False para desbloquear")


class DailyAttendanceLockResponse(BaseModel):
    updated_count: int
    is_locked: bool
    message: str


class AttendanceReportSummary(BaseModel):
    start_date: str
    end_date: str
    total_collaborators: int = 0
    collaborators_with_shift: int = 0
    collaborators_without_shift: int = 0
    total_evaluated_days: int = 0
    working_days_evaluated: int = 0
    rest_days_evaluated: int = 0
    present_count: int = 0
    late_count: int = 0
    absent_count: int = 0
    incomplete_count: int = 0
    rest_day_count: int = 0
    worked_rest_day_count: int = 0
    scheduled_count: int = 0
    no_shift_count: int = 0
    attended_days_count: int = 0
    attendance_rate: float = 0.0
    total_late_minutes: int = 0
    total_net_worked_hours: float = 0.0


class AttendanceDailyReportResponse(BaseModel):
    summary: AttendanceReportSummary
    items: list[CalculatedDailyAttendanceItem] = Field(default_factory=list)
