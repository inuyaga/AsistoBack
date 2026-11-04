import os
from pathlib import Path
import subprocess
import sys

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# Si se ejecuta con el Python global sin activar .venv, redirigir automáticamente a .venv
if sys.prefix == sys.base_prefix:
    for _candidate in (
        _project_root / ".venv" / "bin" / "python",
        _project_root / ".venv" / "Scripts" / "python.exe",
    ):
        if _candidate.exists():
            _env = {**os.environ, "PYTHONPATH": str(_project_root)}
            raise SystemExit(subprocess.call([str(_candidate), *sys.argv], env=_env))

from datetime import datetime, time, timedelta, timezone  # noqa: E402
import json  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import click  # noqa: E402
from sqlalchemy import or_, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import encrypt_secret, get_password_hash  # noqa: E402
from app.core.storage import extract_jpeg_bytes_from_base64, store_collaborator_face_image  # noqa: E402
import app.models  # noqa: E402, F401 - ensure all ORM models are registered
from app.models.attendance import AttendanceLog, Shift  # noqa: E402
from app.models.collaborator import Collaborator, Company  # noqa: E402
from app.models.device import HikvisionDevice, ISAPINotificationConfig  # noqa: E402
from app.models.mobile import Branch, Geofence, GeofenceAssignment  # noqa: E402
from app.models.user import Permission, Role, User  # noqa: E402

DEFAULT_PERMISSIONS: list[tuple[str, str, str]] = [
    ("users:read", "Ver usuarios", "Permite consultar la lista y detalle de usuarios"),
    ("users:write", "Crear y editar usuarios", "Permite crear y actualizar usuarios"),
    ("users:delete", "Eliminar usuarios", "Permite eliminar usuarios del sistema"),
    ("roles:read", "Ver roles", "Permite listar roles y sus permisos"),
    ("roles:write", "Gestionar roles", "Permite crear, editar y eliminar roles"),
    ("permissions:read", "Ver permisos", "Permite listar permisos del sistema"),
    ("permissions:write", "Gestionar permisos", "Permite crear y eliminar permisos"),
    ("attendance:read", "Ver asistencias", "Permite consultar registros de asistencia"),
    ("attendance:write", "Registrar asistencias", "Permite registrar y modificar asistencias"),
    ("shifts:read", "Ver turnos laborales", "Permite listar y consultar turnos laborales y sus reglas"),
    (
        "shifts:write",
        "Crear, editar y asignar turnos",
        "Permite configurar turnos laborales (días laborales, descanso, entrada/salida y comida) y asignarlos a colaboradores",
    ),
    ("shifts:delete", "Eliminar turnos laborales", "Permite eliminar turnos del catálogo"),
    ("companies:read", "Ver empresas", "Permite listar y consultar el catálogo de empresas"),
    ("companies:write", "Crear y editar empresas", "Permite registrar y actualizar empresas"),
    ("companies:delete", "Eliminar empresas", "Permite eliminar empresas del sistema"),
    (
        "collaborators:read",
        "Ver colaboradores",
        "Permite consultar el padrón de colaboradores por empresa",
    ),
    (
        "collaborators:write",
        "Crear y editar colaboradores",
        "Permite registrar y actualizar colaboradores y su fotografía facial",
    ),
    (
        "collaborators:delete",
        "Eliminar colaboradores",
        "Permite dar de baja y eliminar colaboradores del sistema",
    ),
    (
        "collaborators:sync",
        "Sincronizar colaboradores en relojes",
        "Permite registrar o dar de baja colaboradores individual y masivamente en los relojes Hikvision vía ISAPI",
    ),
    ("devices:read", "Ver relojes checadores", "Permite listar y consultar relojes Hikvision"),
    ("devices:write", "Crear y editar relojes checadores", "Permite registrar y actualizar relojes Hikvision"),
    ("devices:delete", "Eliminar relojes checadores", "Permite eliminar relojes Hikvision del sistema"),
    (
        "devices:isapi",
        "Operar relojes por ISAPI",
        "Permite probar conexión, sincronizar hora, consultar usuarios/eventos y operar relojes Hikvision vía ISAPI",
    ),
]


