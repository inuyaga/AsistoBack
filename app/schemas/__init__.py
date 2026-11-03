"""Pydantic schemas package."""

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
from app.schemas.auth import LoginRequest, Token, TokenPayload
from app.schemas.health import DatabaseStatus, HealthResponse
from app.schemas.user import (
    PermissionCreate,
    PermissionRead,
    PermissionUpdate,
    RoleCreate,
    RoleRead,
    RoleUpdate,
    UserCreate,
    UserRead,
    UserUpdate,
)

__all__ = [
    "AttendanceDailyReportResponse",
    "AttendanceReportSummary",
    "AttendanceStatusCode",
    "CalculatedDailyAttendanceItem",
    "DailyAttendanceLockRequest",
    "DailyAttendanceLockResponse",
    "DatabaseStatus",
    "HealthResponse",
    "LoginRequest",
    "PermissionCreate",
    "PermissionRead",
    "PermissionUpdate",
    "RoleCreate",
    "RoleRead",
    "RoleUpdate",
    "ShiftAssignRequest",
    "ShiftAssignResponse",
    "ShiftCreate",
    "ShiftRead",
    "ShiftUpdate",
    "Token",
    "TokenPayload",
    "UserCreate",
    "UserRead",
    "UserUpdate",
]
