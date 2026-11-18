"""Cifrado de campos sensibles (cédulas de menores del padrón).

Compatible byte a byte con `backend/src/common/crypto/field-encryption.ts`:
- AES-256-GCM, IV aleatorio de 12 bytes, formato `base64(iv):base64(tag):base64(ct)`.
- Hash determinístico HMAC-SHA256 con subclave HKDF-SHA256 (salt vacío,
  info "padron-cedula-hash", 32 bytes) para deduplicar/buscar.

El hash NO permite recuperar la cédula: solo sirve para comparar.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

IV_LENGTH = 12
KEY_LENGTH = 32
HASH_HKDF_INFO = b"padron-cedula-hash"


def _master_key() -> bytes:
    hex_key = os.environ.get("PADRON_ENCRYPTION_KEY")
    if not hex_key or len(hex_key) != KEY_LENGTH * 2:
        raise RuntimeError("PADRON_ENCRYPTION_KEY no está definida o no tiene el largo esperado (64 caracteres hex).")
    return bytes.fromhex(hex_key)


def _hash_key(master: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=KEY_LENGTH, salt=None, info=HASH_HKDF_INFO).derive(master)


def encrypt_field(plaintext: str) -> str:
    iv = os.urandom(IV_LENGTH)
    sealed = AESGCM(_master_key()).encrypt(iv, plaintext.encode("utf-8"), None)
    ciphertext, tag = sealed[:-16], sealed[-16:]
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return ":".join([b64(iv), b64(tag), b64(ciphertext)])


def decrypt_field(stored: str) -> str:
    parts = stored.split(":")
    if len(parts) < 3 or not all(parts[:3]):
        raise ValueError("Formato de campo cifrado inválido.")
    iv, tag, ct = (base64.b64decode(p) for p in parts[:3])
    return AESGCM(_master_key()).decrypt(iv, ct + tag, None).decode("utf-8")


def hash_field(plaintext: str) -> str:
    return hmac.new(_hash_key(_master_key()), plaintext.encode("utf-8"), hashlib.sha256).hexdigest()