MIGRATION_DDL_STATEMENTS: list[str] = [
    "ALTER TABLE hikvision_devices ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id) ON DELETE RESTRICT",
    "ALTER TABLE hikvision_devices ADD COLUMN IF NOT EXISTS webhook_target_url VARCHAR(300)",
    "ALTER TABLE hikvision_devices ADD COLUMN IF NOT EXISTS webhook_synced_at TIMESTAMPTZ",
    "ALTER TABLE isapi_notification_configs ADD COLUMN IF NOT EXISTS ip_address VARCHAR(150)",
    "ALTER TABLE isapi_notification_configs ADD COLUMN IF NOT EXISTS domain VARCHAR(150)",
    "ALTER TABLE collaborators ADD COLUMN IF NOT EXISTS nss VARCHAR(32)",
    "ALTER TABLE collaborators ADD COLUMN IF NOT EXISTS rfc VARCHAR(20)",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS timezone VARCHAR(64) NOT NULL DEFAULT 'America/Mexico_City'",
    "ALTER TABLE collaborators ADD COLUMN IF NOT EXISTS shift_id INTEGER REFERENCES shifts(id) ON DELETE SET NULL",
    "ALTER TABLE collaborators ADD COLUMN IF NOT EXISTS branch_id INTEGER REFERENCES branches(id) ON DELETE SET NULL",
    "ALTER TABLE collaborators ADD COLUMN IF NOT EXISTS face_media_path VARCHAR(500)",
    "ALTER TABLE collaborators ADD COLUMN IF NOT EXISTS face_reference_version VARCHAR(64)",
    "ALTER TABLE collaborators ADD COLUMN IF NOT EXISTS face_sha256 VARCHAR(64)",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS client_event_id VARCHAR(64)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_attendance_logs_client_event_id ON attendance_logs (client_event_id)",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS mobile_device_fk INTEGER REFERENCES mobile_devices(id) ON DELETE SET NULL",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS branch_id INTEGER REFERENCES branches(id) ON DELETE SET NULL",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS captured_at TIMESTAMPTZ",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS accuracy_meters DOUBLE PRECISION",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS geofence_id INTEGER REFERENCES geofences(id) ON DELETE SET NULL",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS geofence_version INTEGER",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS geofence_assignment_id VARCHAR(100)",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS inside_geofence BOOLEAN",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS distance_meters DOUBLE PRECISION",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS identity_method VARCHAR(64)",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS face_reference_version VARCHAR(64)",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS liveness_passed BOOLEAN",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS match_passed BOOLEAN",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS match_score DOUBLE PRECISION",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS comments VARCHAR(500)",
    "ALTER TABLE attendance_logs ADD COLUMN IF NOT EXISTS photo_media_path VARCHAR(500)",
    "ALTER TABLE daily_attendances ADD COLUMN IF NOT EXISTS branch_id INTEGER REFERENCES branches(id) ON DELETE SET NULL",
    "ALTER TABLE daily_attendances ADD COLUMN IF NOT EXISTS branch_name VARCHAR(150)",
    "ALTER TABLE daily_attendances ADD COLUMN IF NOT EXISTS check_out_comments VARCHAR(500)",
    "CREATE INDEX IF NOT EXISTS ix_attendance_logs_collab_event_time ON attendance_logs (collaborator_id, event_time)",
]


def sync_base_permissions_and_roles(db: Session) -> list[Permission]:
    """Synchronize the predefined system permission catalog and base admin role."""
    seeded_permissions: list[Permission] = []
    for code, name, description in DEFAULT_PERMISSIONS:
        perm = db.scalar(select(Permission).where(Permission.code == code))
        if not perm:
            perm = Permission(code=code, name=name, description=description)
            db.add(perm)
        else:
            perm.name = name
            perm.description = description
        seeded_permissions.append(perm)

    db.flush()

    admin_role = db.scalar(select(Role).where(Role.name == "admin"))
    if not admin_role:
        admin_role = Role(
            name="admin",
            description="Administrador general con todos los permisos base",
            permissions=seeded_permissions,
        )
        db.add(admin_role)
    else:
        existing_codes = {p.code for p in admin_role.permissions}
        for perm in seeded_permissions:
            if perm.code not in existing_codes:
                admin_role.permissions.append(perm)

    db.commit()
    return seeded_permissions


def apply_migrations(db: Session) -> dict[str, int]:
    """Apply database schema creation, DDL column/index migrations, legacy media migration, and predefined permissions."""
    Base.metadata.create_all(bind=engine)

    for ddl in MIGRATION_DDL_STATEMENTS:
        db.execute(text(ddl))

    # Migrar automáticamente cualquier fotografía facial legada en Base64 hacia archivo media (local / S3)
    migrated_faces = 0
    legacy_face_collabs = list(
        db.scalars(
            select(Collaborator).where(
                Collaborator.face_image_base64.is_not(None),
                Collaborator.face_media_path.is_(None),
            )
        ).all()
    )
    for col in legacy_face_collabs:
        if col.face_image_base64 and col.face_image_base64.strip():
            try:
                jpeg_bytes = extract_jpeg_bytes_from_base64(col.face_image_base64)
                stored = store_collaborator_face_image(
                    company_id=col.company_id,
                    collaborator_id=col.id,
                    jpeg_bytes=jpeg_bytes,
                    previous_version=col.face_reference_version,
                    previous_media_path=col.face_media_path,
                )
                col.face_media_path = stored.media_path
                col.face_reference_version = stored.version
                col.face_sha256 = stored.sha256
                col.face_image_base64 = None
                migrated_faces += 1
            except Exception:
                pass

    # Configuración global singleton para receptor de webhooks ISAPI
    notif_cfg = db.scalar(select(ISAPINotificationConfig).limit(1))
    if not notif_cfg:
        default_host = settings.APP_HOST
        addr_type = (
            "ipaddress"
            if all(part.isdigit() for part in default_host.split(".")) and len(default_host.split(".")) == 4
            else "hostname"
        )
        notif_cfg = ISAPINotificationConfig(
            enabled=True,
            host_id=2,
            protocol_type="HTTP",
            addressing_format_type=addr_type,
            host_address=default_host,
            ip_address=default_host if addr_type == "ipaddress" else "192.168.1.100",
            domain=default_host if addr_type == "hostname" else "attendance.local",
            port_no=settings.APP_PORT,
            url_path=f"{settings.API_V1_STR}/devices/webhook/isapi",
            parameter_format_type="JSON",
            auth_method="none",
            auto_apply_on_create=True,
        )
        db.add(notif_cfg)

    perms = sync_base_permissions_and_roles(db)

    return {
        "ddl_statements": len(MIGRATION_DDL_STATEMENTS),
        "migrated_faces": migrated_faces,
        "permissions_synced": len(perms),
    }


