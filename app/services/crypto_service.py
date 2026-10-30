"""
Cryptographic service for encrypting and decrypting sensitive account tokens.
Uses AES-128-CBC via Fernet symmetric encryption.
"""

import base64
import hashlib
from typing import Optional
from cryptography.fernet import Fernet
from app.config import settings


def get_fernet(key: Optional[str] = None) -> Fernet:
    """
    Initializes a Fernet cipher instance using ENCRYPTION_KEY.
    If a 32 url-safe base64 key is provided, uses it directly.
    If a custom passphrase is provided, deterministically derives a 32-byte Fernet key via SHA256.
    Raises ValueError if the key is not configured.
    """
    secret = key or settings.ENCRYPTION_KEY
    if not secret or not secret.strip():
        raise ValueError(
            "ENCRYPTION_KEY is not configured in .env. Please set ENCRYPTION_KEY in .env."
        )

    raw_bytes = secret.strip().encode("utf-8")
    try:
        return Fernet(raw_bytes)
    except Exception:
        # Gracefully handle custom passphrases by deriving a deterministic 32-byte url-safe base64 Fernet key
        derived = base64.urlsafe_b64encode(hashlib.sha256(raw_bytes).digest())
        return Fernet(derived)


def encrypt_string(plain_text: str, key: Optional[str] = None) -> str:
    """Encrypts a string and returns a base64 string."""
    f = get_fernet(key)
    encrypted_bytes = f.encrypt(plain_text.encode("utf-8"))
    return encrypted_bytes.decode("utf-8")


def decrypt_string(cipher_text: str, key: Optional[str] = None) -> str:
    """Decrypts a base64 cipher string and returns the original plaintext."""
    f = get_fernet(key)
    decrypted_bytes = f.decrypt(cipher_text.encode("utf-8"))
    return decrypted_bytes.decode("utf-8")

