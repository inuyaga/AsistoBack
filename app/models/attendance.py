from datetime import date, datetime, timezone
import json
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.collaborator import Collaborator, CollaboratorDeviceSync, Company
    from app.models.device import HikvisionDevice
    from app.models.mobile import Branch, Geofence, MobileDevice


class Shift(Base):
    """Work shift schedule defining working days, rest days, check-in/out times, tolerances, and meal break rules."""

    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    company_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("companies.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), index=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Días laborales de la semana: 0=Lunes, 1=Martes, 2=Miércoles, 3=Jueves, 4=Viernes, 5=Sábado, 6=Domingo
    # Los días que no estén en working_days se consideran automáticamente días de descanso.
    working_days: Mapped[str] = mapped_column(String(32), default="0,1,2,3,4", nullable=False)

    # Horas de entrada y salida en formato HH:MM (24h)
    check_in_time: Mapped[str] = mapped_column(String(5), default="09:00", nullable=False)
    check_out_time: Mapped[str] = mapped_column(String(5), default="18:00", nullable=False)
    tolerance_minutes: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    early_leave_tolerance_minutes: Mapped[int] = mapped_column(Integer, default=5, nullable=False)

    # Reglas de horario de comida
    has_meal_break: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    meal_start_time: Mapped[Optional[str]] = mapped_column(String(5), default="14:00", nullable=True)
    meal_end_time: Mapped[Optional[str]] = mapped_column(String(5), default="15:00", nullable=True)
    meal_duration_minutes: Mapped[Optional[int]] = mapped_column(Integer, default=60, nullable=True)

    timezone: Mapped[str] = mapped_column(String(64), default="America/Mexico_City", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    company: Mapped[Optional["Company"]] = relationship("Company", lazy="selectin")
    collaborators: Mapped[list["Collaborator"]] = relationship(
        "Collaborator",
        back_populates="shift",
        lazy="select",
    )

    @property
    def company_name(self) -> Optional[str]:
        return self.company.name if self.company else None

    @property
    def company_code(self) -> Optional[str]:
        return self.company.code if self.company else None

    @property
    def working_days_list(self) -> list[int]:
        if not self.working_days:
            return []
        days: list[int] = []
        for part in self.working_days.split(","):
            cleaned = part.strip()
            if cleaned.isdigit():
                val = int(cleaned)
                if 0 <= val <= 6 and val not in days:
                    days.append(val)
        return sorted(days)

    @property
    def rest_days_list(self) -> list[int]:
        work_set = set(self.working_days_list)
        return [d for d in range(7) if d not in work_set]

    @property
    def rest_days(self) -> list[int]:
        return self.rest_days_list

    @property
    def collaborators_count(self) -> int:
        return len(self.collaborators)


class AttendanceLog(Base):
    """Attendance check-in/out log entry originating from a clock device (ISAPI webhook) or a mobile device."""

    __tablename__ = "attendance_logs"
    __table_args__ = (
        Index("ix_attendance_logs_collab_event_time", "collaborator_id", "event_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # Origen del registro: 'clock_device' (Dispositivo Checador ISAPI) o 'mobile_device' (Dispositivo Móvil)
    source_type: Mapped[str] = mapped_column(
        String(32),
        default="clock_device",
        index=True,
        nullable=False,
    )

    company_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("companies.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    collaborator_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("collaborators.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    device_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("hikvision_devices.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    sync_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("collaborator_device_syncs.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    # Datos de identificación recibidos y resueltos
    device_employee_no: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    collaborator_employee_no: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    collaborator_name: Mapped[Optional[str]] = mapped_column(String(220), nullable=True)
    device_name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    source_ip: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Metadatos del evento (ISAPI / Móvil)
    event_type: Mapped[str] = mapped_column(String(64), default="AccessControllerEvent", nullable=False)
    major_event: Mapped[Optional[int]] = mapped_column(Integer, default=5, nullable=True)
    minor_event: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)
    event_label: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    attendance_status: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    verify_mode: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    card_no: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    door_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    serial_no: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_valid_attendance: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Campos para checadas desde dispositivo móvil (Asisto) y auditoría de geocerca/identidad
    client_event_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        unique=True,
        index=True,
        nullable=True,
    )
    mobile_device_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    mobile_device_fk: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("mobile_devices.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    branch_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("branches.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    captured_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    latitude: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    longitude: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    accuracy_meters: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    geofence_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("geofences.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    geofence_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    geofence_assignment_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    inside_geofence: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    distance_meters: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    identity_method: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    face_reference_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    liveness_passed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    match_passed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    match_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    comments: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # Referencia de path media relativo (local / S3) si el evento adjunta foto de evidencia
    photo_media_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    raw_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
        nullable=False,
    )

    company: Mapped[Optional["Company"]] = relationship("Company", lazy="selectin")
    collaborator: Mapped[Optional["Collaborator"]] = relationship("Collaborator", lazy="selectin")
    device: Mapped[Optional["HikvisionDevice"]] = relationship("HikvisionDevice", lazy="selectin")
    device_sync: Mapped[Optional["CollaboratorDeviceSync"]] = relationship(
        "CollaboratorDeviceSync", lazy="selectin"
    )

    @property
    def source_label(self) -> str:
        if self.source_type == "mobile_device":
            return "Dispositivo Móvil"
        return "Dispositivo Checador"

    @property
    def company_name(self) -> Optional[str]:
        return self.company.name if self.company else None

    @property
    def company_code(self) -> Optional[str]:
        return self.company.code if self.company else None

    @property
    def collaborator_nss(self) -> Optional[str]:
        return self.collaborator.nss if self.collaborator else None

    @property
    def collaborator_rfc(self) -> Optional[str]:
        return self.collaborator.rfc if self.collaborator else None


class DailyAttendance(Base):
    """Persisted daily attendance record with an immutable snapshot of the shift rules active on that date."""

    __tablename__ = "daily_attendances"
    __table_args__ = (
        UniqueConstraint(
            "collaborator_id",
            "attendance_date",
            name="uq_daily_attendance_collab_date",
        ),
        Index("ix_daily_attendances_company_date", "company_id", "attendance_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    company_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("companies.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    collaborator_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("collaborators.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    attendance_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)

    # Snapshot del colaborador y sucursal en la fecha evaluada
    employee_no: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    collaborator_name: Mapped[str] = mapped_column(String(220), nullable=False)
    department: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    position: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    branch_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("branches.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    branch_name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    check_out_comments: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Snapshot INMUTABLE del turno vigente en esa fecha (no cambia si después se edita el turno o se reasigna otro)
    shift_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("shifts.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    shift_code: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    shift_name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    working_days_snapshot: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    is_working_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    day_type_label: Mapped[str] = mapped_column(String(64), default="Sin Turno Asignado", nullable=False)

    scheduled_check_in: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    scheduled_check_out: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    tolerance_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    early_leave_tolerance_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    has_meal_break: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scheduled_meal_start: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    scheduled_meal_end: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    scheduled_meal_duration_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Mexico_City", nullable=False)

    # Marcas reales de entrada, comida y salida
    actual_check_in: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_check_in_time: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    actual_meal_out: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_meal_out_time: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    actual_meal_in: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_meal_in_time: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    actual_check_out: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_check_out_time: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)

    # Estado y métricas calculadas del día
    status: Mapped[str] = mapped_column(String(32), default="no_shift", index=True, nullable=False)
    status_label: Mapped[str] = mapped_column(String(120), default="Sin Turno Asignado", nullable=False)
    has_attendance: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    late_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    early_leave_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    meal_taken_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    meal_excess_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gross_worked_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    net_worked_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    net_worked_hours: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    logs_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Control histórico y auditoría
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    @property
    def working_days_list(self) -> list[int]:
        if not self.working_days_snapshot:
            return []
        days: list[int] = []
        for part in self.working_days_snapshot.split(","):
            cleaned = part.strip()
            if cleaned.isdigit():
                val = int(cleaned)
                if 0 <= val <= 6 and val not in days:
                    days.append(val)
        return sorted(days)

    @property
    def notes(self) -> list[str]:
        if not self.notes_json:
            return []
        try:
            parsed = json.loads(self.notes_json)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except Exception:
            pass
        return []

