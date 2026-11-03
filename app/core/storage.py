import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Optional, Protocol

from app.core.config import settings
from app.core.security import create_signed_media_token


@dataclass(frozen=True)
class StoredMediaObject:
    """Metadata returned after persisting a media file in local folder or S3."""

    media_path: str
    sha256: str
    size_bytes: int
    version: str
    content_type: str = "image/jpeg"


@dataclass(frozen=True)
class SignedMediaDownload:
    """Signed temporary download URL and metadata for mobile provisioning."""

    version: str
    download_url: str
    expires_at: datetime
    sha256: str


class MediaStorageBackend(Protocol):
    """Protocol for pluggable media storage backends (local folder vs S3)."""

    def put_bytes(
        self,
        relative_path: str,
        data: bytes,
        content_type: str = "image/jpeg",
    ) -> str: ...

    def get_bytes(self, relative_path: str) -> bytes: ...

    def exists(self, relative_path: str) -> bool: ...

    def delete(self, relative_path: str) -> None: ...

    def get_signed_download_url(
        self,
        relative_path: str,
        sha256: Optional[str] = None,
        expires_in_seconds: Optional[int] = None,
    ) -> tuple[str, datetime]: ...


def _sanitize_relative_media_path(relative_path: str) -> str:
    """Normalize a relative media path using forward slashes and prevent path traversal."""
    cleaned = relative_path.strip().replace("\\", "/").lstrip("/")
    parts = [p for p in cleaned.split("/") if p and p not in (".", "..")]
    if not parts:
        raise ValueError("Ruta media relativa inválida")
    return "/".join(parts)


