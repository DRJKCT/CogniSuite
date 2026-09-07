# test/conftest.py
import tempfile
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import pytest
import config
import passphrase_recovery
import crypto_envelope
import integrity_checker
import deep_integrity_checker
import backup_manager
import ui_main
import ledger
import TestRunnerManager
import desktop_sync_server
# protocol.py's PairingAttemptLimiter needs no patch -- every test call
# site passes state_path explicitly, so the module-level default is
# never reached.

@pytest.fixture(autouse=True)
def sandbox_security_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)

        # config.py source of truth
        monkeypatch.setattr(config, "SECURITY_DIR", d)
        monkeypatch.setattr(config, "BACKUP_DIR", d / "BackupData")
        monkeypatch.setattr(config, "RECOVERY_ESCROW_PATH", d / ".recovery_escrow.json")
        monkeypatch.setattr(config, "AUDIT_CONFIG_PATH", d / ".cognifinance_audit.json")
        monkeypatch.setattr(config, "SYNC_LOG_PATH", d / ".cognifinance_sync.log")
        monkeypatch.setattr(config, "SYNC_IDENTITY_PATH", d / ".cognifinance_sync_identity.key")
        monkeypatch.setattr(config, "DEEP_COOLDOWN_PATH", d / ".deep_integrity_cooldown.json")
        monkeypatch.setattr(config, "INTEGRITY_COOLDOWN_PATH", d / ".integrity_check_cooldown.json")
        monkeypatch.setattr(config, "PAIRING_LOCKOUT_PATH", d / ".cognifinance_pairing_lockout.json")
        monkeypatch.setattr(config, "TRUST_STORE_PATH", d / ".cognifinance_trusted_devices.json")
        monkeypatch.setattr(config, "PAIRED_PHONE_PATH", d / ".cognifinance_paired_phone.json")
        monkeypatch.setattr(config, "UNLOCK_LOCKOUT_PATH", d / ".unlock_lockout.json")
        monkeypatch.setattr(config, "BACKUP_STATUS_PATH", d / "backup_status.json")
        monkeypatch.setattr(config, "NOTIFICATIONS_PATH", d / "notifications.json")

        # Modules with their own value-imported copy of a path constant
        monkeypatch.setattr(passphrase_recovery, "RECOVERY_ESCROW_PATH", d / ".recovery_escrow.json")
        monkeypatch.setattr(crypto_envelope, "ENVELOPE_PATH", d / "ledger_metadata.json")
        monkeypatch.setattr(crypto_envelope, "SECURITY_DIR", d)
        monkeypatch.setattr(integrity_checker, "INTEGRITY_COOLDOWN_PATH", d / ".integrity_check_cooldown.json")
        monkeypatch.setattr(TestRunnerManager, "AUDIT_CONFIG_PATH", d / ".cognifinance_audit.json")
        monkeypatch.setattr(deep_integrity_checker, "DEEP_COOLDOWN_PATH", d / ".deep_integrity_cooldown.json")
        monkeypatch.setattr(backup_manager, "BACKUP_STATUS_PATH", d / "backup_status.json")
        monkeypatch.setattr(ui_main, "NOTIFICATIONS_PATH", d / "notifications.json")
        monkeypatch.setattr(ui_main, "BACKUP_DIR", d / "BackupData")
        monkeypatch.setattr(ledger, "BACKUP_DIR", d / "BackupData")

        # desktop_sync_server.py aliased imports (call-time usage, patchable)
        monkeypatch.setattr(desktop_sync_server, "SYNC_LOG_PATH", d / ".cognifinance_sync.log")
        monkeypatch.setattr(desktop_sync_server, "PAIRED_PHONE_PATH", d / ".cognifinance_paired_phone.json")
        monkeypatch.setattr(desktop_sync_server, "DEFAULT_IDENTITY_PATH", d / ".cognifinance_sync_identity.key")
        monkeypatch.setattr(desktop_sync_server, "DEFAULT_TRUST_STORE_PATH", d / ".cognifinance_trusted_devices.json")

        yield
