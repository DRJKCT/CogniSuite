"""
crypto_envelope.py ver 2.0
For CogniFinance ver 0.9.0
Handles the DEK/KEK envelope architecture.
The heavy cryptographic lifting (Argon2id KDF, AES-GCM, mlock, zeroization)
is now handled entirely by the compiled Rust core (`cogni_vault`).
"""
import os
import json
import base64
import logging
from pathlib import Path
import cogni_vault  # The compiled Rust module
from config import SECURITY_DIR, atomic_secure_write_json

logger = logging.getLogger(__name__)
ENVELOPE_PATH = SECURITY_DIR / "ledger_metadata.json"

def generate_dek() -> bytes:
    """Generates a cryptographically secure 256-bit Data Encryption Key."""
    return os.urandom(32)

def load_envelope() -> dict:
    """Loads the envelope from disk. Returns None if it doesn't exist."""
    if not ENVELOPE_PATH.exists():
        return None
    try:
        with open(ENVELOPE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load envelope: {e}")
        return None

def save_envelope(env: dict):
    """Atomically saves the envelope to disk with 0o600 permissions."""
    atomic_secure_write_json(ENVELOPE_PATH, env)

def create_new_envelope(primary_passphrase: str, recovery_passphrase: str) -> tuple[bytes, dict]:
    """
    Generates a new DEK, wraps it with both passphrases via the Rust core,
    and saves the envelope. Returns (dek_bytes, envelope_dict).
    """
    dek = generate_dek()

    primary_salt = os.urandom(16)
    recovery_salt = os.urandom(16)

    # Call Rust to wrap the DEK for the primary passphrase
    primary_nonce_b64, primary_ct_b64 = cogni_vault.wrap_dek_secure(
        dek, primary_passphrase.encode('utf-8'), primary_salt
    )

    # Call Rust to wrap the DEK for the recovery passphrase
    recovery_nonce_b64, recovery_ct_b64 = cogni_vault.wrap_dek_secure(
        dek, recovery_passphrase.encode('utf-8'), recovery_salt
    )

    env = {
        "version": 2,  # Bump version to indicate Rust-backed envelope
        "kdf": "Argon2id",
        "primary": {
            "salt": base64.b64encode(primary_salt).decode('utf-8'),
            "nonce": primary_nonce_b64,
            "ciphertext": primary_ct_b64
        },
        "recovery": {
            "salt": base64.b64encode(recovery_salt).decode('utf-8'),
            "nonce": recovery_nonce_b64,
            "ciphertext": recovery_ct_b64
        }
    }

    save_envelope(env)
    return dek, env

def unlock_with_primary(passphrase: str) -> bytes:
    """Unwraps and returns the DEK using the primary passphrase via Rust."""
    env = load_envelope()
    if not env:
        raise ValueError("No envelope found.")
    salt = base64.b64decode(env["primary"]["salt"])
    # Rust handles Argon2id, AES-GCM, mlock, and zeroization
    # unwrap_dek_secure now returns raw bytes directly — no hex round-trip
    dek = cogni_vault.unwrap_dek_secure(
        env["primary"]["ciphertext"],
        env["primary"]["nonce"],
        passphrase.encode('utf-8'),
        salt
    )
    return dek


def unlock_with_recovery(passphrase: str) -> bytes:
    """Unwraps and returns the DEK using the recovery passphrase via Rust."""
    env = load_envelope()
    if not env:
        raise ValueError("No envelope found.")
    salt = base64.b64decode(env["recovery"]["salt"])
    dek = cogni_vault.unwrap_dek_secure(
        env["recovery"]["ciphertext"],
        env["recovery"]["nonce"],
        passphrase.encode('utf-8'),
        salt
    )
    return dek

def change_primary_passphrase(old_passphrase: str, new_passphrase: str):
    """
    Instantly changes the primary passphrase.
    Unwraps the DEK with the old passphrase, then re-wraps it with the new one.
    """
    env = load_envelope()
    if not env:
        raise ValueError("No envelope found.")

    # 1. Unwrap DEK with old passphrase
    old_salt = base64.b64decode(env["primary"]["salt"])
    dek_bytes = cogni_vault.unwrap_dek_secure(
        env["primary"]["ciphertext"],
        env["primary"]["nonce"],
        old_passphrase.encode('utf-8'),
        old_salt
    )

    # 2. Wrap DEK with new passphrase
    new_salt = os.urandom(16)
    new_nonce_b64, new_ct_b64 = cogni_vault.wrap_dek_secure(
        dek_bytes, new_passphrase.encode('utf-8'), new_salt
    )

    # 3. Update and save envelope
    env["primary"] = {
        "salt": base64.b64encode(new_salt).decode('utf-8'),
        "nonce": new_nonce_b64,
        "ciphertext": new_ct_b64
    }
    save_envelope(env)
