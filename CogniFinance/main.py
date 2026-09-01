# main.py ver 0.1
# For CogniFinance ver 1.8

import os
import sys
import json
import time
import logging
import traceback
from typing import Optional

if "kde" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
    os.environ.setdefault("QT_QPA_PLATFORMTHEME", "kde")

from PyQt6.QtCore import QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from sqlcipher3 import dbapi2 as sqlite3

from ui_main import BudgetApp
from passphrase_recovery import (
    estimate_strength,
    meets_minimum,
    generate_recovery_passphrase,
    escrow_exists,
    write_recovery_escrow,
)
from passphrase_recovery_dialogs import RecoveryKeyDisplayDialog, RecoveryUnlockDialog

from qt_material import apply_stylesheet

# main.py v0.1

try:
    import keyring
except Exception:
    keyring = None

DB_DIR = os.environ.get(
    "COGNIFINANCE_DATA_DIR",
    os.path.expanduser("~/BudgetData"),
)
DB_PATH = os.path.join(DB_DIR, "ledger_storage.db")

SQLCIPHER_COMPATIBILITY = int(os.environ.get("LEDGER_SQLCIPHER_COMPATIBILITY", "4"))

LOCKOUT_STATE_PATH = os.path.join(DB_DIR, ".unlock_lockout.json")

# Keyring service/key names. Namespaced by DB_DIR so switching between
# data directories (via COGNIFINANCE_DATA_DIR) doesn't fetch a cached
# passphrase that belongs to a different ledger.
KEYRING_SERVICE_NAME = "CogniFinance"
KEYRING_KEY_NAME = f"ledger_db::{os.path.abspath(DB_DIR)}"


def load_lockout_state() -> dict:
    """
    Load persisted failed-attempt/lockout state so restarting the app
    can't be used to reset the backoff timer. Missing/corrupt state is
    treated as "no prior attempts".
    """
    try:
        with open(LOCKOUT_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {
                "attempts": int(data.get("attempts", 0)),
                "locked_until": float(data.get("locked_until", 0)),
            }
    except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError):
        return {"attempts": 0, "locked_until": 0}


def save_lockout_state(attempts: int, locked_until: float) -> None:
    try:
        os.makedirs(os.path.dirname(LOCKOUT_STATE_PATH), exist_ok=True)
        with open(LOCKOUT_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"attempts": attempts, "locked_until": locked_until}, f)
        os.chmod(LOCKOUT_STATE_PATH, 0o600)
    except OSError:
        # Best-effort only -- a failure to persist shouldn't crash the app
        pass


def clear_lockout_state() -> None:
    try:
        os.remove(LOCKOUT_STATE_PATH)
    except OSError:
        pass


def test_passphrase(db_path: str, passphrase: str) -> bool:
    if not os.path.exists(db_path):
        return True  # First run / new database
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        escaped = passphrase.replace("'", "''")
        cur.execute(f"PRAGMA key = '{escaped}'")
        cur.execute(f"PRAGMA cipher_compatibility = {SQLCIPHER_COMPATIBILITY}")

        # This triggers the actual page 1 read/decryption check
        cur.execute("SELECT count(*) FROM sqlite_master")
        cur.fetchone()

        return True
    except (sqlite3.DatabaseError, sqlite3.OperationalError, sqlite3.ProgrammingError):
        # Explicitly catch all sqlite/sqlcipher-related exceptions
        return False
    except Exception:
        # Catch any other unexpected system/OS errors
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


_STRENGTH_LEVELS = ["weak", "fair", "good", "strong"]
_STRENGTH_COLORS = {
    "weak": "#c0392b",
    "fair": "#d68910",
    "good": "#2e7d32",
    "strong": "#1b5e20",
}


