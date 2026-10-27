"""
settings_tab.py ver 1.0
For CogniFinance ver 0.9.3
Encapsulates the Settings & Preferences tab UI.
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


class SettingsTab(QWidget):
    """Settings and preferences tab."""
    theme_changed = pyqtSignal(str)
    simple_mode_changed = pyqtSignal(bool)

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

        self.currency_combo = QComboBox()
        self.currency_combo.addItems(["USD ($)", "CAD ($)", "EUR (€)", "GBP (£)"])
        self.currency_combo.setCurrentText("CAD ($)")
        general_layout.addRow("Currency:", self.currency_combo)

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

    # ------------------------------------------------------------------
    # Persistence (ui_config.json -> "settings"). The theme is NOT stored
    # here: it already travels through theme_changed -> ui_main, which saves
    # and restores it itself.
    # ------------------------------------------------------------------
    _SECTION = "settings"

    def _persisted_widgets(self):
        return {
            "date_format": self.date_format_combo,
            "currency": self.currency_combo,
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
