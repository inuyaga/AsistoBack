import os
from pathlib import Path
import subprocess
import sys

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# Si se ejecuta con el Python global sin activar .venv, redirigir automáticamente a .venv
if sys.prefix == sys.base_prefix:
    _venv_python = _project_root / ".venv" / "Scripts" / "python.exe"
    if _venv_python.exists():
        _env = {**os.environ, "PYTHONPATH": str(_project_root)}
        raise SystemExit(subprocess.call([str(_venv_python), *sys.argv], env=_env))

import click  # noqa: E402
from sqlalchemy import or_, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import get_password_hash  # noqa: E402
import app.models  # noqa: E402, F401 - ensure all ORM models are registered
from app.models.attendance import Shift  # noqa: E402
from app.models.collaborator import Company  # noqa: E402
from app.models.device import HikvisionDevice, ISAPINotificationConfig  # noqa: E402
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


def ensure_tables_and_defaults(db: Session) -> list[Permission]:
    """Create database tables if they do not exist and seed base permissions and admin role."""
    Base.metadata.create_all(bind=engine)

    # Migración ligera para columnas nuevas en tablas existentes
    db.execute(
        text(
            "ALTER TABLE hikvision_devices "
            "ADD COLUMN IF NOT EXISTS company_id INTEGER REFERENCES companies(id) ON DELETE RESTRICT"
        )
    )
    db.execute(
        text(
            "ALTER TABLE hikvision_devices "
            "ADD COLUMN IF NOT EXISTS webhook_target_url VARCHAR(300)"
        )
    )
    db.execute(
        text(
            "ALTER TABLE hikvision_devices "
            "ADD COLUMN IF NOT EXISTS webhook_synced_at TIMESTAMPTZ"
        )
    )
    db.execute(
        text(
            "ALTER TABLE isapi_notification_configs "
            "ADD COLUMN IF NOT EXISTS ip_address VARCHAR(150)"
        )
    )
    db.execute(
        text(
            "ALTER TABLE isapi_notification_configs "
            "ADD COLUMN IF NOT EXISTS domain VARCHAR(150)"
        )
    )
    db.execute(
        text(
            "ALTER TABLE collaborators "
            "ADD COLUMN IF NOT EXISTS nss VARCHAR(32)"
        )
    )
    db.execute(
        text(
            "ALTER TABLE collaborators "
            "ADD COLUMN IF NOT EXISTS rfc VARCHAR(20)"
        )
    )
    db.execute(
        text(
            "ALTER TABLE collaborators "
            "ADD COLUMN IF NOT EXISTS shift_id INTEGER REFERENCES shifts(id) ON DELETE SET NULL"
        )
    )
    db.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_attendance_logs_collab_event_time "
            "ON attendance_logs (collaborator_id, event_time)"
        )
    )

    default_company = db.scalar(select(Company).order_by(Company.id.asc()).limit(1))
    if not default_company:
        default_company = Company(
            code="EMP-001",
            name="Empresa Principal",
            is_active=True,
            notes="Empresa inicial creada automáticamente",
        )
        db.add(default_company)
        db.flush()

    default_shift = db.scalar(select(Shift).order_by(Shift.id.asc()).limit(1))
    if not default_shift:
        default_shift = Shift(
            company_id=None,
            code="TUR-001",
            name="Turno General L-V (09:00 - 18:00)",
            description="Lunes a Viernes de 09:00 a 18:00, comida de 14:00 a 15:00 (60 min) y descanso Sábado y Domingo",
            working_days="0,1,2,3,4",
            check_in_time="09:00",
            check_out_time="18:00",
            tolerance_minutes=10,
            early_leave_tolerance_minutes=5,
            has_meal_break=True,
            meal_start_time="14:00",
            meal_end_time="15:00",
            meal_duration_minutes=60,
            timezone="America/Mexico_City",
            is_active=True,
        )
        db.add(default_shift)
        db.flush()

    orphan_devices = list(
        db.scalars(select(HikvisionDevice).where(HikvisionDevice.company_id.is_(None))).all()
    )
    for dev in orphan_devices:
        dev.company_id = default_company.id

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

    seeded_permissions: list[Permission] = []
    for code, name, description in DEFAULT_PERMISSIONS:
        perm = db.scalar(select(Permission).where(Permission.code == code))
        if not perm:
            perm = Permission(code=code, name=name, description=description)
            db.add(perm)
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


@click.group(help="CLI de administración para Attendance System Backend.")
def cli() -> None:
    pass


@cli.command("init-db")
def init_db_command() -> None:
    """Inicializa las tablas en PostgreSQL y carga los permisos/roles base."""
    try:
        with SessionLocal() as db:
            perms = ensure_tables_and_defaults(db)
            click.secho(
                f"[OK] Tablas creadas y {len(perms)} permisos base sincronizados correctamente.",
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
            ensure_tables_and_defaults(db)

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
