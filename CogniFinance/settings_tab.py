"""
settings_tab.py ver 1.1
For CogniFinance ver 0.9.3
Encapsulates the Settings & Preferences tab UI.

v1.1: the Currency control is the ledger's BASE currency (stored in the
ledger DB, not ui_config.json). It can be changed only until the first
transaction is recorded; after that it is shown read-only.
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QGroupBox, QFormLayout,
    QComboBox, QCheckBox, QPushButton, QHBoxLayout, QMessageBox,
    QScrollArea, QFrame
)
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QFont

from price_fetch_settings import PriceFetchSettingsGroup
import ui_config
import simple_mode

# (ISO code, symbol). The ledger accepts any 3-letter code; this is the
# list offered in the UI. A base currency already in the ledger that isn't
# listed here is added to the combo automatically.
BASE_CURRENCY_CHOICES = [
    ("USD", "$"), ("CAD", "$"), ("EUR", "\u20ac"), ("GBP", "\u00a3"),
    ("AUD", "$"), ("NZD", "$"), ("CHF", "CHF"), ("JPY", "\u00a5"),
    ("MXN", "$"), ("INR", "\u20b9"),
]


class SettingsTab(QWidget):
    """Settings and preferences tab."""
    theme_changed = pyqtSignal(str)
    simple_mode_changed = pyqtSignal(bool)
    base_currency_changed = pyqtSignal(str)  # ISO code, after the ledger accepted it

    def __init__(self, parent=None, gl=None):
        super().__init__(parent)
        self.gl = gl
        self.is_simple_mode = simple_mode.load_simple_mode()
        self._init_ui()

    def _init_ui(self):
        # Content lives in a scroll area so this tab never forces the whole
        # window to be taller than the screen (matters at 125%+ display scaling).
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        content = QWidget()
        content.setObjectName("settingsContent")
        content.setStyleSheet("#settingsContent { background: transparent; }")
        scroll.setWidget(content)
        outer.addWidget(scroll)

        layout = QVBoxLayout(content)

        # Header
        title = QLabel("Settings & Preferences")
        title.setFont(QFont("Inter", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: #10B981; margin-bottom: 10px;")
        layout.addWidget(title)

        # General Settings Group
        general_group = QGroupBox("General Settings")
        general_layout = QFormLayout(general_group)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Light Teal", "Dark Mode", "System Default"])
        self.theme_combo.setCurrentText("Light Teal")
        general_layout.addRow("Theme:", self.theme_combo)

        self.date_format_combo = QComboBox()
        self.date_format_combo.addItems(["YYYY-MM-DD", "MM/DD/YYYY", "DD/MM/YYYY"])
        self.date_format_combo.setCurrentText("YYYY-MM-DD")
        general_layout.addRow("Date Format:", self.date_format_combo)

        # Base (reporting) currency. The ledger is the source of truth, so
        # this is NOT saved to ui_config.json -- see _persisted_widgets().
        self.currency_combo = QComboBox()
        for code, symbol in BASE_CURRENCY_CHOICES:
            self.currency_combo.addItem(self._currency_label(code, symbol), code)
        general_layout.addRow("Base currency:", self.currency_combo)

        self.currency_help = QLabel("")
        self.currency_help.setWordWrap(True)
        self.currency_help.setStyleSheet("color: #6B7280; font-size: 11px;")
        general_layout.addRow(self.currency_help)

        layout.addWidget(general_group)

        # Security Settings Group
        security_group = QGroupBox("Security")
        security_layout = QFormLayout(security_group)

        self.auto_lock_check = QCheckBox("Auto-lock after inactivity")
        self.auto_lock_check.setChecked(True)
        security_layout.addRow(self.auto_lock_check)

        self.lock_timeout_combo = QComboBox()
        self.lock_timeout_combo.addItems(["5 minutes", "10 minutes", "15 minutes", "30 minutes"])
        self.lock_timeout_combo.setCurrentText("10 minutes")
        security_layout.addRow("Lock Timeout:", self.lock_timeout_combo)

        self.require_passphrase_backup = QCheckBox("Require passphrase for backups")
        self.require_passphrase_backup.setChecked(True)
        security_layout.addRow(self.require_passphrase_backup)

        layout.addWidget(security_group)

        # Import/Export Settings
        import_group = QGroupBox("Import & Export")
        import_layout = QFormLayout(import_group)

        self.auto_categorize_check = QCheckBox("Auto-categorize transactions using rules")
        self.auto_categorize_check.setChecked(True)
        import_layout.addRow(self.auto_categorize_check)

        self.default_import_account = QComboBox()
        self.default_import_account.addItems(["Auto-detect", "Checking Account", "Credit Card"])
        import_layout.addRow("Default Import Account:", self.default_import_account)

        layout.addWidget(import_group)

        # Backup Settings
        backup_group = QGroupBox("Backup Settings")
        backup_layout = QFormLayout(backup_group)

        self.auto_backup_check = QCheckBox("Enable automatic backups")
        self.auto_backup_check.setChecked(True)
        backup_layout.addRow(self.auto_backup_check)

        self.backup_interval_combo = QComboBox()
        self.backup_interval_combo.addItems(["Every 4 hours", "Every 8 hours", "Daily", "Weekly"])
        self.backup_interval_combo.setCurrentText("Every 4 hours")
        backup_layout.addRow("Backup Interval:", self.backup_interval_combo)

        self.backup_location_btn = QPushButton("Change Backup Location...")
        backup_layout.addRow(self.backup_location_btn)

        layout.addWidget(backup_group)

        # --- Simple Mode ---
        mode_group = QGroupBox("Simple Mode")
        mode_layout = QVBoxLayout(mode_group)

        self.simple_mode_check = QCheckBox("Show the simplified view of CogniFinance")
        self.simple_mode_check.setChecked(self.is_simple_mode)
        self.simple_mode_check.toggled.connect(self._on_simple_mode_toggled)
        mode_layout.addWidget(self.simple_mode_check)

        mode_help = QLabel(
            "Simple Mode collapses some screens into a plainer view. It "
            "changes what you see, never what is recorded: your "
            "transactions, patterns, budgets, reconciliations and audit "
            "trail are identical in either mode, and switching back and "
            "forth never asks you to redo anything."
        )
        mode_help.setWordWrap(True)
        mode_layout.addWidget(mode_help)

        self.simple_mode_status = QLabel("")
        self.simple_mode_status.setWordWrap(True)
        self.simple_mode_status.setStyleSheet("color: #6B7280; font-size: 11px;")
        mode_layout.addWidget(self.simple_mode_status)

        # Reversibility: the reconciliation tab can be hidden in Simple
        # Mode, but must stay reachable through this toggle.
        self.show_reconcile_check = QCheckBox("Show Reconciliation tab")
        self.show_reconcile_check.setChecked(True)
        self.show_reconcile_check.toggled.connect(
            lambda checked: None  # BudgetApp reads state directly
        )
        mode_layout.addWidget(self.show_reconcile_check)

        layout.addWidget(mode_group)
        self._refresh_simple_mode_status()

        # Online price refresh (opt-in; stored in the ledger DB, applies immediately)
        if self.gl is not None:
            self.price_fetch_group = PriceFetchSettingsGroup(self.gl)
            layout.addWidget(self.price_fetch_group)

        # Save button
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        save_btn = QPushButton("Save Settings")

        save_btn.clicked.connect(self._save_settings)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

        layout.addStretch()
        self._load_settings()
        self.refresh_currency()

    # ------------------------------------------------------------------
    # Persistence (ui_config.json -> "settings"). The theme is NOT stored
    # here: it already travels through theme_changed -> ui_main, which saves
    # and restores it itself.
    # ------------------------------------------------------------------
    _SECTION = "settings"

    def _persisted_widgets(self):
        # The base currency lives in the ledger, not here.
        return {
            "date_format": self.date_format_combo,
            "auto_lock": self.auto_lock_check,
            "lock_timeout": self.lock_timeout_combo,
            "require_passphrase_backup": self.require_passphrase_backup,
            "auto_categorize": self.auto_categorize_check,
            "default_import_account": self.default_import_account,
            "auto_backup": self.auto_backup_check,
            "backup_interval": self.backup_interval_combo,
        }

    def _load_settings(self):
        """Apply saved values. Unknown/invalid values are ignored so a stale
        or hand-edited file can never put a control into an impossible state."""
        saved = ui_config.load_section(self._SECTION)
        for key, widget in self._persisted_widgets().items():
            if key not in saved:
                continue
            value = saved[key]
            if isinstance(widget, QCheckBox):
                if isinstance(value, bool):
                    widget.setChecked(value)
            elif isinstance(widget, QComboBox):
                if isinstance(value, str) and widget.findText(value) >= 0:
                    widget.setCurrentText(value)

    def _collect_settings(self) -> dict:
        values = {}
        for key, widget in self._persisted_widgets().items():
            if isinstance(widget, QCheckBox):
                values[key] = widget.isChecked()
            else:
                values[key] = widget.currentText()
        return values

    def _save_settings(self):
        """Save settings and emit theme change signal."""
        if not self._apply_base_currency():
            return
        try:
            ui_config.update_section(self._SECTION, self._collect_settings())
        except Exception as e:
            QMessageBox.warning(self, "Settings Not Saved",
                f"Your settings could not be saved:\n{e}")
            return

        theme_text = self.theme_combo.currentText()

        if "Dark" in theme_text:
            self.theme_changed.emit("dark")
        elif "System" in theme_text:
            self.theme_changed.emit("system")
        else:
            self.theme_changed.emit("light")

        QMessageBox.information(self, "Settings Saved",
            "Settings have been saved successfully.\n\n"
            "Note: Some settings may require restarting the application.")

    # ------------------------------------------------------------------
    # Base currency (ledger-backed)
    # ------------------------------------------------------------------

    @staticmethod
    def _currency_label(code: str, symbol: str = "") -> str:
        return f"{code} ({symbol})" if symbol else code

    def _selected_currency_code(self) -> str:
        code = self.currency_combo.currentData()
        return code if isinstance(code, str) else self.currency_combo.currentText()[:3].upper()

    def _ledger_has_transactions(self) -> bool:
        return self.gl is not None and bool(getattr(self.gl, "transactions", None))

    def refresh_currency(self):
        """Re-read the base currency from the ledger and set the lock state.
        Safe to call any time (BudgetApp can call it after imports/posting)."""
        if self.gl is None:
            self.currency_combo.setEnabled(False)
            self.currency_help.setText("Open a ledger to set the base currency.")
            return
        code = self.gl.get_base_currency()
        idx = self.currency_combo.findData(code)
        if idx < 0:
            self.currency_combo.addItem(self._currency_label(code), code)
            idx = self.currency_combo.findData(code)
        self.currency_combo.blockSignals(True)
        self.currency_combo.setCurrentIndex(idx)
        self.currency_combo.blockSignals(False)

        locked = self._ledger_has_transactions()
        self.currency_combo.setEnabled(not locked)
        if locked:
            self.currency_help.setText(
                f"Your books are kept in {code}. The base currency is fixed once "
                "the first transaction is recorded, because every recorded amount "
                "is stored in it."
            )
            self.currency_combo.setToolTip("Can't be changed after transactions exist.")
        else:
            self.currency_help.setText(
                "All reports and balances are shown in this currency. You can "
                "change it until you record your first transaction."
            )
            self.currency_combo.setToolTip("")

    def _apply_base_currency(self) -> bool:
        """Push the chosen base currency into the ledger. Returns False if
        the save should stop (user cancelled, or the ledger refused)."""
        if self.gl is None or not self.currency_combo.isEnabled():
            return True
        code = self._selected_currency_code()
        if code == self.gl.get_base_currency():
            return True

        answer = QMessageBox.question(
            self, "Change Base Currency",
            f"Set your base currency to {code}?\n\n"
            "You can change this only until you record your first transaction. "
            "After that it is permanent for this ledger.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self.refresh_currency()
            return False

        ok, msg = self.gl.set_base_currency(code)
        if not ok:
            QMessageBox.warning(self, "Base Currency Not Changed", msg)
            self.refresh_currency()
            return False
        self.refresh_currency()
        self.base_currency_changed.emit(code)
        return True

    def showEvent(self, event):
        # Transactions may have been recorded since the tab was built.
        self.refresh_currency()
        super().showEvent(event)

    # ------------------------------------------------------------------
    # Simple Mode
    # ------------------------------------------------------------------

    def _on_simple_mode_toggled(self, checked: bool):
        self.is_simple_mode = bool(checked)
        self._refresh_simple_mode_status()
        self.simple_mode_changed.emit(bool(checked))

    def set_simple_mode(self, simple: bool):
        simple = bool(simple)
        if simple == self.is_simple_mode:
            return
        self.is_simple_mode = simple
        self.simple_mode_check.blockSignals(True)
        self.simple_mode_check.setChecked(simple)
        self.simple_mode_check.blockSignals(False)
        self._refresh_simple_mode_status()

    def _refresh_simple_mode_status(self):
        self.simple_mode_status.setText(simple_mode.describe_unsimplified())
