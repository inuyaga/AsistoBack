from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.collaborator import Company


class HikvisionDevice(Base):
    """Hikvision time attendance / access control terminal managed via ISAPI."""

    __tablename__ = "hikvision_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    company_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("companies.id", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    location: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    protocol: Mapped[str] = mapped_column(String(10), default="http", nullable=False)
    ip_address: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=80, nullable=False)
    username: Mapped[str] = mapped_column(String(64), default="admin", nullable=False)
    encrypted_password: Mapped[str] = mapped_column(String(512), nullable=False)
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=8, nullable=False)

    # Información reportada por el dispositivo vía /ISAPI/System/deviceInfo
    device_id_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    serial_number: Mapped[Optional[str]] = mapped_column(String(120), index=True, nullable=True)
    firmware_version: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    mac_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    device_type: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Estado de sincronización de HttpHostNotification (Webhook ISAPI en el reloj)
    webhook_target_url: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    webhook_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

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

    company: Mapped[Optional["Company"]] = relationship(
        "Company",
        back_populates="devices",
        lazy="selectin",
    )

    @property
    def company_name(self) -> Optional[str]:
        return self.company.name if self.company else None

    @property
    def company_code(self) -> Optional[str]:
        return self.company.code if self.company else None

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://{self.ip_address}:{self.port}"

    @property
    def has_password(self) -> bool:
        return bool(self.encrypted_password)


class ISAPINotificationConfig(Base):
    """Global ISAPI HttpHostNotification configuration (ConfigServerNotificationsRecibe)."""

    __tablename__ = "isapi_notification_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    host_id: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    protocol_type: Mapped[str] = mapped_column(String(10), default="HTTP", nullable=False)
    addressing_format_type: Mapped[str] = mapped_column(
        String(20), default="ipaddress", nullable=False
    )
    host_address: Mapped[str] = mapped_column(
        String(150), default="192.168.1.100", nullable=False
    )
    ip_address: Mapped[Optional[str]] = mapped_column(
        String(150), default="192.168.1.100", nullable=True
    )
    domain: Mapped[Optional[str]] = mapped_column(
        String(150), default="attendance.local", nullable=True
    )
    port_no: Mapped[int] = mapped_column(Integer, default=8000, nullable=False)
    url_path: Mapped[str] = mapped_column(
        String(255), default="/api/v1/devices/webhook/isapi", nullable=False
    )
    parameter_format_type: Mapped[str] = mapped_column(String(16), default="JSON", nullable=False)
    auth_method: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    auth_username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    encrypted_auth_password: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    auto_apply_on_create: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    @property
    def end_point(self) -> str:
        return self.normalized_path

    @property
    def protocol(self) -> str:
        return self.protocol_type

    @property
    def address_type(self) -> str:
        return self.addressing_format_type

    @property
    def port(self) -> int:
        return self.port_no

    @property
    def normalized_path(self) -> str:
        path = (self.url_path or "/").strip()
        return path if path.startswith("/") else f"/{path}"

    @property
    def effective_host(self) -> str:
        if self.addressing_format_type == "hostname":
            return (self.domain or self.host_address or "attendance.local").strip()
        return (self.ip_address or self.host_address or "127.0.0.1").strip()

    @property
    def full_target_url(self) -> str:
        proto = (self.protocol_type or "HTTP").lower()
        return f"{proto}://{self.effective_host}:{self.port_no}{self.normalized_path}"

    @property
    def has_auth_password(self) -> bool:
        return bool(self.encrypted_auth_password)
