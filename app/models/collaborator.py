from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.attendance import Shift
    from app.models.device import HikvisionDevice
    from app.models.mobile import Branch, GeofenceAssignment, MobileDevice


class Company(Base):
    """Organization/Company in the multi-company attendance system.
    Each collaborator and each Hikvision device belongs exclusively to one Company.
    """

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), unique=True, index=True, nullable=False)
    tax_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Mexico_City", nullable=False)
    contact_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    contact_email: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    contact_phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

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

    devices: Mapped[list["HikvisionDevice"]] = relationship(
        "HikvisionDevice",
        back_populates="company",
        lazy="select",
    )
    collaborators: Mapped[list["Collaborator"]] = relationship(
        "Collaborator",
        back_populates="company",
        cascade="all, delete-orphan",
        lazy="select",
    )

    @property
    def devices_count(self) -> int:
        return len(self.devices)

    @property
    def collaborators_count(self) -> int:
        return len(self.collaborators)


class Collaborator(Base):
    """Employee/Collaborator belonging exclusively to a single Company."""

    __tablename__ = "collaborators"
    __table_args__ = (
        UniqueConstraint("company_id", "employee_no", name="uq_collaborator_company_emp_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    company_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("companies.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    shift_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("shifts.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    branch_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("branches.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    employee_no: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(120), nullable=False)
    nss: Mapped[Optional[str]] = mapped_column(String(32), index=True, nullable=True)
    rfc: Mapped[Optional[str]] = mapped_column(String(20), index=True, nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    department: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    position: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    card_no: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Parámetros ISAPI para el reloj Hikvision y referencia de almacenamiento media (local / S3)
    user_type: Mapped[str] = mapped_column(String(24), default="normal", nullable=False)
    valid_begin_time: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    valid_end_time: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    face_media_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    face_reference_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    face_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Columna legada: se migra automáticamente a archivo en media_path y queda en NULL en BD
    face_image_base64: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

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

    company: Mapped["Company"] = relationship(
        "Company",
        back_populates="collaborators",
        lazy="selectin",
    )
    shift: Mapped[Optional["Shift"]] = relationship(
        "Shift",
        back_populates="collaborators",
        lazy="selectin",
    )
    branch: Mapped[Optional["Branch"]] = relationship(
        "Branch",
        lazy="selectin",
    )
    device_syncs: Mapped[list["CollaboratorDeviceSync"]] = relationship(
        "CollaboratorDeviceSync",
        back_populates="collaborator",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    geofence_assignments: Mapped[list["GeofenceAssignment"]] = relationship(
        "GeofenceAssignment",
        back_populates="collaborator",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    mobile_devices: Mapped[list["MobileDevice"]] = relationship(
        "MobileDevice",
        back_populates="collaborator",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def public_id(self) -> str:
        return f"emp_{self.id:05d}"

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def company_name(self) -> str:
        return self.company.name if self.company else ""

    @property
    def company_code(self) -> str:
        return self.company.code if self.company else ""

    @property
    def shift_name(self) -> Optional[str]:
        return self.shift.name if self.shift else None

    @property
    def shift_code(self) -> Optional[str]:
        return self.shift.code if self.shift else None

    @property
    def branch_name(self) -> Optional[str]:
        return self.branch.name if self.branch else None

    @property
    def _active_geofence_assignment(self) -> Optional["GeofenceAssignment"]:
        active_list = [
            a
            for a in self.geofence_assignments
            if a.is_active and a.geofence and a.geofence.is_active
        ]
        if not active_list:
            return None
        active_list.sort(key=lambda a: (a.priority, -a.id))
        return active_list[0]

    @property
    def primary_geofence_id(self) -> Optional[int]:
        assign = self._active_geofence_assignment
        if assign and assign.geofence:
            return assign.geofence.id
        if self.branch and self.branch.geofences:
            active_branch_geos = [g for g in self.branch.geofences if g.is_active]
            if active_branch_geos:
                return active_branch_geos[0].id
        return None

    @property
    def primary_geofence_name(self) -> Optional[str]:
        assign = self._active_geofence_assignment
        if assign and assign.geofence:
            return assign.geofence.name
        if self.branch and self.branch.geofences:
            active_branch_geos = [g for g in self.branch.geofences if g.is_active]
            if active_branch_geos:
                return active_branch_geos[0].name
        return None

    @property
    def primary_geofence_type(self) -> Optional[str]:
        assign = self._active_geofence_assignment
        if assign and assign.geofence:
            return assign.geofence.geofence_type
        if self.branch and self.branch.geofences:
            active_branch_geos = [g for g in self.branch.geofences if g.is_active]
            if active_branch_geos:
                return active_branch_geos[0].geofence_type
        return None

    @property
    def geofence_assignments_count(self) -> int:
        return sum(1 for a in self.geofence_assignments if a.is_active)

    @property
    def has_active_mobile_device(self) -> bool:
        return any(d.status == "active" for d in self.mobile_devices)

    @property
    def mobile_device_model(self) -> Optional[str]:
        for d in sorted(self.mobile_devices, key=lambda x: x.id, reverse=True):
            if d.status == "active":
                label = f"{d.manufacturer or ''} {d.model or ''}".strip()
                return label or d.platform or "Móvil Activo"
        return None

    @property
    def has_face_image(self) -> bool:
        return bool(
            (self.face_media_path and self.face_media_path.strip())
            or (self.face_image_base64 and self.face_image_base64.strip())
        )


class CollaboratorDeviceSync(Base):
    """Tracks synchronization state of a Collaborator on a specific HikvisionDevice."""

    __tablename__ = "collaborator_device_syncs"
    __table_args__ = (
        UniqueConstraint("collaborator_id", "device_id", name="uq_collab_device_sync"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    collaborator_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("collaborators.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    device_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("hikvision_devices.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), default="synced", nullable=False)
    face_synced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    last_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    collaborator: Mapped["Collaborator"] = relationship(
        "Collaborator",
        back_populates="device_syncs",
    )
    device: Mapped["HikvisionDevice"] = relationship(
        "HikvisionDevice",
        lazy="selectin",
    )

    @property
    def device_name(self) -> Optional[str]:
        return self.device.name if self.device else None
