# main.py ver 0.1
# For CogniFinance ver 0.9.67

import os
import sys
import json
import time
import logging
import traceback
from typing import Optional
from PyQt6.QtGui import QGuiApplication
from crypto_envelope import (
    load_envelope, create_new_envelope, unlock_with_primary,
    unlock_with_recovery, change_primary_passphrase, ENVELOPE_PATH
)

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
from config import atomic_secure_write_json

from config import (
    ensure_directories, BUDGET_DIR, DB_PATH,
    UNLOCK_LOCKOUT_PATH, SECURITY_DIR, UI_CONFIG_PATH,
)

from qt_material import apply_stylesheet

try:
    import keyring
except Exception:
    keyring = None

DB_DIR = str(BUDGET_DIR)

SQLCIPHER_COMPATIBILITY = int(os.environ.get("LEDGER_SQLCIPHER_COMPATIBILITY", "4"))

LOCKOUT_STATE_PATH = str(UNLOCK_LOCKOUT_PATH)

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
        atomic_secure_write_json(
            LOCKOUT_STATE_PATH,
            {"attempts": attempts, "locked_until": locked_until},
        )
    except OSError:
        # Best-effort only -- a failure to persist shouldn't crash the app
        pass


def clear_lockout_state() -> None:
    try:
        os.remove(LOCKOUT_STATE_PATH)
    except OSError:
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
    def __init__(self, legacy_mode=False):
        super().__init__()
        self.used_recovery = False
        self.setWindowTitle("Unlock CogniFinance")

        state = load_lockout_state()
        self.attempts = state["attempts"]
        self.locked = False
        self.base_lock_seconds = 5
        self.passphrase = ""
        self.save_to_keyring = False
        self.remaining_seconds = 0
        self.used_recovery = False

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

        if not ENVELOPE_PATH.exists():
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
        success = False

        try:
            unlock_with_primary(passphrase)
            self.passphrase = passphrase
            self.save_to_keyring = self.save_wallet_cb.isChecked()
            self.used_recovery = False
            success = True
        except Exception:
            try:
                unlock_with_recovery(passphrase)
                self.passphrase = passphrase
                self.used_recovery = True
                success = True
            except Exception:
                pass

        if success:
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

        # Double-check the recovered passphrase actually still unlocks
        # this database's envelope -- it normally will, but this guards
        # against a stale escrow file (e.g. the passphrase was changed
        # by some other means without regenerating the recovery key).
        # NOTE: this must check the modern envelope (unlock_with_primary),
        # not test_passphrase_legacy -- a DEK/KEK-envelope database is
        # keyed with the raw DEK, never with a human passphrase directly,
        # so the legacy check would fail here even on a fully successful
        # recovery.

        try:
            unlock_with_recovery(recovered)
        except Exception:
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
            reset_dialog = SetPassphraseDialog(reset_mode=True)
            if reset_dialog.exec() == QDialog.DialogCode.Accepted:
                try:
                    # change_primary_passphrase() already correctly
                    # unwraps the DEK with the OLD primary and re-wraps
                    # it with the NEW one -- no need to hand-roll that
                    # here (the previous version of this code tried to
                    # unwrap env["recovery"] using the recovered PRIMARY
                    # passphrase, which can never work: that slot is
                    # wrapped with a KEK derived from the RECOVERY
                    # passphrase, a different secret entirely).
                    change_primary_passphrase(recovered, reset_dialog.passphrase)

                    # The primary passphrase just changed, so the escrow
                    # file (which encrypts the primary under the recovery
                    # key) must be rewritten too, or the next "Forgot
                    # passphrase?" attempt would recover the now-stale
                    # OLD primary instead of this new one.
                    if recovery_key_used:
                        write_recovery_escrow(DB_PATH, reset_dialog.passphrase, recovery_key_used)

                    final_passphrase = reset_dialog.passphrase
                    save_to_keyring = reset_dialog.save_to_keyring
                    QMessageBox.information(self, "Success", "Passphrase changed successfully.")
                except Exception as e:
                    QMessageBox.warning(self, "Could Not Change Passphrase", str(e))

        self.passphrase = final_passphrase
        self.save_to_keyring = save_to_keyring
        clear_lockout_state()
        super().accept()

