from datetime import datetime
from typing import Annotated, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def validate_email_address(value: str) -> str:
    """Validate email format while allowing internal/local domains like .local."""
    cleaned = value.strip().lower()
    if "@" not in cleaned or cleaned.startswith("@") or cleaned.endswith("@"):
        raise ValueError("Formato de correo electrónico inválido")
    local_part, _, domain_part = cleaned.partition("@")
    if (
        not local_part
        or "." not in domain_part
        or domain_part.startswith(".")
        or domain_part.endswith(".")
    ):
        raise ValueError("Formato de correo electrónico inválido")
    return cleaned


EmailAddress = Annotated[
    str,
    Field(min_length=3, max_length=255, examples=["admin@attendance.local"]),
    AfterValidator(validate_email_address),
]


class PermissionBase(BaseModel):
    code: str = Field(..., min_length=2, max_length=100, examples=["users:read"])
    name: str = Field(..., min_length=2, max_length=150, examples=["Ver usuarios"])
    description: Optional[str] = Field(default=None, max_length=255)


class PermissionCreate(PermissionBase):
    pass


class PermissionUpdate(BaseModel):
    code: Optional[str] = Field(default=None, min_length=2, max_length=100)
    name: Optional[str] = Field(default=None, min_length=2, max_length=150)
    description: Optional[str] = Field(default=None, max_length=255)


class PermissionRead(PermissionBase):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RoleBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=100, examples=["supervisor"])
    description: Optional[str] = Field(default=None, max_length=255)


class RoleCreate(RoleBase):
    permission_codes: list[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=100)
    description: Optional[str] = Field(default=None, max_length=255)
    permission_codes: Optional[list[str]] = None


class RoleRead(RoleBase):
    id: int
    created_at: datetime
    permissions: list[PermissionRead] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class UserBase(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    email: EmailAddress
    full_name: Optional[str] = Field(default=None, max_length=150)
    is_active: bool = True
    is_superuser: bool = False


class UserCreate(UserBase):
    password: str = Field(..., min_length=6, max_length=128)
    role_names: list[str] = Field(default_factory=list)
    permission_codes: list[str] = Field(default_factory=list)


class UserUpdate(BaseModel):
    username: Optional[str] = Field(default=None, min_length=3, max_length=64)
    email: Optional[EmailAddress] = None
    full_name: Optional[str] = Field(default=None, max_length=150)
    password: Optional[str] = Field(default=None, min_length=6, max_length=128)
    is_active: Optional[bool] = None
    is_superuser: Optional[bool] = None
    role_names: Optional[list[str]] = None
    permission_codes: Optional[list[str]] = None


class UserRead(UserBase):
    id: int
    created_at: datetime
    updated_at: datetime
    roles: list[RoleRead] = Field(default_factory=list)
    direct_permissions: list[PermissionRead] = Field(default_factory=list)
    all_permission_codes: list[str] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)
