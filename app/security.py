"""Crypto helpers: ids, password hashing, signed tokens, encryption at rest, hashing secrets."""
import base64
import hashlib
import hmac
import secrets
import time
from typing import Any, Optional

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12).replace('-', 'x').replace('_', 'y')}"


def opaque_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt(rounds=10 if settings.env != "test" else 4)).decode()


def verify_password(password: str, hashed: Optional[str]) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except ValueError:
        return False


# ---------------------------------------------------------------- secrets (API keys, codes)
def hash_secret(value: str) -> str:
    """Deterministic keyed hash for lookup of API keys / OTP codes / SCIM tokens."""
    return hmac.new(settings.secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ---------------------------------------------------------------- signed tokens (slot tokens, reset links, oauth state)
def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=salt)


def sign(data: Any, salt: str) -> str:
    return _serializer(salt).dumps(data)


def unsign(token: str, salt: str, max_age: Optional[int] = None) -> Optional[Any]:
    try:
        return _serializer(salt).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


# ---------------------------------------------------------------- encryption at rest (envelope-style; swap for KMS in prod)
def _fernet() -> Fernet:
    key = settings.encryption_key
    if not key:
        key = base64.urlsafe_b64encode(hashlib.sha256(("enc:" + settings.secret_key).encode()).digest()).decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: Optional[str]) -> Optional[str]:
    if plaintext is None:
        return None
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: Optional[str]) -> Optional[str]:
    if ciphertext is None:
        return None
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return None


def webhook_signature(secret: str, body: bytes, timestamp: Optional[int] = None) -> tuple[str, int]:
    """Signature header format: t=<ts>,v1=<hex hmac sha256 of f"{ts}.{body}">"""
    ts = timestamp or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}", ts


def verify_webhook_signature(secret: str, body: bytes, header: str, tolerance: int = 300) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except Exception:
        return False
    if abs(time.time() - ts) > tolerance:
        return False
    expected, _ = webhook_signature(secret, body, ts)
    return hmac.compare_digest(expected, header)
