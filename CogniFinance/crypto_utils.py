from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
import base64
import os
import secrets
from typing import Optional

KEY_FILE = os.path.join(os.path.expanduser("~"), ".budget_key.key")

# Iteration count for deriving a Fernet key from a human passphrase (as
# opposed to SQLCipher's own, separately-configured KDF for the database
# itself). NIST/OWASP-in-the-ballpark for PBKDF2-HMAC-SHA256 in 2026.
PASSPHRASE_KDF_ITERATIONS = 480_000

def generate_key():
    if not os.path.exists(KEY_FILE):
        key = Fernet.generate_key()
        with open(KEY_FILE, "wb") as f:
            f.write(key)

        os.chmod(KEY_FILE, 0o600)

def load_key():
    if not os.path.exists(KEY_FILE):
        generate_key()

    with open(KEY_FILE, "rb") as f:
        return f.read()

def encrypt_file(input_path: str, output_path: str):
    key = load_key()
    f = Fernet(key)
    with open(input_path, "rb") as fi:
        data = fi.read()
    encrypted = f.encrypt(data)
    with open(output_path, "wb") as fo:
        fo.write(encrypted)

def decrypt_file(input_path: str, output_path: str):
    key = load_key()
    f = Fernet(key)
    with open(input_path, "rb") as fi:
        encrypted = fi.read()
    decrypted = f.decrypt(encrypted)
    with open(output_path, "wb") as fo:
        fo.write(decrypted)


def _derive_fernet_key(passphrase: str, salt: bytes, iterations: int = PASSPHRASE_KDF_ITERATIONS) -> bytes:
    """Derive a Fernet-compatible (32-byte, urlsafe-base64) key from a
    human passphrase and salt via PBKDF2-HMAC-SHA256."""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def encrypt_string_with_passphrase(plaintext: str, passphrase: str, iterations: int = PASSPHRASE_KDF_ITERATIONS) -> dict:
    """
    Encrypt a short string (e.g. a recovery-escrowed database passphrase)
    under a separate passphrase. Returns a dict of the pieces needed to
    decrypt it later -- safe to serialize to JSON and store on disk,
    since the salt and iteration count aren't secret on their own.
    """
    salt = secrets.token_bytes(16)
    key = _derive_fernet_key(passphrase, salt, iterations)
    token = Fernet(key).encrypt(plaintext.encode("utf-8"))
    return {
        "salt": base64.b64encode(salt).decode("ascii"),
        "iterations": iterations,
        "token": token.decode("ascii"),
    }


def decrypt_string_with_passphrase(blob: dict, passphrase: str) -> Optional[str]:
    """
    Reverse of encrypt_string_with_passphrase. Returns None (rather than
    raising) if the passphrase is wrong or the blob is corrupt/tampered,
    since a wrong-passphrase attempt during recovery is an expected,
    ordinary outcome, not an error condition the caller needs a
    traceback for.
    """
    try:
        salt = base64.b64decode(blob["salt"])
        iterations = int(blob["iterations"])
        key = _derive_fernet_key(passphrase, salt, iterations)
        plaintext = Fernet(key).decrypt(blob["token"].encode("ascii"))
        return plaintext.decode("utf-8")
    except (InvalidToken, KeyError, ValueError, TypeError):
        return None
