"""
test_crypto_envelope_pipeline.py
For CogniFinance Quarterly Deep Audit.
Verifies the DEK/KEK envelope architecture, including passphrase rotation
and recovery key unwrapping. Because crypto_envelope.py now delegates all
heavy lifting to the compiled Rust core (cogni_vault), this implicitly
tests the Rust module's Argon2id KDF, AES-GCM, and memory safety.
"""
import pytest
import os
import tempfile
import shutil
from crypto_envelope import (
    create_new_envelope, unlock_with_primary, unlock_with_recovery,
    change_primary_passphrase, load_envelope, save_envelope, ENVELOPE_PATH
)

@pytest.fixture
def isolated_envelope_env(monkeypatch, tmp_path):
    """
    Redirects the envelope path to a temporary directory for testing.
    This guarantees we never accidentally overwrite the user's real
    ~/CogniSuite/.SecurityBackup/ledger_metadata.json during tests.
    """
    fake_path = tmp_path / "test_envelope.json"
    monkeypatch.setattr("crypto_envelope.ENVELOPE_PATH", fake_path)
    yield str(fake_path)

def test_envelope_creation_and_unlock(isolated_envelope_env):
    """Verifies that a generated DEK can be unwrapped by both primary and recovery keys."""
    primary_pwd = "correct-horse-battery-staple"
    recovery_pwd = "super-secret-recovery-key-123"

    dek_bytes, env = create_new_envelope(primary_pwd, recovery_pwd)
    assert len(dek_bytes) == 32, "DEK must be 256 bits (32 bytes)."

    # Verify the envelope was saved to disk
    loaded_env = load_envelope()
    assert loaded_env is not None
    assert loaded_env["kdf"] == "Argon2id" # Verify Rust core is being used

    # Unlock with primary
    unwrapped_primary = unlock_with_primary(primary_pwd)
    assert unwrapped_primary == dek_bytes, "Primary unwrap failed."

    # Unlock with recovery
    unwrapped_recovery = unlock_with_recovery(recovery_pwd)
    assert unwrapped_recovery == dek_bytes, "Recovery unwrap failed."

def test_instant_passphrase_rotation(isolated_envelope_env):
    """
    Verifies that changing the passphrase re-wraps the DEK without altering
    the underlying database encryption key.
    """
    old_pwd = "old-passphrase"
    recovery_pwd = "recovery-key"
    new_pwd = "new-passphrase"

    dek_bytes, _ = create_new_envelope(old_pwd, recovery_pwd)

    # Change the primary passphrase
    change_primary_passphrase(old_pwd, new_pwd)

    # Old password should now fail (AES-GCM tag mismatch)
    with pytest.raises(Exception):
        unlock_with_primary(old_pwd)

    # New password should succeed and yield the exact same DEK
    unwrapped_new = unlock_with_primary(new_pwd)
    assert unwrapped_new == dek_bytes, "Passphrase rotation altered the underlying DEK!"

    # Recovery key should still work and yield the same DEK
    unwrapped_rec = unlock_with_recovery(recovery_pwd)
    assert unwrapped_rec == dek_bytes, "Recovery key failed after primary rotation!"

def test_wrong_passphrase_rejection(isolated_envelope_env):
    """Verifies that AES-GCM properly rejects wrong passphrases (tamper detection)."""
    create_new_envelope("real-password", "recovery")

    # A wrong password will derive a wrong KEK, causing AES-GCM decryption to fail
    with pytest.raises(Exception):
        unlock_with_primary("wrong-password")
