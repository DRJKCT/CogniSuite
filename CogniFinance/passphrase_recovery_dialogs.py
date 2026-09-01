"""
passphrase_recovery_dialogs.py ver 0.1
For CogniFinance ver 1.8
Qt dialogs for the passphrase-hardening/recovery-key feature. Kept
separate from passphrase_recovery.py so that module's pure logic
(strength checking, escrow encode/decode) stays importable and testable
without a PyQt6 environment.
"""
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QCheckBox,
    QApplication,
)

from passphrase_recovery import estimate_strength, meets_minimum, read_recovery_escrow

_STRENGTH_COLORS = {
    "weak": "#c0392b",
    "fair": "#d68910",
    "good": "#2e7d32",
    "strong": "#1b5e20",
}


class SetupPassphraseDialog(QDialog):
    """Shown on first run (no database exists yet) to choose the primary passphrase."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Create a Passphrase - CogniBudget")
        self.passphrase = ""

        self.info_label = QLabel(
            "This passphrase encrypts your entire budget database. There is no way to "
            "recover your data without it or the recovery key you'll be shown next -- "
            "choose something you can remember, and store the recovery key somewhere safe."
        )
        self.info_label.setWordWrap(True)

        self.passphrase_edit = QLineEdit()
        self.passphrase_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.passphrase_edit.textChanged.connect(self._update_strength)

        self.confirm_edit = QLineEdit()
        self.confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_edit.textChanged.connect(self._update_strength)

        self.strength_label = QLabel("")

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

        form = QFormLayout()
        form.addRow("Passphrase:", self.passphrase_edit)
        form.addRow("Confirm:", self.confirm_edit)

        layout = QVBoxLayout(self)
        layout.addWidget(self.info_label)
        layout.addLayout(form)
        layout.addWidget(self.strength_label)
        layout.addWidget(self.buttons)

    def _update_strength(self):
        passphrase = self.passphrase_edit.text()
        level, message = estimate_strength(passphrase)
        color = _STRENGTH_COLORS.get(level, "#c0392b")
        text = f"<span style='color:{color}'><b>{level.title()}</b> - {message}</span>"

        ok_enabled = (
            meets_minimum(passphrase)
            and passphrase == self.confirm_edit.text()
            and len(passphrase) > 0
        )

        if self.confirm_edit.text() and passphrase != self.confirm_edit.text():
            text += "  <span style='color:#c0392b'>(does not match confirmation)</span>"

        self.strength_label.setText(text)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok_enabled)

    def _on_accept(self):
        self.passphrase = self.passphrase_edit.text()
        self.passphrase_edit.clear()
        self.confirm_edit.clear()
        self.accept()


class RecoveryKeyDisplayDialog(QDialog):
    """Shown once, right after a primary passphrase is set, to display the generated recovery passphrase."""

    def __init__(self, recovery_passphrase: str):
        super().__init__()
        self.setWindowTitle("Your Recovery Key - CogniBudget")
        self.setModal(True)

        info = QLabel(
            "Write this recovery key down and store it somewhere safe and separate from this "
            "computer (e.g. a password manager or a physical safe) -- it's the only way back "
            "into your data if you forget your passphrase. Anyone with this key and access to "
            "your database file can also unlock it, so treat it with the same care as the "
            "passphrase itself."
        )
        info.setWordWrap(True)

        self.key_edit = QLineEdit(recovery_passphrase)
        self.key_edit.setReadOnly(True)
        font = self.key_edit.font()
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        self.key_edit.setFont(font)
        self.key_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)

        copy_button = QPushButton("Copy to Clipboard")
        copy_button.clicked.connect(self._copy_to_clipboard)

        self.confirm_checkbox = QCheckBox("I have saved this recovery key somewhere safe")
        self.confirm_checkbox.toggled.connect(self._update_ok_enabled)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        self.buttons.accepted.connect(self.accept)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

        row = QHBoxLayout()
        row.addWidget(self.key_edit)
        row.addWidget(copy_button)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addLayout(row)
        layout.addWidget(self.confirm_checkbox)
        layout.addWidget(self.buttons)

    def _copy_to_clipboard(self):
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.key_edit.text())

    def _update_ok_enabled(self, checked: bool):
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(checked)

    def reject(self):
        # Force acknowledgment -- this is the only time the recovery key
        # will ever be shown, so closing without confirming isn't allowed
        # to silently discard it.
        pass


class RecoveryUnlockDialog(QDialog):
    """
    Reached via "Forgot your passphrase?" from the normal unlock screen.
    Verifies a recovery passphrase against the escrow file and, if valid,
    hands back the recovered primary passphrase.
    """

    def __init__(self, db_path: str):
        super().__init__()
        self.db_path = db_path
        self.recovered_passphrase: Optional[str] = None
        self.setWindowTitle("Recover Access - CogniBudget")

        info = QLabel("Enter your recovery key to regain access to your database.")
        info.setWordWrap(True)

        self.recovery_edit = QLineEdit()
        self.recovery_edit.setPlaceholderText("Recovery key")
        self.recovery_edit.returnPressed.connect(self._on_accept)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._on_accept)
        self.buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("Recovery key:", self.recovery_edit)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addWidget(self.buttons)

    def _on_accept(self):
        recovery_passphrase = self.recovery_edit.text().strip()
        if not recovery_passphrase:
            self.status_label.setText("Enter your recovery key.")
            return

        primary = read_recovery_escrow(self.db_path, recovery_passphrase)
        self.recovery_edit.clear()

        if primary is None:
            self.status_label.setText("That recovery key didn't work. Please try again.")
            return

        self.recovered_passphrase = primary
        self.accept()