def ensure_tables_and_defaults(db: Session) -> list[Permission]:
    """Ensure schema migrations and predefined permissions are applied (used on app startup and permission sync)."""
    apply_migrations(db)
    return list(db.scalars(select(Permission).order_by(Permission.code)).all())


def seed_test_data(db: Session) -> dict[str, int]:
    """Populate idempotent test/demo seed data (companies, branches, geofences, shifts, devices, collaborators, users, and attendance logs)."""
    from app.api.routes.attendance import sync_collaborator_daily_attendance

    counts: dict[str, int] = {
        "companies": 0,
        "branches": 0,
        "geofences": 0,
        "shifts": 0,
        "devices": 0,
        "collaborators": 0,
        "geofence_assignments": 0,
        "users": 0,
        "attendance_logs": 0,
    }

    # 1. Empresas de prueba
    companies_spec = [
        {
            "code": "EMP-001",
            "name": "Empresa Principal",
            "tax_id": "EPR210101AAA",
            "address": "Av. Tecnológico 1000, Villahermosa, Tabasco",
            "timezone": "America/Mexico_City",
            "contact_name": "Laura Méndez",
            "contact_email": "contacto@empresaprincipal.mx",
            "contact_phone": "+52 993 310 0001",
            "notes": "Empresa corporativa de prueba",
        },
        {
            "code": "EMP-002",
            "name": "Logística y Operaciones del Sureste",
            "tax_id": "LOS220515BBB",
            "address": "Parque Industrial Cárdenas Km 12, Tabasco",
            "timezone": "America/Mexico_City",
            "contact_name": "Ing. Marco Antonio Ríos",
            "contact_email": "operaciones@logisticasureste.mx",
            "contact_phone": "+52 937 372 4500",
            "notes": "Empresa operativa/CEDIS de prueba",
        },
    ]
    companies_by_code: dict[str, Company] = {}
    for spec in companies_spec:
        comp = db.scalar(
            select(Company).where(or_(Company.code == spec["code"], Company.name == spec["name"]))
        )
        if not comp:
            comp = Company(**spec, is_active=True)
            db.add(comp)
            db.flush()
            counts["companies"] += 1
        companies_by_code[spec["code"]] = comp

    emp1 = companies_by_code["EMP-001"]
    emp2 = companies_by_code["EMP-002"]

    # Asignar dispositivos huérfanos si existieran
    orphan_devices = list(
        db.scalars(select(HikvisionDevice).where(HikvisionDevice.company_id.is_(None))).all()
    )
    for dev in orphan_devices:
        dev.company_id = emp1.id

    # 2. Sucursales de prueba
    branches_spec = [
        {
            "company": emp1,
            "code": "SUC-001",
            "name": "Oficina Central",
            "address": "Av. Tecnológico 1000, Villahermosa, Tabasco",
            "timezone": "America/Mexico_City",
        },
        {
            "company": emp1,
            "code": "SUC-002",
            "name": "Sucursal Tabasco 2000",
            "address": "Prol. Paseo Tabasco 1405, Tabasco 2000, Villahermosa, Tabasco",
            "timezone": "America/Mexico_City",
        },
        {
            "company": emp2,
            "code": "SUC-003",
            "name": "CEDIS Industrial Cárdenas",
            "address": "Parque Industrial Cárdenas Km 12, Cárdenas, Tabasco",
            "timezone": "America/Mexico_City",
        },
    ]
    branches_by_code: dict[str, Branch] = {}
    for b_spec in branches_spec:
        comp_obj: Company = b_spec["company"]  # type: ignore[assignment]
        branch = db.scalar(
            select(Branch).where(
                Branch.company_id == comp_obj.id,
                Branch.code == b_spec["code"],
            )
        )
        if not branch:
            branch = Branch(
                company_id=comp_obj.id,
                code=str(b_spec["code"]),
                name=str(b_spec["name"]),
                address=str(b_spec["address"]),
                timezone=str(b_spec["timezone"]),
                is_active=True,
            )
            db.add(branch)
            db.flush()
            counts["branches"] += 1
        branches_by_code[str(b_spec["code"])] = branch

    suc1 = branches_by_code["SUC-001"]
    suc2 = branches_by_code["SUC-002"]
    suc3 = branches_by_code["SUC-003"]

    # 3. Geocercas de prueba (radiales y poligonal)
    cedis_polygon = json.dumps(
        [
            {"latitude": 17.991200, "longitude": -93.376500},
            {"latitude": 17.991200, "longitude": -93.374200},
            {"latitude": 17.989400, "longitude": -93.374200},
            {"latitude": 17.989400, "longitude": -93.376500},
        ]
    )
    geofences_spec = [
        {
            "company": emp1,
            "branch": suc1,
            "code": "GEO-001",
            "name": "Geocerca Oficina Central",
            "geofence_type": "radial",
            "center_latitude": 17.97884283887797,
            "center_longitude": -92.98384174523528,
            "radius_meters": 150.0,
            "vertices_json": None,
            "max_accuracy_meters": 50.0,
        },
        {
            "company": emp1,
            "branch": suc2,
            "code": "GEO-002",
            "name": "Geocerca Sucursal Tabasco 2000",
            "geofence_type": "radial",
            "center_latitude": 17.999510,
            "center_longitude": -92.936120,
            "radius_meters": 120.0,
            "vertices_json": None,
            "max_accuracy_meters": 45.0,
        },
        {
            "company": emp2,
            "branch": suc3,
            "code": "GEO-003",
            "name": "Polígono Patio de Maniobras CEDIS",
            "geofence_type": "polygonal",
            "center_latitude": None,
            "center_longitude": None,
            "radius_meters": None,
            "vertices_json": cedis_polygon,
            "max_accuracy_meters": 60.0,
        },
    ]
    geofences_by_code: dict[str, Geofence] = {}
    for g_spec in geofences_spec:
        comp_obj = g_spec["company"]  # type: ignore[assignment]
        branch_obj: Branch = g_spec["branch"]  # type: ignore[assignment]
        geo = db.scalar(
            select(Geofence).where(
                Geofence.company_id == comp_obj.id,
                Geofence.code == g_spec["code"],
            )
        )
        if not geo:
            geo = Geofence(
                company_id=comp_obj.id,
                branch_id=branch_obj.id,
                code=str(g_spec["code"]),
                name=str(g_spec["name"]),
                version=1,
                geofence_type=str(g_spec["geofence_type"]),
                center_latitude=g_spec["center_latitude"],  # type: ignore[arg-type]
                center_longitude=g_spec["center_longitude"],  # type: ignore[arg-type]
                radius_meters=g_spec["radius_meters"],  # type: ignore[arg-type]
                vertices_json=g_spec["vertices_json"],  # type: ignore[arg-type]
                max_accuracy_meters=float(g_spec["max_accuracy_meters"]),  # type: ignore[arg-type]
                is_active=True,
            )
            db.add(geo)
            db.flush()
            counts["geofences"] += 1
        geofences_by_code[str(g_spec["code"])] = geo

    geo1 = geofences_by_code["GEO-001"]
    geo2 = geofences_by_code["GEO-002"]
    geo3 = geofences_by_code["GEO-003"]

    # 4. Turnos laborales de prueba
    shifts_spec = [
        {
            "company_id": None,
            "code": "TUR-001",
            "name": "Turno General L-V (09:00 - 18:00)",
            "description": "Lunes a Viernes de 09:00 a 18:00, comida de 14:00 a 15:00 (60 min) y descanso Sábado y Domingo",
            "working_days": "0,1,2,3,4",
            "check_in_time": "09:00",
            "check_out_time": "18:00",
            "tolerance_minutes": 10,
            "early_leave_tolerance_minutes": 5,
            "has_meal_break": True,
            "meal_start_time": "14:00",
            "meal_end_time": "15:00",
            "meal_duration_minutes": 60,
        },
        {
            "company_id": emp1.id,
            "code": "TUR-002",
            "name": "Turno Matutino L-S (07:00 - 15:00)",
            "description": "Lunes a Sábado de 07:00 a 15:00, comida de 11:30 a 12:00 (30 min)",
            "working_days": "0,1,2,3,4,5",
            "check_in_time": "07:00",
            "check_out_time": "15:00",
            "tolerance_minutes": 5,
            "early_leave_tolerance_minutes": 5,
            "has_meal_break": True,
            "meal_start_time": "11:30",
            "meal_end_time": "12:00",
            "meal_duration_minutes": 30,
        },
        {
            "company_id": emp2.id,
            "code": "TUR-003",
            "name": "Turno Operativo CEDIS L-V (08:00 - 17:00)",
            "description": "Lunes a Viernes de 08:00 a 17:00 con 60 min de comida",
            "working_days": "0,1,2,3,4",
            "check_in_time": "08:00",
            "check_out_time": "17:00",
            "tolerance_minutes": 10,
            "early_leave_tolerance_minutes": 5,
            "has_meal_break": True,
            "meal_start_time": "13:00",
            "meal_end_time": "14:00",
            "meal_duration_minutes": 60,
        },
    ]
    shifts_by_code: dict[str, Shift] = {}
    for s_spec in shifts_spec:
        sh = db.scalar(select(Shift).where(Shift.code == s_spec["code"]))
        if not sh:
            sh = Shift(**s_spec, timezone="America/Mexico_City", is_active=True)
            db.add(sh)
            db.flush()
            counts["shifts"] += 1
        shifts_by_code[str(s_spec["code"])] = sh

    tur1 = shifts_by_code["TUR-001"]
    tur2 = shifts_by_code["TUR-002"]
    tur3 = shifts_by_code["TUR-003"]

    # 5. Relojes checadores Hikvision de prueba
    devices_spec = [
        {
            "company_id": emp1.id,
            "name": "Reloj Acceso Principal (Demo)",
            "location": "Recepción Planta Baja - Oficina Central",
            "ip_address": "192.168.1.201",
            "port": 80,
            "model": "DS-K1T341AMF",
            "serial_number": "DS-K1T341AMF20260001",
            "firmware_version": "V3.2.30_build240101",
            "device_type": "AccessController",
        },
        {
            "company_id": emp2.id,
            "name": "Reloj Caseta CEDIS (Demo)",
            "location": "Acceso Peatonal Patio de Maniobras",
            "ip_address": "192.168.1.202",
            "port": 80,
            "model": "DS-K1T671MF",
            "serial_number": "DS-K1T671MF20260002",
            "firmware_version": "V3.2.30_build240101",
            "device_type": "AccessController",
        },
    ]
    devices_by_name: dict[str, HikvisionDevice] = {}
    for d_spec in devices_spec:
        dev = db.scalar(select(HikvisionDevice).where(HikvisionDevice.name == d_spec["name"]))
        if not dev:
            dev = HikvisionDevice(
                **d_spec,
                protocol="http",
                username="admin",
                encrypted_password=encrypt_secret("HikvisionDemo123!"),
                verify_ssl=False,
                timeout_seconds=8,
                is_active=True,
                status="online",
                notes="Dispositivo semilla para pruebas",
            )
            db.add(dev)
            db.flush()
            counts["devices"] += 1
        devices_by_name[str(d_spec["name"])] = dev

    dev_main = devices_by_name["Reloj Acceso Principal (Demo)"]
    dev_cedis = devices_by_name["Reloj Caseta CEDIS (Demo)"]

    # 6. Colaboradores de prueba y asignaciones de geocerca
    collabs_spec = [
        {
            "company": emp1,
            "branch": suc1,
            "shift": tur1,
            "geofence": geo1,
            "employee_no": "1001",
            "first_name": "Juan Carlos",
            "last_name": "Pérez Hernández",
            "nss": "12908845123",
            "rfc": "PEHJ900512AB1",
            "email": "juan.perez@empresaprincipal.mx",
            "phone": "9931010001",
            "department": "Tecnologías de la Información",
            "position": "Desarrollador Full Stack",
            "card_no": "0001001",
        },
        {
            "company": emp1,
            "branch": suc1,
            "shift": tur1,
            "geofence": geo1,
            "employee_no": "1002",
            "first_name": "María Fernanda",
            "last_name": "López Gómez",
            "nss": "12929133456",
            "rfc": "LOGM920823CD2",
            "email": "maria.lopez@empresaprincipal.mx",
            "phone": "9931010002",
            "department": "Recursos Humanos",
            "position": "Coordinadora de Capital Humano",
            "card_no": "0001002",
        },
        {
            "company": emp1,
            "branch": suc2,
            "shift": tur2,
            "geofence": geo2,
            "employee_no": "1003",
            "first_name": "Carlos Alberto",
            "last_name": "Sánchez Ruiz",
            "nss": "12887654321",
            "rfc": "SARC881104EF3",
            "email": "carlos.sanchez@empresaprincipal.mx",
            "phone": "9931010003",
            "department": "Atención a Clientes",
            "position": "Ejecutivo de Sucursal",
            "card_no": "0001003",
        },
        {
            "company": emp1,
            "branch": suc1,
            "shift": tur1,
            "geofence": geo1,
            "employee_no": "1004",
            "first_name": "Ana Sofía",
            "last_name": "Martínez Cruz",
            "nss": "12954433221",
            "rfc": "MACA950319GH4",
            "email": "ana.martinez@empresaprincipal.mx",
            "phone": "9931010004",
            "department": "Finanzas",
            "position": "Analista Contable",
            "card_no": "0001004",
        },
        {
            "company": emp2,
            "branch": suc3,
            "shift": tur3,
            "geofence": geo3,
            "employee_no": "2001",
            "first_name": "Roberto",
            "last_name": "Méndez Vázquez",
            "nss": "12851122334",
            "rfc": "MEVR850730IJ5",
            "email": "roberto.mendez@logisticasureste.mx",
            "phone": "9371200001",
            "department": "Almacén y Distribución",
            "position": "Jefe de Patio",
            "card_no": "0002001",
        },
    ]
    collabs_by_emp_no: dict[str, Collaborator] = {}
    for c_spec in collabs_spec:
        comp_obj = c_spec["company"]  # type: ignore[assignment]
        branch_obj = c_spec["branch"]  # type: ignore[assignment]
        shift_obj: Shift = c_spec["shift"]  # type: ignore[assignment]
        geo_obj: Geofence = c_spec["geofence"]  # type: ignore[assignment]

        collab = db.scalar(
            select(Collaborator).where(
                Collaborator.company_id == comp_obj.id,
                Collaborator.employee_no == c_spec["employee_no"],
            )
        )
        if not collab:
            collab = Collaborator(
                company_id=comp_obj.id,
                branch_id=branch_obj.id,
                shift_id=shift_obj.id,
                employee_no=str(c_spec["employee_no"]),
                first_name=str(c_spec["first_name"]),
                last_name=str(c_spec["last_name"]),
                nss=str(c_spec["nss"]),
                rfc=str(c_spec["rfc"]),
                email=str(c_spec["email"]),
                phone=str(c_spec["phone"]),
                department=str(c_spec["department"]),
                position=str(c_spec["position"]),
                card_no=str(c_spec["card_no"]),
                user_type="normal",
                is_active=True,
                notes="Colaborador semilla de prueba",
            )
            db.add(collab)
            db.flush()
            counts["collaborators"] += 1

        assign = db.scalar(
            select(GeofenceAssignment).where(
                GeofenceAssignment.collaborator_id == collab.id,
                GeofenceAssignment.geofence_id == geo_obj.id,
            )
        )
        if not assign:
            assign = GeofenceAssignment(
                company_id=comp_obj.id,
                collaborator_id=collab.id,
                branch_id=branch_obj.id,
                shift_id=shift_obj.id,
                geofence_id=geo_obj.id,
                event_type="all",
                priority=10,
                is_active=True,
            )
            db.add(assign)
            db.flush()
            counts["geofence_assignments"] += 1

        collabs_by_emp_no[str(c_spec["employee_no"])] = collab

    # 7. Roles y Usuarios de prueba (RRHH y Supervisor)
    all_perms = {p.code: p for p in db.scalars(select(Permission)).all()}
    rrhh_codes = [
        "attendance:read",
        "attendance:write",
        "shifts:read",
        "shifts:write",
        "companies:read",
        "collaborators:read",
        "collaborators:write",
        "collaborators:sync",
        "devices:read",
    ]
    supervisor_codes = [
        "attendance:read",
        "shifts:read",
        "companies:read",
        "collaborators:read",
        "devices:read",
    ]

    rrhh_role = db.scalar(select(Role).where(Role.name == "rrhh"))
    if not rrhh_role:
        rrhh_role = Role(
            name="rrhh",
            description="Recursos Humanos: gestión de colaboradores, turnos y asistencias",
            permissions=[all_perms[c] for c in rrhh_codes if c in all_perms],
        )
        db.add(rrhh_role)
        db.flush()

    supervisor_role = db.scalar(select(Role).where(Role.name == "supervisor"))
    if not supervisor_role:
        supervisor_role = Role(
            name="supervisor",
            description="Supervisor: consulta de asistencias, turnos y colaboradores",
            permissions=[all_perms[c] for c in supervisor_codes if c in all_perms],
        )
        db.add(supervisor_role)
        db.flush()

    demo_users = [
        ("rrhh_demo", "rrhh@asisto.local", "Laura Méndez (RRHH Demo)", "rrhh123456", rrhh_role),
        (
            "supervisor_demo",
            "supervisor@asisto.local",
            "Marco Ríos (Supervisor Demo)",
            "super123456",
            supervisor_role,
        ),
    ]
    for u_name, u_email, u_full, u_pass, u_role in demo_users:
        existing_user = db.scalar(
            select(User).where(or_(User.username == u_name, User.email == u_email))
        )
        if not existing_user:
            db.add(
                User(
                    username=u_name,
                    email=u_email,
                    full_name=u_full,
                    hashed_password=get_password_hash(u_pass),
                    is_active=True,
                    is_superuser=False,
                    roles=[u_role],
                )
            )
            counts["users"] += 1

    db.flush()

    # 8. Checadas (AttendanceLog) y cálculo de asistencia diaria (DailyAttendance) para ayer y hoy
    tz_mx = ZoneInfo("America/Mexico_City")
    today_local = datetime.now(tz_mx).date()
    yesterday_local = today_local - timedelta(days=1)
    # Si ayer cayó en domingo, retroceder al viernes para tener jornada laboral completa de muestra
    if yesterday_local.weekday() == 6:
        sample_workday = yesterday_local - timedelta(days=2)
    elif yesterday_local.weekday() == 5:
        sample_workday = yesterday_local - timedelta(days=1)
    else:
        sample_workday = yesterday_local

    sample_events = [
        # Juan Carlos (1001) - Puntual completo ayer por reloj checador
        (
            "seed-evt-1001-in",
            collabs_by_emp_no["1001"],
            dev_main,
            "clock_device",
            sample_workday,
            time(8, 56, 12),
            "checkIn",
            "Entrada registrada en reloj",
            None,
        ),
        (
            "seed-evt-1001-bout",
            collabs_by_emp_no["1001"],
            dev_main,
            "clock_device",
            sample_workday,
            time(14, 2, 10),
            "breakOut",
            "Salida a comida en reloj",
            None,
        ),
        (
            "seed-evt-1001-bin",
            collabs_by_emp_no["1001"],
            dev_main,
            "clock_device",
            sample_workday,
            time(14, 55, 40),
            "breakIn",
            "Regreso de comida en reloj",
            None,
        ),
        (
            "seed-evt-1001-out",
            collabs_by_emp_no["1001"],
            dev_main,
            "clock_device",
            sample_workday,
            time(18, 4, 22),
            "checkOut",
            "Salida registrada en reloj",
            None,
        ),
        # María Fernanda (1002) - Retardo ayer desde dispositivo móvil dentro de geocerca
        (
            "seed-evt-1002-in",
            collabs_by_emp_no["1002"],
            None,
            "mobile_device",
            sample_workday,
            time(9, 18, 5),
            "checkIn",
            "Entrada desde App Móvil",
            geo1,
        ),
        (
            "seed-evt-1002-out",
            collabs_by_emp_no["1002"],
            None,
            "mobile_device",
            sample_workday,
            time(18, 10, 0),
            "checkOut",
            "Salida desde App Móvil",
            geo1,
        ),
        # Roberto Méndez (2001) - CEDIS puntual ayer en reloj
        (
            "seed-evt-2001-in",
            collabs_by_emp_no["2001"],
            dev_cedis,
            "clock_device",
            sample_workday,
            time(7, 58, 30),
            "checkIn",
            "Entrada registrada en reloj CEDIS",
            None,
        ),
        (
            "seed-evt-2001-out",
            collabs_by_emp_no["2001"],
            dev_cedis,
            "clock_device",
            sample_workday,
            time(17, 5, 15),
            "checkOut",
            "Salida registrada en reloj CEDIS",
            None,
        ),
    ]

    touched_collab_dates: set[tuple[int, datetime]] = set()
    for (
        client_evt_id,
        col_obj,
        dev_obj,
        src_type,
        evt_date,
        evt_time,
        att_status,
        evt_label,
        geo_ref,
    ) in sample_events:
        existing_log = db.scalar(
            select(AttendanceLog).where(AttendanceLog.client_event_id == client_evt_id)
        )
        if not existing_log:
            local_dt = datetime.combine(evt_date, evt_time, tzinfo=tz_mx)
            utc_dt = local_dt.astimezone(timezone.utc)
            log_entry = AttendanceLog(
                source_type=src_type,
                company_id=col_obj.company_id,
                collaborator_id=col_obj.id,
                device_id=dev_obj.id if dev_obj else None,
                branch_id=col_obj.branch_id,
                device_employee_no=col_obj.employee_no,
                collaborator_employee_no=col_obj.employee_no,
                collaborator_name=col_obj.full_name,
                device_name=dev_obj.name if dev_obj else "App Móvil Asisto (Demo)",
                source_ip=dev_obj.ip_address if dev_obj else "10.0.0.50",
                event_type="MobileAttendanceEvent" if src_type == "mobile_device" else "AccessControllerEvent",
                major_event=5,
                minor_event=75,
                event_label=evt_label,
                attendance_status=att_status,
                verify_mode="face",
                card_no=col_obj.card_no,
                is_valid_attendance=True,
                client_event_id=client_evt_id,
                captured_at=utc_dt,
                latitude=str(geo_ref.center_latitude) if geo_ref and geo_ref.center_latitude else None,
                longitude=str(geo_ref.center_longitude) if geo_ref and geo_ref.center_longitude else None,
                accuracy_meters=12.5 if geo_ref else None,
                geofence_id=geo_ref.id if geo_ref else None,
                geofence_version=geo_ref.version if geo_ref else None,
                inside_geofence=True if geo_ref else None,
                distance_meters=18.4 if geo_ref else None,
                identity_method="face_liveness" if src_type == "mobile_device" else None,
                liveness_passed=True if src_type == "mobile_device" else None,
                match_passed=True if src_type == "mobile_device" else None,
                match_score=0.96 if src_type == "mobile_device" else None,
                event_time=utc_dt,
            )
            db.add(log_entry)
            counts["attendance_logs"] += 1
            touched_collab_dates.add((col_obj.id, utc_dt))

    db.flush()
    for col_id, ref_dt in touched_collab_dates:
        sync_collaborator_daily_attendance(db, col_id, ref_dt)

    db.commit()
    return counts