def main():
    ensure_directories()
    os.makedirs(DB_DIR, exist_ok=True)
    os.chmod(DB_DIR, 0o700)

    app = QApplication(sys.argv)

    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("cognifinance.cognifinance.1")
    except Exception:
        pass

    #setup stylesheet
    apply_stylesheet(app, theme='light_teal_500.xml')

    base_dir = os.path.dirname(os.path.abspath(__file__))
    icon_path = os.path.join(base_dir, "icon.png")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    db_exists = os.path.exists(DB_PATH)
    envelope_exists = ENVELOPE_PATH.exists()

    dek_hex = None
    save_to_keyring = False
    # The plaintext PRIMARY passphrase, only ever populated when the user
    # has opted in via a checkbox, and only ever used to write it to the
    # OS keyring below -- never logged, never cached anywhere else, and
    # cleared as soon as we're done with it.
    passphrase_to_cache: Optional[str] = None

    # =========================================================================
    # SCENARIO A: First Run (No DB, No Envelope)
    # =========================================================================
    if not db_exists and not envelope_exists:
        setup_dialog = SetPassphraseDialog(reset_mode=False)
        if setup_dialog.exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)

        primary_pwd = setup_dialog.passphrase
        save_to_keyring = setup_dialog.save_to_keyring
        passphrase_to_cache = primary_pwd if save_to_keyring else None

        # Generate recovery key and show it to the user
        recovery_key = generate_recovery_passphrase()
        key_dialog = RecoveryKeyDisplayDialog(recovery_key)
        key_dialog.exec()

        # Create envelope and get DEK
        dek_bytes, _ = create_new_envelope(primary_pwd, recovery_key)
        dek_hex = dek_bytes.hex()

        # Escrow the primary passphrase under the recovery key, so
        # "Forgot passphrase?" has something to actually recover later.
        write_recovery_escrow(DB_PATH, primary_pwd, recovery_key)

        # Initialize the empty DB with the raw DEK
        win_temp = BudgetApp(DB_PATH, dek_hex)
        win_temp.gl.close() # Just let it create the schema and close

    # =========================================================================
    # SCENARIO B: Modern Setup (Envelope Exists)
    # =========================================================================
    elif envelope_exists:
        # Try OS Keyring first
        if keyring is not None:
            try:
                cached_pwd = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_KEY_NAME)
                if cached_pwd:
                    dek_hex = unlock_with_primary(cached_pwd).hex()
                else:
                    logging.info("Keyring lookup returned no cached passphrase (service=%s, username=%s).", KEYRING_SERVICE_NAME, KEYRING_KEY_NAME)
            except Exception as e:
                logging.warning(f"Keyring lookup/unlock failed, falling back to manual unlock: {e!r}")

        if not dek_hex:
            # Prompt user for Primary Passphrase
            dialog = UnlockDialog() # We will need to tweak UnlockDialog slightly, see below
            if dialog.exec() != QDialog.DialogCode.Accepted:
                sys.exit(0)

            if dialog.used_recovery:
                # Deliberately never cache the recovery passphrase itself --
                # it's meant to stay a break-glass secret, not a day-to-day
                # cached credential.
                dek_hex = unlock_with_recovery(dialog.passphrase).hex()
            else:
                dek_hex = unlock_with_primary(dialog.passphrase).hex()
                save_to_keyring = dialog.save_to_keyring
                passphrase_to_cache = dialog.passphrase if save_to_keyring else None

    # =========================================================================
    # COMPUTE WINDOW SIZE FROM SCREEN LOGICAL GEOMETRY
    # =========================================================================

    # Pick the primary screen
    primary_screen = QGuiApplication.primaryScreen()
    geometry = primary_screen.availableGeometry()  # logical pixels, excludes taskbar

    # Default: 85% of available logical area
    frac = 0.85
    win_w = int(geometry.width() * frac)
    win_h = int(geometry.height() * frac)

    # Clamp minimum so the UI never breaks on tiny screens
    win_w = max(win_w, 960)
    win_h = max(win_h, 600)

    # Cap maximum to avoid absurdly wide windows on ultrawide monitors
    win_w = min(win_w, 2200)
    win_h = min(win_h, 1400)

    # Try to load last saved geometry for continuity across restarts.
    ui_config_path = os.path.join(DB_DIR, "ui_config.json")
    last_w, last_h = None, None
    try:
        if os.path.exists(ui_config_path):
            with open(ui_config_path, "r") as f:
                cfg = json.load(f)
            last_w = cfg.get("window_width")
            last_h = cfg.get("window_height")
    except Exception:
        pass

    if last_w and last_h:
        # Use saved size, but clamp to availableGeometry so the window
        # never exceeds the current monitor's bounds (e.g. user was on
        # a larger dual-monitor setup and moved to a laptop).
        win_w = max(min(last_w, geometry.width()), 960)
        win_h = max(min(last_h, geometry.height()), 600)

    if not dek_hex:
        sys.exit(1)

    try:
        win = BudgetApp(DB_PATH, dek_hex, target_width=win_w, target_height=win_h)
        dek_hex = "" # Clear from memory

        if save_to_keyring and keyring is not None and passphrase_to_cache:
            try:
                keyring.set_password(KEYRING_SERVICE_NAME, KEYRING_KEY_NAME, passphrase_to_cache)
            except Exception as e:
                logging.warning(f"Failed to save passphrase to OS keyring: {e}")
                QMessageBox.warning(
                    win, "Keyring Save Failed",
                    f"Your passphrase could not be saved to the OS keyring, "
                    f"so you'll be asked for it again next time.\n\nDetails: {e}"
                )
        passphrase_to_cache = None # Clear from memory regardless of outcome

    except Exception as e:
        QMessageBox.critical(None, "Startup failed", f"Could not open the database:\n{e}")
        sys.exit(1)

    win.layout().activate()

    screen = QGuiApplication.primaryScreen().availableGeometry()

    # Never open bigger than the screen, regardless of the layout's minimum.
    # Also nudge Qt's minimum down so the user can actually shrink it later.
    max_w = screen.width()
    max_h = screen.height()

    if win_w > max_w: win_w = max_w
    if win_h > max_h: win_h = max_h

    win.resize(win_w, win_h)

    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
