"""
passphrase_recovery.py ver 0.2
For CogniFinance ver 1.95
Passphrase strength checking and the recovery-passphrase safety net.

How recovery works: SQLCipher only ever has one valid passphrase for a
given database at a time -- there's no native concept of "two passphrases
both unlock this file." So instead of trying to make the database itself
multi-key, we escrow the *primary* passphrase: at setup, a second,
independently strong "recovery passphrase" is generated, and the primary
passphrase is encrypted with it (via crypto_utils' PBKDF2-derived Fernet
helpers) into a small file next to the database. Losing your primary
passphrase but still having the recovery passphrase lets you decrypt that
file to recover it.

This means the recovery passphrase is exactly as sensitive as the
primary one -- anyone with both the recovery passphrase and the escrow
file can recover full access to the database. It should be written down
and stored somewhere as protected as you'd store the database access
itself (e.g. a safe, a password manager separate from this app), not
saved next to the database it's meant to be a fallback for.
"""
import os
import json
import secrets
import hashlib
from pathlib import Path
from typing import Optional, Tuple

from crypto_utils import encrypt_string_with_passphrase, decrypt_string_with_passphrase
from config import RECOVERY_ESCROW_PATH
from config import atomic_secure_write_json

ESCROW_FILENAME = ".recovery_escrow.json"

COMMON_WEAK_PASSPHRASES = {
    "password", "password1", "password123", "123456", "12345678", "123456789",
    "qwerty", "qwerty123", "letmein", "changeme", "admin", "welcome",
    "monkey", "dragon", "iloveyou", "budget", "budget123", "cognifinance",
    "trustno1", "sunshine", "princess", "football", "abc123",
}


def estimate_strength(passphrase: str) -> Tuple[str, str]:
    """
    Returns (level, message). level is one of "weak", "fair", "good",
    "strong". Favors length over complexity rules (a long memorable
    phrase is treated as strong even without symbols), consistent with
    current guidance on passphrase strength -- but a passphrase that's
    short, a known-common choice, or trivially repetitive is always
    flagged weak regardless of what else it contains.
    """
    if not passphrase:
        return "weak", "Enter a passphrase."

    lowered = passphrase.lower().strip()
    if lowered in COMMON_WEAK_PASSPHRASES:
        return "weak", "This is a commonly used passphrase and offers very little protection."

    if len(set(passphrase)) <= 2:
        return "weak", "Too repetitive to be secure."

    length = len(passphrase)
    class_count = sum([
        any(c.islower() for c in passphrase),
        any(c.isupper() for c in passphrase),
        any(c.isdigit() for c in passphrase),
        any(not c.isalnum() for c in passphrase),
    ])

    if length < 8:
        return "weak", "Too short. Use at least 12 characters -- a memorable phrase works well."
    if length < 12:
        return "fair", "A bit short. 12 or more characters is recommended."
    if length >= 20:
        return "strong", "Strong passphrase."
    if length >= 16 and class_count >= 2:
        return "strong", "Strong passphrase."
    if length >= 12 and class_count >= 2:
        return "good", "Good passphrase."
    return "fair", "Consider a longer phrase, or mixing in a number or symbol."


def meets_minimum(passphrase: str) -> bool:
    """Minimum bar required to actually set/change the primary passphrase."""
    level, _ = estimate_strength(passphrase)
    return level in ("fair", "good", "strong")


def generate_recovery_passphrase(groups: int = 5, group_length: int = 4) -> str:
    """
    A cryptographically random, human-transcribable recovery passphrase,
    e.g. "XK7P-M3QN-7VWT-2ZRC-9HJD". Uses secrets (CSPRNG) and an
    alphabet with visually ambiguous characters (0/O, 1/I/L) removed so
    it's easier to copy correctly by hand.
    """
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    parts = ["".join(secrets.choice(alphabet) for _ in range(group_length)) for _ in range(groups)]
    return "-".join(parts)


def escrow_path_for(db_path: str) -> str:
    """
    Namespaced by the target database's own absolute path -- the same
    principle as main.py's KEYRING_KEY_NAME being namespaced by DB_DIR --
    so pointing the app at a different database directory can't
    accidentally read (or silently overwrite) an escrow file that
    actually belongs to a completely different database.
    """
    base = Path(RECOVERY_ESCROW_PATH)
    db_key = hashlib.sha256(os.path.abspath(db_path).encode("utf-8")).hexdigest()[:16]
    return str(base.with_name(f"{base.stem}_{db_key}{base.suffix}"))


def escrow_exists(db_path: str) -> bool:
    return os.path.exists(escrow_path_for(db_path))


def write_recovery_escrow(db_path: str, primary_passphrase: str, recovery_passphrase: str) -> str:
    """Encrypt the primary passphrase under the recovery passphrase and write it to disk, 0600-permissioned."""
    path = escrow_path_for(db_path)
    blob = encrypt_string_with_passphrase(primary_passphrase, recovery_passphrase)
    atomic_secure_write_json(path, blob)
    return path


def read_recovery_escrow(db_path: str, recovery_passphrase: str) -> Optional[str]:
    """
    Attempt to recover the primary passphrase using a recovery
    passphrase. Returns None (rather than raising) if there's no escrow
    file, it's unreadable, or the recovery passphrase is wrong -- all of
    which are ordinary, expected outcomes of a recovery attempt.
    """
    path = escrow_path_for(db_path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, ValueError):
        return None
    return decrypt_string_with_passphrase(blob, recovery_passphrase)


def delete_recovery_escrow(db_path: str) -> None:
    path = escrow_path_for(db_path)
    try:
        os.remove(path)
    except OSError:
        pass
