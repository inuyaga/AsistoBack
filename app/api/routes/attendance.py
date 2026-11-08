from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
import json
import threading
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, SessionDep
from app.core.database import SessionLocal
from app.models.attendance import AttendanceLog, DailyAttendance, Shift
from app.models.collaborator import Collaborator, Company
from app.models.user import User
from app.schemas.attendance import (
    AttendanceDailyReportResponse,
    AttendanceReportSummary,
    AttendanceStatusCode,
    CalculatedDailyAttendanceItem,
    DailyAttendanceLockRequest,
    DailyAttendanceLockResponse,
    ShiftAssignRequest,
    ShiftAssignResponse,
    ShiftCreate,
    ShiftRead,
    ShiftUpdate,
)
from app.schemas.device import AttendanceLogRead

router = APIRouter()

WEEKDAY_NAMES_ES: list[str] = [
    "Lunes",
    "Martes",
    "Miércoles",
    "Jueves",
    "Viernes",
    "Sábado",
    "Domingo",
]

FIXED_TZ_OFFSETS: dict[str, int] = {
    "America/Mexico_City": -6,
    "America/Monterrey": -6,
    "America/Merida": -6,
    "America/Chihuahua": -6,
    "America/Cancun": -5,
    "America/Mazatlan": -7,
    "America/Hermosillo": -7,
    "America/Tijuana": -8,
    "UTC": 0,
}


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


def _resolve_tz(tz_name: Optional[str]) -> tzinfo:
    cleaned = (tz_name or "America/Mexico_City").strip()
    try:
        return ZoneInfo(cleaned)
    except Exception:
        offset_hours = FIXED_TZ_OFFSETS.get(cleaned, -6)
        return timezone(timedelta(hours=offset_hours))


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_hhmm_to_time(hhmm: Optional[str], fallback: time = time(9, 0)) -> time:
    if not hhmm:
        return fallback
    parts = hhmm.strip().split(":")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        h = max(0, min(23, int(parts[0])))
        m = max(0, min(59, int(parts[1])))
        return time(h, m)
    return fallback


def _get_shift_or_404(db: Session, shift_id: int) -> Shift:
    shift = db.get(Shift, shift_id)
    if not shift:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Turno laboral no encontrado",
        )
    return shift


def _deduplicate_rapid_logs(
    logs: list[AttendanceLog],
    min_gap_seconds: int = 60,
) -> list[AttendanceLog]:
    """Filter out accidental duplicate scans within `min_gap_seconds` while preserving explicit mobile transitions."""
    if len(logs) <= 1:
        return list(logs)
    filtered: list[AttendanceLog] = [logs[0]]
    for current in logs[1:]:
        prev = filtered[-1]
        if (
            current.source_type == "mobile_device"
            and current.attendance_status
            and current.attendance_status != prev.attendance_status
        ):
            filtered.append(current)
            continue
        gap = abs((_ensure_utc(current.event_time) - _ensure_utc(prev.event_time)).total_seconds())
        if gap < min_gap_seconds:
            continue
        filtered.append(current)
    return filtered


@dataclass(frozen=True)
class ShiftRuleSnapshot:
    """Immutable snapshot of shift rules used to evaluate a specific day's attendance."""

    shift_id: Optional[int]
    code: Optional[str]
    name: Optional[str]
    working_days_str: str
    working_days_list: list[int]
    check_in_time: str
    check_out_time: str
    tolerance_minutes: int
    early_leave_tolerance_minutes: int
    has_meal_break: bool
    meal_start_time: Optional[str]
    meal_end_time: Optional[str]
    meal_duration_minutes: Optional[int]
    timezone: str

    @classmethod
    def from_shift(cls, shift: Optional[Shift]) -> Optional["ShiftRuleSnapshot"]:
        if shift is None:
            return None
        return cls(
            shift_id=shift.id,
            code=shift.code,
            name=shift.name,
            working_days_str=shift.working_days or "",
            working_days_list=list(shift.working_days_list),
            check_in_time=shift.check_in_time,
            check_out_time=shift.check_out_time,
            tolerance_minutes=shift.tolerance_minutes,
            early_leave_tolerance_minutes=shift.early_leave_tolerance_minutes,
            has_meal_break=shift.has_meal_break,
            meal_start_time=shift.meal_start_time if shift.has_meal_break else None,
            meal_end_time=shift.meal_end_time if shift.has_meal_break else None,
            meal_duration_minutes=shift.meal_duration_minutes if shift.has_meal_break else None,
            timezone=shift.timezone or "America/Mexico_City",
        )

    @classmethod
    def from_daily_record(cls, record: DailyAttendance) -> Optional["ShiftRuleSnapshot"]:
        if record.status == "no_shift" and record.shift_id is None and not record.scheduled_check_in:
            return None
        return cls(
            shift_id=record.shift_id,
            code=record.shift_code,
            name=record.shift_name or "Turno Histórico",
            working_days_str=record.working_days_snapshot or "",
            working_days_list=list(record.working_days_list),
            check_in_time=record.scheduled_check_in or "09:00",
            check_out_time=record.scheduled_check_out or "18:00",
            tolerance_minutes=record.tolerance_minutes,
            early_leave_tolerance_minutes=record.early_leave_tolerance_minutes,
            has_meal_break=record.has_meal_break,
            meal_start_time=record.scheduled_meal_start if record.has_meal_break else None,
            meal_end_time=record.scheduled_meal_end if record.has_meal_break else None,
            meal_duration_minutes=record.scheduled_meal_duration_minutes if record.has_meal_break else None,
            timezone=record.timezone or "America/Mexico_City",
        )


SHIFT_CALCULATED_STATUS_LABELS: dict[str, str] = {
    "checkIn": "Entrada (Calculada por Turno)",
    "breakOut": "Salida a Comida (Calculada por Turno)",
    "breakIn": "Regreso de Comida (Calculada por Turno)",
    "checkOut": "Salida (Calculada por Turno)",
}


def _classify_logs_by_shift(
    effective_logs: list[AttendanceLog],
    eval_date: date,
    eff_shift: Optional[ShiftRuleSnapshot],
    tz: tzinfo,
) -> tuple[
    Optional[AttendanceLog],
    Optional[AttendanceLog],
    Optional[AttendanceLog],
    Optional[AttendanceLog],
]:
    """Determine Entrada, Salida a Comida, Regreso de Comida, and Salida from explicit mobile logs and/or Shift schedule."""
    if not effective_logs:
        return None, None, None, None

    check_in_log: Optional[AttendanceLog] = None
    meal_out_log: Optional[AttendanceLog] = None
    meal_in_log: Optional[AttendanceLog] = None
    check_out_log: Optional[AttendanceLog] = None

    # 1. Asignar primero cualquier marca explícita originada desde la app móvil ('mobile_device')
    unassigned_logs: list[AttendanceLog] = []
    for lg in effective_logs:
        if lg.source_type == "mobile_device" and lg.attendance_status in (
            "checkIn",
            "breakOut",
            "breakIn",
            "checkOut",
        ):
            if lg.attendance_status == "checkIn" and check_in_log is None:
                check_in_log = lg
            elif lg.attendance_status == "breakOut" and meal_out_log is None:
                meal_out_log = lg
            elif lg.attendance_status == "breakIn" and meal_in_log is None:
                meal_in_log = lg
            elif lg.attendance_status == "checkOut":
                check_out_log = lg
            else:
                unassigned_logs.append(lg)
        else:
            unassigned_logs.append(lg)

    # 2. Si aún faltan marcas y existen checadas de reloj biométrico ('clock_device'), clasificarlas por horario de turno
    if check_in_log is None and unassigned_logs:
        check_in_log = unassigned_logs[0]
        rest_logs = unassigned_logs[1:]
    else:
        rest_logs = unassigned_logs

    if check_in_log is None:
        check_in_log = effective_logs[0]
        rest_logs = [lg for lg in effective_logs[1:] if lg not in (meal_out_log, meal_in_log, check_out_log)]

    if rest_logs:
        if eff_shift is None or not eff_shift.has_meal_break:
            if check_out_log is None:
                candidate_last = rest_logs[-1]
                if (
                    _ensure_utc(candidate_last.event_time) - _ensure_utc(check_in_log.event_time)
                ).total_seconds() >= 60:
                    check_out_log = candidate_last
        else:
            sched_in_time = _parse_hhmm_to_time(eff_shift.check_in_time, time(9, 0))
            sched_out_time = _parse_hhmm_to_time(eff_shift.check_out_time, time(18, 0))
            sched_in_dt = datetime.combine(eval_date, sched_in_time, tzinfo=tz)
            sched_out_dt = datetime.combine(eval_date, sched_out_time, tzinfo=tz)
            if sched_out_dt <= sched_in_dt:
                sched_out_dt += timedelta(days=1)

            if eff_shift.meal_start_time:
                meal_start_t = _parse_hhmm_to_time(eff_shift.meal_start_time, time(14, 0))
                sched_meal_start_dt = datetime.combine(eval_date, meal_start_t, tzinfo=tz)
                if sched_meal_start_dt < sched_in_dt:
                    sched_meal_start_dt += timedelta(days=1)
            else:
                sched_meal_start_dt = sched_in_dt + (sched_out_dt - sched_in_dt) / 2

            meal_dur = eff_shift.meal_duration_minutes or 60
            if eff_shift.meal_end_time:
                meal_end_t = _parse_hhmm_to_time(eff_shift.meal_end_time, time(15, 0))
                sched_meal_end_dt = datetime.combine(eval_date, meal_end_t, tzinfo=tz)
                if sched_meal_end_dt <= sched_meal_start_dt:
                    sched_meal_end_dt = sched_meal_start_dt + timedelta(minutes=meal_dur)
            else:
                sched_meal_end_dt = sched_meal_start_dt + timedelta(minutes=meal_dur)

            # Punto de corte entre ventana de comida y ventana de salida final del turno
            meal_to_checkout_cutoff_dt = (
                sched_meal_end_dt + (sched_out_dt - sched_meal_end_dt) / 2
            )

            if len(rest_logs) == 1:
                second_local_dt = _ensure_utc(rest_logs[0].event_time).astimezone(tz)
                if second_local_dt < meal_to_checkout_cutoff_dt and meal_out_log is None:
                    meal_out_log = rest_logs[0]
                elif check_out_log is None:
                    check_out_log = rest_logs[0]
            elif len(rest_logs) == 2:
                if meal_out_log is None:
                    meal_out_log = rest_logs[0]
                third_local_dt = _ensure_utc(rest_logs[1].event_time).astimezone(tz)
                if third_local_dt < meal_to_checkout_cutoff_dt and meal_in_log is None:
                    meal_in_log = rest_logs[1]
                elif check_out_log is None:
                    check_out_log = rest_logs[1]
            else:
                if meal_out_log is None:
                    meal_out_log = rest_logs[0]
                if meal_in_log is None:
                    meal_in_log = rest_logs[1]
                if check_out_log is None:
                    check_out_log = rest_logs[-1]

    # Actualizar el tipo de checada (attendance_status) calculado por el turno en cada log
    check_in_log.attendance_status = "checkIn"
    if meal_out_log is not None:
        meal_out_log.attendance_status = "breakOut"
    if meal_in_log is not None:
        meal_in_log.attendance_status = "breakIn"
    if check_out_log is not None:
        check_out_log.attendance_status = "checkOut"

    return check_in_log, meal_out_log, meal_in_log, check_out_log