@click.group(help="CLI de administración para Attendance System Backend.")
def cli() -> None:
    pass


@cli.command("migrate")
def migrate_command() -> None:
    """Aplica exclusivamente las migraciones de esquema (tablas, columnas, índices) y carga los permisos predefinidos de la plataforma."""
    try:
        with SessionLocal() as db:
            stats = apply_migrations(db)
            msg = (
                f"[OK] Migraciones aplicadas correctamente "
                f"({stats['ddl_statements']} sentencias DDL verificadas, "
                f"{stats['permissions_synced']} permisos predefinidos y rol 'admin' sincronizados"
            )
            if stats["migrated_faces"]:
                msg += f", {stats['migrated_faces']} fotografías migradas a almacenamiento media"
            msg += ")."
            click.secho(msg, fg="green")
    except Exception as exc:
        click.secho(f"[ERROR] Error al aplicar las migraciones: {exc}", fg="red", err=True)
        sys.exit(1)


@cli.command("seed-test")
def seed_test_command() -> None:
    """Carga exclusivamente datos semilla de prueba (empresas, sucursales, geocercas, turnos, colaboradores, relojes y checadas)."""
    try:
        with SessionLocal() as db:
            apply_migrations(db)
            counts = seed_test_data(db)
            click.secho(
                "[OK] Datos semilla de prueba sincronizados correctamente:\n"
                f"  • Empresas nuevas:               {counts['companies']}\n"
                f"  • Sucursales nuevas:             {counts['branches']}\n"
                f"  • Geocercas nuevas:              {counts['geofences']}\n"
                f"  • Turnos laborales nuevos:       {counts['shifts']}\n"
                f"  • Relojes Hikvision demo:        {counts['devices']}\n"
                f"  • Colaboradores nuevos:          {counts['collaborators']}\n"
                f"  • Asignaciones de geocerca:      {counts['geofence_assignments']}\n"
                f"  • Usuarios demo (RRHH/Superv.):  {counts['users']}\n"
                f"  • Checadas de asistencia demo:   {counts['attendance_logs']}",
                fg="green",
            )
    except Exception as exc:
        click.secho(f"[ERROR] Error al cargar datos semilla de prueba: {exc}", fg="red", err=True)
        sys.exit(1)


