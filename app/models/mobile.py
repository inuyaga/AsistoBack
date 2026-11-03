from datetime import date, datetime, timezone
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
from app.core.geofence import parse_polygon_vertices

if TYPE_CHECKING:
    from app.models.attendance import Shift
    from app.models.collaborator import Collaborator, Company


class Branch(Base):
    """Company branch / workplace location (Sucursal)."""

    __tablename__ = "branches"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_branch_company_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    company_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("companies.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    code: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), index=True, nullable=False)
    address: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
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

    company: Mapped["Company"] = relationship("Company", lazy="selectin")
    geofences: Mapped[list["Geofence"]] = relationship(
        "Geofence",
        back_populates="branch",
        lazy="select",
    )

    @property
    def public_id(self) -> str:
        return f"br_{self.id:03d}"

    @property
    def company_name(self) -> str:
        return self.company.name if self.company else ""


class Geofence(Base):
    """Versioned radial or polygonal geofence associated with a Company and optionally a Branch."""

    __tablename__ = "geofences"
    __table_args__ = (
        UniqueConstraint("company_id", "code", name="uq_geofence_company_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    company_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("companies.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    branch_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("branches.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    code: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Tipo: 'radial' o 'polygonal'
    geofence_type: Mapped[str] = mapped_column(String(24), default="radial", nullable=False)

    # Parámetros radiales
    center_latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    center_longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    radius_meters: Mapped[Optional[float]] = mapped_column(Float, default=150.0, nullable=True)

    # Parámetros poligonales (JSON array de {"latitude": float, "longitude": float})
    vertices_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Reglas operativas
    max_accuracy_meters: Mapped[float] = mapped_column(Float, default=50.0, nullable=False)
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

    company: Mapped["Company"] = relationship("Company", lazy="selectin")
    branch: Mapped[Optional["Branch"]] = relationship(
        "Branch",
        back_populates="geofences",
        lazy="selectin",
    )

    @property
    def public_id(self) -> str:
        return f"geo_{self.id:03d}"

    @property
    def vertices(self) -> list[dict[str, float]]:
        return parse_polygon_vertices(self.vertices_json)

    @property
    def branch_name(self) -> Optional[str]:
        return self.branch.name if self.branch else None


class GeofenceAssignment(Base):
    """Dynamic assignment of a Geofence to a collaborator, shift, or branch with optional date validity."""

    __tablename__ = "geofence_assignments"
    __table_args__ = (
        Index("ix_geofence_assign_collab_active", "collaborator_id", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    company_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("companies.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    collaborator_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("collaborators.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    shift_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("shifts.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    branch_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("branches.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    geofence_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("geofences.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # Tipo de evento al que aplica: 'all', 'check_in', 'check_out'
    event_type: Mapped[str] = mapped_column(String(24), default="all", nullable=False)
    valid_from_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    valid_until_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
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

    company: Mapped["Company"] = relationship("Company", lazy="selectin")
    collaborator: Mapped[Optional["Collaborator"]] = relationship(
        "Collaborator",
        back_populates="geofence_assignments",
        lazy="select",
    )
    shift: Mapped[Optional["Shift"]] = relationship("Shift", lazy="selectin")
    branch: Mapped[Optional["Branch"]] = relationship("Branch", lazy="selectin")
    geofence: Mapped["Geofence"] = relationship("Geofence", lazy="selectin")

    @property
    def company_name(self) -> str:
        return self.company.name if self.company else ""

    @property
    def collaborator_name(self) -> Optional[str]:
        return self.collaborator.full_name if self.collaborator else None

    @property
    def employee_no(self) -> Optional[str]:
        return self.collaborator.employee_no if self.collaborator else None

    @property
    def shift_name(self) -> Optional[str]:
        return self.shift.name if self.shift else None

    @property
    def branch_name(self) -> Optional[str]:
        return self.branch.name if self.branch else None

    @property
    def geofence_name(self) -> Optional[str]:
        return self.geofence.name if self.geofence else None

    @property
    def geofence_code(self) -> Optional[str]:
        return self.geofence.code if self.geofence else None

    @property
    def geofence_type(self) -> Optional[str]:
        return self.geofence.geofence_type if self.geofence else None

    @property
    def geofence_version(self) -> int:
        return self.geofence.version if self.geofence else 1


class MobileActivationCode(Base):
    """One-time activation code (manual or QR) generated for a Collaborator to link their mobile device."""

    __tablename__ = "mobile_activation_codes"

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
    branch_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("branches.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    # Se almacena únicamente el hash SHA-256 del código normalizado y una máscara parcial para auditoría
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    code_hint: Mapped[str] = mapped_column(String(24), nullable=False)

    # Estado: 'pending', 'used', 'expired', 'revoked'
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    company: Mapped["Company"] = relationship("Company", lazy="selectin")
    collaborator: Mapped["Collaborator"] = relationship("Collaborator", lazy="selectin")
    branch: Mapped[Optional["Branch"]] = relationship("Branch", lazy="selectin")

    @property
    def public_id(self) -> str:
        return f"act_{self.id:03d}"


class MobileActivationChallenge(Base):
    """Short-lived validationToken challenge issued by POST /activations/validate."""

    __tablename__ = "mobile_activation_challenges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    activation_code_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("mobile_activation_codes.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    collaborator_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("collaborators.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    installation_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    # Estado: 'pending', 'consumed', 'expired'
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    activation_code: Mapped["MobileActivationCode"] = relationship(
        "MobileActivationCode",
        lazy="selectin",
    )
    collaborator: Mapped["Collaborator"] = relationship("Collaborator", lazy="selectin")


class MobileDevice(Base):
    """Authorized mobile device linked to a Collaborator via Asisto onboarding."""

    __tablename__ = "mobile_devices"
    __table_args__ = (
        Index("ix_mobile_devices_collab_status", "collaborator_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    activation_code_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("mobile_activation_codes.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
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
    branch_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("branches.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )

    installation_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), default="android", nullable=False)
    operating_system: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    manufacturer: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    app_version: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    app_build: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    push_token: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    push_provider: Mapped[Optional[str]] = mapped_column(String(32), default="fcm", nullable=True)

    # Hashes SHA-256 de credenciales (nunca se almacenan en texto plano)
    device_credential_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    refresh_token_hash: Mapped[Optional[str]] = mapped_column(String(64), index=True, nullable=True)
    refresh_token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Estado: 'active', 'revoked', 'replaced'
    status: Mapped[str] = mapped_column(String(24), default="active", index=True, nullable=False)
    revoke_reason: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    failed_session_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=True,
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

    company: Mapped["Company"] = relationship("Company", lazy="selectin")
    collaborator: Mapped["Collaborator"] = relationship(
        "Collaborator",
        back_populates="mobile_devices",
        lazy="select",
    )
    branch: Mapped[Optional["Branch"]] = relationship("Branch", lazy="selectin")
    activation_code: Mapped[Optional["MobileActivationCode"]] = relationship(
        "MobileActivationCode",
        lazy="select",
    )

    @property
    def public_id(self) -> str:
        return f"dev_{self.id:03d}"

    @property
    def activation_public_id(self) -> str:
        if self.activation_code_id:
            return f"act_{self.activation_code_id:03d}"
        return f"act_{self.id:03d}"

    @property
    def collaborator_name(self) -> str:
        return self.collaborator.full_name if self.collaborator else ""

    @property
    def employee_no(self) -> str:
        return self.collaborator.employee_no if self.collaborator else ""

    @property
    def company_name(self) -> str:
        return self.company.name if self.company else ""

    @property
    def branch_name(self) -> Optional[str]:
        return self.branch.name if self.branch else None

    @property
    def is_authorized(self) -> bool:
        return self.status == "active"
