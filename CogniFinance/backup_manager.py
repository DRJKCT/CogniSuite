"""
backup_manager.py ver 1.0
For CogniFinance ver 1.9
Handles the core logic for manual external backups, restore operations,
and backup health tracking. Completely isolated from the UI layer.
"""
import os
import json
import time
import shutil
import logging
from datetime import datetime
from ledger import GeneralLedger

logger = logging.getLogger(__name__)

class BackupManager:
    def __init__(self, db_dir: str):
        self.db_dir = db_dir
        self.status_path = os.path.join(self.db_dir, "backup_status.json")

    # ==========================================
    # Health Tracking & State Management
    # ==========================================
    def _load_status(self) -> dict:
        if os.path.exists(self.status_path):
            try:
                with open(self.status_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {"last_manual_backup": 0.0, "tx_count_at_backup": 0}

    def _save_status(self, status: dict):
        try:
            with open(self.status_path, 'w', encoding='utf-8') as f:
                json.dump(status, f, indent=2)
        except OSError as e:
            logger.warning(f"Could not save backup status: {e}")

    def get_backup_health(self, current_tx_count: int) -> tuple[str, str]:
        """Returns (color_hex, html_message) for the Overview tab."""
        status = self._load_status()
        last_ts = status.get("last_manual_backup", 0.0)
        tx_at_backup = status.get("tx_count_at_backup", 0)

        entries_since = max(0, current_tx_count - tx_at_backup)

        if last_ts == 0:
            days_since = 9999
            last_date_str = "Never"
        else:
            days_since = (time.time() - last_ts) / 86400
            last_date_str = datetime.fromtimestamp(last_ts).strftime("%Y-%m-%d")

        # Determine health tier
        if entries_since >= 50 or days_since >= 28:
            color = "#c62828"  # Red
        elif entries_since >= 25 or days_since >= 14:
            color = "#f57c00"  # Amber
        else:
            color = "#2e7d32"  # Green

        # Build detail message
        details = []
        if entries_since > 0:
            details.append(f"{entries_since} new entries")
        if last_ts > 0:
            details.append(f"{int(days_since)} days ago")

        detail_str = ", ".join(details) if details else "No activity since backup"
        msg = f"Last manual backup: <b>{last_date_str}</b> ({detail_str})"

        return color, msg

    def reset_backup_status(self, current_tx_count: int):
        """Resets the counters after a successful manual backup."""
        status = {
            "last_manual_backup": time.time(),
            "tx_count_at_backup": current_tx_count
        }
        self._save_status(status)

    # ==========================================
    # Core Backup & Restore Operations
    # ==========================================
    def execute_manual_backup(self, ledger: GeneralLedger, target_path: str) -> tuple[bool, str]:
        """
        Creates an encrypted backup at the target path.
        Returns (success: bool, message: str).
        """
        try:
            ledger.checkpoint()
            ledger.create_encrypted_backup(target_path)
            self.reset_backup_status(len(ledger.transactions))
            return True, target_path
        except Exception as e:
            return False, str(e)

    def execute_restore(self, ledger: GeneralLedger, source_path: str) -> tuple[bool, str]:
        """
        Safely overwrites the live database with a selected backup file.
        Returns (success: bool, message: str).
        WARNING: If successful, the ledger connection is permanently closed.
        The caller MUST terminate the application immediately.
        """
        live_db = ledger.db_path
        snapshot_path = live_db + ".pre_restore_snapshot"

        # 1. Create safety snapshot
        try:
            ledger.checkpoint()
            shutil.copy2(live_db, snapshot_path)
        except Exception as e:
            return False, f"Could not create safety snapshot of current DB: {e}"

        # 2. Close active connection to release file locks
        try:
            ledger.close()
        except Exception:
            pass

        # 3. Overwrite live DB with the selected backup
        try:
            shutil.copy2(source_path, live_db)

            # CRITICAL: Delete WAL and SHM files to prevent corruption on next launch
            for ext in ["-wal", "-shm"]:
                wal_path = live_db + ext
                if os.path.exists(wal_path):
                    os.remove(wal_path)
        except Exception as e:
            # Rollback: Restore the safety snapshot if the copy fails
            try:
                shutil.copy2(snapshot_path, live_db)
            except Exception:
                pass
            return False, f"Restore failed: {e}. Original DB restored from snapshot."

        # 4. Cleanup snapshot
        try:
            os.remove(snapshot_path)
        except OSError:
            pass

        return True, "Restore successful. App must be restarted."