def infer_webhook_log_status_by_shift(
    db: Session,
    collab: Optional[Collaborator],
    event_time: datetime,
) -> tuple[str, str]:
    """Calculate the `attendance_status` ('checkIn', 'breakOut', 'breakIn', 'checkOut') and label for an incoming webhook event based on the collaborator's shift."""
    if collab is None:
        return "checkIn", "Checada registrada en reloj"

    default_tz = _resolve_tz(collab.shift.timezone if collab.shift else "America/Mexico_City")
    approx_date = _ensure_utc(event_time).astimezone(default_tz).date()

    existing_daily = db.scalar(
        select(DailyAttendance).where(
            DailyAttendance.collaborator_id == collab.id,
            DailyAttendance.attendance_date == approx_date,
        )
    )
    snapshot = ShiftRuleSnapshot.from_daily_record(existing_daily) if existing_daily else None
    if snapshot is None:
        snapshot = ShiftRuleSnapshot.from_shift(collab.shift)

    tz = _resolve_tz(snapshot.timezone if snapshot else "America/Mexico_City")
    eval_date = _ensure_utc(event_time).astimezone(tz).date()

    local_start = datetime.combine(eval_date, time(0, 0, 0), tzinfo=tz).astimezone(timezone.utc)
    local_end = datetime.combine(eval_date, time(23, 59, 59, 999999), tzinfo=tz).astimezone(timezone.utc)

    prior_logs = list(
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

    probe_log = AttendanceLog(
        collaborator_id=collab.id,
        event_time=_ensure_utc(event_time),
        is_valid_attendance=True,
    )
    combined = sorted(
        [*prior_logs, probe_log],
        key=lambda lg: _ensure_utc(lg.event_time),
    )
    effective = _deduplicate_rapid_logs(combined)
    if probe_log not in effective and effective:
        # Si fue una doble checada en < 60s, hereda el tipo de la checada inmediata anterior
        inferred_status = effective[-1].attendance_status or "checkIn"
    else:
        _classify_logs_by_shift(effective, eval_date, snapshot, tz)
        inferred_status = probe_log.attendance_status or "checkIn"

    label = SHIFT_CALCULATED_STATUS_LABELS.get(inferred_status, "Entrada (Calculada por Turno)")
    return inferred_status, label


def _has_shift_changed(
    snapshot: Optional[ShiftRuleSnapshot],
    current_shift: Optional[Shift],
) -> bool:
    if snapshot is None:
        return False
    if current_shift is None:
        return True
    if snapshot.shift_id is not None and current_shift.id != snapshot.shift_id:
        return True
    if current_shift.check_in_time != snapshot.check_in_time:
        return True
    if current_shift.check_out_time != snapshot.check_out_time:
        return True
    if current_shift.working_days_list != snapshot.working_days_list:
        return True
    if current_shift.tolerance_minutes != snapshot.tolerance_minutes:
        return True
    if current_shift.has_meal_break != snapshot.has_meal_break:
        return True
    return False


def _calculate_shift_required_minutes(
    eff_shift: Optional[ShiftRuleSnapshot],
    eval_date: date,
    tz: tzinfo,
) -> tuple[int, int, int, int]:
    """Return `(scheduled_gross_minutes, scheduled_meal_minutes, scheduled_net_minutes, min_required_minutes)` for a shift."""
    if eff_shift is None:
        return 0, 0, 0, 0

    sched_in_time = _parse_hhmm_to_time(eff_shift.check_in_time, time(9, 0))
    sched_out_time = _parse_hhmm_to_time(eff_shift.check_out_time, time(18, 0))
    sched_in_dt = datetime.combine(eval_date, sched_in_time, tzinfo=tz)
    sched_out_dt = datetime.combine(eval_date, sched_out_time, tzinfo=tz)
    if sched_out_dt <= sched_in_dt:
        sched_out_dt += timedelta(days=1)

    scheduled_gross_minutes = max(0, int((sched_out_dt - sched_in_dt).total_seconds() // 60))
    scheduled_meal_minutes = 0
    if eff_shift.has_meal_break:
        if eff_shift.meal_duration_minutes is not None and eff_shift.meal_duration_minutes > 0:
            scheduled_meal_minutes = eff_shift.meal_duration_minutes
        elif eff_shift.meal_start_time and eff_shift.meal_end_time:
            m_start_t = _parse_hhmm_to_time(eff_shift.meal_start_time, time(14, 0))
            m_end_t = _parse_hhmm_to_time(eff_shift.meal_end_time, time(15, 0))
            m_start_dt = datetime.combine(eval_date, m_start_t, tzinfo=tz)
            m_end_dt = datetime.combine(eval_date, m_end_t, tzinfo=tz)
            if m_end_dt <= m_start_dt:
                m_end_dt += timedelta(days=1)
            scheduled_meal_minutes = max(0, int((m_end_dt - m_start_dt).total_seconds() // 60))

    scheduled_net_minutes = max(0, scheduled_gross_minutes - scheduled_meal_minutes)
    early_tol = max(0, eff_shift.early_leave_tolerance_minutes or 0)
    min_required_minutes = (
        max(1, scheduled_net_minutes - early_tol) if scheduled_net_minutes > 0 else 0
    )
    return (
        scheduled_gross_minutes,
        scheduled_meal_minutes,
        scheduled_net_minutes,
        min_required_minutes,
    )


def _calculate_collaborator_day_attendance(
    collab: Collaborator,
    eval_date: date,
    day_logs: list[AttendanceLog],
    today_local: date,
    tz: tzinfo,
    shift_snapshot: Optional[ShiftRuleSnapshot] = None,
    use_explicit_snapshot: bool = False,
) -> CalculatedDailyAttendanceItem:
    weekday = eval_date.weekday()
    weekday_name = WEEKDAY_NAMES_ES[weekday]
    current_shift = collab.shift
    eff_shift = shift_snapshot if use_explicit_snapshot else ShiftRuleSnapshot.from_shift(current_shift)
    shift_changed = _has_shift_changed(eff_shift, current_shift)

    effective_logs = _deduplicate_rapid_logs(day_logs)

    # Caso 1: Colaborador sin turno asignado en esa fecha
    if eff_shift is None:
        check_in_log_ns, _, _, check_out_log_ns = _classify_logs_by_shift(
            effective_logs, eval_date, None, tz
        )
        log_reads = [AttendanceLogRead.model_validate(lg) for lg in day_logs]
        actual_in = check_in_log_ns.event_time if check_in_log_ns else None
        actual_out = check_out_log_ns.event_time if check_out_log_ns else None
        local_in_str = _ensure_utc(actual_in).astimezone(tz).strftime("%H:%M:%S") if actual_in else None
        local_out_str = _ensure_utc(actual_out).astimezone(tz).strftime("%H:%M:%S") if actual_out else None
        gross_min = (
            max(0, int((actual_out - actual_in).total_seconds() // 60))
            if (actual_in and actual_out and actual_out > actual_in)
            else 0
        )
        notes = [
            "El colaborador no tiene un turno laboral asignado. Asigna un turno para evaluar días laborales, descansos, retardos, comida y cumplimiento de la jornada completa."
        ]
        if day_logs:
            notes.append(f"Se registraron {len(day_logs)} checada(s) en esta fecha (no cuenta como asistencia al no tener turno asignado).")

        return CalculatedDailyAttendanceItem(
            date=eval_date.isoformat(),
            weekday=weekday,
            weekday_name=weekday_name,
            collaborator_id=collab.id,
            employee_no=collab.employee_no,
            collaborator_name=collab.full_name,
            department=collab.department,
            position=collab.position,
            company_id=collab.company_id,
            company_name=collab.company_name,
            company_code=collab.company_code,
            shift_id=None,
            shift_code=None,
            shift_name=None,
            working_days_snapshot=[],
            current_shift_id=current_shift.id if current_shift else None,
            current_shift_code=current_shift.code if current_shift else None,
            current_shift_name=current_shift.name if current_shift else None,
            shift_changed_since_calculation=False,
            is_working_day=False,
            day_type_label="Sin Turno Asignado",
            scheduled_check_in=None,
            scheduled_check_out=None,
            tolerance_minutes=0,
            early_leave_tolerance_minutes=0,
            has_meal_break=False,
            scheduled_meal_start=None,
            scheduled_meal_end=None,
            scheduled_meal_duration_minutes=None,
            timezone="America/Mexico_City",
            actual_check_in=actual_in,
            actual_check_in_time=local_in_str,
            actual_meal_out=None,
            actual_meal_out_time=None,
            actual_meal_in=None,
            actual_meal_in_time=None,
            actual_check_out=actual_out,
            actual_check_out_time=local_out_str,
            status="no_shift",
            status_label="Sin Turno Asignado",
            has_attendance=False,
            late_minutes=0,
            early_leave_minutes=0,
            meal_taken_minutes=None,
            meal_excess_minutes=0,
            gross_worked_minutes=gross_min,
            net_worked_minutes=gross_min,
            net_worked_hours=round(gross_min / 60.0, 2),
            scheduled_worked_minutes=0,
            scheduled_worked_hours=0.0,
            missing_worked_minutes=0,
            logs_count=len(day_logs),
            logs=log_reads,
            notes=notes,
        )

    # Caso 2: Colaborador con reglas de turno (vigente o snapshot histórico congelado)
    working_days = eff_shift.working_days_list
    is_working_day = weekday in working_days
    day_type_label = "Día Laboral" if is_working_day else "Día de Descanso"
    (
        _scheduled_gross_min,
        scheduled_meal_min,
        scheduled_net_min,
        min_required_min,
    ) = _calculate_shift_required_minutes(eff_shift, eval_date, tz)
    scheduled_worked_hours = round(scheduled_net_min / 60.0, 2)

    # Sin registros de checada en el día
    if not effective_logs:
        if not is_working_day:
            status_code: AttendanceStatusCode = "rest_day"
            status_label = "Día de Descanso"
            notes = [f"Día de descanso conforme al turno '{eff_shift.name}'."]
        elif eval_date > today_local:
            status_code = "scheduled"
            status_label = "Programado"
            notes = [
                f"Día laboral futuro programado ({eff_shift.check_in_time} a {eff_shift.check_out_time} • Jornada requerida: {scheduled_worked_hours} hrs)."
            ]
        else:
            status_code = "absent"
            status_label = "Falta / Sin Asistencia"
            notes = [
                f"No se encontraron checadas en día laboral ({eff_shift.check_in_time} a {eff_shift.check_out_time} • Jornada requerida: {scheduled_worked_hours} hrs)."
            ]

        if shift_changed:
            notes.append(
                f"Histórico congelado con el turno '{eff_shift.name}' ({eff_shift.check_in_time} - {eff_shift.check_out_time})."
            )

        return CalculatedDailyAttendanceItem(
            date=eval_date.isoformat(),
            weekday=weekday,
            weekday_name=weekday_name,
            collaborator_id=collab.id,
            employee_no=collab.employee_no,
            collaborator_name=collab.full_name,
            department=collab.department,
            position=collab.position,
            company_id=collab.company_id,
            company_name=collab.company_name,
            company_code=collab.company_code,
            shift_id=eff_shift.shift_id,
            shift_code=eff_shift.code,
            shift_name=eff_shift.name,
            working_days_snapshot=working_days,
            current_shift_id=current_shift.id if current_shift else None,
            current_shift_code=current_shift.code if current_shift else None,
            current_shift_name=current_shift.name if current_shift else None,
            shift_changed_since_calculation=shift_changed,
            is_working_day=is_working_day,
            day_type_label=day_type_label,
            scheduled_check_in=eff_shift.check_in_time,
            scheduled_check_out=eff_shift.check_out_time,
            tolerance_minutes=eff_shift.tolerance_minutes,
            early_leave_tolerance_minutes=eff_shift.early_leave_tolerance_minutes,
            has_meal_break=eff_shift.has_meal_break,
            scheduled_meal_start=eff_shift.meal_start_time if eff_shift.has_meal_break else None,
            scheduled_meal_end=eff_shift.meal_end_time if eff_shift.has_meal_break else None,
            scheduled_meal_duration_minutes=eff_shift.meal_duration_minutes if eff_shift.has_meal_break else None,
            timezone=eff_shift.timezone,
            actual_check_in=None,
            actual_check_in_time=None,
            actual_meal_out=None,
            actual_meal_out_time=None,
            actual_meal_in=None,
            actual_meal_in_time=None,
            actual_check_out=None,
            actual_check_out_time=None,
            status=status_code,
            status_label=status_label,
            has_attendance=False,
            late_minutes=0,
            early_leave_minutes=0,
            meal_taken_minutes=None,
            meal_excess_minutes=0,
            gross_worked_minutes=0,
            net_worked_minutes=0,
            net_worked_hours=0.0,
            scheduled_worked_minutes=scheduled_net_min,
            scheduled_worked_hours=scheduled_worked_hours,
            missing_worked_minutes=scheduled_net_min if (is_working_day and eval_date <= today_local) else 0,
            logs_count=0,
            logs=[],
            notes=notes,
        )

    # Con registros de checada: clasificar Entrada, Comida (Salida/Regreso) y Salida de acuerdo al Turno
    check_in_log, meal_out_log, meal_in_log, check_out_log = _classify_logs_by_shift(
        effective_logs, eval_date, eff_shift, tz
    )
    assert check_in_log is not None
    log_reads = [AttendanceLogRead.model_validate(lg) for lg in day_logs]

    # Convertir tiempos a zona horaria local del turno
    local_in_dt = _ensure_utc(check_in_log.event_time).astimezone(tz)
    local_meal_out_dt = _ensure_utc(meal_out_log.event_time).astimezone(tz) if meal_out_log else None
    local_meal_in_dt = _ensure_utc(meal_in_log.event_time).astimezone(tz) if meal_in_log else None
    local_out_dt = _ensure_utc(check_out_log.event_time).astimezone(tz) if check_out_log else None

    sched_in_time = _parse_hhmm_to_time(eff_shift.check_in_time, time(9, 0))
    sched_out_time = _parse_hhmm_to_time(eff_shift.check_out_time, time(18, 0))
    sched_in_dt = datetime.combine(eval_date, sched_in_time, tzinfo=tz)
    sched_out_dt = datetime.combine(eval_date, sched_out_time, tzinfo=tz)
    if sched_out_dt <= sched_in_dt:
        sched_out_dt += timedelta(days=1)

    notes: list[str] = []
    late_minutes = 0
    is_late = False

    if is_working_day:
        grace_dt = sched_in_dt + timedelta(minutes=eff_shift.tolerance_minutes)
        if local_in_dt > grace_dt:
            is_late = True
            late_minutes = max(1, int((local_in_dt - sched_in_dt).total_seconds() // 60))
            notes.append(
                f"Retardo de {late_minutes} min (Entrada: {local_in_dt.strftime('%H:%M')}, programada: {eff_shift.check_in_time} + {eff_shift.tolerance_minutes} min tolerancia)."
            )
        else:
            notes.append(
                f"Entrada puntual a las {local_in_dt.strftime('%H:%M')} (Programada: {eff_shift.check_in_time})."
            )
    else:
        notes.append(
            f"Checada de entrada registrada en día de descanso a las {local_in_dt.strftime('%H:%M')}."
        )

    meal_taken_minutes: Optional[int] = None
    meal_excess_minutes = 0
    if meal_out_log and meal_in_log and meal_in_log.event_time > meal_out_log.event_time:
        meal_taken_minutes = max(
            0, int((meal_in_log.event_time - meal_out_log.event_time).total_seconds() // 60)
        )
        allowed_meal = eff_shift.meal_duration_minutes or 0
        if eff_shift.has_meal_break and allowed_meal > 0 and meal_taken_minutes > allowed_meal:
            meal_excess_minutes = meal_taken_minutes - allowed_meal
            notes.append(
                f"Comida: {meal_taken_minutes} min ({local_meal_out_dt.strftime('%H:%M') if local_meal_out_dt else ''} - {local_meal_in_dt.strftime('%H:%M') if local_meal_in_dt else ''}), excedió por {meal_excess_minutes} min el límite de {allowed_meal} min."
            )
        else:
            notes.append(
                f"Comida registrada: {meal_taken_minutes} min ({local_meal_out_dt.strftime('%H:%M') if local_meal_out_dt else ''} - {local_meal_in_dt.strftime('%H:%M') if local_meal_in_dt else ''})."
            )
    elif meal_out_log and not meal_in_log:
        notes.append(
            f"Salida a comida registrada ({local_meal_out_dt.strftime('%H:%M') if local_meal_out_dt else ''}) sin registro de regreso."
        )

    gross_worked_minutes = 0
    net_worked_minutes = 0
    early_leave_minutes = 0

    if check_out_log and check_out_log.event_time > check_in_log.event_time and local_out_dt:
        gross_worked_minutes = max(
            0, int((check_out_log.event_time - check_in_log.event_time).total_seconds() // 60)
        )
        if meal_taken_minutes is not None:
            net_worked_minutes = max(0, gross_worked_minutes - meal_taken_minutes)
        elif eff_shift.has_meal_break and scheduled_meal_min > 0 and gross_worked_minutes > (scheduled_net_min // 2):
            # Si el turno contempla comida y la estancia supera media jornada sin checadas intermedias de comida,
            # descontar el tiempo de comida programado del turno para obtener las horas netas reales trabajadas.
            net_worked_minutes = max(0, gross_worked_minutes - scheduled_meal_min)
            notes.append(
                f"Se descontaron {scheduled_meal_min} min de horario de comida del turno (no se registraron checadas intermedias de comida)."
            )
        else:
            net_worked_minutes = gross_worked_minutes

        if is_working_day:
            early_limit_dt = sched_out_dt - timedelta(minutes=eff_shift.early_leave_tolerance_minutes)
            if local_out_dt < early_limit_dt:
                early_leave_minutes = max(
                    1, int((sched_out_dt - local_out_dt).total_seconds() // 60)
                )
                notes.append(
                    f"Salida anticipada por {early_leave_minutes} min (Salida: {local_out_dt.strftime('%H:%M')}, programada: {eff_shift.check_out_time})."
                )
            else:
                notes.append(f"Salida registrada a las {local_out_dt.strftime('%H:%M')}.")
        else:
            notes.append(f"Salida en día de descanso a las {local_out_dt.strftime('%H:%M')}.")
    else:
        notes.append("Sin checada de salida registrada.")

    net_worked_hours = round(net_worked_minutes / 60.0, 2)

    # Validar cumplimiento de la jornada completa de acuerdo al número de horas trabajadas del turno asignado
    is_full_shift_completed = bool(
        check_out_log is not None and net_worked_minutes >= min_required_min
    )
    missing_worked_minutes = (
        max(0, scheduled_net_min - net_worked_minutes) if not is_full_shift_completed else 0
    )

    if check_out_log is None:
        final_status: AttendanceStatusCode = "incomplete"
        final_label = (
            f"Jornada Incompleta (Sin Salida • Retardo +{late_minutes}m)"
            if is_late
            else "Jornada Incompleta (Sin Salida)"
        )
        has_valid_attendance = False
        notes.append(
            f"No se considera asistencia: falta checada de salida y debe cumplir la jornada completa de {scheduled_worked_hours} hrs netas ({scheduled_net_min} min) según su turno '{eff_shift.name}'."
        )
    elif not is_full_shift_completed:
        final_status = "incomplete"
        final_label = f"Jornada Incompleta ({net_worked_hours}h / {scheduled_worked_hours}h)"
        has_valid_attendance = False
        missing_hrs = round(missing_worked_minutes / 60.0, 2)
        notes.append(
            f"No se considera asistencia: trabajó {net_worked_hours} hrs ({net_worked_minutes} min) de las {scheduled_worked_hours} hrs ({scheduled_net_min} min) requeridas por el turno '{eff_shift.name}' (faltaron {missing_worked_minutes} min / {missing_hrs} hrs)."
        )
    else:
        has_valid_attendance = True
        notes.append(
            f"Jornada laboral completa cumplida: {net_worked_hours} hrs trabajadas ({net_worked_minutes} min) de {scheduled_worked_hours} hrs ({scheduled_net_min} min) requeridas por el turno '{eff_shift.name}'."
        )
        if not is_working_day:
            final_status = "worked_rest_day"
            final_label = "Descanso Laborado"
        elif is_late:
            final_status = "late"
            final_label = f"Retardo (+{late_minutes} min)"
        else:
            final_status = "present"
            final_label = "Asistencia Puntual"

    if shift_changed:
        notes.append(
            f"Histórico congelado con el turno '{eff_shift.name}' ({eff_shift.check_in_time} - {eff_shift.check_out_time})."
        )

    return CalculatedDailyAttendanceItem(
        date=eval_date.isoformat(),
        weekday=weekday,
        weekday_name=weekday_name,
        collaborator_id=collab.id,
        employee_no=collab.employee_no,
        collaborator_name=collab.full_name,
        department=collab.department,
        position=collab.position,
        company_id=collab.company_id,
        company_name=collab.company_name,
        company_code=collab.company_code,
        shift_id=eff_shift.shift_id,
        shift_code=eff_shift.code,
        shift_name=eff_shift.name,
        working_days_snapshot=working_days,
        current_shift_id=current_shift.id if current_shift else None,
        current_shift_code=current_shift.code if current_shift else None,
        current_shift_name=current_shift.name if current_shift else None,
        shift_changed_since_calculation=shift_changed,
        is_working_day=is_working_day,
        day_type_label=day_type_label,
        scheduled_check_in=eff_shift.check_in_time,
        scheduled_check_out=eff_shift.check_out_time,
        tolerance_minutes=eff_shift.tolerance_minutes,
        early_leave_tolerance_minutes=eff_shift.early_leave_tolerance_minutes,
        has_meal_break=eff_shift.has_meal_break,
        scheduled_meal_start=eff_shift.meal_start_time if eff_shift.has_meal_break else None,
        scheduled_meal_end=eff_shift.meal_end_time if eff_shift.has_meal_break else None,
        scheduled_meal_duration_minutes=eff_shift.meal_duration_minutes if eff_shift.has_meal_break else None,
        timezone=eff_shift.timezone,
        actual_check_in=check_in_log.event_time,
        actual_check_in_time=local_in_dt.strftime("%H:%M:%S"),
        actual_meal_out=meal_out_log.event_time if meal_out_log else None,
        actual_meal_out_time=local_meal_out_dt.strftime("%H:%M:%S") if local_meal_out_dt else None,
        actual_meal_in=meal_in_log.event_time if meal_in_log else None,
        actual_meal_in_time=local_meal_in_dt.strftime("%H:%M:%S") if local_meal_in_dt else None,
        actual_check_out=check_out_log.event_time if check_out_log else None,
        actual_check_out_time=local_out_dt.strftime("%H:%M:%S") if local_out_dt else None,
        status=final_status,
        status_label=final_label,
        has_attendance=has_valid_attendance,
        late_minutes=late_minutes,
        early_leave_minutes=early_leave_minutes,
        meal_taken_minutes=meal_taken_minutes,
        meal_excess_minutes=meal_excess_minutes,
        gross_worked_minutes=gross_worked_minutes,
        net_worked_minutes=net_worked_minutes,
        net_worked_hours=net_worked_hours,
        scheduled_worked_minutes=scheduled_net_min,
        scheduled_worked_hours=scheduled_worked_hours,
        missing_worked_minutes=missing_worked_minutes,
        logs_count=len(day_logs),
        logs=log_reads,
        notes=notes,
    )


def _apply_calculated_item_to_record(
    record: DailyAttendance,
    item: CalculatedDailyAttendanceItem,
    eval_date: date,
    snapshot: Optional[ShiftRuleSnapshot],
) -> None:
    now_utc = datetime.now(timezone.utc)
    record.company_id = item.company_id
    record.collaborator_id = item.collaborator_id
    record.attendance_date = eval_date
    record.weekday = item.weekday
    record.employee_no = item.employee_no
    record.collaborator_name = item.collaborator_name
    record.department = item.department
    record.position = item.position

    record.shift_id = snapshot.shift_id if snapshot else None
    record.shift_code = snapshot.code if snapshot else None
    record.shift_name = snapshot.name if snapshot else None
    record.working_days_snapshot = snapshot.working_days_str if snapshot else None
    record.is_working_day = item.is_working_day
    record.day_type_label = item.day_type_label

    record.scheduled_check_in = item.scheduled_check_in
    record.scheduled_check_out = item.scheduled_check_out
    record.tolerance_minutes = item.tolerance_minutes
    record.early_leave_tolerance_minutes = item.early_leave_tolerance_minutes
    record.has_meal_break = item.has_meal_break
    record.scheduled_meal_start = item.scheduled_meal_start
    record.scheduled_meal_end = item.scheduled_meal_end
    record.scheduled_meal_duration_minutes = item.scheduled_meal_duration_minutes
    record.timezone = item.timezone

    record.actual_check_in = item.actual_check_in
    record.actual_check_in_time = item.actual_check_in_time
    record.actual_meal_out = item.actual_meal_out
    record.actual_meal_out_time = item.actual_meal_out_time
    record.actual_meal_in = item.actual_meal_in
    record.actual_meal_in_time = item.actual_meal_in_time
    record.actual_check_out = item.actual_check_out
    record.actual_check_out_time = item.actual_check_out_time

    record.status = item.status
    record.status_label = item.status_label
    record.has_attendance = item.has_attendance
    record.late_minutes = item.late_minutes
    record.early_leave_minutes = item.early_leave_minutes
    record.meal_taken_minutes = item.meal_taken_minutes
    record.meal_excess_minutes = item.meal_excess_minutes
    record.gross_worked_minutes = item.gross_worked_minutes
    record.net_worked_minutes = item.net_worked_minutes
    record.net_worked_hours = item.net_worked_hours
    record.logs_count = item.logs_count
    record.notes_json = json.dumps(item.notes, ensure_ascii=False)
    record.calculated_at = now_utc


def _build_item_from_persisted_record(
    record: DailyAttendance,
    collab: Collaborator,
    day_logs: list[AttendanceLog],
) -> CalculatedDailyAttendanceItem:
    current_shift = collab.shift
    snapshot = ShiftRuleSnapshot.from_daily_record(record)
    rec_tz = _resolve_tz(snapshot.timezone if snapshot else record.timezone)
    if day_logs:
        _classify_logs_by_shift(
            _deduplicate_rapid_logs(day_logs),
            record.attendance_date,
            snapshot,
            rec_tz,
        )
    shift_changed = _has_shift_changed(snapshot, current_shift)
    notes = list(record.notes)
    if shift_changed and not any("Histórico congelado" in n for n in notes) and snapshot is not None:
        notes.append(
            f"Histórico congelado con el turno '{snapshot.name}' ({snapshot.check_in_time} - {snapshot.check_out_time})."
        )

    _, _, scheduled_net_min, _ = _calculate_shift_required_minutes(
        snapshot, record.attendance_date, rec_tz
    )
    scheduled_worked_hours = round(scheduled_net_min / 60.0, 2)
    missing_worked_min = (
        max(0, scheduled_net_min - record.net_worked_minutes)
        if (not record.has_attendance and scheduled_net_min > 0 and record.status in ("incomplete", "absent"))
        else 0
    )

    weekday_idx = max(0, min(6, record.weekday))
    status_val: AttendanceStatusCode = (
        record.status  # type: ignore[assignment]
        if record.status
        in (
            "present",
            "late",
            "absent",
            "incomplete",
            "rest_day",
            "worked_rest_day",
            "scheduled",
            "no_shift",
        )
        else "no_shift"
    )

    return CalculatedDailyAttendanceItem(
        id=record.id,
        is_persisted=True,
        is_locked=record.is_locked,
        calculated_at=record.calculated_at,
        date=record.attendance_date.isoformat(),
        weekday=weekday_idx,
        weekday_name=WEEKDAY_NAMES_ES[weekday_idx],
        collaborator_id=record.collaborator_id,
        employee_no=record.employee_no or collab.employee_no,
        collaborator_name=record.collaborator_name or collab.full_name,
        department=record.department if record.department is not None else collab.department,
        position=record.position if record.position is not None else collab.position,
        company_id=record.company_id or collab.company_id,
        company_name=collab.company_name,
        company_code=collab.company_code,
        shift_id=record.shift_id,
        shift_code=record.shift_code,
        shift_name=record.shift_name,
        working_days_snapshot=record.working_days_list,
        current_shift_id=current_shift.id if current_shift else None,
        current_shift_code=current_shift.code if current_shift else None,
        current_shift_name=current_shift.name if current_shift else None,
        shift_changed_since_calculation=shift_changed,
        is_working_day=record.is_working_day,
        day_type_label=record.day_type_label,
        scheduled_check_in=record.scheduled_check_in,
        scheduled_check_out=record.scheduled_check_out,
        tolerance_minutes=record.tolerance_minutes,
        early_leave_tolerance_minutes=record.early_leave_tolerance_minutes,
        has_meal_break=record.has_meal_break,
        scheduled_meal_start=record.scheduled_meal_start,
        scheduled_meal_end=record.scheduled_meal_end,
        scheduled_meal_duration_minutes=record.scheduled_meal_duration_minutes,
        timezone=record.timezone,
        actual_check_in=record.actual_check_in,
        actual_check_in_time=record.actual_check_in_time,
        actual_meal_out=record.actual_meal_out,
        actual_meal_out_time=record.actual_meal_out_time,
        actual_meal_in=record.actual_meal_in,
        actual_meal_in_time=record.actual_meal_in_time,
        actual_check_out=record.actual_check_out,
        actual_check_out_time=record.actual_check_out_time,
        status=status_val,
        status_label=record.status_label,
        has_attendance=record.has_attendance,
        late_minutes=record.late_minutes,
        early_leave_minutes=record.early_leave_minutes,
        meal_taken_minutes=record.meal_taken_minutes,
        meal_excess_minutes=record.meal_excess_minutes,
        gross_worked_minutes=record.gross_worked_minutes,
        net_worked_minutes=record.net_worked_minutes,
        net_worked_hours=record.net_worked_hours,
        scheduled_worked_minutes=scheduled_net_min,
        scheduled_worked_hours=scheduled_worked_hours,
        missing_worked_minutes=missing_worked_min,
        logs_count=len(day_logs) if day_logs else record.logs_count,
        logs=[AttendanceLogRead.model_validate(lg) for lg in day_logs],
        notes=notes,
    )


def sync_collaborator_daily_attendance(
    db: Session,
    collaborator_id: int,
    reference_dt: datetime,
) -> Optional[DailyAttendance]:
    """Calculate and persist/update a collaborator's DailyAttendance for a single day while preserving frozen historical shift rules."""
    collab = db.get(Collaborator, collaborator_id)
    if not collab:
        return None

    existing = db.scalar(
        select(DailyAttendance).where(
            DailyAttendance.collaborator_id == collab.id,
            DailyAttendance.attendance_date == _ensure_utc(reference_dt).astimezone(
                _resolve_tz(collab.shift.timezone if collab.shift else "America/Mexico_City")
            ).date(),
        )
    )

    if existing and existing.is_locked:
        return existing

    # Determinar el snapshot de reglas a usar:
    # - Si el día YA tenía un turno congelado en daily_attendances, usar SIEMPRE ese snapshot histórico.
    # - Si es nuevo o estaba como 'no_shift', congelar el turno actual del colaborador.
    frozen_snapshot = ShiftRuleSnapshot.from_daily_record(existing) if existing else None
    if frozen_snapshot is None:
        frozen_snapshot = ShiftRuleSnapshot.from_shift(collab.shift)

    tz = _resolve_tz(frozen_snapshot.timezone if frozen_snapshot else "America/Mexico_City")
    eval_date = _ensure_utc(reference_dt).astimezone(tz).date()
    today_local = datetime.now(tz).date()

    # Si al convertir con la zona horaria del snapshot cambió la fecha, verificar existing en esa fecha
    if existing is None or existing.attendance_date != eval_date:
        existing = db.scalar(
            select(DailyAttendance).where(
                DailyAttendance.collaborator_id == collab.id,
                DailyAttendance.attendance_date == eval_date,
            )
        )
        if existing and existing.is_locked:
            return existing
        if existing and ShiftRuleSnapshot.from_daily_record(existing) is not None:
            frozen_snapshot = ShiftRuleSnapshot.from_daily_record(existing)

    local_start = datetime.combine(eval_date, time(0, 0, 0), tzinfo=tz).astimezone(timezone.utc)
    local_end = datetime.combine(eval_date, time(23, 59, 59, 999999), tzinfo=tz).astimezone(timezone.utc)

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

    item = _calculate_collaborator_day_attendance(
        collab=collab,
        eval_date=eval_date,
        day_logs=day_logs,
        today_local=today_local,
        tz=tz,
        shift_snapshot=frozen_snapshot,
        use_explicit_snapshot=True,
    )

    if existing is None:
        existing = DailyAttendance(
            company_id=collab.company_id,
            collaborator_id=collab.id,
            attendance_date=eval_date,
            weekday=eval_date.weekday(),
            employee_no=collab.employee_no,
            collaborator_name=collab.full_name,
        )
        db.add(existing)

    _apply_calculated_item_to_record(existing, item, eval_date, frozen_snapshot)
    if existing.branch_id is None and collab.branch_id is not None:
        existing.branch_id = collab.branch_id
        existing.branch_name = collab.branch_name
    for lg in day_logs:
        if lg.branch_id is not None:
            existing.branch_id = lg.branch_id
        if lg.comments:
            existing.check_out_comments = lg.comments
    db.commit()
    db.refresh(existing)
    return existing


# Control de concurrencia y agrupación (coalescing) ante ráfagas de checadas atrasadas del mismo colaborador y día
_BURST_LOCK = threading.Lock()
_IN_FLIGHT_COLLAB_DATES: set[tuple[int, str]] = set()
_DIRTY_COLLAB_DATES: set[tuple[int, datetime]] = set()


def process_collaborator_day_attendance_task(
    collaborator_id: int,
    event_time: datetime,
) -> None:
    """Background worker function that updates `daily_attendances` in its own short-lived DB session with burst coalescing."""
    local_day_key = (
        collaborator_id,
        _ensure_utc(event_time).astimezone(_resolve_tz("America/Mexico_City")).date().isoformat(),
    )

    with _BURST_LOCK:
        if local_day_key in _IN_FLIGHT_COLLAB_DATES:
            # Ya hay un cálculo en curso para este (colaborador, fecha): marcarlo como pendiente para consolidar en 1 sola pasada
            _DIRTY_COLLAB_DATES.add((collaborator_id, event_time))
            return
        _IN_FLIGHT_COLLAB_DATES.add(local_day_key)

    try:
        current_dt = event_time
        while True:
            db = SessionLocal()
            try:
                sync_collaborator_daily_attendance(db, collaborator_id, current_dt)
            except Exception:
                db.rollback()
            finally:
                db.close()

            with _BURST_LOCK:
                pending = [
                    item for item in _DIRTY_COLLAB_DATES if item[0] == collaborator_id
                ]
                if not pending:
                    _IN_FLIGHT_COLLAB_DATES.discard(local_day_key)
                    break
                for item in pending:
                    _DIRTY_COLLAB_DATES.discard(item)
                current_dt = pending[-1][1]
    finally:
        with _BURST_LOCK:
            _IN_FLIGHT_COLLAB_DATES.discard(local_day_key)


# ---------------------------------------------------------------------------
# Endpoints de Catálogo y Asignación de Turnos Laborales (/shifts)
# ---------------------------------------------------------------------------


@router.get(
    "/shifts",
    response_model=list[ShiftRead],
    dependencies=[
        Depends(
            require_any_permission(
                "shifts:read",
                "attendance:read",
                "collaborators:read",
            )
        )
    ],
    summary="Listar turnos laborales configurados",
)
def list_shifts(
    db: SessionDep,
    company_id: Optional[int] = Query(
        default=None,
        description="Si se indica, devuelve los turnos de la empresa más los turnos globales",
    ),
    search: Optional[str] = Query(default=None, description="Filtrar por código o nombre del turno"),
    is_active: Optional[bool] = Query(default=None),
) -> list[Shift]:
    stmt = select(Shift)
    if company_id is not None:
        stmt = stmt.where(or_(Shift.company_id == company_id, Shift.company_id.is_(None)))
    if search and search.strip():
        term = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                Shift.code.ilike(term),
                Shift.name.ilike(term),
                Shift.description.ilike(term),
            )
        )
    if is_active is not None:
        stmt = stmt.where(Shift.is_active == is_active)

    stmt = stmt.order_by(Shift.id.asc())
    return list(db.scalars(stmt).all())


@router.post(
    "/shifts",
    response_model=ShiftRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_any_permission("shifts:write", "attendance:write", "collaborators:write"))],
    summary="Crear un nuevo turno laboral con sus reglas de asistencia y comida",
)
def create_shift(
    db: SessionDep,
    payload: ShiftCreate,
) -> Shift:
    if payload.company_id is not None:
        company = db.get(Company, payload.company_id)
        if not company:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="La empresa seleccionada para el turno no existe",
            )

    existing_code = db.scalar(select(Shift).where(Shift.code == payload.code))
    if existing_code:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe un turno con el código '{payload.code}'",
        )

    working_days_str = ",".join(str(d) for d in sorted(set(payload.working_days)))

    shift = Shift(
        company_id=payload.company_id,
        code=payload.code,
        name=payload.name,
        description=payload.description.strip() if payload.description else None,
        working_days=working_days_str,
        check_in_time=payload.check_in_time,
        check_out_time=payload.check_out_time,
        tolerance_minutes=payload.tolerance_minutes,
        early_leave_tolerance_minutes=payload.early_leave_tolerance_minutes,
        has_meal_break=payload.has_meal_break,
        meal_start_time=payload.meal_start_time if payload.has_meal_break else None,
        meal_end_time=payload.meal_end_time if payload.has_meal_break else None,
        meal_duration_minutes=payload.meal_duration_minutes if payload.has_meal_break else None,
        timezone=payload.timezone.strip() or "America/Mexico_City",
        is_active=payload.is_active,
    )
    db.add(shift)
    db.commit()
    db.refresh(shift)
    return shift


@router.get(
    "/shifts/{shift_id}",
    response_model=ShiftRead,
    dependencies=[Depends(require_any_permission("shifts:read", "attendance:read", "collaborators:read"))],
    summary="Obtener detalle de un turno laboral",
)
def get_shift(db: SessionDep, shift_id: int) -> Shift:
    return _get_shift_or_404(db, shift_id)


@router.patch(
    "/shifts/{shift_id}",
    response_model=ShiftRead,
    dependencies=[Depends(require_any_permission("shifts:write", "attendance:write", "collaborators:write"))],
    summary="Actualizar las reglas de un turno laboral",
)
def update_shift(
    db: SessionDep,
    shift_id: int,
    payload: ShiftUpdate,
) -> Shift:
    shift = _get_shift_or_404(db, shift_id)

    if "company_id" in payload.model_fields_set:
        if payload.company_id is not None and payload.company_id > 0:
            company = db.get(Company, payload.company_id)
            if not company:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="La empresa seleccionada no existe",
                )
            shift.company_id = company.id
        else:
            shift.company_id = None

    if payload.code is not None and payload.code.strip().upper() != shift.code:
        clean_code = payload.code.strip().upper()
        conflict = db.scalar(select(Shift).where(Shift.code == clean_code, Shift.id != shift.id))
        if conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Ya existe otro turno con el código '{clean_code}'",
            )
        shift.code = clean_code

    if payload.name is not None:
        shift.name = payload.name.strip()
    if payload.description is not None:
        shift.description = payload.description.strip() or None
    if payload.working_days is not None:
        shift.working_days = ",".join(str(d) for d in sorted(set(payload.working_days)))
    if payload.check_in_time is not None:
        shift.check_in_time = payload.check_in_time
    if payload.check_out_time is not None:
        shift.check_out_time = payload.check_out_time
    if payload.tolerance_minutes is not None:
        shift.tolerance_minutes = payload.tolerance_minutes
    if payload.early_leave_tolerance_minutes is not None:
        shift.early_leave_tolerance_minutes = payload.early_leave_tolerance_minutes
    if payload.has_meal_break is not None:
        shift.has_meal_break = payload.has_meal_break
    if payload.meal_start_time is not None:
        shift.meal_start_time = payload.meal_start_time
    if payload.meal_end_time is not None:
        shift.meal_end_time = payload.meal_end_time
    if payload.meal_duration_minutes is not None:
        shift.meal_duration_minutes = payload.meal_duration_minutes
    if payload.timezone is not None and payload.timezone.strip():
        shift.timezone = payload.timezone.strip()
    if payload.is_active is not None:
        shift.is_active = payload.is_active

    db.commit()
    db.refresh(shift)
    return shift


@router.delete(
    "/shifts/{shift_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_any_permission("shifts:delete", "attendance:write", "collaborators:delete"))],
    summary="Eliminar un turno laboral (desasigna automáticamente a los colaboradores que lo tenían)",
)
def delete_shift(db: SessionDep, shift_id: int) -> None:
    shift = _get_shift_or_404(db, shift_id)
    for collab in shift.collaborators:
        collab.shift_id = None
    db.delete(shift)
    db.commit()


@router.post(
    "/shifts/{shift_id}/assign",
    response_model=ShiftAssignResponse,
    dependencies=[Depends(require_any_permission("shifts:write", "collaborators:write", "attendance:write"))],
    summary="Asignar un turno laboral a uno o varios colaboradores (o a toda una empresa)",
)
def assign_shift_to_collaborators(
    db: SessionDep,
    shift_id: int,
    payload: ShiftAssignRequest,
) -> ShiftAssignResponse:
    shift: Optional[Shift] = None
    if shift_id > 0:
        shift = _get_shift_or_404(db, shift_id)

    if payload.assign_all_company_id is not None:
        company = db.get(Company, payload.assign_all_company_id)
        if not company:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Empresa no encontrada",
            )
        if shift and shift.company_id is not None and shift.company_id != company.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"El turno '{shift.name}' es exclusivo de otra empresa.",
            )
        collabs = list(
            db.scalars(
                select(Collaborator).where(
                    Collaborator.company_id == company.id,
                    Collaborator.is_active.is_(True),
                )
            ).all()
        )
    else:
        if not payload.collaborator_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Selecciona al menos un colaborador o indica una empresa.",
            )
        collabs = list(
            db.scalars(
                select(Collaborator).where(Collaborator.id.in_(payload.collaborator_ids))
            ).all()
        )

    if not collabs:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No se encontraron colaboradores para asignar el turno.",
        )

    if shift and shift.company_id is not None:
        invalid = [c for c in collabs if c.company_id != shift.company_id]
        if invalid:
            names = ", ".join(c.full_name for c in invalid[:5])
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"El turno '{shift.name}' pertenece a otra empresa y no aplica para: {names}",
            )

    updated_ids: list[int] = []
    for c in collabs:
        c.shift_id = shift.id if shift else None
        updated_ids.append(c.id)

    db.commit()

    action_msg = (
        f"Turno '{shift.name}' ({shift.code}) asignado a {len(updated_ids)} colaborador(es)."
        if shift
        else f"Turno desasignado a {len(updated_ids)} colaborador(es)."
    )
    return ShiftAssignResponse(
        shift_id=shift.id if shift else None,
        shift_name=shift.name if shift else None,
        updated_count=len(updated_ids),
        collaborator_ids=updated_ids,
        message=action_msg,
    )


