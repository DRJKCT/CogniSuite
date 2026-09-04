# ui_main.py ver 0.5 Changed add/edit tab to remove accounting logic within ui_main.  Removed all math validation and left it to ledger.py
# For CogniFinance ver 1.8

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

from desktop_sync_server import CogniSyncServer
from protocol import make_pairing_code
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

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

logger = logging.getLogger(__name__)

class BudgetApp(QMainWindow):

    def __init__(self, db_path: str, passphrase: str):
        super().__init__()
        self.gl = GeneralLedger(db_path, passphrase)
        self._tx_table_id_map = {}
        self.db_dir = os.path.dirname(self.gl.db_path)
        store_path = Path(self.db_dir) / "notifications.json"
        self.notifications = NotificationCenter(NotificationStore(store_path))
        self.notifications.clear_resolved()
        self.backup_manager = BackupManager(self.db_dir)
        self.import_manager = ImportManager(self.gl, self)
        self.import_manager.data_imported.connect(self._refresh_all_data)

        # Mobile sync state.
        self.sync_server = None
        self.sync_thread = None
        self.sync_status_timer = None
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
        self.tab_reconcile = QWidget()
        self.tab_transactions = TransactionsTab(self.gl)
        self.tab_transactions2 = AddEditTab(self.gl)
        self.tab_budgets = BudgetsTab(self.gl)
        self.tab_charts = ChartsTab(self.gl)
        self.tab_reports = QWidget()
        self.tab_accounts = QWidget()
        self.tab_balance = QWidget()
        self.tab_rules = CategoryRulesTab(self.gl)
        self.tab_taxes = TaxTab(self.gl)
        self.tab_sync = QWidget()
        self.tab_log = LogTab()
        self.tab_audit = AuditTab(db_path=self.gl.db_path, passphrase=self.gl.passphrase)

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

        self._init_overview()
        self._init_reconcile()
        self._init_reports_tab()
        self._init_accounts()
        self._init_balance_sheet()
        self._init_sync_tab()

        logging.getLogger().addHandler(self.tab_log.log_handler)

        test_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test")
        self.test_manager = TestRunnerManager(self.db_dir, test_dir, self)
        self.test_manager.log_output.connect(self._log_audit_message)
        self.test_manager.audit_completed.connect(self._on_audit_completed)

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

        self.sync_start_button.setEnabled(False)
        self.sync_start_button.setToolTip("Sync temporarily locked: Background system audit in progress (~1 min).")

        self.notifications.add(
            Severity.INFO,
            "Running Background Integrity Checks",
            "CogniFinance is verifying your ledger's math and security in the background. "
            "Program might experience slow down during tests.",
            "integrity_check_running"
        )
        self.test_manager.start_scheduled_audit()

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
        self.sync_start_button.setEnabled(True)
        self.sync_start_button.setToolTip("Start Mobile Sync")

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
            self.tab_reports: self.refresh_monthly_report,
            self.tab_reconcile: self.load_unreconciled,
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
        self.apply_transactions_tab_filters()
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

        self.accounts_table = QTableWidget()
        self.accounts_table.setColumnCount(2)
        self.accounts_table.setHorizontalHeaderLabels(["Account Name", "Type"])
        setup_table_headers(self.accounts_table, [0.7, 0.3])
        layout.addWidget(self.accounts_table)

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
        layout.addLayout(btn_row)

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

    def _init_reports_tab(self):
        layout = QVBoxLayout(self.tab_reports)
        add_refresh_row(layout, self.refresh_monthly_report)

        month_box = QGroupBox("Month")
        mlayout = QFormLayout()

        current_date = QDate.currentDate()
        current_year = current_date.year()
        current_month = current_date.month()

        self.report_year = QComboBox()
        for y in range(current_year - 5, current_year + 2):
            self.report_year.addItem(str(y), y)
        self.report_year.setCurrentText(str(current_year))

        self.report_month = QComboBox()
        for m in range(1, 13):
            self.report_month.addItem(f"{m:02d}", m)
        self.report_month.setCurrentText(f"{current_month:02d}")

        mlayout.addRow("Year:", self.report_year)
        mlayout.addRow("Month:", self.report_month)
        month_box.setLayout(mlayout)
        layout.addWidget(month_box)

        btn_row = QHBoxLayout()
        btn_export = QPushButton("Export Report (XLSX)")
        btn_export.clicked.connect(self.export_monthly_report)

        btn_row.addWidget(btn_export)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.report_summary = QTextEdit()
        self.report_summary.setReadOnly(True)
        self.report_summary.setFont(QFont("Monospace", 10))
        layout.addWidget(self.report_summary)

        self.refresh_monthly_report()

    def refresh_monthly_report(self):
        year = self.report_year.currentData()
        month = self.report_month.currentData()

        # 1. Fetch Data
        data = self.gl.monthly_income_expense_report(year, month)

        # 2. Render and Display
        html = html_renderers.render_monthly_report_html(year, month, data)
        self.report_summary.setHtml(html)

    def export_monthly_report(self):
        year = self.report_year.currentData()
        month = self.report_month.currentData()

        path, _ = QFileDialog.getSaveFileName(self, "Export Monthly Report", "", "Excel Files (*.xlsx);;All Files (*)")
        if not path:
            return
        if not path.endswith(".xlsx"):
            path += ".xlsx"

        try:
            self.gl.export_monthly_income_expense_xlsx(year, month, path)
            QMessageBox.information(self, "Export complete", f"Monthly report exported to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))

    def _init_reconcile(self):
        layout = QVBoxLayout(self.tab_reconcile)
        add_refresh_row(layout, self.load_unreconciled)

        info = QLabel(
            "Unreconciled transactions (usually from imported statements).\n"
            "Select a row and mark it as reconciled once it matches your ledger."
        )
        layout.addWidget(info)

        self.reconcile_tree = QTreeWidget()
        self.reconcile_tree.setColumnCount(5)
        self.reconcile_tree.setHeaderLabels(["ID", "Date", "Description / Account", "Imported ID / Amount", "Reconciled"])

        header = self.reconcile_tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setHighlightSections(False)

        self.reconcile_tree.setRootIsDecorated(True)
        self.reconcile_tree.setAlternatingRowColors(True)
        layout.addWidget(self.reconcile_tree)

        btn_row = QHBoxLayout()
        btn_mark = QPushButton("Mark Selected as Reconciled")
        btn_mark.clicked.connect(self.mark_selected_reconciled)
        btn_row.addWidget(btn_mark)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.load_unreconciled()

    def load_unreconciled(self):
        txs = self.gl.get_unreconciled_transactions()
        self.reconcile_tree.clear()
        for tx in txs:
            # --- TOP LEVEL ITEM (Main Transaction) ---
            item = QTreeWidgetItem(self.reconcile_tree)
            item.setData(0, Qt.ItemDataRole.UserRole, tx.id) # Store DB ID
            item.setText(0, str(tx.id))
            item.setText(1, tx.date)
            item.setText(2, tx.description)
            item.setText(3, tx.imported_id or "")
            item.setText(4, "Yes" if tx.reconciled else "No")

            # Make it selectable
            flags = item.flags() | Qt.ItemFlag.ItemIsSelectable
            item.setFlags(flags)

            # --- CHILD ITEMS (Split Lines) ---
            for line in tx.lines:
                sub_item = QTreeWidgetItem(item)
                sub_item.setText(2, line.account_name)

                dr_text = f"Dr: {line.dr:.2f}" if line.dr > 0 else ""
                cr_text = f"Cr: {line.cr:.2f}" if line.cr > 0 else ""
                sub_item.setText(3, f"{dr_text}  {cr_text}".strip())

                # Style sub-rows to look like indented split-lines
                sub_color = QColor("#f0f4f8")
                for col in range(5):
                    sub_item.setBackground(col, sub_color)

                # Prevent sub-rows from being selected independently
                sub_item.setFlags(sub_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)


    def mark_selected_reconciled(self):
        item = self.reconcile_tree.currentItem()
        if not item:
            QMessageBox.information(self, "No selection", "Please select a transaction row.")
            return

        # Ensure we are acting on the top-level item (in case user clicked a sub-row)
        while item.parent() is not None:
            item = item.parent()

        tx_id = item.data(0, Qt.ItemDataRole.UserRole)
        if tx_id is not None:
            self.gl.mark_reconciled(tx_id, True)
            self.load_unreconciled()

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

    def _init_sync_tab(self):
        layout = QVBoxLayout(self.tab_sync)

        server_box = QGroupBox("Mobile Sync")
        form = QFormLayout()

        self.sync_state_label = QLabel("Stopped")
        self.sync_state_label.setFont(QFont("Monospace", 10))
        form.addRow("Server:", self.sync_state_label)

        self.sync_address_label = QLabel("—")
        form.addRow("Address:", self.sync_address_label)

        self.sync_pairing_label = QLabel("—")
        self.sync_pairing_label.setFont(QFont("Monospace", 14))
        form.addRow("Pairing code:", self.sync_pairing_label)

        self.sync_fingerprint_label = QLabel("—")
        self.sync_fingerprint_label.setFont(QFont("Monospace", 9))
        form.addRow("Desktop fingerprint:", self.sync_fingerprint_label)

        server_box.setLayout(form)
        layout.addWidget(server_box)

        controls = QHBoxLayout()
        self.sync_start_button = QPushButton("Start Mobile Sync")
        self.sync_start_button.clicked.connect(self.start_mobile_sync)
        controls.addWidget(self.sync_start_button)

        self.sync_stop_button = QPushButton("Stop Sync Server")
        self.sync_stop_button.clicked.connect(self.stop_mobile_sync)
        self.sync_stop_button.setEnabled(False)
        controls.addWidget(self.sync_stop_button)
        controls.addStretch()
        layout.addLayout(controls)

        qr_box = QGroupBox("Scan this QR code with the Android companion")
        qr_layout = QHBoxLayout(qr_box)
        self.sync_qr_label = QLabel("Start Mobile Sync to generate a QR code.")
        self.sync_qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sync_qr_label.setMinimumSize(0, 0)
        self.sync_qr_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        qr_layout.addWidget(self.sync_qr_label, 1)
        layout.addWidget(qr_box, 3)

        status_box = QGroupBox("Sync Status")
        status_layout = QVBoxLayout(status_box)
        self.sync_handshake_label = QLabel("Handshake: —")
        self.sync_peer_label = QLabel("Phone identity: —")
        self.sync_last_cursor_label = QLabel("Last sync cursor: —")
        self.sync_accounts_label = QLabel("Unknown accounts: none")
        self.sync_held_label = QLabel("Held transactions: 0")

        for widget in (
            self.sync_handshake_label,
            self.sync_peer_label,
            self.sync_last_cursor_label,
            self.sync_accounts_label,
            self.sync_held_label,
        ):
            status_layout.addWidget(widget)
        layout.addWidget(status_box)

        account_row = QHBoxLayout()
        self.sync_create_account_button = QPushButton("Create Missing Account(s)")
        self.sync_create_account_button.clicked.connect(self.create_sync_accounts)
        self.sync_create_account_button.setEnabled(False)
        account_row.addWidget(self.sync_create_account_button)

        self.sync_reprocess_button = QPushButton("Reprocess Held Transactions")
        self.sync_reprocess_button.clicked.connect(self.reprocess_sync_transactions)
        self.sync_reprocess_button.setEnabled(False)
        account_row.addWidget(self.sync_reprocess_button)
        account_row.addStretch()
        layout.addLayout(account_row)

        self.sync_log = QTextEdit()
        self.sync_log.setReadOnly(True)
        self.sync_log.setFont(QFont("Monospace", 9))
        self.sync_log.setPlaceholderText("Sync events will appear here.")
        layout.addWidget(self.sync_log, 1)

    def _append_sync_log(self, message: str):
        self.sync_log.append(message)
        self.statusBar().showMessage(message, 5000)

    def _render_sync_qr(self, payload: dict):
        qr = qrcode.QRCode(version=None, box_size=8, border=4)
        qr.add_data(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white").convert("RGB")

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        qimage = QImage.fromData(buffer.getvalue(), "PNG")
        pixmap = QPixmap.fromImage(qimage)
        self.sync_qr_pixmap = pixmap
        self._resize_sync_qr()

    def _resize_sync_qr(self):
        pixmap = getattr(self, "sync_qr_pixmap", None)
        if pixmap is None or pixmap.isNull():
            return

        available = self.sync_qr_label.size()
        margin = 12
        width = max(1, available.width() - margin * 2)
        height = max(1, available.height() - margin * 2)
        size = min(width, height)

        self.sync_qr_label.setPixmap(
            pixmap.scaled(
                size,
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "sync_qr_label"):
            self._resize_sync_qr()

    def start_mobile_sync(self):
        if self.sync_server is not None:
            return

        try:
            pairing_code = make_pairing_code()
            server = CogniSyncServer(
                self.gl,
                pairing_code,
                on_sync_completed=lambda: self._create_safety_backup("post_sync")
            )
            self.sync_server = server
            thread = threading.Thread(
                target=server.serve_forever,
                name="CogniSyncServer",
                daemon=True,
            )
            thread.start()
        except Exception as e:
            QMessageBox.critical(self, "Mobile Sync", f"Could not start the sync server:\n{e}")
            return

        self.sync_thread = thread
        bootstrap = server.bootstrap_data

        self.sync_state_label.setText("Running — waiting for Android")
        self.sync_address_label.setText(f"http://{bootstrap['desktop_ip']}:{bootstrap['port']}")
        self.sync_pairing_label.setText(pairing_code)
        self.sync_fingerprint_label.setText(bootstrap.get("desktop_fingerprint", "—"))
        self._render_sync_qr(bootstrap)

        self.sync_start_button.setEnabled(False)
        self.sync_stop_button.setEnabled(True)
        self.sync_log.clear()
        self._append_sync_log("Sync server started.")
        self._append_sync_log("Scan the QR code with the Android companion and enter the pairing code.")

        self.sync_status_timer = QTimer(self)
        self.sync_status_timer.setInterval(1000)
        self.sync_status_timer.timeout.connect(self.poll_mobile_sync_status)
        self.sync_status_timer.start()
        self.poll_mobile_sync_status()

        self.sync_status_timer = QTimer(self)
        self.sync_status_timer.setInterval(1000)
        self.sync_status_timer.timeout.connect(self.poll_mobile_sync_status)
        self.sync_status_timer.start()
        self.poll_mobile_sync_status()

        # --- NEW: 5-Minute Hard Timeout for Security ---
        # If the user starts the server but never syncs, this forces it to close,
        # freeing the port and unblocking the audit scheduler.
        self.sync_timeout_timer = QTimer(self)
        self.sync_timeout_timer.setSingleShot(True)
        self.sync_timeout_timer.timeout.connect(self._force_stop_sync_timeout)
        self.sync_timeout_timer.start(5 * 60 * 1000)  # 5 minutes

    def _force_stop_sync_timeout(self):
        """Auto-closes the sync server after 5 minutes of inactivity."""
        if self.sync_server is not None:
            self._append_sync_log("Sync server auto-closed: 5-minute inactivity timeout reached.")
            self.stop_mobile_sync(from_server=True)

    def _sync_request(self, path: str):
        if self.sync_server is None:
            return None
        ctx = self.sync_server.sync_context
        url = f"http://127.0.0.1:{self.sync_server.server_port}{path}"
        body = json.dumps({"session_id": ctx.session_id}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {ctx.http_token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=0.5) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            print("STATUS DEBUG - request failed:", repr(e))
            return None

    def poll_mobile_sync_status(self):
        if self.sync_server is None:
            return

        result = self._sync_request("/api/status")
        if result is None:
            if self.sync_thread is not None and not self.sync_thread.is_alive():
                self.stop_mobile_sync(from_server=True)
            return

        handshake = bool(result.get("handshake_confirmed"))
        reconnect = bool(result.get("reconnect_available"))
        peer = result.get("peer_fingerprint") or "none"
        unknown = result.get("unknown_accounts") or []
        held = int(result.get("held_transaction_count", 0) or 0)
        cursor = result.get("last_sync_cursor") or "—"

        if handshake:
            self.sync_state_label.setText("Connected — handshake confirmed")
        elif reconnect:
            self.sync_state_label.setText("Running — reconnect available")
        else:
            self.sync_state_label.setText("Running — waiting for Android")

        self.sync_handshake_label.setText(f"Handshake: {'confirmed' if handshake else 'not confirmed'}")
        self.sync_peer_label.setText(f"Phone identity: {peer}")
        self.sync_last_cursor_label.setText(f"Last sync cursor: {cursor}")
        self.sync_accounts_label.setText("Unknown accounts: " + (", ".join(unknown) if unknown else "none"))
        self.sync_held_label.setText(f"Held transactions: {held}")
        self.sync_create_account_button.setEnabled(bool(unknown))
        self.sync_reprocess_button.setEnabled(held > 0)

    def create_sync_accounts(self):
        if self.sync_server is None:
            return

        ctx = self.sync_server.sync_context
        with ctx._state_lock:
            names = sorted(ctx.unknown_accounts)

        for name in names:
            dialog = QDialog(self)
            dialog.setWindowTitle("Create Missing Account")
            form = QFormLayout(dialog)
            name_edit = QLineEdit(name)
            type_combo = QComboBox()
            for account_type in ("Asset", "Liability", "Equity", "Revenue", "Expense"):
                type_combo.addItem(account_type)
            form.addRow("Account:", name_edit)
            form.addRow("Type:", type_combo)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            form.addRow(buttons)

            if dialog.exec() != QDialog.DialogCode.Accepted:
                continue

            account_name = name_edit.text().strip()
            if not account_name:
                continue
            ok, message = ctx.create_account(account_name, type_combo.currentText())
            if ok:
                self._append_sync_log(message)
                self.tab_transactions.refresh_data()
                self.tab_transactions2.refresh_data()
                self.refresh_overview()
            else:
                QMessageBox.warning(self, "Create Account", message)

        self.poll_mobile_sync_status()

    def reprocess_sync_transactions(self):
        if self.sync_server is None:
            return
        try:
            result = self.sync_server.sync_context.reprocess_held()
        except Exception as e:
            QMessageBox.critical(self, "Reprocess Held Transactions", str(e))
            return

        posted = result.get("posted", [])
        still_missing = result.get("still_missing", [])
        self._append_sync_log(
            f"Reprocessed {len(posted)} held transaction(s)."
            + (f" Still missing: {', '.join(still_missing)}." if still_missing else "")
        )
        self.tab_transactions.refresh_data()
        self.tab_transactions2.refresh_data()
        self.refresh_overview()
        self.poll_mobile_sync_status()

    def stop_mobile_sync(self, from_server: bool = False):
        server = self.sync_server
        if server is None:
            return

        if hasattr(self, 'sync_timeout_timer') and self.sync_timeout_timer is not None:
            self.sync_timeout_timer.stop()
            self.sync_timeout_timer = None

        if self.sync_status_timer is not None:
            self.sync_status_timer.stop()
            self.sync_status_timer.deleteLater()
            self.sync_status_timer = None

        try:
            server.sync_context.request_shutdown()
            server.shutdown()
            server.server_close()
        except Exception as e:
            if not from_server:
                self._append_sync_log(f"Error stopping sync server: {e}")

        self.sync_server = None
        self.sync_thread = None
        self.sync_state_label.setText("Stopped")
        self.sync_address_label.setText("—")
        self.sync_pairing_label.setText("—")
        self.sync_fingerprint_label.setText("—")
        self.sync_qr_label.clear()
        self.sync_qr_label.setText("Start Mobile Sync to generate a QR code.")
        self.sync_handshake_label.setText("Handshake: —")
        self.sync_peer_label.setText("Phone identity: —")
        self.sync_last_cursor_label.setText("Last sync cursor: —")
        self.sync_accounts_label.setText("Unknown accounts: none")
        self.sync_held_label.setText("Held transactions: 0")
        self.sync_start_button.setEnabled(True)
        self.sync_stop_button.setEnabled(False)
        self.sync_create_account_button.setEnabled(False)
        self.sync_reprocess_button.setEnabled(False)
        if not from_server:
            self._append_sync_log("Sync server stopped.")

    def _run_silent_auto_backup(self, trigger: str = "close"):
        """Creates a timestamped encrypted backup in ~/BudgetData/backups/ and prunes backups older than 30 days."""
        try:
            backup_dir = os.path.join(os.path.dirname(self.gl.db_path), "backups")
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
            self.stop_mobile_sync()
            QApplication.instance().quit()
        else:
            QMessageBox.critical(self, "Restore Failed", msg)

    def closeEvent(self, event):
        if hasattr(self, 'test_manager'):
            self.test_manager.cleanup()
        self._run_silent_auto_backup("close")
        self.stop_mobile_sync()
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