class LocalFolderMediaStorage:
    """Stores media files inside a local directory (`back/media/`) and serves signed URLs."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir.resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_safe_path(self, relative_path: str) -> Path:
        clean_rel = _sanitize_relative_media_path(relative_path)
        full_path = (self.root_dir / clean_rel).resolve()
        if self.root_dir != full_path and self.root_dir not in full_path.parents:
            raise ValueError("Acceso fuera del directorio media no permitido")
        return full_path

    def put_bytes(
        self,
        relative_path: str,
        data: bytes,
        content_type: str = "image/jpeg",
    ) -> str:
        clean_rel = _sanitize_relative_media_path(relative_path)
        target = self._resolve_safe_path(clean_rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return clean_rel

    def get_bytes(self, relative_path: str) -> bytes:
        target = self._resolve_safe_path(relative_path)
        if not target.is_file():
            raise FileNotFoundError(f"Archivo media no encontrado: {relative_path}")
        return target.read_bytes()

    def exists(self, relative_path: str) -> bool:
        try:
            target = self._resolve_safe_path(relative_path)
            return target.is_file()
        except ValueError:
            return False

    def delete(self, relative_path: str) -> None:
        if not relative_path:
            return
        try:
            target = self._resolve_safe_path(relative_path)
            if target.is_file():
                target.unlink(missing_ok=True)
        except Exception:
            pass

    def get_signed_download_url(
        self,
        relative_path: str,
        sha256: Optional[str] = None,
        expires_in_seconds: Optional[int] = None,
    ) -> tuple[str, datetime]:
        clean_rel = _sanitize_relative_media_path(relative_path)
        token, expires_at = create_signed_media_token(
            media_path=clean_rel,
            sha256=sha256,
            expires_in_seconds=expires_in_seconds,
        )
        url = f"{settings.public_api_base_url}/media/signed/{token}"
        return url, expires_at


class S3CompatibleMediaStorage:
    """Stores media files in an S3-compatible bucket (AWS S3, Cloudflare R2, MinIO) using the same relative media_path."""

    def __init__(self) -> None:
        self.bucket = settings.S3_BUCKET_NAME or ""
        self.region = settings.S3_REGION
        self.endpoint_url = settings.S3_ENDPOINT_URL
        self.access_key = settings.S3_ACCESS_KEY_ID
        self.secret_key = settings.S3_SECRET_ACCESS_KEY

    def _get_client(self):  # type: ignore[no-untyped-def]
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "Para usar MEDIA_STORAGE_BACKEND='s3' instala 'boto3' en el entorno virtual."
            ) from exc
        if not self.bucket:
            raise RuntimeError("S3_BUCKET_NAME no está configurado.")
        return boto3.client(
            "s3",
            region_name=self.region,
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
        )

    def put_bytes(
        self,
        relative_path: str,
        data: bytes,
        content_type: str = "image/jpeg",
    ) -> str:
        clean_rel = _sanitize_relative_media_path(relative_path)
        client = self._get_client()
        client.put_object(
            Bucket=self.bucket,
            Key=clean_rel,
            Body=data,
            ContentType=content_type,
        )
        return clean_rel

    def get_bytes(self, relative_path: str) -> bytes:
        clean_rel = _sanitize_relative_media_path(relative_path)
        client = self._get_client()
        resp = client.get_object(Bucket=self.bucket, Key=clean_rel)
        return bytes(resp["Body"].read())

    def exists(self, relative_path: str) -> bool:
        clean_rel = _sanitize_relative_media_path(relative_path)
        client = self._get_client()
        try:
            client.head_object(Bucket=self.bucket, Key=clean_rel)
            return True
        except Exception:
            return False

    def delete(self, relative_path: str) -> None:
        if not relative_path:
            return
        try:
            clean_rel = _sanitize_relative_media_path(relative_path)
            client = self._get_client()
            client.delete_object(Bucket=self.bucket, Key=clean_rel)
        except Exception:
            pass

    def get_signed_download_url(
        self,
        relative_path: str,
        sha256: Optional[str] = None,
        expires_in_seconds: Optional[int] = None,
    ) -> tuple[str, datetime]:
        clean_rel = _sanitize_relative_media_path(relative_path)
        ttl = expires_in_seconds or settings.MOBILE_SIGNED_MEDIA_EXPIRE_SECONDS
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        client = self._get_client()
        url = str(
            client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": clean_rel},
                ExpiresIn=ttl,
            )
        )
        return url, expires_at


def get_media_storage() -> MediaStorageBackend:
    """Return the active media storage backend ('local' folder or 's3' bucket)."""
    if settings.MEDIA_STORAGE_BACKEND == "s3":
        return S3CompatibleMediaStorage()
    return LocalFolderMediaStorage(settings.media_root_path)


def extract_jpeg_bytes_from_base64(raw_b64: str) -> bytes:
    """Validate and decode a JPEG base64 string (with or without data URL prefix) into raw bytes."""
    cleaned = raw_b64.strip()
    if not cleaned:
        raise ValueError("La cadena base64 está vacía.")

    if "," in cleaned and cleaned.startswith("data:"):
        header, b64_part = cleaned.split(",", 1)
        if "image/jpeg" not in header.lower() and "image/jpg" not in header.lower():
            raise ValueError("La fotografía debe estar en formato JPEG (image/jpeg).")
    else:
        b64_part = cleaned

    try:
        img_bytes = base64.b64decode(b64_part, validate=False)
    except Exception as exc:
        raise ValueError("La cadena base64 de la fotografía no es válida.") from exc

    if len(img_bytes) < 4 or not img_bytes.startswith(b"\xff\xd8\xff"):
        raise ValueError("El archivo de imagen no es un JPEG válido. Usa una fotografía JPG/JPEG.")

    return img_bytes


def next_face_version(current_version: Optional[str]) -> str:
    """Increment face reference version label (e.g. None -> 'face_1', 'face_1' -> 'face_2')."""
    if not current_version:
        return "face_1"
    cleaned = current_version.strip().lower()
    if cleaned.startswith("face_"):
        suffix = cleaned[5:]
        if suffix.isdigit():
            return f"face_{int(suffix) + 1}"
    return "face_1"


def store_collaborator_face_image(
    company_id: int,
    collaborator_id: int,
    jpeg_bytes: bytes,
    previous_version: Optional[str] = None,
    previous_media_path: Optional[str] = None,
) -> StoredMediaObject:
    """Persist a collaborator's JPEG face reference using the active storage backend and return its media_path metadata."""
    storage = get_media_storage()
    new_version = next_face_version(previous_version)
    sha256_hex = hashlib.sha256(jpeg_bytes).hexdigest()
    relative_path = f"faces/company_{company_id}/collab_{collaborator_id}_{new_version}.jpg"

    saved_path = storage.put_bytes(relative_path, jpeg_bytes, content_type="image/jpeg")
    if previous_media_path and previous_media_path != saved_path:
        storage.delete(previous_media_path)

    return StoredMediaObject(
        media_path=saved_path,
        sha256=sha256_hex,
        size_bytes=len(jpeg_bytes),
        version=new_version,
        content_type="image/jpeg",
    )


def store_event_photo_image(
    company_id: int,
    collaborator_id: Optional[int],
    event_id_or_token: str,
    jpeg_bytes: bytes,
) -> StoredMediaObject:
    """Persist an attendance log event photo in 'events/...' and return its media_path metadata."""
    storage = get_media_storage()
    sha256_hex = hashlib.sha256(jpeg_bytes).hexdigest()
    collab_part = f"collab_{collaborator_id}" if collaborator_id else "unmatched"
    relative_path = f"events/company_{company_id}/{collab_part}/{event_id_or_token}.jpg"
    saved_path = storage.put_bytes(relative_path, jpeg_bytes, content_type="image/jpeg")
    return StoredMediaObject(
        media_path=saved_path,
        sha256=sha256_hex,
        size_bytes=len(jpeg_bytes),
        version="event_1",
        content_type="image/jpeg",
    )


def read_media_as_data_url(media_path: Optional[str]) -> Optional[str]:
    """Read a stored JPEG by its media_path and return a data:image/jpeg;base64,... string for ISAPI sync or UI preview."""
    if not media_path or not media_path.strip():
        return None
    storage = get_media_storage()
    try:
        raw = storage.get_bytes(media_path)
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception:
        return None