# ---------------------------------------------------------------------------
# Endpoints de Cálculo de Asistencias basadas en Logs y Turnos (/attendance)
# ---------------------------------------------------------------------------


@router.get(
    "/attendance/daily-report",
    response_model=AttendanceDailyReportResponse,
    dependencies=[
        Depends(
            require_any_permission(
                "attendance:read",
                "shifts:read",
                "collaborators:read",
                "devices:read",
            )
        )
    ],
    summary="Calcular asistencias de colaboradores en una fecha o rango de fechas de acuerdo a su turno y los logs",
)
def get_calculated_attendance_report(
    db: SessionDep,
    start_date: Optional[date] = Query(
        default=None,
        description="Fecha inicial YYYY-MM-DD (por defecto hoy en horario local)",
    ),
    end_date: Optional[date] = Query(
        default=None,
        description="Fecha final YYYY-MM-DD (por defecto igual a start_date)",
    ),
    company_id: Optional[int] = Query(default=None, description="Filtrar por empresa"),
    collaborator_id: Optional[int] = Query(default=None, description="Filtrar por un colaborador específico"),
    shift_id: Optional[int] = Query(
        default=None,
        description="Filtrar por turno asignado (usar 0 para colaboradores sin turno)",
    ),
    status_filter: Optional[AttendanceStatusCode] = Query(
        default=None,
        description="Filtrar por estado calculado: present, late, absent, incomplete, rest_day, worked_rest_day, scheduled, no_shift",
    ),
    only_active_collaborators: bool = Query(default=True),
    search: Optional[str] = Query(
        default=None,
        description="Buscar colaborador por nombre, No. empleado, departamento o puesto",
    ),
) -> AttendanceDailyReportResponse:
    default_tz = _resolve_tz("America/Mexico_City")
    today_local = datetime.now(default_tz).date()

    eff_start = start_date or today_local
    eff_end = end_date or eff_start
    if eff_end < eff_start:
        eff_start, eff_end = eff_end, eff_start

    # Limitar rango máximo a 62 días para proteger rendimiento
    if (eff_end - eff_start).days > 62:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El rango de fechas a calcular no puede exceder los 62 días por consulta.",
        )

    collab_stmt = select(Collaborator)
    if company_id is not None:
        collab_stmt = collab_stmt.where(Collaborator.company_id == company_id)
    if collaborator_id is not None:
        collab_stmt = collab_stmt.where(Collaborator.id == collaborator_id)
    if only_active_collaborators:
        collab_stmt = collab_stmt.where(Collaborator.is_active.is_(True))
    if search and search.strip():
        term = f"%{search.strip()}%"
        collab_stmt = collab_stmt.where(
            or_(
                Collaborator.employee_no.ilike(term),
                Collaborator.first_name.ilike(term),
                Collaborator.last_name.ilike(term),
                Collaborator.department.ilike(term),
                Collaborator.position.ilike(term),
                Collaborator.nss.ilike(term),
                Collaborator.rfc.ilike(term),
            )
        )

    collab_stmt = collab_stmt.order_by(Collaborator.company_id.asc(), Collaborator.employee_no.asc())
    collaborators = list(db.scalars(collab_stmt).all())

    if not collaborators:
        empty_summary = AttendanceReportSummary(
            start_date=eff_start.isoformat(),
            end_date=eff_end.isoformat(),
        )
        return AttendanceDailyReportResponse(summary=empty_summary, items=[])

    collab_ids = [c.id for c in collaborators]

    # 1. Consultar asistencias históricas ya guardadas en daily_attendances para el rango
    persisted_stmt = select(DailyAttendance).where(
        DailyAttendance.collaborator_id.in_(collab_ids),
        DailyAttendance.attendance_date >= eff_start,
        DailyAttendance.attendance_date <= eff_end,
    )
    persisted_rows = list(db.scalars(persisted_stmt).all())
    persisted_map: dict[tuple[int, date], DailyAttendance] = {
        (row.collaborator_id, row.attendance_date): row for row in persisted_rows
    }

    # 2. Consultar todos los logs válidos dentro de una ventana amplia alrededor del rango
    utc_window_start = datetime.combine(eff_start - timedelta(days=1), time(0, 0), tzinfo=timezone.utc)
    utc_window_end = datetime.combine(eff_end + timedelta(days=2), time(23, 59, 59), tzinfo=timezone.utc)

    logs_stmt = (
        select(AttendanceLog)
        .where(
            AttendanceLog.collaborator_id.in_(collab_ids),
            AttendanceLog.is_valid_attendance.is_(True),
            AttendanceLog.event_time >= utc_window_start,
            AttendanceLog.event_time <= utc_window_end,
        )
        .order_by(AttendanceLog.event_time.asc(), AttendanceLog.id.asc())
    )
    all_logs = list(db.scalars(logs_stmt).all())

    # Agrupar logs por (collaborator_id, local_date) respetando la zona horaria histórica de ese día si ya existe
    collab_default_tz_map: dict[int, tzinfo] = {
        c.id: _resolve_tz(c.shift.timezone if c.shift else "America/Mexico_City")
        for c in collaborators
    }
    logs_by_collab_date: dict[tuple[int, date], list[AttendanceLog]] = {}
    for lg in all_logs:
        if lg.collaborator_id is None:
            continue
        c_tz = collab_default_tz_map.get(lg.collaborator_id, default_tz)
        local_dt = _ensure_utc(lg.event_time).astimezone(c_tz)
        key = (lg.collaborator_id, local_dt.date())
        persisted_for_day = persisted_map.get(key)
        if persisted_for_day and persisted_for_day.timezone:
            hist_tz = _resolve_tz(persisted_for_day.timezone)
            local_dt = _ensure_utc(lg.event_time).astimezone(hist_tz)
            key = (lg.collaborator_id, local_dt.date())
        logs_by_collab_date.setdefault(key, []).append(lg)

    num_days = (eff_end - eff_start).days + 1
    date_list = [eff_start + timedelta(days=i) for i in range(num_days)]

    all_items: list[CalculatedDailyAttendanceItem] = []
    dirty_persistence = False
    newly_created_records: list[tuple[DailyAttendance, CalculatedDailyAttendanceItem]] = []

    present_count = 0
    late_count = 0
    absent_count = 0
    incomplete_count = 0
    rest_day_count = 0
    worked_rest_day_count = 0
    scheduled_count = 0
    no_shift_count = 0
    working_days_evaluated = 0
    rest_days_evaluated = 0
    attended_days_count = 0
    total_late_minutes = 0
    total_net_worked_minutes = 0
    matched_collab_ids: set[int] = set()

    for eval_d in date_list:
        for collab in collaborators:
            day_logs = logs_by_collab_date.get((collab.id, eval_d), [])
            existing_record = persisted_map.get((collab.id, eval_d))

            if eval_d > today_local and existing_record is None and not day_logs:
                # Fecha futura sin checadas: proyectar como 'scheduled' en memoria sin congelar todavía en BD
                c_tz = collab_default_tz_map.get(collab.id, default_tz)
                item = _calculate_collaborator_day_attendance(
                    collab=collab,
                    eval_date=eval_d,
                    day_logs=day_logs,
                    today_local=today_local,
                    tz=c_tz,
                )
                item.is_persisted = False
            elif existing_record is not None:
                # Ya existe registro en daily_attendances para este colaborador y fecha
                frozen_snapshot = ShiftRuleSnapshot.from_daily_record(existing_record)

                if (
                    not existing_record.is_locked
                    and frozen_snapshot is None
                    and collab.shift is not None
                ):
                    # Estaba guardado como 'no_shift' porque no tenía turno y ya se le asignó uno
                    new_snapshot = ShiftRuleSnapshot.from_shift(collab.shift)
                    c_tz = _resolve_tz(new_snapshot.timezone if new_snapshot else "America/Mexico_City")
                    item = _calculate_collaborator_day_attendance(
                        collab=collab,
                        eval_date=eval_d,
                        day_logs=day_logs,
                        today_local=today_local,
                        tz=c_tz,
                        shift_snapshot=new_snapshot,
                        use_explicit_snapshot=True,
                    )
                    _apply_calculated_item_to_record(existing_record, item, eval_d, new_snapshot)
                    dirty_persistence = True
                    item = _build_item_from_persisted_record(existing_record, collab, day_logs)
                elif not existing_record.is_locked:
                    hist_tz = _resolve_tz(
                        frozen_snapshot.timezone if frozen_snapshot else existing_record.timezone
                    )
                    _, sched_meal_m, sched_net_m, min_req_m = _calculate_shift_required_minutes(
                        frozen_snapshot, eval_d, hist_tz
                    )
                    needs_recalc = (
                        len(day_logs) != existing_record.logs_count
                        or (existing_record.status in ("incomplete", "no_shift") and existing_record.has_attendance)
                        or (existing_record.actual_check_in is not None and existing_record.actual_check_out is None and (existing_record.has_attendance or existing_record.status != "incomplete"))
                        or (frozen_snapshot is not None and existing_record.has_attendance and existing_record.net_worked_minutes < min_req_m)
                        or (
                            frozen_snapshot is not None
                            and frozen_snapshot.has_meal_break
                            and sched_meal_m > 0
                            and existing_record.meal_taken_minutes is None
                            and existing_record.actual_check_out is not None
                            and existing_record.net_worked_minutes == existing_record.gross_worked_minutes
                            and existing_record.gross_worked_minutes > (sched_net_m // 2)
                        )
                    )
                    if needs_recalc:
                        # Recalcular las marcas USANDO EL SNAPSHOT CONGELADO DE ESE DÍA (nunca el turno nuevo)
                        item = _calculate_collaborator_day_attendance(
                            collab=collab,
                            eval_date=eval_d,
                            day_logs=day_logs,
                            today_local=today_local,
                            tz=hist_tz,
                            shift_snapshot=frozen_snapshot,
                            use_explicit_snapshot=True,
                        )
                        _apply_calculated_item_to_record(existing_record, item, eval_d, frozen_snapshot)
                        dirty_persistence = True
                    item = _build_item_from_persisted_record(existing_record, collab, day_logs)
                else:
                    # Devolver directamente el registro histórico bloqueado sin recalcular
                    item = _build_item_from_persisted_record(existing_record, collab, day_logs)
            else:
                # Primera vez que se evalúa esta fecha (<= hoy): calcular con el turno actual y congelar en daily_attendances
                current_snapshot = ShiftRuleSnapshot.from_shift(collab.shift)
                c_tz = _resolve_tz(
                    current_snapshot.timezone if current_snapshot else "America/Mexico_City"
                )
                item = _calculate_collaborator_day_attendance(
                    collab=collab,
                    eval_date=eval_d,
                    day_logs=day_logs,
                    today_local=today_local,
                    tz=c_tz,
                    shift_snapshot=current_snapshot,
                    use_explicit_snapshot=True,
                )
                new_record = DailyAttendance(
                    company_id=collab.company_id,
                    collaborator_id=collab.id,
                    attendance_date=eval_d,
                    weekday=eval_d.weekday(),
                    employee_no=collab.employee_no,
                    collaborator_name=collab.full_name,
                )
                _apply_calculated_item_to_record(new_record, item, eval_d, current_snapshot)
                db.add(new_record)
                persisted_map[(collab.id, eval_d)] = new_record
                dirty_persistence = True
                item.is_persisted = True
                item.is_locked = False
                item.calculated_at = new_record.calculated_at
                newly_created_records.append((new_record, item))

            # Filtrar por shift_id si se solicitó (considerando tanto el turno histórico del día como el actual)
            if shift_id is not None:
                if shift_id == 0:
                    if item.shift_id is not None:
                        continue
                else:
                    if item.shift_id != shift_id and collab.shift_id != shift_id:
                        continue

            matched_collab_ids.add(collab.id)

            # Acumular KPIs globales antes de aplicar status_filter visual
            if item.is_working_day:
                working_days_evaluated += 1
            elif item.shift_id is not None:
                rest_days_evaluated += 1

            if item.status == "present":
                present_count += 1
            elif item.status == "late":
                late_count += 1
            elif item.status == "absent":
                absent_count += 1
            elif item.status == "incomplete":
                incomplete_count += 1
            elif item.status == "rest_day":
                rest_day_count += 1
            elif item.status == "worked_rest_day":
                worked_rest_day_count += 1
            elif item.status == "scheduled":
                scheduled_count += 1
            elif item.status == "no_shift":
                no_shift_count += 1

            if item.has_attendance:
                attended_days_count += 1
            total_late_minutes += item.late_minutes
            total_net_worked_minutes += item.net_worked_minutes

            if status_filter is None or item.status == status_filter:
                all_items.append(item)

    if dirty_persistence:
        db.commit()
        for created_rec, created_item in newly_created_records:
            created_item.id = created_rec.id

    eff_collabs = (
        [c for c in collaborators if c.id in matched_collab_ids]
        if shift_id is not None
        else collaborators
    )
    with_shift = sum(1 for c in eff_collabs if c.shift_id is not None)
    without_shift = len(eff_collabs) - with_shift

    # Porcentaje de asistencia sobre días laborales (únicamente cuentan como asistidos los días con jornada completa cumplida)
    actionable_working_days = present_count + late_count + incomplete_count + absent_count
    attended_working_days = present_count + late_count
    attendance_rate = (
        round((attended_working_days / actionable_working_days) * 100.0, 1)
        if actionable_working_days > 0
        else 0.0
    )

    summary = AttendanceReportSummary(
        start_date=eff_start.isoformat(),
        end_date=eff_end.isoformat(),
        total_collaborators=len(eff_collabs),
        collaborators_with_shift=with_shift,
        collaborators_without_shift=without_shift,
        total_evaluated_days=len(eff_collabs) * num_days,
        working_days_evaluated=working_days_evaluated,
        rest_days_evaluated=rest_days_evaluated,
        present_count=present_count,
        late_count=late_count,
        absent_count=absent_count,
        incomplete_count=incomplete_count,
        rest_day_count=rest_day_count,
        worked_rest_day_count=worked_rest_day_count,
        scheduled_count=scheduled_count,
        no_shift_count=no_shift_count,
        attended_days_count=attended_days_count,
        attendance_rate=attendance_rate,
        total_late_minutes=total_late_minutes,
        total_net_worked_hours=round(total_net_worked_minutes / 60.0, 2),
    )

    return AttendanceDailyReportResponse(summary=summary, items=all_items)


@router.post(
    "/attendance/daily-report/lock",
    response_model=DailyAttendanceLockResponse,
    dependencies=[Depends(require_any_permission("attendance:write", "shifts:write"))],
    summary="Bloquear o desbloquear registros históricos de asistencia en daily_attendances",
)
def toggle_lock_daily_attendance(
    db: SessionDep,
    payload: DailyAttendanceLockRequest,
) -> DailyAttendanceLockResponse:
    if not payload.record_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Debes indicar al menos un registro de asistencia para bloquear/desbloquear.",
        )

    records = list(
        db.scalars(
            select(DailyAttendance).where(DailyAttendance.id.in_(payload.record_ids))
        ).all()
    )
    for rec in records:
        rec.is_locked = payload.is_locked

    db.commit()
    action_str = "bloqueados" if payload.is_locked else "desbloqueados"
    return DailyAttendanceLockResponse(
        updated_count=len(records),
        is_locked=payload.is_locked,
        message=f"{len(records)} registro(s) de asistencia {action_str} correctamente.",
    )
