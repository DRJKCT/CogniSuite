"""
config.py ver 1.0
For CogniFinance ver 0.9.67
Centralized path configuration and atomic secure file I/O.

Directory layout:
    ~/CogniSuite/                        (COGNIFINANCE_BASE_DIR)
        /BudgetData/                     ledger_storage.db
        /BackupData/                     auto encrypted backups
        /SystemState                     state files
        /.SecurityBackup/                all hidden config
"""
import csv
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

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
UI_CONFIG_PATH = STATE_DIR / "ui_config.json"

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
# Windows owner-only folder permissions (the equivalent of Linux 0o700/0o600)
# ---------------------------------------------------------------------------
# On Windows, os.chmod()/os.open() mode bits are ignored (chmod only toggles
# the read-only flag), so the Linux-style calls below protect nothing there.
# Windows uses ACLs instead. For each data directory we:
#   1. grant Full Control to the current user and SYSTEM (inheritable, so
#      files created inside get the same protection -- the 0o600 equivalent),
#   2. remove the inherited ACEs (this drops Users/Administrators), and
#   3. verify we can still write to the directory, rolling back if not.
# SYSTEM is kept so antivirus/OS services keep working; Administrators can
# still take ownership, much as root can read anything on Linux.
# Applied once per directory per user (tracked by a marker in SECURITY_DIR),
# and never allowed to stop the app from starting.
_WIN_ACL_MARKER = SECURITY_DIR / ".windows_acl_applied.json"
_WIN_ACL_VERSION = 1
_SID_RE = re.compile(r"^S-1-\d+(?:-\d+)+$")
_SYSTEM_SID = "S-1-5-18"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_windows_tool(args: list):
    return subprocess.run(
        args, capture_output=True, text=True, timeout=60, creationflags=_NO_WINDOW
    )


def _current_user_sid() -> str | None:
    """SID of the account running the app, via `whoami /user`."""
    r = _run_windows_tool(["whoami", "/user", "/fo", "csv", "/nh"])
    if r.returncode != 0:
        return None
    for row in csv.reader(r.stdout.strip().splitlines()):
        if len(row) >= 2 and _SID_RE.match(row[1].strip()):
            return row[1].strip()
    return None


def _can_use_dir(path: Path) -> bool:
    """True if we can still list and write in the directory."""
    try:
        os.listdir(path)
        with tempfile.TemporaryFile(dir=str(path)):
            pass
        return True
    except OSError:
        return False


def _restrict_dir_to_owner_windows(path: Path, sid: str) -> bool:
    """Owner(+SYSTEM)-only ACL on one directory. Returns True on success.
    Grants BEFORE removing inheritance and verifies access afterwards, so a
    failure can never leave the user locked out of their own data."""
    grants = [f"*{sid}:(OI)(CI)F", f"*{_SYSTEM_SID}:(OI)(CI)F"]
    r = _run_windows_tool(["icacls", str(path), "/grant:r", *grants])
    if r.returncode != 0:
        logger.warning(f"ACL grant failed for {path}: {r.stderr.strip() or r.stdout.strip()}")
        return False
    r = _run_windows_tool(["icacls", str(path), "/inheritance:r"])
    if r.returncode != 0:
        logger.warning(f"ACL inheritance removal failed for {path}: {r.stderr.strip() or r.stdout.strip()}")
        _run_windows_tool(["icacls", str(path), "/inheritance:e"])
        return False
    if not _can_use_dir(path):
        logger.error(f"Owner-only ACL left {path} inaccessible; rolling back.")
        _run_windows_tool(["icacls", str(path), "/inheritance:e"])
        return False
    return True


def _read_acl_marker() -> dict:
    try:
        with open(_WIN_ACL_MARKER, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _apply_windows_acls(dirs) -> None:
    if sys.platform != "win32":
        return
    try:
        user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
        marker = _read_acl_marker()
        done = set()
        if marker.get("version") == _WIN_ACL_VERSION and marker.get("user") == user:
            done = set(marker.get("dirs", []))

        todo = [d for d in dirs if str(d) not in done]
        if not todo:
            return
        sid = _current_user_sid()
        if sid is None:
            logger.warning("Could not determine current user SID; leaving default folder permissions.")
            return
        for d in todo:
            if _restrict_dir_to_owner_windows(d, sid):
                done.add(str(d))
        atomic_secure_write_json(
            _WIN_ACL_MARKER,
            {"version": _WIN_ACL_VERSION, "user": user, "dirs": sorted(done)},
        )
    except Exception as e:
        # Never block startup; default profile permissions still apply.
        logger.warning(f"Could not apply Windows folder permissions: {e}")


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

    # Windows ignores the chmod calls above; apply real ACLs there.
    _apply_windows_acls(
        [BUDGET_DIR, BACKUP_DIR, STATE_DIR, TAX_TEMPLATES_DIR, SECURITY_DIR]
    )


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