@cli.command("seed")
@click.pass_context
def seed_alias_command(ctx: click.Context) -> None:
    """Alias de 'seed-test': carga exclusivamente los datos semilla de prueba."""
    ctx.invoke(seed_test_command)


@cli.command("init-db")
@click.option(
    "--with-seed",
    is_flag=True,
    default=False,
    help="Cargar también los datos semilla de prueba después de migrar.",
)
def init_db_command(with_seed: bool) -> None:
    """Inicializa las tablas, migraciones y permisos predefinidos en PostgreSQL (usa --with-seed para incluir datos de prueba)."""
    try:
        with SessionLocal() as db:
            stats = apply_migrations(db)
            click.secho(
                f"[OK] Tablas migradas y {stats['permissions_synced']} permisos predefinidos sincronizados correctamente.",
                fg="green",
            )
            if with_seed:
                counts = seed_test_data(db)
                click.secho(
                    f"[OK] Datos semilla de prueba cargados ({counts['collaborators']} colaboradores, "
                    f"{counts['companies']} empresas, {counts['attendance_logs']} checadas).",
                    fg="green",
                )
    except Exception as exc:
        click.secho(f"[ERROR] Error al conectar o inicializar la base de datos: {exc}", fg="red", err=True)
        sys.exit(1)


@cli.command("create-superuser")
@click.option("--username", prompt="Nombre de usuario", help="Username único para el superusuario.")
@click.option("--email", prompt="Correo electrónico", help="Email único para el superusuario.")
@click.option("--full-name", prompt="Nombre completo", default="Administrador", show_default=True)
@click.option(
    "--password",
    prompt="Contraseña",
    hide_input=True,
    confirmation_prompt="Confirmar contraseña",
    help="Contraseña del superusuario (mínimo 6 caracteres).",
)
def create_superuser_command(username: str, email: str, full_name: str, password: str) -> None:
    """Crea un usuario con privilegios de superusuario (is_superuser=True)."""
    if len(password) < 6:
        click.secho("✖ La contraseña debe tener al menos 6 caracteres.", fg="red", err=True)
        sys.exit(1)

    try:
        with SessionLocal() as db:
            apply_migrations(db)

            existing = db.scalar(
                select(User).where(or_(User.username == username, User.email == email))
            )
            if existing:
                click.secho(
                    f"✖ Ya existe un usuario con username '{username}' o email '{email}'.",
                    fg="red",
                    err=True,
                )
                sys.exit(1)

            admin_role = db.scalar(select(Role).where(Role.name == "admin"))
            roles = [admin_role] if admin_role else []

            superuser = User(
                username=username.strip(),
                email=email.strip().lower(),
                full_name=full_name.strip(),
                hashed_password=get_password_hash(password),
                is_active=True,
                is_superuser=True,
                roles=roles,
            )
            db.add(superuser)
            db.commit()
            db.refresh(superuser)

            click.secho(
                f"✔ Superusuario '{superuser.username}' ({superuser.email}) creado con éxito [ID: {superuser.id}].",
                fg="green",
            )
    except Exception as exc:
        click.secho(f"✖ Error al crear el superusuario: {exc}", fg="red", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()

