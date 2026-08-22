import pytest
from crypto_utils import encrypt_string_with_passphrase, decrypt_string_with_passphrase
from passphrase_recovery import estimate_strength, generate_recovery_passphrase

# ==========================================
# 1. CRYPTO ROUNDTRIPS
# ==========================================
def test_crypto_roundtrip():
    """Ensure we can encrypt and decrypt a string successfully."""
    passphrase = "SuperSecretRecoveryKey123!"
    plaintext = "MyPrimaryDatabasePassphrase"

    blob = encrypt_string_with_passphrase(plaintext, passphrase)
    assert "salt" in blob
    assert "iterations" in blob
    assert "token" in blob

    decrypted = decrypt_string_with_passphrase(blob, passphrase)
    assert decrypted == plaintext

def test_crypto_wrong_passphrase_returns_none():
    """Ensure a wrong passphrase returns None gracefully instead of crashing."""
    passphrase = "CorrectHorseBatteryStaple"
    plaintext = "Secret"

    blob = encrypt_string_with_passphrase(plaintext, passphrase)

    # This MUST return None, not raise an InvalidToken exception
    decrypted = decrypt_string_with_passphrase(blob, "WrongPassphrase")
    assert decrypted is None

# ==========================================
# 2. SECURITY & STRENGTH ESTIMATOR
# ==========================================
def test_passphrase_strength_estimator():
    """Ensure the length-favoring strength meter works correctly."""
    # Weak: too short
    level, _ = estimate_strength("pass")
    assert level == "weak"

    # Weak: common dictionary word
    level, _ = estimate_strength("password123")
    assert level == "weak"

    # Weak: repetitive
    level, _ = estimate_strength("aaaaaaaaaaaa")
    assert level == "weak"

    # Strong: long memorable phrase (NIST compliant)
    level, _ = estimate_strength("correct horse battery staple")
    assert level == "strong"

def test_recovery_passphrase_generation():
    """Ensure generated recovery keys are formatted correctly and lack ambiguous characters."""
    key = generate_recovery_passphrase()
    parts = key.split("-")

    assert len(parts) == 5
    assert all(len(p) == 4 for p in parts)

    # Ensure no ambiguous characters like 0, O, 1, I, L
    assert "0" not in key and "O" not in key
    assert "1" not in key and "I" not in key and "L" not in key
