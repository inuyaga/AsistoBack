"""Almacenamiento temporal en disco de fotografías JPEG para descarga vía faceURL por biométricos ISUP 5.0.

En Hikvision ISUP 5.0, NET_ECMS_ISAPIPassThrough no transmite cabeceras HTTP
(`Content-Type: multipart/form-data; boundary=...`), por lo que el registro de rostro
(`PUT /ISAPI/Intelligent/FDLib/FDSetUp?format=json`) debe enviarse como JSON puro con `faceURL`.

Este módulo guarda temporalmente el JPEG en `{media_root_path}/tmp_isup_faces/{token}.jpg`
con un token aleatorio corto (32 hex) y TTL configurable, lo que permite:
- URLs cortas (< 120 chars) compatibles con límites estrictos de firmware Hikvision.
- Compatibilidad entre múltiples workers Uvicorn/Gunicorn sin requerir migraciones de BD.
- Limpieza automática de archivos expirados.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import time
from typing import Optional

from app.core.config import settings

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_STAGING_SUBDIR = "tmp_isup_faces"


def _staging_dir() -> Path:
    directory = (settings.media_root_path / _STAGING_SUBDIR).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def cleanup_expired_staged_isup_faces(ttl_seconds: Optional[int] = None) -> int:
    """Elimina imágenes temporales expiradas del directorio de staging ISUP."""
    ttl = max(15, int(ttl_seconds if ttl_seconds is not None else settings.ISUP_FACE_URL_TTL_SECONDS))
    now = time.time()
    removed = 0
    try:
        directory = _staging_dir()
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            try:
                if now - entry.stat().st_mtime > ttl:
                    entry.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
    except OSError:
        return 0
    return removed


def stage_isup_face_image(
    jpeg_bytes: bytes,
    *,
    ttl_seconds: Optional[int] = None,
) -> tuple[str, str]:
    """Guarda temporalmente un JPEG para ISUP y devuelve `(token, face_url)`."""
    if not jpeg_bytes or len(jpeg_bytes) < 4 or not jpeg_bytes.startswith(b"\xff\xd8\xff"):
        raise ValueError("El archivo de imagen no es un JPEG válido.")

    cleanup_expired_staged_isup_faces(ttl_seconds=ttl_seconds)

    token = secrets.token_hex(16)
    directory = _staging_dir()
    final_path = directory / f"{token}.jpg"
    tmp_path = directory / f".{token}.tmp"

    tmp_path.write_bytes(jpeg_bytes)
    os.replace(tmp_path, final_path)

    face_url = f"{settings.isup_face_base_url}/devices/isup-face/{token}.jpg"
    return token, face_url


def read_staged_isup_face_image(
    token: str,
    *,
    ttl_seconds: Optional[int] = None,
) -> Optional[bytes]:
    """Lee un JPEG temporal staged por token si existe y no ha expirado."""
    clean_token = (token or "").strip().lower()
    if clean_token.endswith(".jpg"):
        clean_token = clean_token[:-4]
    if not _TOKEN_RE.fullmatch(clean_token):
        return None

    ttl = max(15, int(ttl_seconds if ttl_seconds is not None else settings.ISUP_FACE_URL_TTL_SECONDS))
    directory = _staging_dir()
    target = (directory / f"{clean_token}.jpg").resolve()
    if target.parent != directory or not target.is_file():
        return None

    try:
        age_seconds = time.time() - target.stat().st_mtime
        if age_seconds > ttl:
            target.unlink(missing_ok=True)
            return None
        return target.read_bytes()
    except OSError:
        return None

