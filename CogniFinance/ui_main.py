# ui_main.py ver 1.1 Refactored to remove majority of functions not related to UI
# For CogniFinance ver 1.95

import os
import io
import json
import threading
import urllib.error
import urllib.request
import logging
import qrcode
import time
import html_renderers
from typing import List, Dict, Any, Optional
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QPushButton, QFileDialog, QMessageBox,
    QGridLayout, QLineEdit, QFormLayout, QScrollArea, QSplitter,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView,
    QComboBox, QDateEdit, QGroupBox, QDialog, QDialogButtonBox, QSizePolicy, QAbstractItemView, QStyledItemDelegate, QTreeWidget, QTreeWidgetItem, QProgressDialog, QStyleOptionViewItem, QStyle
)
from PyQt6.QtCore import Qt, QDate, QEvent, QTimer
from PyQt6.QtGui import QColor, QFont, QPixmap, QImage, QDoubleValidator, QStandardItemModel, QStandardItem, QPalette, QBrush
from pathlib import Path
from datetime import datetime

from ledger import GeneralLedger, Budget, ZERO
from parser_integration import parse_statement_to_transactions
from tax_manager import TaxTemplateManager
from pdf_import import PDFImportThread
from decimal import Decimal
from notification_center import NotificationCenter, NotificationStore, Severity
from notification_widgets import NotificationBell

from log_tab import LogTab
from audit_tab import AuditTab
from tax_tab import TaxTab
from ofx_import import parse_ofx_to_ledger_transactions, OFXImportThread
from TestRunnerManager import TestRunnerManager
from graphs import ChartsTab
from category_rules_tab import CategoryRulesTab
from backup_manager import BackupManager
from ui_helpers import (
    add_refresh_row, setup_table_headers, fit_table_columns,
    money_item, get_transaction_display_amount, create_account_combo
)
from transaction_dialogs import AddTransactionDialog, EditTransactionDialog
from ui_transactions_tab import TransactionsTab
from budgets_tab import BudgetsTab
from add_edit_tab import AddEditTab
from import_manager import ImportManager
from sync_tab import SyncTab
from reports_tab import ReportsTab
from reconcile_tab import ReconcileTab
from config import NOTIFICATIONS_PATH, BACKUP_DIR

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

logger = logging.getLogger(__name__)