class SetPassphraseDialog(QDialog):
    """
    Collects and validates a new primary passphrase -- used both for
    first-run setup and for setting a fresh passphrase after a recovery
    (via "reset_mode=True", which only changes the on-screen copy).

    Uses a length-favoring strength check (passphrase_recovery.estimate_strength)
    rather than character-composition rules: a long memorable phrase is
    treated as strong even without a mix of upper/lower/digit/symbol,
    while short or trivially repetitive passphrases are rejected
    regardless of what character classes they contain.
    """

    def __init__(self, reset_mode: bool = False):
        super().__init__()
        self.passphrase = ""
        self.save_to_keyring = False

        if reset_mode:
            self.setWindowTitle("Set a New Passphrase")
            intro = "Choose a new passphrase for your database."
        else:
            self.setWindowTitle("Create Database Passphrase")
            intro = "Welcome! Create a passphrase for your new database."

        self.status_label = QLabel(intro)
        self.status_label.setWordWrap(True)

        self.passphrase_edit = QLineEdit()
        self.passphrase_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.passphrase_edit.setPlaceholderText("New Passphrase")
        self.passphrase_edit.textChanged.connect(self.validate_inputs)

        self.confirm_edit = QLineEdit()
        self.confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_edit.setPlaceholderText("Confirm Passphrase")
        self.confirm_edit.textChanged.connect(self.validate_inputs)

        # Visual strength meter, backed by estimate_strength's length-favoring logic
        self.strength_bar = QProgressBar()
        self.strength_bar.setRange(0, len(_STRENGTH_LEVELS) - 1)
        self.strength_bar.setValue(0)
        self.strength_bar.setTextVisible(False)
        self.strength_bar.setFixedHeight(10)

        self.strength_label = QLabel("Strength: -")

        # Defaults to OFF: caching your passphrase in the OS keyring
        # trades "gated by a memorized secret" for "gated by your OS
        # session" -- a real change to the security model, not a purely
        # cosmetic convenience, so it should be an active opt-in rather
        # than a silent default.
        self.save_wallet_cb = QCheckBox("Save passphrase to OS keyring (Keychain/KWallet/etc.)")
        self.save_wallet_cb.setChecked(False)

        if keyring is None:
            self.save_wallet_cb.setEnabled(False)
            self.save_wallet_cb.setToolTip("Python keyring is not available on this system.")

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

        form = QFormLayout()
        form.addRow("Passphrase:", self.passphrase_edit)
        form.addRow("Strength:", self.strength_bar)
        form.addRow("", self.strength_label)
        form.addRow("Confirm:", self.confirm_edit)
        form.addRow("", self.save_wallet_cb)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status_label)
        layout.addLayout(form)
        layout.addWidget(self.buttons)

    def validate_inputs(self):
        pwd = self.passphrase_edit.text()
        conf = self.confirm_edit.text()

        level, message = estimate_strength(pwd)
        self.strength_bar.setValue(_STRENGTH_LEVELS.index(level) if level in _STRENGTH_LEVELS else 0)

        color = _STRENGTH_COLORS.get(level, "#c0392b")
        text = f"<span style='color:{color}'><b>{level.title()}</b> - {message}</span>"

        is_strong_enough = meets_minimum(pwd)
        is_match = pwd == conf and bool(pwd)

        if conf and not is_match:
            text += "  <span style='color:#c0392b'>(does not match confirmation)</span>"

        self.strength_label.setText(text)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(is_strong_enough and is_match)

    def _on_accept(self):
        # Server-side backstop, not just the button's enabled state: the
        # dialog will not close as "accepted" unless both checks pass,
        # no matter how accept() ends up triggered (e.g. Enter key).
        pwd = self.passphrase_edit.text()
        conf = self.confirm_edit.text()

        if not meets_minimum(pwd) or pwd != conf or not pwd:
            self.validate_inputs()
            return

        self.passphrase = pwd
        self.save_to_keyring = self.save_wallet_cb.isChecked()
        self.passphrase_edit.clear()
        self.confirm_edit.clear()
        self.accept()


class UnlockDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Unlock CogniFinance")

        state = load_lockout_state()
        self.attempts = state["attempts"]
        self.locked = False
        self.base_lock_seconds = 5
        self.passphrase = ""
        self.save_to_keyring = False
        self.remaining_seconds = 0

        # Countdown timer for updating the UI every second
        self.countdown_timer = QTimer(self)
        self.countdown_timer.timeout.connect(self.update_countdown)

        self.status_label = QLabel("Enter your database passphrase.")

        self.passphrase_edit = QLineEdit()
        self.passphrase_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.passphrase_edit.setPlaceholderText("Passphrase")

        self.save_wallet_cb = QCheckBox("Save passphrase to OS keyring (Keychain/KWallet/etc.)")
        self.save_wallet_cb.setChecked(False)

        if keyring is None:
            self.save_wallet_cb.setEnabled(False)
            self.save_wallet_cb.setToolTip("Python keyring is not available on this system.")

        self.forgot_button = QPushButton("Forgot passphrase?")
        self.forgot_button.setFlat(True)
        self.forgot_button.clicked.connect(self._on_forgot_passphrase)
        if not escrow_exists(DB_PATH):
            self.forgot_button.setEnabled(False)
            self.forgot_button.setToolTip("No recovery key was ever set up for this database.")

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow(self.status_label)
        form.addRow("Passphrase:", self.passphrase_edit)
        form.addRow("", self.save_wallet_cb)

        forgot_row = QHBoxLayout()
        forgot_row.addStretch()
        forgot_row.addWidget(self.forgot_button)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(forgot_row)
        layout.addWidget(self.buttons)

        self.passphrase_edit.returnPressed.connect(self.accept)

        # Resume any lockout that was already in progress before the app
        # was closed/restarted, so quitting and relaunching can't be used
        # to bypass the backoff.
        remaining = state["locked_until"] - time.time()
        if remaining > 0:
            self.remaining_seconds = int(remaining) + 1
            self.status_label.setText(
                f"Wrong passphrase. Try again in {self.remaining_seconds} seconds."
            )
            self.start_lockout()

    def reject(self):
        if self.locked:
            return
        super().reject()

    def accept(self):
        if self.locked:
            return

        passphrase = self.passphrase_edit.text().strip()
        if not passphrase:
            self.status_label.setText("Passphrase cannot be empty.")
            return

        self.status_label.setText("Checking passphrase...")

        # This safely returns False on failure rather than crashing
        if test_passphrase(DB_PATH, passphrase):
            self.passphrase = passphrase
            self.save_to_keyring = self.save_wallet_cb.isChecked()
            self.passphrase_edit.clear()
            clear_lockout_state()
            super().accept()
            return

        self.passphrase_edit.clear()
        self.attempts += 1
        self.remaining_seconds = self.base_lock_seconds * self.attempts
        locked_until = time.time() + self.remaining_seconds
        save_lockout_state(self.attempts, locked_until)
        self.status_label.setText(f"Wrong passphrase. Try again in {self.remaining_seconds} seconds.")
        self.start_lockout()

    def start_lockout(self):
        self.locked = True
        self.passphrase_edit.setEnabled(False)
        self.forgot_button.setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(False)
        # Start the 1-second interval ticker
        self.countdown_timer.start(1000)

    def update_countdown(self):
        self.remaining_seconds -= 1
        if self.remaining_seconds > 0:
            self.status_label.setText(f"Wrong passphrase. Try again in {self.remaining_seconds} seconds.")
        else:
            # Timer finished, unlock inputs for a new attempt
            self.countdown_timer.stop()
            self.locked = False
            self.passphrase_edit.setEnabled(True)
            self.forgot_button.setEnabled(escrow_exists(DB_PATH))
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
            self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(True)
            self.status_label.setText("Enter your database passphrase.")
            self.passphrase_edit.setFocus()

    def _on_forgot_passphrase(self):
        if self.locked:
            return

        recovery_dialog = RecoveryUnlockDialog(DB_PATH)
        if recovery_dialog.exec() != QDialog.DialogCode.Accepted:
            return

        recovered = recovery_dialog.recovered_passphrase
        # Double-check the recovered passphrase actually still opens the
        # database -- it normally will, but this guards against a stale
        # escrow file (e.g. the passphrase was changed by some other
        # means without regenerating the recovery key).
        if not recovered or not test_passphrase(DB_PATH, recovered):
            QMessageBox.warning(
                self, "Recovery Failed",
                "Your recovery key was valid, but the recovered passphrase no longer "
                "opens this database. It may be out of date."
            )
            return

        choice = QMessageBox.question(
            self, "Access Recovered",
            "Access recovered successfully. Would you like to set a new passphrase now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )

        final_passphrase = recovered
        save_to_keyring = False

        if choice == QMessageBox.StandardButton.Yes:
            from ledger import GeneralLedger  # local import: avoid a hard dependency at module load time

            reset_dialog = SetPassphraseDialog(reset_mode=True)
            if reset_dialog.exec() == QDialog.DialogCode.Accepted:
                try:
                    gl = GeneralLedger(DB_PATH, recovered)
                    ok, msg = gl.rekey(reset_dialog.passphrase)
                    if not ok:
                        QMessageBox.warning(self, "Could Not Change Passphrase", msg)
                    else:
                        final_passphrase = reset_dialog.passphrase
                        save_to_keyring = reset_dialog.save_to_keyring
                        new_recovery_key = generate_recovery_passphrase()
                        write_recovery_escrow(DB_PATH, final_passphrase, new_recovery_key)
                        key_dialog = RecoveryKeyDisplayDialog(new_recovery_key)
                        key_dialog.exec()
                except Exception as e:
                    QMessageBox.warning(self, "Could Not Change Passphrase", str(e))

        self.passphrase = final_passphrase
        self.save_to_keyring = save_to_keyring
        clear_lockout_state()
        super().accept()


def _setup_new_database(db_path: str) -> Optional[tuple]:
    """
    First-run flow: collect a new primary passphrase, generate a
    recovery passphrase, show it once, and write the escrow file.
    Returns (passphrase, save_to_keyring) or None if cancelled.
    """
    setup_dialog = SetPassphraseDialog(reset_mode=False)
    if setup_dialog.exec() != QDialog.DialogCode.Accepted:
        return None

    passphrase = setup_dialog.passphrase
    save_to_keyring = setup_dialog.save_to_keyring

    recovery_key = generate_recovery_passphrase()
    write_recovery_escrow(db_path, passphrase, recovery_key)

    key_dialog = RecoveryKeyDisplayDialog(recovery_key)
    key_dialog.exec()  # forces acknowledgment before continuing (see dialog's reject())

    return passphrase, save_to_keyring


def main():
    os.makedirs(DB_DIR, exist_ok=True)
    os.chmod(DB_DIR, 0o700)

    app = QApplication(sys.argv)

    #setup stylesheet
    apply_stylesheet(app, theme='light_teal_500.xml')

    base_dir = os.path.dirname(os.path.abspath(__file__))
    icon_path = os.path.join(base_dir, "icon.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    db_exists = os.path.exists(DB_PATH)
    passphrase = None
    save_to_keyring = False

    # 1. Attempt to fetch a cached passphrase silently from the OS keyring.
    if keyring is not None:
        try:
            passphrase = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_KEY_NAME)
        except Exception as e:
            logging.warning(f"Keyring lookup failed: {e}")
            passphrase = None

    if db_exists and passphrase and test_passphrase(DB_PATH, passphrase):
        # Cached passphrase is valid -- bypass the dialogs entirely.
        clear_lockout_state()
    else:
        if not db_exists:
            result = _setup_new_database(DB_PATH)
        else:
            dialog = UnlockDialog()
            if dialog.exec() != QDialog.DialogCode.Accepted:
                sys.exit(0)
            result = (dialog.passphrase, dialog.save_to_keyring)
            dialog.passphrase = ""
            del dialog

        if result is None:
            sys.exit(0)

        passphrase, save_to_keyring = result

        if keyring is not None and save_to_keyring:
            try:
                keyring.set_password(KEYRING_SERVICE_NAME, KEYRING_KEY_NAME, passphrase)
            except Exception as e:
                logging.warning(f"Failed to save to OS keyring: {e}")

    try:
        win = BudgetApp(DB_PATH, passphrase)
        passphrase = ""
        if os.path.exists(DB_PATH):
            os.chmod(DB_PATH, 0o600)
    except Exception as e:
        QMessageBox.critical(None, "Startup failed", f"Could not open the database:\n{e}")
        logging.exception("Startup failed")
        sys.exit(1)

    win.setWindowTitle("CogniFinance")
    if os.path.exists(icon_path):
        win.setWindowIcon(QIcon(icon_path))

    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
