"""
config.py ver 1.0
For CogniFinance ver 1.9
Centralized path configuration and atomic secure file I/O.

Directory layout:
    ~/CogniSuite/                        (COGNIFINANCE_BASE_DIR)
        /BudgetData/                     ledger_storage.db
        /BackupData/                     auto encrypted backups
        /SystemState                     state files
        /.SecurityBackup/                all hidden config
"""
import os
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Base directory (override with COGNIFINANCE_BASE_DIR env var)
# ---------------------------------------------------------------------------
BASE_DIR = Path(
    os.environ.get("COGNIFINANCE_BASE_DIR", os.path.expanduser("~/CogniSuite"))
)

# ---------------------------------------------------------------------------
# Subdirectories
# ---------------------------------------------------------------------------
BUDGET_DIR = BASE_DIR / "BudgetData"
BACKUP_DIR = BASE_DIR / "BackupData"
STATE_DIR = BASE_DIR / "SystemState"
SECURITY_DIR = BASE_DIR / ".SecurityBackup"
TAX_TEMPLATES_DIR = BASE_DIR / "TaxTemplates"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH = BUDGET_DIR / "ledger_storage.db"

# ---------------------------------------------------------------------------
# System State/Config files
# ---------------------------------------------------------------------------
NOTIFICATIONS_PATH = STATE_DIR / "notifications.json"
BACKUP_STATUS_PATH = STATE_DIR / "backup_status.json"

# ---------------------------------------------------------------------------
# Security (all live in .SecurityBackup/)
# ---------------------------------------------------------------------------
AUDIT_CONFIG_PATH = SECURITY_DIR / ".cognifinance_audit.json"
SYNC_LOG_PATH = SECURITY_DIR / ".cognifinance_sync.log"
SYNC_IDENTITY_PATH = SECURITY_DIR / ".cognifinance_sync_identity.key"
DEEP_COOLDOWN_PATH = SECURITY_DIR / ".deep_integrity_cooldown.json"
INTEGRITY_COOLDOWN_PATH = SECURITY_DIR / ".integrity_check_cooldown.json"
RECOVERY_ESCROW_PATH = SECURITY_DIR / ".recovery_escrow.json"
PAIRING_LOCKOUT_PATH = SECURITY_DIR / ".cognifinance_pairing_lockout.json"
TRUST_STORE_PATH = SECURITY_DIR / ".cognifinance_trusted_devices.json"
PAIRED_PHONE_PATH = SECURITY_DIR / ".cognifinance_paired_phone.json"
UNLOCK_LOCKOUT_PATH = SECURITY_DIR / ".unlock_lockout.json"


# ---------------------------------------------------------------------------
# Directory bootstrap
# ---------------------------------------------------------------------------
def ensure_directories() -> None:
    """Create all required directories with restrictive permissions."""
    for d in (BUDGET_DIR, BACKUP_DIR, STATE_DIR, TAX_TEMPLATES_DIR):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    # .SecurityBackup gets 0o700 so only the owner can even list it
    SECURITY_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(SECURITY_DIR, 0o700)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Atomic secure file I/O
# ---------------------------------------------------------------------------
def create_secure_file(path: str | Path, data: bytes) -> None:
    """
    Create a NEW file with 0o600 permissions atomically.
    Raises FileExistsError if the file already exists (O_EXCL).
    Use this for one-time secrets (identity keys, escrow files).
    """
    path = str(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def atomic_secure_write(path: str | Path, data: bytes) -> None:
    """
    Atomically overwrite a file with 0o600 permissions.
    Writes to a .tmp sibling first, then os.replace() for atomicity.
    The temp file is also created with O_EXCL | 0o600 so there is
    never a window where data is readable by other users.
    Use this for files that are updated regularly (cooldowns, state, logs).
    """
    path = os.path.abspath(str(path))
    dirname = os.path.dirname(path)
    os.makedirs(dirname, exist_ok=True)
    tmp = path + ".tmp"
    # Clean up stale temp from a previous crash
    try:
        os.remove(tmp)
    except FileNotFoundError:
        pass
    except PermissionError:
        # On Windows, stale temp might be read-only if previous run failed mid-chmod?
        # Unlikely with O_EXCL 0o600, but safe to ignore.
        pass

    # 2. Open with secure flags: Write-only, Create, Exclusive, Mode 0o600
    # O_EXCL guarantees we are the creator; no symlink following.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        # Ensure data hits physical disk (metadata integrity)
        # os.fsync(fd) # Optional: Uncomment for EXTREME durability (slower).
                       # For envelope keys, durability > speed. Recommend ON.
        os.fsync(fd)
    finally:
        os.close(fd)

    # 3. Atomic Replace
    # Windows: os.replace fails if target is Read-Only.
    try:
        os.replace(tmp, path)
    except PermissionError:
        if sys.platform == "win32":
            # Target is likely Read-Only. Clear attribute and retry.
            try:
                # FILE_ATTRIBUTE_NORMAL = 0x80
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x80)
                os.replace(tmp, path)
            except Exception:
                # Final cleanup attempt
                try: os.remove(tmp)
                except: pass
                raise
        else:
            raise



def atomic_secure_write_json(path: str | Path, obj: object) -> None:
    """Convenience wrapper: serialize to JSON then atomic_secure_write."""
    atomic_secure_write(path, json.dumps(obj, indent=2).encode("utf-8"))
