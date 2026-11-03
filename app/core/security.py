import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
from typing import Any

import bcrypt
import jwt

from app.core.config import settings


def _derive_keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """Generate a deterministic keystream using HMAC-SHA256 in counter mode."""
    blocks: list[bytes] = []
    counter = 0
    produced = 0
    while produced < length:
        counter_bytes = counter.to_bytes(4, "big")
        block = hmac.new(key, nonce + counter_bytes, hashlib.sha256).digest()
        blocks.append(block)
        produced += len(block)
        counter += 1
    return b"".join(blocks)[:length]


def encrypt_secret(plaintext: str) -> str:
    """Symmetrically encrypt a secret (e.g. ISAPI password) using SECRET_KEY."""
    raw = plaintext.encode("utf-8")
    key = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    nonce = secrets.token_bytes(16)
    stream = _derive_keystream(key, nonce, len(raw))
    ciphertext = bytes(a ^ b for a, b in zip(raw, stream))
    mac = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()[:16]
    blob = nonce + mac + ciphertext
    return "enc1:" + base64.urlsafe_b64encode(blob).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Decrypt a secret previously encrypted with encrypt_secret."""
    if not token:
        return ""
    if not token.startswith("enc1:"):
        return token
    encoded = token[5:]
    blob = base64.urlsafe_b64decode(encoded.encode("ascii"))
    if len(blob) < 32:
        raise ValueError("Secret token corrupto")
    nonce = blob[:16]
    mac = blob[16:32]
    ciphertext = blob[32:]
    key = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    expected_mac = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(mac, expected_mac):
        raise ValueError("Firma de secreto inválida")
    stream = _derive_keystream(key, nonce, len(ciphertext))
    raw = bytes(a ^ b for a, b in zip(ciphertext, stream))
    return raw.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    return bcrypt.checkpw(
        plain_password.encode("utf-8"),
        hashed_password.encode("utf-8"),
    )


def get_password_hash(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def create_access_token(
    subject: str | int,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Create a signed JWT access token."""
    now = datetime.now(timezone.utc)
    if expires_delta is not None:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": now,
        "exp": expire,
    }
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(
        payload,
        settings.SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT access token."""
    return jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
    )
