"""Password-based encryption for exchange API keys.

Uses AES-256-GCM with PBKDF2-derived keys. All primitives are from
the Python stdlib (hashlib + os) so no extra dependencies are needed.

File format for secrets.enc:
    salt (16 bytes) | nonce (12 bytes) | tag (16 bytes) | ciphertext (variable)

Password verification uses bcrypt-style PBKDF2-SHA256 hash stored in auth.json.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import struct
from pathlib import Path
from typing import Any

logger = logging.getLogger("tradeautonom.crypto")

# PBKDF2 parameters
_PBKDF2_ITERATIONS = 600_000
_SALT_LEN = 16
_KEY_LEN = 32  # AES-256
_NONCE_LEN = 12  # GCM standard
_TAG_LEN = 16


def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 256-bit AES key from a password using PBKDF2-HMAC-SHA256."""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS, dklen=_KEY_LEN
    )


def _derive_verify_hash(password: str, salt: bytes) -> str:
    """Derive a password verification hash (separate from encryption key)."""
    # Use a different purpose string so the verify hash != the encryption key
    material = hashlib.pbkdf2_hmac(
        "sha256",
        (password + ":verify").encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
        dklen=32,
    )
    return material.hex()


# ── AES-256-GCM using PyCryptodome or stdlib fallback ────────

def _aes_gcm_encrypt(key: bytes, nonce: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    """Encrypt with AES-256-GCM. Returns (ciphertext, tag)."""
    try:
        from Crypto.Cipher import AES
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(plaintext)
        return ciphertext, tag
    except ImportError:
        pass
    # Fallback: use OpenSSL via subprocess (available on all Linux/macOS)
    import subprocess
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False) as f_in:
        f_in.write(plaintext)
        f_in_path = f_in.name
    f_out_path = f_in_path + ".enc"
    try:
        subprocess.run(
            [
                "openssl", "enc", "-aes-256-gcm",
                "-K", key.hex(),
                "-iv", nonce.hex(),
                "-in", f_in_path,
                "-out", f_out_path,
            ],
            check=True, capture_output=True,
        )
        with open(f_out_path, "rb") as f_out:
            raw = f_out.read()
        # OpenSSL appends the 16-byte tag
        return raw[:-_TAG_LEN], raw[-_TAG_LEN:]
    finally:
        Path(f_in_path).unlink(missing_ok=True)
        Path(f_out_path).unlink(missing_ok=True)


def _aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes) -> bytes:
    """Decrypt with AES-256-GCM. Raises ValueError on bad password/tamper."""
    try:
        from Crypto.Cipher import AES
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag)
    except ImportError:
        pass
    import subprocess
    import tempfile
    combined = ciphertext + tag
    with tempfile.NamedTemporaryFile(delete=False) as f_in:
        f_in.write(combined)
        f_in_path = f_in.name
    f_out_path = f_in_path + ".dec"
    try:
        result = subprocess.run(
            [
                "openssl", "enc", "-d", "-aes-256-gcm",
                "-K", key.hex(),
                "-iv", nonce.hex(),
                "-in", f_in_path,
                "-out", f_out_path,
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            raise ValueError("Decryption failed — wrong password or tampered data")
        with open(f_out_path, "rb") as f_out:
            return f_out.read()
    finally:
        Path(f_in_path).unlink(missing_ok=True)
        Path(f_out_path).unlink(missing_ok=True)


# ── Public API ────────────────────────────────────────────────

def encrypt_secrets(secrets: dict[str, str], password: str) -> bytes:
    """Encrypt a secrets dict with a password. Returns raw bytes for secrets.enc."""
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    key = _derive_key(password, salt)
    plaintext = json.dumps(secrets).encode("utf-8")
    ciphertext, tag = _aes_gcm_encrypt(key, nonce, plaintext)
    # Format: salt | nonce | tag | ciphertext
    return salt + nonce + tag + ciphertext


def decrypt_secrets(data: bytes, password: str) -> dict[str, str]:
    """Decrypt secrets.enc bytes with a password. Raises ValueError on bad password."""
    if len(data) < _SALT_LEN + _NONCE_LEN + _TAG_LEN + 1:
        raise ValueError("Encrypted data too short")
    offset = 0
    salt = data[offset : offset + _SALT_LEN]; offset += _SALT_LEN
    nonce = data[offset : offset + _NONCE_LEN]; offset += _NONCE_LEN
    tag = data[offset : offset + _TAG_LEN]; offset += _TAG_LEN
    ciphertext = data[offset:]
    key = _derive_key(password, salt)
    plaintext = _aes_gcm_decrypt(key, nonce, ciphertext, tag)
    return json.loads(plaintext.decode("utf-8"))


def create_auth_file(password: str) -> dict:
    """Create auth.json content: salt + password verification hash."""
    salt = os.urandom(_SALT_LEN)
    verify_hash = _derive_verify_hash(password, salt)
    return {
        "salt": salt.hex(),
        "password_hash": verify_hash,
    }


def verify_password(password: str, auth_data: dict) -> bool:
    """Verify a password against stored auth.json data."""
    salt = bytes.fromhex(auth_data["salt"])
    expected = auth_data["password_hash"]
    actual = _derive_verify_hash(password, salt)
    return hmac.compare_digest(actual, expected)