class BudgetApp(QMainWindow):

    def __init__(self, db_path: str, dek_hex: str):
        super().__init__()
        self.gl = GeneralLedger(db_path, dek_hex)
        self._tx_table_id_map = {}
        self.db_dir = os.path.dirname(self.gl.db_path)
        store_path = NOTIFICATIONS_PATH
        self.notifications = NotificationCenter(NotificationStore(store_path))
        self.notifications.clear_resolved()
        self.backup_manager = BackupManager(self.db_dir)
        self.import_manager = ImportManager(self.gl, self)
        self.import_manager.data_imported.connect(self._refresh_all_data)

        # Mobile sync state.
        self.setWindowTitle("CogniFinance")
        self.resize(1100, 750)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        self.tab_overview = QWidget()
        self.tab_reconcile = ReconcileTab(self.gl)
        self.tab_transactions = TransactionsTab(self.gl)
        self.tab_transactions2 = AddEditTab(self.gl)
        self.tab_budgets = BudgetsTab(self.gl)
        self.tab_charts = ChartsTab(self.gl)
        self.tab_reports = ReportsTab(self.gl)
        self.tab_accounts = QWidget()
        self.tab_balance = QWidget()
        self.tab_rules = CategoryRulesTab(self.gl)
        self.tab_taxes = TaxTab(self.gl)
        self.tab_sync = SyncTab(self.gl)
        self.tab_log = LogTab()
        self.tab_audit = AuditTab(db_path=self.gl.db_path, dek_hex=self.gl.dek_hex)

        self.tabs.addTab(self.tab_overview, "Overview")
        self.tabs.addTab(self.tab_reconcile, "Reconciliation")
        self.tabs.addTab(self.tab_transactions, "Transactions")
        self.tabs.addTab(self.tab_transactions2, "Add/Edit")
        self.tabs.addTab(self.tab_budgets, "Budgets")
        self.tabs.addTab(self.tab_charts, "Charts")
        self.tabs.addTab(self.tab_reports, "Reports")
        self.tabs.addTab(self.tab_accounts, "Accounts")
        self.tabs.addTab(self.tab_balance, "Balance Sheet")
        self.tabs.addTab(self.tab_rules, "Category Rules")
        self.tabs.addTab(self.tab_taxes, "Taxes")
        self.tabs.addTab(self.tab_sync, "Mobile Sync")
        self.tabs.addTab(self.tab_log, "Logs")
        self.tabs.addTab(self.tab_audit, "Audit Log")

        # Connect Add/Edit tab signals
        self.tab_transactions2.history_requested.connect(self.tab_audit.show_history_for_tx_id)
        self.tab_transactions2.data_modified.connect(self._refresh_all_data)

        self.tab_sync.data_synced.connect(self._refresh_all_data)

        self._init_overview()
        self._init_accounts()
        self._init_balance_sheet()

        logging.getLogger().addHandler(self.tab_log.log_handler)

        base_dir = os.path.dirname(os.path.abspath(__file__))
        test_dir = os.path.join(base_dir, "test")
        req_path = os.path.join(base_dir, "requirements.txt")

        self.test_manager = TestRunnerManager(self.db_dir, test_dir, req_path, self.gl.dek_hex, self)
        self.test_manager.log_output.connect(self._log_audit_message)
        self.test_manager.audit_completed.connect(self._on_audit_completed)
        self.test_manager.supply_chain_warning.connect(self._handle_supply_chain_warning)


        QTimer.singleShot(5000, self._start_audit_sequence)

        self.tabs.currentChanged.connect(self._refresh_current_tab)

        self.backup_timer = QTimer(self)
        self.backup_timer.timeout.connect(lambda: self._create_safety_backup("periodic"))
        self.backup_timer.start(4 * 60 * 60 * 1000)

    def safe_trigger_audit(self):
        """Safely initiates scheduled integrity checks."""
        self._start_audit_sequence()

    def _create_safety_backup(self, trigger: str = "manual"):
        """Bridge interface for timed/event-based silent backups."""
        self._run_silent_auto_backup(trigger=trigger)

    def _start_audit_sequence(self):
        """Wrapper to show the UI notification before kicking off the manager."""
        if getattr(self, 'sync_server', None) is not None:
            QTimer.singleShot(30000, self.safe_trigger_audit)
            self.statusBar().showMessage("Integrity audit deferred: Mobile sync currently active.", 4000)
            return

        self.tab_sync.set_start_button_enabled(False)

        self.notifications.add(
            Severity.INFO,
            "Running Background Integrity Checks",
            "CogniFinance is verifying your ledger's math and security in the background. "
            "Program might experience slow down during tests.",
            "integrity_check_running"
        )
        self.test_manager.start_scheduled_audit()

    def _handle_supply_chain_warning(self, total: int, critical: int, details: list):
        """Routes pip-audit findings to the Notification Center."""
        if critical > 0:
            severity = Severity.CRITICAL
            title = "Critical Supply Chain Vulnerabilities Detected"
            msg = (f"{critical} dependencies have critical vulnerabilities with no available patch. "
                f"({total} total vulnerabilities found). Please review the Log Tab and update dependencies immediately.")
        else:
            severity = Severity.WARNING
            title = "Dependency Vulnerabilities Found"
            msg = f"{total} dependencies have known vulnerabilities. Please run 'pip-audit' and update your requirements.txt."

        self.notifications.add(severity, title, msg, "supply_chain_audit")

    def _log_audit_message(self, message: str):
        """Routes the test output directly into the Log Tab."""
        if not message.strip():
            return

        if message.startswith(("platform ", "rootdir:", "configfile:", "plugins:", "cache_dir:")):
            return

        if message.startswith("collected "):
            return

        if message.startswith("===") and not any(k in message.lower() for k in ["passed", "failed", "error"]):
            return

        if any(keyword in message for keyword in ["FAILED", "ERROR", "CRITICAL", "❌", "___"]):
            logging.error(message)
        else:
            logging.info(message)

    def _on_audit_completed(self, success: bool, message: str):
        """Called when the entire audit sequence (Weekly + Deep) finishes."""
        self.notifications.resolve("integrity_check_running")
        self.tab_sync.set_start_button_enabled(True)

        if success:
            if "No audits currently due" not in message:
                self.notifications.resolve("integrity_check_complete")
                self.notifications.add(
                    Severity.INFO,
                    "Integrity Checks Complete",
                    message,
                    "integrity_check_complete"
                )
            self._log_audit_message(f"✅ {message}")
            self.notifications.resolve("weekly_integrity_check")
            self.notifications.resolve("deep_integrity_check")
        else:
            self.notifications.add(
                Severity.CRITICAL,
                "Integrity Check Failed",
                message,
                "integrity_check_failed"
            )
            self._log_audit_message(f"❌ {message}")
            QMessageBox.critical(
                self,
                "CRITICAL LEDGER FAILURE",
                f"The background integrity check FAILED.\n\n{message}\n\n"
                "Please open the 'Logs' tab to see exactly which invariants were violated."
            )


    def _refresh_current_tab(self, index):
        """Refresh only tabs whose primary purpose is displaying ledger data."""
        refreshers = {
            self.tab_overview: self.refresh_overview,
            self.tab_accounts: self.refresh_accounts_table,
            self.tab_transactions: self.tab_transactions.refresh_data,
            self.tab_transactions2: self.tab_transactions2.refresh_data,
            self.tab_budgets: self.tab_budgets.refresh_budget_list,
            self.tab_charts: self.tab_charts.refresh_charts,
            self.tab_reports: self.tab_reports.refresh_data,
            self.tab_reconcile: self.tab_reconcile.refresh_data,
            self.tab_balance: self.refresh_balance_sheet,
            self.tab_taxes: self.tab_taxes.refresh_summary,
            self.tab_audit: self.tab_audit.refresh,
        }

        widget = self.tabs.widget(index)
        refresher = refreshers.get(widget)

        if refresher is not None:
            refresher()

        if widget is not None:
            for table in widget.findChildren(QTableWidget):
                fit_table_columns(table)

    def _refresh_all_data(self):
        """Master refresh triggered when data is modified in a tab."""
        self.tab_transactions.refresh_data()
        self.refresh_overview()
        self.refresh_accounts_table()
        if hasattr(self, 'tab_budgets'):
            self.tab_budgets.refresh_budget_list()

    def _position_notification_bell(self):
        """Pins the notification bell to the top-right corner of the HTML area."""
        if not hasattr(self, 'overview_text') or not hasattr(self, 'notification_bell'):
            return

        padding = 15
        x = self.overview_text.width() - self.notification_bell.width() - padding
        y = padding

        self.notification_bell.move(x, y)
        self.notification_bell.raise_()

    def eventFilter(self, obj, event):
        """Intercepts resize events to keep the bell pinned to the corner."""
        if obj == getattr(self, 'overview_text', None) and event.type() == QEvent.Type.Resize:
            self._position_notification_bell()

        return super().eventFilter(obj, event)

    def _init_accounts(self):
        layout = QVBoxLayout(self.tab_accounts)

        info = QLabel("Manage your Chart of Accounts. Categories used in CogniCapture and Budgets are 'Expense' accounts.")
        info.setWordWrap(True)
        layout.addWidget(info)

        accounts_box = QGroupBox("Chart of Accounts")
        box_layout = QVBoxLayout(accounts_box)

        self.accounts_table = QTableWidget()
        self.accounts_table.setColumnCount(2)
        self.accounts_table.setHorizontalHeaderLabels(["Account Name", "Type"])
        setup_table_headers(self.accounts_table, [0.7, 0.3])
        box_layout.addWidget(self.accounts_table)

        btn_row = QHBoxLayout()
        btn_add = QPushButton("Add New Account")
        btn_add.clicked.connect(self.add_new_account)
        btn_edit = QPushButton("Edit Selected")
        btn_edit.clicked.connect(self.edit_selected_account)
        btn_delete = QPushButton("Delete Selected")
        btn_delete.clicked.connect(self.delete_selected_account)

        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_edit)
        btn_row.addWidget(btn_delete)
        btn_row.addStretch()
        box_layout.addLayout(btn_row)

        layout.addWidget(accounts_box)

        self.refresh_accounts_table()

    def refresh_accounts_table(self):
        self.accounts_table.setRowCount(len(self.gl.accounts))
        for row, (name, acc) in enumerate(sorted(self.gl.accounts.items())):
            self.accounts_table.setItem(row, 0, QTableWidgetItem(name))
            self.accounts_table.setItem(row, 1, QTableWidgetItem(acc.account_type))

    def add_new_account(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Add New Account")
        layout = QVBoxLayout(dialog)

        form = QFormLayout()
        name_edit = QLineEdit()
        type_combo = QComboBox()
        for t in ["Asset", "Liability", "Equity", "Revenue", "Expense"]:
            type_combo.addItem(t)
        type_combo.setCurrentText("Expense")

        form.addRow("Account Name:", name_edit)
        form.addRow("Account Type:", type_combo)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            name = name_edit.text().strip()
            acc_type = type_combo.currentText()
            if not name:
                QMessageBox.warning(self, "Error", "Account name cannot be empty.")
                return
            ok, msg = self.gl.create_account(name, acc_type)
            if ok:
                self.refresh_accounts_table()
                self.refresh_account_combos()
                QMessageBox.information(self, "Success", msg)
            else:
                QMessageBox.warning(self, "Error", msg)

    def delete_selected_account(self):
        selected = self.accounts_table.selectedItems()
        if not selected:
            QMessageBox.information(self, "No selection", "Please select an account to delete.")
            return
        name = selected[0].text()
        ok, msg = self.gl.delete_account(name)
        if ok:
            self.refresh_accounts_table()
            self.refresh_account_combos()
            QMessageBox.information(self, "Success", msg)
        else:
            QMessageBox.warning(self, "Error", msg)

    def edit_selected_account(self):
        selected = self.accounts_table.selectedItems()
        if not selected:
            QMessageBox.information(self, "No selection", "Please select an account to edit.")
            return

        old_name = selected[0].text()
        acc = self.gl.accounts.get(old_name)
        if not acc:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Edit Account")
        layout = QVBoxLayout(dialog)

        form = QFormLayout()
        name_edit = QLineEdit(old_name)
        type_combo = QComboBox()
        for t in ["Asset", "Liability", "Equity", "Revenue", "Expense"]:
            type_combo.addItem(t)
        type_combo.setCurrentText(acc.account_type)

        form.addRow("Account Name:", name_edit)
        form.addRow("Account Type:", type_combo)
        layout.addLayout(form)

        warning = QLabel("<b>Note:</b> Changing the name will automatically update all historical transactions, budgets, and rules associated with this account.")
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #d68910;")
        layout.addWidget(warning)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_name = name_edit.text().strip()
            new_type = type_combo.currentText()

            if not new_name:
                QMessageBox.warning(self, "Error", "Account name cannot be empty.")
                return

            ok, msg = self.gl.update_account(old_name, new_name, new_type)
            if ok:
                self.refresh_accounts_table()
                self.refresh_account_combos()
                self.tab_rules.refresh_rules_table()
                QMessageBox.information(self, "Success", msg)
            else:
                QMessageBox.warning(self, "Error", msg)

    def _init_overview(self):
        layout = QVBoxLayout(self.tab_overview)
        self.overview_text = QTextEdit()
        self.overview_text.setReadOnly(True)
        self.overview_text.setFont(QFont("Monospace", 10))

        self.notification_bell = NotificationBell(self.notifications, self)
        self.notification_bell.setParent(self.overview_text)

        self.overview_text.installEventFilter(self)
        self._position_notification_bell()
        self.notification_bell.raise_()
        self.notification_bell.show()

        layout.addWidget(self.overview_text)
        add_refresh_row(layout, self.refresh_overview)

        btn_row = QHBoxLayout()
        self.btn_import_pdf = QPushButton("Import Statement (PDF)")
        self.btn_import_pdf.clicked.connect(lambda: self.import_manager.start_pdf_import(
            self.import_bank.currentData(),
            self.import_statement_type.currentData() or "auto"
        ))
        statement_row = QHBoxLayout()

        statement_row.addWidget(QLabel("Bank:"))
        self.import_bank = QComboBox()
        self.import_bank.addItem("Auto Detect", None)
        self.import_bank.addItem("CIBC", "cibc")
        self.import_bank.addItem("Tangerine", "tangerine")
        statement_row.addWidget(self.import_bank)

        statement_row.addWidget(QLabel("Statement Type:"))
        self.import_statement_type = QComboBox()
        self.import_statement_type.addItem("Auto Detect", "auto")
        self.import_statement_type.addItem("Credit Card", "credit_card")
        self.import_statement_type.addItem("Bank Account", "bank")
        statement_row.addWidget(self.import_statement_type)

        statement_row.addStretch()
        layout.addLayout(statement_row)

        btn_export = QPushButton("Export Ledger (CSV)")
        btn_export.clicked.connect(self.import_manager.export_ledger_csv)

        btn_import = QPushButton("Import Ledger (CSV)")
        btn_import.clicked.connect(self.import_manager.import_ledger_csv)

        btn_import_ofx = QPushButton("Import OFX/QFX (Bank Feed)")
        btn_import_ofx.clicked.connect(self.import_manager.start_ofx_import)
        btn_manual_backup = QPushButton("Manual Backup to External Drive")
        btn_manual_backup.clicked.connect(self.manual_external_backup)
        btn_row.addWidget(self.btn_import_pdf)
        btn_row.addWidget(btn_import_ofx)
        btn_row.addWidget(btn_import)
        btn_row.addWidget(btn_export)
        btn_row.addWidget(btn_manual_backup)
        btn_restore = QPushButton("Restore from Backup")
        btn_restore.clicked.connect(self.restore_from_backup)
        btn_restore.setStyleSheet("color: #c62828; font-weight: bold;")
        btn_row.addWidget(btn_restore)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.refresh_overview()

    def refresh_overview(self):
        today = QDate.currentDate()
        month = today.toString("yyyy-MM")

        # 1. Fetch Data
        report = self.gl.monthly_income_expense_report(today.year(), today.month())
        budgets = [b for b in self.gl.get_all_budgets() if b.month == month]
        spending = self.gl.spending_by_category(month)
        unreconciled_count = len(self.gl.get_unreconciled_transactions())

        # 2. Prepare Recent Transactions (Pre-calculate display amounts)
        recent_txs_data = []
        for tx in list(reversed(self.gl.transactions[-10:])):
            recent_txs_data.append({
                "date": tx.date,
                "description": tx.description,
                "amount": get_transaction_display_amount(self.gl, tx)
            })

        # 3. Prepare Backup Snippet (Kept in UI layer as it touches BackupManager)
        backup_color, backup_msg = self.backup_manager.get_backup_health(len(self.gl.transactions))
        backup_html_snippet = f'<font color="{backup_color}" size="4">●</font>&nbsp;&nbsp;{backup_msg}'

        # 4. Render and Display
        html = html_renderers.render_overview_html(
            today_str=today.toString('MMMM yyyy'),
            month_str=month,
            report=report,
            budgets=budgets,
            spending=spending,
            recent_txs=recent_txs_data,
            unreconciled_count=unreconciled_count,
            backup_html_snippet=backup_html_snippet
        )
        self.overview_text.setHtml(html)

    def refresh_account_combos(self):
        # Delegate dropdown refreshes to the respective tabs
        if hasattr(self, 'tab_rules') and hasattr(self.tab_rules, '_populate_category_combo'):
            self.tab_rules._populate_category_combo()
        if hasattr(self, 'tab_transactions') and hasattr(self.tab_transactions, 'refresh_account_dropdown'):
            self.tab_transactions.refresh_account_dropdown()
        if hasattr(self, 'tab_budgets') and hasattr(self.tab_budgets, 'refresh_account_dropdown'):
            self.tab_budgets.refresh_account_dropdown()
        if hasattr(self, 'tab_transactions2') and hasattr(self.tab_transactions2, 'refresh_account_dropdown'):
            self.tab_transactions2.refresh_account_dropdown()

    def _init_balance_sheet(self):
        layout = QVBoxLayout(self.tab_balance)
        add_refresh_row(layout, self.refresh_balance_sheet)

        self.balance_text = QTextEdit()
        self.balance_text.setReadOnly(True)
        self.balance_text.setFont(QFont("Monospace", 10))
        layout.addWidget(self.balance_text)

        self.refresh_balance_sheet()

    def refresh_balance_sheet(self):
        # 1. Fetch Data
        bs = self.gl.balance_sheet()

        # 2. Render and Display
        html = html_renderers.render_balance_sheet_html(bs)
        self.balance_text.setHtml(html)

    def update_status_bar(self, percent: int, message: str):
        self.statusBar().showMessage(message)

    def _run_silent_auto_backup(self, trigger: str = "close"):
        """Creates a timestamped encrypted backup in ~/BudgetData/backups/ and prunes backups older than 30 days."""
        try:
            backup_dir = str(BACKUP_DIR)
            os.makedirs(backup_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            backup_path = os.path.join(backup_dir, f"auto_backup_{trigger}_{timestamp}.db")

            self.gl.checkpoint()
            self.gl.create_encrypted_backup(backup_path)

            cutoff_time = time.time() - (30 * 86400)
            for filename in os.listdir(backup_dir):
                file_path = os.path.join(backup_dir, filename)
                if os.path.isfile(file_path) and os.path.getmtime(file_path) < cutoff_time:
                    try:
                        os.remove(file_path)
                    except OSError:
                        pass
        except Exception as e:
            logging.warning(f"Silent auto-backup on {trigger} failed: {e}")
            self.notifications.add(
                Severity.WARNING,
                f"{trigger.replace('_', ' ').title()} Backup Failed",
                f"CogniFinance could not create a safety backup. Your data is safe, "
                f"but not backed up to disk. Error: {e}",
                "backup_failure"
            )

    def manual_external_backup(self):
        """UI wrapper for manual external backup."""
        timestamp = datetime.now().strftime("%Y-%m-%d")
        default_name = f"cognifinance_manual_backup_{timestamp}.db"

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Encrypted Backup", default_name,
            "Database Files (*.db);;All Files (*)"
        )
        if not path:
            return

        success, msg = self.backup_manager.execute_manual_backup(self.gl, path)

        if success:
            self.refresh_overview()
            QMessageBox.information(self, "Backup Successful", f"Encrypted backup saved to:\n{msg}")
        else:
            QMessageBox.critical(self, "Backup Failed", f"Could not save backup:\n{msg}")

    def restore_from_backup(self):
        """UI wrapper for database restore."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Backup to Restore", "",
            "Database Files (*.db *.cfb);;All Files (*)"
        )
        if not path:
            return

        reply = QMessageBox.warning(
            self, "Confirm Restore",
            "Restoring from a backup will OVERWRITE your current database.\n\n"
            "The app will close your current database and force a restart.\n\n"
            "WARNING: Ensure the backup was created with the SAME passphrase.\n\n"
            "Are you sure you want to continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        success, msg = self.backup_manager.execute_restore(self.gl, path)

        if success:
            QMessageBox.information(
                self, "Restore Successful",
                "The database has been successfully restored.\n\n"
                "CogniFinance will now close. Please reopen the application "
                "to load your restored data."
            )
            # Gracefully shut down background threads before quitting
            self.tab_sync.stop_sync()
            QApplication.instance().quit()
        else:
            QMessageBox.critical(self, "Restore Failed", msg)

    def closeEvent(self, event):
        if hasattr(self, 'test_manager'):
            self.test_manager.cleanup()
        self._run_silent_auto_backup("close")
        self.tab_sync.stop_sync()
        try:
            self.gl.close()
        except Exception:
            pass
        super().closeEvent(event)

    def add_new_transaction(self):
        dlg = AddTransactionDialog(self.gl, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_date, new_desc, lines_data = dlg.get_data()
            ok, msg = self.gl.record_transaction(new_date, new_desc, lines_data)
            if not ok:
                QMessageBox.warning(self, "Transaction Not Added", msg)
                return
            self.statusBar().showMessage("Transaction added.", 3000)
            self.tab_transactions2.refresh_data()
            self.tab_transactions.refresh_data()
            self.refresh_overview()
            self.refresh_accounts_table()
