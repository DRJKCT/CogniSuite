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

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

logger = logging.getLogger(__name__)

class SuggestedMatchesDialog(QDialog):
    def __init__(self, matches: List[Dict[str, Any]], ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review Suggested Mobile Matches")
        self.resize(950, 550)
        self.matches = matches
        self.ledger = ledger
        self.user_actions = {}

        layout = QVBoxLayout(self)
        info = QLabel(
         "The following imported transactions closely match existing unreconciled entries.\n"
         "<b>Confirm</b>: Consolidate into a single transaction and mark as reconciled.\n"
         "<b>Reject</b>: Ignore the match and import the new line as a separate transaction."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["New Date", "New Description", "Existing Description", "Status", "Date Diff", "Merchant Match", "Action"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        for i, m in enumerate(matches):
            self.table.insertRow(i)
            p_tx = m["parsed_tx"]
            self.table.setItem(i, 0, QTableWidgetItem(p_tx.get("date", "")))
            self.table.setItem(i, 1, QTableWidgetItem(p_tx.get("description", "")))

            # Fetch existing transaction description from ledger
            existing_tx = next((t for t in self.ledger.transactions if t.id == m["pending_id"]), None)
            existing_desc = existing_tx.description if existing_tx else "Unknown"
            self.table.setItem(i, 2, QTableWidgetItem(existing_desc))

            date_diff = m.get("date_diff_days", 0)
            merchant_sim = m.get("merchant_similarity", 0.0)
            merchant_pct = f"{merchant_sim * 100:.0f}%"

            # 3: Status (Colored Circle)
            if m["tier"] == "green":
                tooltip = f"GREEN MATCH\nExact amount, Date diff: {date_diff}d, Merchant: {merchant_pct}"
            else:
                # Explain exactly why it's yellow
                reasons = []
                if date_diff > 1:
                    reasons.append(f"Date is {date_diff} days apart")
                if merchant_sim < 0.80:
                    reasons.append(f"Merchant name similarity is only {merchant_pct}")
                reason_text = ", ".join(reasons) if reasons else "Borderline match"
                tooltip = f"YELLOW MATCH\nExact amount, but: {reason_text}"

            tier_widget = self._create_tier_indicator(m["tier"], tooltip)
            self.table.setCellWidget(i, 3, tier_widget)

            # 4: Date Diff
            diff_item = QTableWidgetItem(f"{date_diff} days")
            diff_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 4, diff_item)

            # 5: Merchant Match (Color-coded text)
            match_item = QTableWidgetItem(merchant_pct)
            match_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if merchant_sim >= 0.80:
                match_item.setForeground(QColor("#2e7d32"))  # Green text
            elif merchant_sim >= 0.60:
                match_item.setForeground(QColor("#f57c00"))  # Amber/Orange text
            else:
                match_item.setForeground(QColor("#c62828"))  # Red text
            self.table.setItem(i, 5, match_item)

            combo = QComboBox()
            combo.addItems(["Confirm (Merge)", "Reject (Import as New)"])
            # Default to Confirm for GREEN, Reject for YELLOW
            combo.setCurrentIndex(0 if m["tier"] == "green" else 1)
            combo.currentIndexChanged.connect(lambda idx, row=i: self._on_action_changed(row, idx))
            self.table.setCellWidget(i, 6, combo)
            self.user_actions[i] = "confirm" if combo.currentIndex() == 0 else "reject"

        layout.addWidget(self.table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _create_tier_indicator(self, tier: str, tooltip: str) -> QWidget:
        """Creates a perfectly centered colored circle using a styled QLabel."""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        label = QLabel()
        label.setFixedSize(18, 18)  # Size of the circle
        label.setToolTip(tooltip)

        if tier == "green":
            label.setStyleSheet("background-color: #2e7d32; border-radius: 9px;")
        elif tier == "yellow":
            label.setStyleSheet("background-color: #f9a825; border-radius: 9px;")
        else:
            label.setStyleSheet("background-color: #c62828; border-radius: 9px;")

        layout.addWidget(label)
        return container

    def _on_action_changed(self, row, index):
        self.user_actions[row] = "confirm" if index == 0 else "reject"

    def get_confirmations(self):
        return [self.matches[i] for i, action in self.user_actions.items() if action == "confirm"]

    def get_rejections(self):
        return [self.matches[i]["parsed_tx"] for i, action in self.user_actions.items() if action == "reject"]

class BudgetApp(QMainWindow):
    ACCOUNT_COLORS = {
        "Asset": "#2e7d32",      # Green
        "Liability": "#333333",  # Dark Gray / Black
        "Equity": "#1565c0",     # Blue
        "Revenue": "#6a1b9a",    # Purple
        "Expense": "#c62828"     # Red
    }

    def __init__(self, db_path: str, passphrase: str):
        super().__init__()
        self.gl = GeneralLedger(db_path, passphrase)
        self._tx_table_id_map = {}
        self.db_dir = os.path.dirname(self.gl.db_path)
        store_path = Path(self.db_dir) / "notifications.json"
        self.notifications = NotificationCenter(NotificationStore(store_path))
        self.notifications.clear_resolved()

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
        self.tab_transactions = QWidget()
        self.tab_transactions2 = QWidget()
        self.tab_budgets = QWidget()
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

        self._init_overview()
        self._init_reconcile()
        self._init_transactions()
        self._init_transactions_tab()
        self._init_budgets()
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

    def _create_account_combo(self, account_name=None):
        """Creates a QComboBox for account selection with color-coded account types."""

        class _AccountColorDelegate(QStyledItemDelegate):
            def paint(self, painter, option, index):
                """Manually paint each dropdown item to bypass Linux native theme overrides."""
                painter.save()

                # 1. Draw the background
                if option.state & QStyle.StateFlag.State_Selected:
                    painter.fillRect(option.rect, option.palette.highlight())
                elif option.state & QStyle.StateFlag.State_MouseOver:
                    painter.fillRect(option.rect, QColor("#e8f0fe"))
                else:
                    painter.fillRect(option.rect, option.palette.base())

                # 2. Extract the custom color from the model's ForegroundRole
                brush = index.data(Qt.ItemDataRole.ForegroundRole)
                text_color = QColor("#000000")
                if brush is not None:
                    c = brush.color() if hasattr(brush, 'color') else brush
                    if isinstance(c, QColor):
                        text_color = c

                # 3. Draw the text with the custom color
                painter.setPen(text_color)
                font = option.font
                font.setBold(True)
                painter.setFont(font)

                text = index.data(Qt.ItemDataRole.DisplayRole) or ""
                painter.drawText(
                    option.rect.adjusted(8, 0, -8, 0),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    text
                )

                painter.restore()

        combo = QComboBox()
        delegate = _AccountColorDelegate(combo)
        combo.setItemDelegate(delegate)
        combo.view().setItemDelegate(delegate)

        model = QStandardItemModel()
        account_names = sorted(self.gl.accounts.keys())

        for name in account_names:
            acc = self.gl.accounts.get(name)
            acc_type = acc.account_type if acc else "Expense"
            color_hex = self.ACCOUNT_COLORS.get(acc_type, "#000000")

            item = QStandardItem(name)
            item.setData(name, Qt.ItemDataRole.UserRole)
            item.setForeground(QColor(color_hex))
            model.appendRow(item)

        combo.setModel(model)

        if account_name is not None:
            for i in range(model.rowCount()):
                if model.item(i).data(Qt.ItemDataRole.UserRole) == account_name:
                    combo.setCurrentIndex(i)
                    break

        # Update the closed combo box text color to match the selected account type
        def _update_text_color(index):
            if index >= 0:
                item = model.item(index)
                if item:
                    color = item.foreground().color()
                    combo.setStyleSheet(
                        f"QComboBox {{ color: {color.name()}; font-weight: bold; }}"
                    )

        combo.currentIndexChanged.connect(_update_text_color)
        _update_text_color(combo.currentIndex())

        return combo

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

    def _add_refresh_row(self, layout, callback, search_line=None):
        """Add the standard refresh control used by ledger-backed views."""
        row = QHBoxLayout()
        row.addStretch()

        if search_line is not None:
            clear_button = QPushButton("Clear Search")
            clear_button.clicked.connect(lambda *_: search_line.clear())
            row.addWidget(clear_button)

        button = QPushButton("Refresh")
        button.clicked.connect(callback)
        row.addWidget(button)
        layout.addLayout(row)
        return button

    def _refresh_current_tab(self, index):
        """Refresh only tabs whose primary purpose is displaying ledger data."""
        refreshers = {
            self.tab_overview: self.refresh_overview,
            self.tab_accounts: self.refresh_accounts_table,
            self.tab_transactions: self.apply_transaction_filters,
            self.tab_transactions2: self.apply_transactions_tab_filters,
            self.tab_budgets: self.refresh_budget_list,
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
                self._fit_table_columns(table)

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
        self._setup_table_headers(self.accounts_table, [0.7, 0.3])
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

    """
    ======================================================================
    IMPORT LOGIC & MATCH PROCESSING
    ======================================================================
    """

    def _process_confirmed_matches(self, confirmations: List[Dict[str, Any]]):
        """Updates pending transactions with bank data, marks reconciled, and deletes duplicates."""
        for conf in confirmations:
            pending_id = conf["pending_id"]
            p_tx = conf["parsed_tx"]
            match_type = conf.get("match_type", "mobile_to_bank") # Default to legacy behavior

            bank_date = p_tx.get("date")
            if "statement_balance" in p_tx:
                bank_date = normalize_statement_date(bank_date) or datetime.now().strftime("%Y-%m-%d")
            bank_desc = p_tx.get("description", "")

            tx = next((t for t in self.gl.transactions if t.id == pending_id), None)
            if tx:
                if match_type == "mobile_to_bank":
                    # LEGACY BEHAVIOR: Preserve the mobile app's existing split-lines/categories
                    existing_lines = [(l.id, l.account_name, l.dr, l.cr) for l in tx.lines]
                    ok, msg = self.gl.update_transaction(pending_id, bank_date, bank_desc, existing_lines)
                    if ok:
                        self.gl.mark_reconciled(pending_id, True)
                    else:
                        logger.warning(f"Failed to merge mobile match {pending_id}: {msg}")

                elif match_type == "bank_to_bank":
                    # NEW BEHAVIOR: Both are bank imports. Keep the existing one, update it,
                    # and physically delete the newly imported duplicate to prevent double-counting.
                    existing_lines = [(l.id, l.account_name, l.dr, l.cr) for l in tx.lines]
                    ok, msg = self.gl.update_transaction(pending_id, bank_date, bank_desc, existing_lines)

                    if ok:
                        self.gl.mark_reconciled(pending_id, True)

                        # Now delete the duplicate imported transaction
                        duplicate_imp_id = p_tx.get("imported_id")
                        if duplicate_imp_id:
                            duplicate_tx = next((t for t in self.gl.transactions if t.imported_id == duplicate_imp_id), None)
                            if duplicate_tx:
                                del_ok, del_msg = self.gl.delete_transaction(duplicate_tx.id)
                                if del_ok:
                                    logger.info(f"Bank-to-Bank merge: Deleted duplicate transaction {duplicate_tx.id} (Imported ID: {duplicate_imp_id})")
                                else:
                                    logger.warning(f"Failed to delete duplicate transaction {duplicate_tx.id}: {del_msg}")
                    else:
                        logger.warning(f"Failed to update existing transaction for bank-to-bank merge {pending_id}: {msg}")

    def _process_rejected_matches(self, rejected_txs: List[Dict[str, Any]], source: str):
        """Imports rejected matches as brand new transactions."""
        imported = 0
        for tx in rejected_txs:
            date_str = tx.get("date")
            if source == "pdf":
                date_str = normalize_statement_date(date_str) or datetime.now().strftime("%Y-%m-%d")

            desc = tx.get("description", "")
            lines = tx.get("lines", [])
            imp_id = tx.get("imported_id")

            if not imp_id and source == "pdf":
                amount = tx.get("amount", 0)
                imp_id = f"rejected::{date_str}::{desc[:40]}::{amount}"

            ok, msg = self.gl.record_transaction(date_str, desc, lines, imported_id=imp_id, persist=True)
            if ok:
                imported += 1
                if source == "pdf" and tx.get("statement_balance") is not None:
                    self.gl.record_statement_balance(tx["contra_account"], date_str, tx["statement_balance"], source="rejected_match")
        return imported

    def update_progress_dialog(self, percent: int, message: str):
        """Updates the visual progress dialog."""
        dialog = getattr(self, 'progress_dialog', None)

        if dialog is not None:
            try:
                dialog.setValue(percent)
                dialog.setLabelText(message)
            except RuntimeError:
                # Catch edge cases where the underlying C++ Qt object
                # was already deleted/garbage-collected
                pass

    def import_ofx_qfx(self):
        """Handles importing structured OFX/QFX bank feeds in a background thread."""
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import OFX/QFX Bank Feed", "",
            "Bank Feeds (*.ofx *.qfx);;All Files (*)"
        )
        if not files:
            return

        # 1. Setup the visual progress dialog
        self.progress_dialog = QProgressDialog("Importing OFX/QFX...", "Cancel", 0, 100, self)
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setAutoClose(True)
        self.progress_dialog.setMinimumDuration(500) # Only shows if it takes > 0.5s
        self.progress_dialog.show()

        # 2. Setup and start the background thread
        self.ofx_thread = OFXImportThread(files, self.gl)
        self.ofx_thread.progress_signal.connect(self.update_progress_dialog)
        self.ofx_thread.finished_signal.connect(self.on_ofx_import_finished)
        self.ofx_thread.error_signal.connect(self.on_import_error)
        self.ofx_thread.finished.connect(self.ofx_thread.deleteLater)

        self.ofx_thread.start()

    def on_ofx_import_finished(self, imported_count: int, review: dict):
        """Called when the OFX background thread completes."""
        # Close the progress dialog
        if hasattr(self, 'progress_dialog') and self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None

        suggested_matches = review.get("suggested_matches", [])
        total_skipped = review.get("skipped_count", 0)
        new_accounts = review.get("new_accounts", [])

        if new_accounts:
            names = ", ".join([f"{n} ({t})" for n, t in new_accounts])
            logger.info(f"OFX Import: Auto-created accounts: {names}")

        # Show the Suggested Matches Dialog if needed
        if suggested_matches:
            dialog = SuggestedMatchesDialog(suggested_matches, self.gl, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._process_confirmed_matches(dialog.get_confirmations())
                rejected_count = self._process_rejected_matches(dialog.get_rejections(), source="ofx")
                imported_count += rejected_count

        # Final summary message
        summary_msg = f"Successfully imported {imported_count} transaction(s).\n"
        if total_skipped > 0:
            summary_msg += f"Skipped/Rejected by Ledger: {total_skipped}.\n"
        summary_msg += "\nNote: Transactions are currently categorized as 'Uncategorized'. "
        summary_msg += "Please review them in the Transactions tab or set up Category Rules."

        QMessageBox.information(self, "Import Complete", summary_msg)

        # Refresh UI
        self.apply_transaction_filters()
        self.apply_transactions_tab_filters()
        self.refresh_overview()
        self.refresh_account_combos()

    def _money_item(self, value, editable=False):
        """Create a right-aligned, non-editable table item for monetary values."""
        item = QTableWidgetItem(f"{value:,.2f}")
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

        return item

    def _setup_table_headers(self, table: QTableWidget, fractions=None):
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setHighlightSections(False)
        header.setSectionsClickable(True)

        table.verticalHeader().setVisible(False)

        if fractions:
            table.setProperty("fractions", tuple(fractions))
            table.setProperty("user_adjusted", False)
            table.setProperty("applying_widths", False)

            header.sectionResized.connect(lambda *_: self._table_section_resized(table))
            QTimer.singleShot(0, lambda: self._fit_table_columns(table))

    def _table_section_resized(self, table: QTableWidget):
        if table.property("applying_widths"):
            return
        table.setProperty("user_adjusted", True)

    def _fit_table_columns(self, table: QTableWidget):
        fractions = table.property("fractions")
        if not fractions or table.property("user_adjusted"):
            return

        column_count = table.columnCount()
        if column_count == 0:
            return

        total_width = table.viewport().width() or table.width() or 1000
        usable_width = max(200, total_width - 2)
        last_column = column_count - 1

        table.setProperty("applying_widths", True)
        try:
            for col in range(column_count):
                if col == last_column:
                    continue
                fraction = fractions[col] if col < len(fractions) else 1.0 / column_count
                width = max(70, int(usable_width * fraction))
                table.setColumnWidth(col, width)
        finally:
            table.setProperty("applying_widths", False)

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
        self._add_refresh_row(layout, self.refresh_overview)

        btn_row = QHBoxLayout()
        self.btn_import_pdf = QPushButton("Import Statement (PDF)")
        self.btn_import_pdf.clicked.connect(self.import_statement_pdf)
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
        btn_export.clicked.connect(self.export_ledger_csv)

        btn_import = QPushButton("Import Ledger (CSV)")
        btn_import.clicked.connect(self.import_ledger_csv)

        btn_import_ofx = QPushButton("Import OFX/QFX (Bank Feed)")
        btn_import_ofx.clicked.connect(self.import_ofx_qfx)

        btn_row.addWidget(self.btn_import_pdf)
        btn_row.addWidget(btn_import_ofx)
        btn_row.addWidget(btn_import)
        btn_row.addWidget(btn_export)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.refresh_overview()

    def refresh_overview(self):
        from html import escape

        today = QDate.currentDate()
        month = today.toString("yyyy-MM")
        report = self.gl.monthly_income_expense_report(today.year(), today.month())

        label_style = "width: 100%; white-space: pre; padding-right: 10px;"
        amount_style = "text-align: right; white-space: pre; padding-left: 10px;"
        section_style = "font-weight: bold; font-size: 12pt; padding-top: 8px; padding-bottom: 2px;"
        muted_style = "color: #666;"

        def amount_row(label, amount, bold=False, color=None):
            weight = "font-weight: bold;" if bold else ""
            color_style = f"color: {color};" if color else ""

            return (
                "<tr>"
                f'<td style="{label_style}{weight}{color_style}">{escape(str(label))}</td>'
                f'<td align="right" style="{amount_style}{weight}{color_style}">{amount:,.2f}</td>'
                "</tr>"
            )

        def section(title):
            return f'<div style="{section_style}">{escape(str(title))}</div>'

        def table(rows):
            return '<table width="100%" cellspacing="0" cellpadding="2">' + "".join(rows) + "</table>"

        net_color = "#007a00" if report["net"] >= ZERO else "#b00000"

        budgets = [b for b in self.gl.get_all_budgets() if b.month == month]
        spending = self.gl.spending_by_category(month)
        alert_rows = []

        for b in sorted(budgets, key=lambda x: x.category):
            actual = spending.get(b.category, ZERO)
            if actual > b.amount:
                over_by = actual - b.amount
                alert_rows.append(amount_row(f"{b.category} over budget", over_by, color="#b00000"))

        if not alert_rows:
            msg = "No budget overruns this month." if budgets else "No budgets set for this month."
            alert_rows.append(f'<tr><td style="{muted_style}">{msg}</td></tr>')

        recent_rows = []
        for tx in list(reversed(self.gl.transactions[-10:])):
            amount = self._transaction_display_amount(tx)
            recent_rows.append(
                "<tr>"
                f'<td style="white-space: pre;">{escape(tx.date)}</td>'
                f'<td style="width: 100%; padding-left: 10px;">{escape(tx.description)}</td>'
                f'<td align="right" style="{amount_style}">{amount:,.2f}</td>'
                "</tr>"
            )

        if not recent_rows:
            recent_rows.append(f'<tr><td style="{muted_style}">No transactions yet.</td></tr>')

        unreconciled_count = len(self.gl.get_unreconciled_transactions())
        reconciliation_rows = [f"<tr><td>{unreconciled_count} unreconciled transaction(s)</td></tr>"]

        html = f"""
        <html>
        <body style="font-family: 'Segoe UI', sans-serif; font-size: 10pt;">
            <div style="font-size: 20pt; font-weight: bold; color: #3388FF;">
                Welcome to CogniFinance
            </div>
            <div style="{muted_style}">
                {escape(today.toString('MMMM yyyy'))}
            </div>
            <br>
            <div>Use the tabs above to:</div>
            <ul>
                <li>View and filter transactions</li>
                <li>Set and track monthly budgets per category</li>
                <li>See charts of spending</li>
                <li>Reconcile imported statement transactions</li>
                <li>View balance sheet and export/import ledger CSV</li>
            </ul>
            <br/>
            {section("Budget Alerts")}
            {table(alert_rows)}
            <br>
            {section("Reconciliation")}
            {table(reconciliation_rows)}
            <br>
            {section("Recent Transactions")}
            <table width="100%" cellspacing="0" cellpadding="2">
                {''.join(recent_rows)}
            </table>
        </body>
        </html>
        """
        self.overview_text.setHtml(html)

    def _init_transactions(self):
        layout = QVBoxLayout(self.tab_transactions)

        self.filter_start = QDateEdit()
        self.filter_start.setCalendarPopup(True)

        self.filter_end = QDateEdit()
        self.filter_end.setCalendarPopup(True)
        self.filter_end.setDate(QDate.currentDate())

        self.filter_account = QComboBox()
        self.filter_account.addItem("All accounts", "")
        for name in sorted(self.gl.accounts.keys()):
            self.filter_account.addItem(name, name)

        self.filter_search = QLineEdit()
        self.filter_search.setPlaceholderText("Search description or account...")
        self.filter_search.setClearButtonEnabled(True)

        self._add_refresh_row(layout, self.apply_transaction_filters, self.filter_search)

        filter_box = QGroupBox("Filters")
        flayout = QFormLayout()

        flayout.addRow("From:", self.filter_start)
        flayout.addRow("To:", self.filter_end)
        flayout.addRow("Account:", self.filter_account)
        flayout.addRow("Search:", self.filter_search)
        filter_box.setLayout(flayout)
        layout.addWidget(filter_box)

        btn_row = QHBoxLayout()
        btn_apply = QPushButton("Apply Filters")
        btn_apply.clicked.connect(self.apply_transaction_filters)
        btn_row.addWidget(btn_apply)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.tx_table = QTableWidget()
        self.tx_table.setColumnCount(4)
        self.tx_table.setHorizontalHeaderLabels(["Date", "Description", "Accounts", "Amount"])
        self._setup_table_headers(self.tx_table, [0.11, 0.42, 0.31, 0.16])

        layout.addWidget(self.tx_table)
        self.apply_transaction_filters()

    def _transaction_display_amount(self, tx) -> Decimal:
        amount = Decimal("0.00")
        for line in tx.lines:
            acc = self.gl.accounts.get(line.account_name)
            if acc.account_type == "Expense" and line.dr > 0:
                amount += line.dr
        if amount > 0:
            return amount

        for line in tx.lines:
            acc = self.gl.accounts.get(line.account_name)
            if acc.account_type == "Revenue" and line.cr > 0:
                amount += line.cr
        if amount > 0:
            return amount

        for line in tx.lines:
            if line.dr > 0:
                return line.dr
            if line.cr > 0:
                return line.cr

        return Decimal("0.00")

    def apply_transaction_filters(self):
        start = self.filter_start.date().toString("yyyy-MM-dd") or None
        end = self.filter_end.date().toString("yyyy-MM-dd") or None
        account = self.filter_account.currentData() or None
        search = self.filter_search.text().strip() or None

        txs = self.gl.get_transactions(start_date=start, end_date=end, account=account, search=search)

        self.tx_table.setRowCount(len(txs))
        for row, tx in enumerate(txs):
            self.tx_table.setItem(row, 0, QTableWidgetItem(tx.date))
            self.tx_table.setItem(row, 1, QTableWidgetItem(tx.description))
            accs = ", ".join(l.account_name for l in tx.lines)
            self.tx_table.setItem(row, 2, QTableWidgetItem(accs))
            amount = self._transaction_display_amount(tx)
            self.tx_table.setItem(row, 3, QTableWidgetItem(f"{amount:,.2f}"))

    def _init_transactions_tab(self):
        layout = QVBoxLayout(self.tab_transactions2)

        self._add_refresh_row(layout, self.apply_transactions_tab_filters)

        form_box = QGroupBox("Add Transaction")
        flayout = QFormLayout()

        self.tx_date = QDateEdit()
        self.tx_date.setCalendarPopup(True)
        self.tx_date.setDate(QDate.currentDate())

        self.tx_desc = QLineEdit()
        self.tx_desc.setPlaceholderText("e.g. Groceries, Salary, etc.")

        self.tx_from = QComboBox()
        self.tx_to = QComboBox()

        for name in sorted(self.gl.accounts.keys()):
            self.tx_from.addItem(name, name)
            self.tx_to.addItem(name, name)

        self.tx_amount = QLineEdit()
        self.tx_amount.setPlaceholderText("e.g. 123.45")

        flayout.addRow("Date:", self.tx_date)
        flayout.addRow("Description:", self.tx_desc)
        flayout.addRow("From Account:", self.tx_from)
        flayout.addRow("To Account:", self.tx_to)
        flayout.addRow("Amount:", self.tx_amount)

        form_box.setLayout(flayout)
        layout.addWidget(form_box)

        btn_row = QHBoxLayout()
        btn_add = QPushButton("Post Transaction")
        btn_add.clicked.connect(self.post_quick_transaction)
        btn_row.addWidget(btn_add)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        filter_box = QGroupBox("Filters")
        flay2 = QFormLayout()

        today = QDate.currentDate()
        self.tx_filter_start = QDateEdit()
        self.tx_filter_start.setCalendarPopup(True)
        self.tx_filter_start.setDate(QDate(today.year(), today.month(), 1))

        self.tx_filter_end = QDateEdit()
        self.tx_filter_end.setCalendarPopup(True)
        self.tx_filter_end.setDate(today)

        self.tx_filter_account = QComboBox()
        self.tx_filter_account.addItem("All accounts", "")
        for name in sorted(self.gl.accounts.keys()):
            self.tx_filter_account.addItem(name, name)

        flay2.addRow("From:", self.tx_filter_start)
        flay2.addRow("To:", self.tx_filter_end)
        flay2.addRow("Account:", self.tx_filter_account)

        filter_box.setLayout(flay2)
        layout.addWidget(filter_box)

        btn_apply = QPushButton("Apply Filters")
        btn_apply.clicked.connect(self.apply_transactions_tab_filters)
        layout.addWidget(btn_apply)

        btn_edit_row = QHBoxLayout()
        btn_edit = QPushButton("Edit Selected")
        btn_edit.clicked.connect(self.edit_selected_transaction)

        btn_delete = QPushButton("Delete Selected")
        btn_delete.clicked.connect(self.delete_selected_transaction)

        btn_history = QPushButton("View History")
        btn_history.clicked.connect(self._show_selected_tx_history)

        btn_edit_row.addWidget(btn_history)
        btn_edit_row.addWidget(btn_edit)
        btn_edit_row.addWidget(btn_delete)
        btn_edit_row.addStretch()

        layout.addLayout(btn_edit_row)

        self.tx_table2 = QTableWidget()
        self.tx_table2.setColumnCount(5)
        self.tx_table2.setHorizontalHeaderLabels(["Date", "Description", "From", "To", "Amount"])
        self._setup_table_headers(self.tx_table2, [0.17, 0.37, 0.18, 0.18, 0.10])
        self.tx_table2.doubleClicked.connect(self.edit_selected_transaction)

        layout.addWidget(self.tx_table2)
        self.apply_transactions_tab_filters()

    def post_quick_transaction(self):
        date_str = self.tx_date.date().toString("yyyy-MM-dd")
        desc = self.tx_desc.text().strip()
        from_acc = self.tx_from.currentData()
        to_acc = self.tx_to.currentData()
        amt_str = self.tx_amount.text().strip()

        if not desc or not amt_str:
            QMessageBox.warning(self, "Invalid input", "Description and amount are required.")
            return

        try:
            amount = Decimal(amt_str)
            if amount <= 0:
                raise ValueError()
        except ValueError:
            QMessageBox.warning(self, "Invalid amount", "Amount must be a positive number.")
            return

        ok, msg = self.gl.post_simple_transaction(date_str, desc, from_acc, to_acc, amount)
        if ok:
            QMessageBox.information(self, "Success", "Transaction posted.")
            self.tx_desc.clear()
            self.tx_amount.clear()
            self.apply_transactions_tab_filters()
            self.apply_transaction_filters()
        else:
            logger.warning(f"Quick transaction failed: {msg}")
            QMessageBox.warning(self, "Error", msg)

    def _show_selected_tx_history(self):
        selected = self.tx_table2.selectedItems()
        if not selected:
            QMessageBox.information(self, "No selection", "Please select a transaction.")
            return
        tx_id = self._tx_table_id_map.get(selected[0].row())
        if tx_id is None:
            return
        self.tab_audit.show_history_for_tx_id(tx_id)

    def refresh_account_combos(self):
        account_names = sorted(self.gl.accounts.keys())

        current_from = self.tx_from.currentData()
        current_to = self.tx_to.currentData()
        current_budget = self.budget_category.currentData()

        self.tx_from.clear()
        self.tx_to.clear()
        self.budget_category.clear()

        for name in account_names:
            self.tx_from.addItem(name, name)
            self.tx_to.addItem(name, name)

        expense_accounts = [name for name, acc in self.gl.accounts.items() if acc.account_type == "Expense"]

        for name in sorted(expense_accounts):
            self.budget_category.addItem(name, name)

        if current_from:
            idx = self.tx_from.findData(current_from)
            if idx >= 0:
                self.tx_from.setCurrentIndex(idx)

        if current_to:
            idx = self.tx_to.findData(current_to)
            if idx >= 0:
                self.tx_to.setCurrentIndex(idx)

        if current_budget:
            idx = self.budget_category.findData(current_budget)
            if idx >= 0:
                self.budget_category.setCurrentIndex(idx)

        if hasattr(self, 'tab_rules') and hasattr(self.tab_rules, '_populate_category_combo'):
            self.tab_rules._populate_category_combo()

    def apply_transactions_tab_filters(self):
        start = self.tx_filter_start.date().toString("yyyy-MM-dd") or None
        end = self.tx_filter_end.date().toString("yyyy-MM-dd") or None
        account = self.tx_filter_account.currentData() or None

        txs = self.gl.get_transactions(start_date=start, end_date=end, account=account)

        self.tx_table2.setRowCount(len(txs))
        self._tx_table_id_map = {}

        for row, tx in enumerate(txs):
            self.tx_table2.setItem(row, 0, QTableWidgetItem(tx.date))
            self.tx_table2.setItem(row, 1, QTableWidgetItem(tx.description))

            if len(tx.lines) >= 2:
                from_acc = tx.lines[1].account_name
                to_acc = tx.lines[0].account_name
            else:
                from_acc = tx.lines[0].account_name if tx.lines else ""
                to_acc = ""

            self.tx_table2.setItem(row, 2, QTableWidgetItem(from_acc))
            self.tx_table2.setItem(row, 3, QTableWidgetItem(to_acc))

            amount = self._transaction_display_amount(tx)
            self.tx_table2.setItem(row, 4, QTableWidgetItem(f"{amount:,.2f}"))

            if tx.id is not None:
                self._tx_table_id_map[row] = tx.id

    def _init_budgets(self):
        layout = QVBoxLayout(self.tab_budgets)
        self._add_refresh_row(layout, self.refresh_budget_list)

        form_box = QGroupBox("Set Monthly Budget")
        flayout = QFormLayout()

        self.budget_category = QComboBox()
        expense_accounts = [name for name, acc in self.gl.accounts.items() if acc.account_type == "Expense"]
        for name in sorted(expense_accounts):
            self.budget_category.addItem(name, name)

        self.budget_month = QDateEdit()
        self.budget_month.setCalendarPopup(True)
        self.budget_month.setDate(QDate.currentDate())
        self.budget_month.setDisplayFormat("yyyy-MM")

        self.budget_amount = QLineEdit()

        flayout.addRow("Category (Expense Account):", self.budget_category)
        flayout.addRow("Month:", self.budget_month)
        flayout.addRow("Budget Amount:", self.budget_amount)

        form_box.setLayout(flayout)
        layout.addWidget(form_box)

        btn_row = QHBoxLayout()
        btn_save = QPushButton("Save Budget")
        btn_save.clicked.connect(self.save_budget)
        btn_row.addWidget(btn_save)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        view_box = QGroupBox("Budget vs Actual")
        view_layout = QHBoxLayout()
        view_layout.addWidget(QLabel("Budget Month:"))

        self.budget_view_month = QDateEdit()
        self.budget_view_month.setCalendarPopup(True)
        self.budget_view_month.setDate(QDate.currentDate())
        self.budget_view_month.setDisplayFormat("yyyy-MM")

        view_layout.addWidget(self.budget_view_month)
        view_layout.addStretch()
        view_box.setLayout(view_layout)
        layout.addWidget(view_box)

        self.budget_table = QTableWidget()
        self.budget_table.setColumnCount(5)
        self.budget_table.setHorizontalHeaderLabels(["Category", "Budget", "Actual", "Difference", "Used"])

        self._setup_table_headers(self.budget_table)
        layout.addWidget(self.budget_table)

        self.budget_view_month.dateChanged.connect(lambda *_: self.refresh_budget_list())

        budget_action_row = QHBoxLayout()
        btn_edit_budget = QPushButton("Edit Selected Budget")
        btn_edit_budget.clicked.connect(self.edit_selected_budget)
        budget_action_row.addWidget(btn_edit_budget)
        self.budget_table.doubleClicked.connect(lambda *_: self.edit_selected_budget())

        btn_delete_budget = QPushButton("Delete Selected Budget")
        btn_delete_budget.clicked.connect(self.delete_selected_budget)
        budget_action_row.addWidget(btn_delete_budget)

        budget_action_row.addStretch()
        layout.addLayout(budget_action_row)

        self.refresh_budget_list()

    def save_budget(self):
        category = self.budget_category.currentData()
        month = self.budget_month.date().toString("yyyy-MM")
        text = self.budget_amount.text().strip()
        try:
            amount = float(text)
            if amount < 0:
                raise ValueError()
        except ValueError:
            QMessageBox.warning(self, "Invalid amount", "Please enter a valid non‑negative number.")
            return

        ok, msg = self.gl.set_budget(category, month, amount)
        if ok:
            QMessageBox.information(self, "Budget saved", msg)
            self.refresh_budget_list()
        else:
            logger.warning(f"Failed to save budget for {category} ({month}): {msg}")
            QMessageBox.warning(self, "Budget error", msg)

    def refresh_budget_list(self):
        month = self.budget_view_month.date().toString("yyyy-MM") if hasattr(self, "budget_view_month") else self.budget_month.date().toString("yyyy-MM")
        budgets = [b for b in self.gl.get_all_budgets() if b.month == month]
        actuals = self.gl.spending_by_category(month)

        self.budget_table.setRowCount(len(budgets))
        red, green, orange = QColor("#b00000"), QColor("#007a00"), QColor("#b06000")

        for row, b in enumerate(budgets):
            actual = actuals.get(b.category, ZERO)
            diff = b.amount - actual

            category_item = QTableWidgetItem(b.category)
            budget_item = self._money_item(b.amount)
            budget_item.setData(Qt.ItemDataRole.UserRole, float(b.amount))
            actual_item = self._money_item(actual)
            diff_item = self._money_item(diff)

            if diff < ZERO:
                diff_item.setForeground(red)
            elif diff > ZERO:
                diff_item.setForeground(green)

            if actual > b.amount:
                actual_item.setForeground(red)
            elif b.amount > ZERO and actual > b.amount * Decimal("0.90"):
                actual_item.setForeground(orange)

            if b.amount > ZERO:
                used = (actual / b.amount) * Decimal("100")
                used_item = QTableWidgetItem(f"{used:.0f}%")
                if used > Decimal("100"):
                    used_item.setForeground(red)
                elif used > Decimal("90"):
                    used_item.setForeground(orange)
                else:
                    used_item.setForeground(green)
            else:
                used_item = QTableWidgetItem("Over" if actual > ZERO else "0%")
                used_item.setForeground(red if actual > ZERO else green)

            used_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            for item in (category_item, budget_item, actual_item, diff_item, used_item):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            self.budget_table.setItem(row, 0, category_item)
            self.budget_table.setItem(row, 1, budget_item)
            self.budget_table.setItem(row, 2, actual_item)
            self.budget_table.setItem(row, 3, diff_item)
            self.budget_table.setItem(row, 4, used_item)

    def _selected_budget_data(self):
        row = self.budget_table.currentRow()
        if row < 0:
            selected = self.budget_table.selectedItems()
            if not selected:
                return None
            row = selected[0].row()

        if row < 0:
            return None

        category_item = self.budget_table.item(row, 0)
        budget_item = self.budget_table.item(row, 1)

        if category_item is None or budget_item is None:
            return None

        month = self.budget_view_month.date().toString("yyyy-MM") if hasattr(self, "budget_view_month") else self.budget_month.date().toString("yyyy-MM")
        amount = budget_item.data(Qt.ItemDataRole.UserRole)

        if amount is None:
            try:
                amount = float(budget_item.text().replace(",", ""))
            except ValueError:
                amount = 0.0

        return category_item.text(), month, float(amount)

    def edit_selected_budget(self):
        selection = self._selected_budget_data()
        if selection is None:
            QMessageBox.information(self, "No selection", "Please select a budget row to edit.")
            return

        category, month, amount = selection
        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Budget")

        layout = QVBoxLayout(dlg)
        form = QFormLayout()
        form.addRow("Category:", QLabel(category))
        form.addRow("Month:", QLabel(month))

        amount_edit = QLineEdit(f"{amount:.2f}")
        form.addRow("Budget Amount:", amount_edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        text = amount_edit.text().strip().replace(",", "")
        try:
            new_amount = float(text)
            if new_amount < 0:
                raise ValueError()
        except ValueError:
            QMessageBox.warning(self, "Invalid amount", "Please enter a valid non-negative number.")
            return

        ok, msg = self.gl.set_budget(category, month, new_amount)
        if ok:
            QMessageBox.information(self, "Budget updated", msg)
            self.refresh_budget_list()
        else:
            logger.warning(f"Failed to save budget for {category} ({month}): {msg}")
            QMessageBox.warning(self, "Budget error", msg)

    def delete_selected_budget(self):
        selection = self._selected_budget_data()
        if selection is None:
            QMessageBox.information(self, "No selection", "Please select a budget row to delete.")
            return

        category, month, _ = selection
        reply = QMessageBox.question(
            self,
            "Delete budget",
            f"Delete budget for:\n\n{category}\n{month}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        ok, msg = self.gl.delete_budget(category, month)
        if ok:
            QMessageBox.information(self, "Budget deleted", msg)
            self.refresh_budget_list()
        else:
            QMessageBox.warning(self, "Budget error", msg)


    def _init_reports_tab(self):
        layout = QVBoxLayout(self.tab_reports)
        self._add_refresh_row(layout, self.refresh_monthly_report)

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
        from html import escape

        year = self.report_year.currentData()
        month = self.report_month.currentData()
        data = self.gl.monthly_income_expense_report(year, month)

        label_style = "width: 100%; white-space: pre; padding-right: 12px;"
        amount_style = "text-align: right; white-space: pre; padding-left: 12px;"
        section_style = "font-weight: bold; padding-top: 6px;"

        def row(label, amount, bold=False, indent=0):
            weight = "font-weight: bold;" if bold else ""
            left_pad = f"padding-left: {indent * 18}px;"
            return (
                "<tr>"
                f'<td style="{label_style}{weight}{left_pad}">{escape(str(label))}</td>'
                f'<td align="right" style="{amount_style}{weight}">{amount:,.2f}</td>'
                "</tr>"
            )

        def section(title):
            return f'<tr><td colspan="2" style="{section_style}">{escape(str(title))}</td></tr>'

        income_rows = "".join(row(acc, amt, indent=1) for acc, amt in data["income_by_account"].items()) or row("(none)", ZERO, indent=1)
        expense_rows = "".join(row(acc, amt, indent=1) for acc, amt in data["expense_by_account"].items()) or row("(none)", ZERO, indent=1)

        html = f"""
        <html>
        <body style="font-family: 'Inter', sans-serif; font-size: 10pt;">
            <div style="font-weight: bold; font-size: 12pt;">
            Monthly Income &amp; Expense Report – {year}-{month:02d}
            </div>
            <br/>
            <table width="100%" cellspacing="0" cellpadding="1">
                {row('Total Income', data['income_total'], bold=True)}
                {row('Total Expenses', data['expense_total'], bold=True)}
                {row('Net (Income − Expenses)', data['net'], bold=True)}
                <tr><td colspan="2">&nbsp;</td></tr>
                {section('Income by Account:')}
                {income_rows}
                <tr><td colspan="2">&nbsp;</td></tr>
                {section('Expenses by Account:')}
                {expense_rows}
            </table>
        </body>
        </html>
        """
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
        self._add_refresh_row(layout, self.load_unreconciled)

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
        self._add_refresh_row(layout, self.refresh_balance_sheet)

        self.balance_text = QTextEdit()
        self.balance_text.setReadOnly(True)
        self.balance_text.setFont(QFont("Monospace", 10))
        layout.addWidget(self.balance_text)

        self.refresh_balance_sheet()

    def refresh_balance_sheet(self):
        from html import escape

        bs = self.gl.balance_sheet()

        label_style = "width: 100%; white-space: pre; padding-right: 12px;"
        amount_style = "text-align: right; white-space: pre; padding-left: 12px;"
        section_style = "font-weight: bold; padding-top: 6px;"

        def row(label, amount, bold=False, indent=0):
            weight = "font-weight: bold;" if bold else ""
            left_pad = f"padding-left: {indent * 18}px;"
            return (
                "<tr>"
                f'<td style="{label_style}{weight}{left_pad}">{escape(str(label))}</td>'
                f'<td align="right" style="{amount_style}{weight}">{amount:,.2f}</td>'
                "</tr>"
            )

        def section(title):
            return f'<tr><td colspan="2" style="{section_style}">{escape(str(title))}</td></tr>'

        def rule():
            return '<tr><td colspan="2"><hr/></td></tr>'

        def spacer(count=1):
            return '<tr><td colspan="2">&nbsp;</td></tr>' * count

        def section_rows(title, section_data):
            rows = [section(title)]
            for name, bal in section_data["accounts"].items():
                dname = (name[:28] + "..") if len(name) > 30 else name
                rows.append(row(dname, bal, indent=1))

            rows.append(rule())
            rows.append(row(f"TOTAL {title}", section_data["total"], bold=True))
            rows.append(spacer(2))
            return "".join(rows)

        net = bs["assets"]["total"] - bs["liabilities"]["total"] - bs["equity"]["total"]

        html = f"""
        <html>
        <body style="font-family: 'Inter', sans-serif; font-size: 10pt;">
            <div style="font-weight: bold; font-size: 12pt;">BALANCE SHEET</div>
            <br/>
            <table width="100%" cellspacing="0" cellpadding="1">
                {section_rows('Assets', bs['assets'])}
                {rule()}
                {section_rows('Liabilities', bs['liabilities'])}
                {rule()}
                {section_rows('Equity', bs['equity'])}
                {rule()}
                {section('INCOME AND EXPENSES')}
                {rule()}
                {section_rows('Income', bs['income'])}
                {section_rows('Expenses', bs['expenses'])}
                {rule()}
                {row('Net (Assets − Liabilities − Equity)', net, bold=True)}
                {rule()}
            </table>
        </body>
        </html>
        """
        self.balance_text.setHtml(html)

    def export_ledger_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Ledger CSV", "", "CSV Files (*.csv);;All Files (*)")
        if not path:
            return
        try:
            self.gl.export_journal_csv(path)
            QMessageBox.information(self, "Export complete", f"Ledger exported to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))

    def import_ledger_csv(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Import Ledger CSV", "", "CSV Files (*.csv);;All Files (*)")
        if not files:
            return
        total = 0
        for path in files:
            try:
                n = self.gl.import_ledger_csv(path)
                total += n
            except Exception as e:
                QMessageBox.warning(self, "Import error", f"Failed to import {os.path.basename(path)}:\n{e}")
        QMessageBox.information(self, "Import complete", f"Imported {total} transactions from CSV.")
        self.apply_transaction_filters()

    def import_statement_pdf(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Import Bank Statement PDF", "", "PDF Files (*.pdf);;All Files (*)")
        if not files:
            return

        for name, t in [
            ("Credit Card", "Liability"),
            ("Uncategorized Expense", "Expense"),
            ("checking Account", "Asset")
        ]:
            if name not in self.gl.accounts:
                self.gl.create_account(name, t)

        self.progress_dialog = QProgressDialog("Importing PDF Statements...", "Cancel", 0, 100, self)
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setAutoClose(True)
        self.progress_dialog.setMinimumDuration(500)
        self.progress_dialog.show()

        bank = self.import_bank.currentData()
        statement_type = self.import_statement_type.currentData() or "auto"
        self.import_thread = PDFImportThread(files=files, ledger=self.gl, bank=bank, statement_type=statement_type)

        # 2. Connect to the new progress dialog updater
        self.import_thread.progress_signal.connect(self.update_progress_dialog)
        self.import_thread.finished_signal.connect(self.on_import_success)
        self.import_thread.error_signal.connect(self.on_import_error)
        self.import_thread.finished.connect(self.import_thread.deleteLater)

        self.btn_import_pdf.setEnabled(False)
        self.import_thread.start()

    def update_status_bar(self, percent: int, message: str):
        self.statusBar().showMessage(message)

    def on_import_success(self, imported_count: int, review: dict):
        if hasattr(self, 'progress_dialog') and self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None

        self.btn_import_pdf.setEnabled(True)

        skipped = review.get("skipped_lines", [])
        mismatches = review.get("balance_mismatches", [])
        unverified = review.get("unverified_count", 0)
        needs_review = bool(skipped or mismatches or unverified)
        suggested_matches = review.get("suggested_matches", [])

        needs_review = bool(skipped or mismatches or unverified)
        summary = f"Imported {imported_count} transactions successfully."

        if suggested_matches:
            dialog = SuggestedMatchesDialog(suggested_matches, self.gl, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._process_confirmed_matches(dialog.get_confirmations())
                rejected_count = self._process_rejected_matches(dialog.get_rejections(), source="pdf")
                imported_count += rejected_count

        needs_review = bool(skipped or mismatches or unverified)
        summary = f"Imported {imported_count} transactions successfully."

        if needs_review:
            parts = []
            if skipped:
                parts.append(f"{len(skipped)} line(s) could not be parsed as transactions")
            if mismatches:
                parts.append(f"{len(mismatches)} transaction(s) failed a balance sanity check")
            if unverified:
                parts.append(f"{unverified} transaction(s) could not be balance-checked (e.g. no opening balance found)")
            summary += "\n\nHowever, " + "; ".join(parts) + ". Please review before trusting this import."

            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning if (skipped or mismatches) else QMessageBox.Icon.Information)
            box.setWindowTitle("Import Complete - Review Needed")
            box.setText(summary)

            detail_lines = []

            if skipped:
                detail_lines.append("=== Lines that could not be parsed ===")
                for item in skipped[:50]:
                    detail_lines.append(f"[{item['file']}] {item['reason']}\n    {item['line']}")
                if len(skipped) > 50:
                    detail_lines.append(f"... and {len(skipped) - 50} more")
                detail_lines.append("")
            if mismatches:
                detail_lines.append("=== Transactions that failed the balance check ===")
                for item in mismatches[:50]:
                    detail_lines.append(f"[{item['file']}] {item['date']} - {item['description']}: {item['detail']}")
                if len(mismatches) > 50:
                    detail_lines.append(f"... and {len(mismatches) - 50} more")

            if detail_lines:
                box.setDetailedText("\n".join(detail_lines))
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            box.exec()
        else:
            QMessageBox.information(self, "Success", summary)

        self.apply_transaction_filters()
        self.apply_transactions_tab_filters()
        self.refresh_overview()

    def on_import_error(self, error_message: str):
        if hasattr(self, 'progress_dialog') and self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        self.btn_import_pdf.setEnabled(True)
        QMessageBox.critical(self, "Import Error", error_message)

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
                self.apply_transaction_filters()
                self.apply_transactions_tab_filters()
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
        self.apply_transaction_filters()
        self.apply_transactions_tab_filters()
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

    def edit_selected_transaction(self):
        COL_ACCOUNT = 0
        COL_DEBIT = 1
        COL_CREDIT = 2
        COL_LINEID = 3
        COL_DELETE = 4

        row = self.tx_table2.currentRow()
        if row < 0:
            QMessageBox.information(self, "No selection", "Please select a transaction to edit.")
            return

        tx_id = self._tx_table_id_map.get(row)
        if tx_id is None:
            QMessageBox.warning(self, "Error", "Could not find transaction ID for selected row.")
            return

        tx = next((t for t in self.gl.transactions if t.id == tx_id), None)
        if tx is None:
            QMessageBox.warning(self, "Error", "Transaction not found.")
            return

        # Numeric delegate: constrains what characters CAN be typed into the
        # cell (a keystroke-level input mask) -- this stays, since it's about
        # the editing widget's behavior, not a business rule. Whatever ends
        # up in the cell still goes to the ledger unvalidated by this dialog.
        class _NumericDelegate(QStyledItemDelegate):
            def createEditor(self, parent, option, index):
                editor = QLineEdit(parent)
                validator = QDoubleValidator(parent)
                validator.setNotation(QDoubleValidator.Notation.StandardNotation)
                validator.setDecimals(10)
                editor.setValidator(validator)
                return editor

            def setEditorData(self, editor, index):
                val = index.model().data(index, Qt.ItemDataRole.DisplayRole)
                editor.setText(str(val) if val is not None else "")

            def setModelData(self, editor, model, index):
                text = editor.text().strip()
                model.setData(index, text, Qt.ItemDataRole.EditRole)

        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Transaction")
        dlg.resize(700, 600)
        dlg.setMinimumSize(500, 400)
        dlg_layout = QVBoxLayout(dlg)

        form = QFormLayout()
        dlg_date = QDateEdit()
        dlg_date.setCalendarPopup(True)
        try:
            dt = datetime.strptime(tx.date[:10], "%Y-%m-%d")
            dlg_date.setDate(QDate(dt.year, dt.month, dt.day))
        except Exception:
            dlg_date.setDate(QDate.currentDate())

        dlg_desc = QLineEdit()
        dlg_desc.setText(tx.description or "")
        form.addRow("Date:", dlg_date)
        form.addRow("Description:", dlg_desc)
        dlg_layout.addLayout(form)

        lines_box = QGroupBox("Transaction Lines (Double-entry)")
        lines_layout = QVBoxLayout(lines_box)

        legend_html = """
        <span style='color:#2e7d32; font-weight:bold;'>■ Asset</span> &nbsp;&nbsp;
        <span style='color:#c62828; font-weight:bold;'>■ Expense</span> &nbsp;&nbsp;
        <span style='color:#1565c0; font-weight:bold;'>■ Equity</span> &nbsp;&nbsp;
        <span style='color:#6a1b9a; font-weight:bold;'>■ Revenue</span> &nbsp;&nbsp;
        <span style='color:#333333; font-weight:bold;'>■ Liability</span>
        """
        legend_label = QLabel(legend_html)
        legend_label.setTextFormat(Qt.TextFormat.RichText)
        legend_label.setStyleSheet("margin-bottom: 4px;")
        lines_layout.addWidget(legend_label)

        lines_table = QTableWidget()
        lines_table.setColumnCount(5)
        lines_table.setHorizontalHeaderLabels(["Account", "Debit", "Credit", "LineId", ""])
        lines_table.setColumnHidden(COL_LINEID, True)
        lines_table.setRowCount(0)
        lines_table.setSortingEnabled(False)

        header = lines_table.horizontalHeader()
        header.setSectionResizeMode(COL_DEBIT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_CREDIT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_DELETE, QHeaderView.ResizeMode.Fixed)
        lines_table.setColumnWidth(COL_DELETE, 120)
        header.setSectionResizeMode(COL_ACCOUNT, QHeaderView.ResizeMode.Interactive)

        def _stretch_account():
            total_width = lines_table.viewport().width()
            other_width = sum(lines_table.columnWidth(i) for i in [COL_DEBIT, COL_CREDIT, COL_DELETE])
            account_width = total_width - other_width - 15
            if account_width > 150:
                lines_table.setColumnWidth(COL_ACCOUNT, account_width)

        QTimer.singleShot(0, _stretch_account)

        num_delegate = _NumericDelegate(lines_table)
        lines_table.setItemDelegateForColumn(COL_DEBIT, num_delegate)
        lines_table.setItemDelegateForColumn(COL_CREDIT, num_delegate)

        lines_layout.addWidget(lines_table)

        # Totals: informational only -- see add_new_transaction for the same note.
        totals_layout = QHBoxLayout()
        totals_label = QLabel("Total Debit: 0.00    Total Credit: 0.00")
        totals_layout.addWidget(totals_label)
        totals_layout.addStretch()
        lines_layout.addLayout(totals_layout)

        account_names = sorted(self.gl.accounts.keys())

        def create_amount_item(value):
            item = QTableWidgetItem(str(value))
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return item

        def create_line_row(account=None, debit="0.00", credit="0.00", line_id=""):
            r = lines_table.rowCount()
            lines_table.insertRow(r)

            acc_combo = self._create_account_combo(account)
            lines_table.setCellWidget(r, COL_ACCOUNT, acc_combo)

            lines_table.setItem(r, COL_DEBIT, create_amount_item(debit))
            lines_table.setItem(r, COL_CREDIT, create_amount_item(credit))

            # line_id is the real sql_transaction_lines.id -- carried through
            # so update_transaction can preserve it for the audit trail
            # rather than reassigning a fresh id to every line on every edit.
            line_id_item = QTableWidgetItem(str(line_id) if line_id not in (None, "") else "")
            line_id_item.setFlags(line_id_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            lines_table.setItem(r, COL_LINEID, line_id_item)

            del_btn = QPushButton("Delete")
            def _on_delete():
                sender = dlg.sender()
                if sender is None:
                    return
                for rr in range(lines_table.rowCount()):
                    if lines_table.cellWidget(rr, COL_DELETE) is sender:
                        lines_table.removeRow(rr)
                        _update_totals()
                        return
            del_btn.clicked.connect(_on_delete)
            lines_table.setCellWidget(r, COL_DELETE, del_btn)

        for line in tx.lines:
            create_line_row(
                account=line.account_name,
                debit=line.dr,
                credit=line.cr,
                line_id=line.id
            )

        # A brand new row has no line_id, which is exactly what
        # update_transaction needs to see to treat it as a new line rather
        # than trying to match it against an existing one.
        add_btn = QPushButton("Add Line")
        def add_line():
            create_line_row()
            _update_totals()
        add_btn.clicked.connect(add_line)
        lines_layout.addWidget(add_btn)

        dlg_layout.addWidget(lines_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        dlg_layout.addWidget(buttons)

        def _update_totals():
            # Best-effort display only -- not a gate on Save.
            total_dr = Decimal("0")
            total_cr = Decimal("0")
            for rr in range(lines_table.rowCount()):
                dr_item = lines_table.item(rr, COL_DEBIT)
                cr_item = lines_table.item(rr, COL_CREDIT)
                for text, side in ((dr_item.text() if dr_item else "0", "dr"), (cr_item.text() if cr_item else "0", "cr")):
                    try:
                        val = Decimal(text.strip()) if text.strip() else Decimal("0")
                    except Exception:
                        val = Decimal("0")
                    if side == "dr":
                        total_dr += val
                    else:
                        total_cr += val
            totals_label.setText(f"Total Debit: {format(total_dr, 'f')}    Total Credit: {format(total_cr, 'f')}")

        lines_table.itemChanged.connect(lambda *_: _update_totals())
        _update_totals()

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        # -------------------------
        # Collect raw input and hand it straight to the ledger. No amount
        # parsing, no balance check, no negative/zero check, no minimum-line
        # count here -- update_transaction is the sole authority on all of
        # that. The only things checked here are genuine UI-completeness
        # concerns: did the dialog actually have a working account selector
        # in every row.
        # -------------------------
        new_lines = []  # list of (line_id_or_None, account, dr_text, cr_text)
        for i in range(lines_table.rowCount()):
            acc_widget = lines_table.cellWidget(i, COL_ACCOUNT)
            if acc_widget is None:
                QMessageBox.warning(self, "Error", f"Row {i + 1} has no account selector -- this shouldn't happen.")
                return

            account = acc_widget.currentData()
            if not account:
                QMessageBox.warning(self, "No accounts available", "Create at least one account before editing transactions.")
                return

            dr_item = lines_table.item(i, COL_DEBIT)
            cr_item = lines_table.item(i, COL_CREDIT)
            line_id_item = lines_table.item(i, COL_LINEID)

            dr_text = dr_item.text().strip() if dr_item is not None else "0"
            cr_text = cr_item.text().strip() if cr_item is not None else "0"
            line_id_text = line_id_item.text().strip() if line_id_item is not None else ""
            line_id = int(line_id_text) if line_id_text else None

            new_lines.append((line_id, account, dr_text, cr_text))

        new_date = dlg_date.date().toString("yyyy-MM-dd")
        new_desc = dlg_desc.text().strip()

        ok, msg = self.gl.update_transaction(tx_id, new_date, new_desc, new_lines)
        if not ok:
            QMessageBox.warning(self, "Transaction Not Updated", msg)
            return

        self.statusBar().showMessage("Transaction updated.", 3000)
        self.apply_transactions_tab_filters()
        self.apply_transaction_filters()
        self.refresh_overview()
        self.refresh_accounts_table()

    def add_new_transaction(self):

        COL_ACCOUNT = 0
        COL_DEBIT = 1
        COL_CREDIT = 2
        COL_DELETE = 3

        # Numeric delegate: constrains what characters CAN be typed into the
        # cell (a keystroke-level input mask), which is a different thing
        # from validating the resulting value -- this stays, since it's
        # about the editing widget's behavior, not a business rule. Whatever
        # ends up in the cell still goes to the ledger unvalidated by this
        # dialog; the ledger is what decides if it's actually acceptable.
        class _NumericDelegate(QStyledItemDelegate):
            def createEditor(self, parent, option, index):
                editor = QLineEdit(parent)
                validator = QDoubleValidator(parent)
                validator.setNotation(QDoubleValidator.Notation.StandardNotation)
                validator.setDecimals(10)
                editor.setValidator(validator)
                return editor

            def setEditorData(self, editor, index):
                val = index.model().data(index, Qt.ItemDataRole.DisplayRole)
                editor.setText(str(val) if val is not None else "")

            def setModelData(self, editor, model, index):
                text = editor.text().strip()
                model.setData(index, text, Qt.ItemDataRole.EditRole)

        dlg = QDialog(self)
        dlg.setWindowTitle("Add Transaction")
        dlg.resize(700, 600)
        dlg.setMinimumSize(500, 400)
        dlg_layout = QVBoxLayout(dlg)

        form = QFormLayout()
        dlg_date = QDateEdit()
        dlg_date.setCalendarPopup(True)
        dlg_date.setDate(QDate.currentDate())

        dlg_desc = QLineEdit()
        form.addRow("Date:", dlg_date)
        form.addRow("Description:", dlg_desc)
        dlg_layout.addLayout(form)

        lines_box = QGroupBox("Transaction Lines (Double-entry)")
        lines_layout = QVBoxLayout(lines_box)

        legend_html = """
        <span style='color:#2e7d32; font-weight:bold;'>■ Asset</span> &nbsp;&nbsp;
        <span style='color:#c62828; font-weight:bold;'>■ Expense</span> &nbsp;&nbsp;
        <span style='color:#1565c0; font-weight:bold;'>■ Equity</span> &nbsp;&nbsp;
        <span style='color:#6a1b9a; font-weight:bold;'>■ Revenue</span> &nbsp;&nbsp;
        <span style='color:#333333; font-weight:bold;'>■ Liability</span>
        """
        legend_label = QLabel(legend_html)
        legend_label.setTextFormat(Qt.TextFormat.RichText)
        legend_label.setStyleSheet("margin-bottom: 4px;")
        lines_layout.addWidget(legend_label)

        lines_table = QTableWidget()
        lines_table.setColumnCount(4)
        lines_table.setHorizontalHeaderLabels(["Account", "Debit", "Credit", ""])
        lines_table.setRowCount(0)
        lines_table.setSortingEnabled(False)

        header = lines_table.horizontalHeader()
        header.setSectionResizeMode(COL_DEBIT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_CREDIT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_DELETE, QHeaderView.ResizeMode.Fixed)
        lines_table.setColumnWidth(COL_DELETE, 120)
        header.setSectionResizeMode(COL_ACCOUNT, QHeaderView.ResizeMode.Interactive)

        def _stretch_account():
            total_width = lines_table.viewport().width()
            other_width = sum(lines_table.columnWidth(i) for i in [COL_DEBIT, COL_CREDIT, COL_DELETE])
            account_width = total_width - other_width - 15
            if account_width > 150:
                lines_table.setColumnWidth(COL_ACCOUNT, account_width)

        QTimer.singleShot(0, _stretch_account)

        num_delegate = _NumericDelegate(lines_table)
        lines_table.setItemDelegateForColumn(COL_DEBIT, num_delegate)
        lines_table.setItemDelegateForColumn(COL_CREDIT, num_delegate)

        lines_layout.addWidget(lines_table)

        # Totals: informational only, purely a live readout for the user's
        # own benefit while filling the form -- has no bearing on whether
        # Save succeeds, so a best-effort display (falling back to 0 on
        # anything unparseable) is fine here in a way it wouldn't be if this
        # fed into validation.
        totals_layout = QHBoxLayout()
        totals_label = QLabel("Total Debit: 0.00    Total Credit: 0.00")
        totals_layout.addWidget(totals_label)
        totals_layout.addStretch()
        lines_layout.addLayout(totals_layout)


        def create_amount_item(value):
            item = QTableWidgetItem(str(value))
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return item

        def create_line_row(account=None, debit="0.00", credit="0.00"):
            r = lines_table.rowCount()
            lines_table.insertRow(r)

            acc_combo = self._create_account_combo(account)
            lines_table.setCellWidget(r, COL_ACCOUNT, acc_combo)

            lines_table.setItem(r, COL_DEBIT, create_amount_item(debit))
            lines_table.setItem(r, COL_CREDIT, create_amount_item(credit))

            del_btn = QPushButton("Delete")
            def _on_delete():
                sender = dlg.sender()
                if sender is None:
                    return
                for rr in range(lines_table.rowCount()):
                    if lines_table.cellWidget(rr, COL_DELETE) is sender:
                        lines_table.removeRow(rr)
                        _update_totals()
                        return
            del_btn.clicked.connect(_on_delete)
            lines_table.setCellWidget(r, COL_DELETE, del_btn)

        create_line_row()
        create_line_row()

        add_btn = QPushButton("Add Line")
        def add_line():
            create_line_row()
            _update_totals()
        add_btn.clicked.connect(add_line)
        lines_layout.addWidget(add_btn)

        dlg_layout.addWidget(lines_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        dlg_layout.addWidget(buttons)

        def _update_totals():
            # Best-effort display only -- see note above. Not a gate on Save.
            total_dr = Decimal("0")
            total_cr = Decimal("0")
            for rr in range(lines_table.rowCount()):
                dr_item = lines_table.item(rr, COL_DEBIT)
                cr_item = lines_table.item(rr, COL_CREDIT)
                for text, side in ((dr_item.text() if dr_item else "0", "dr"), (cr_item.text() if cr_item else "0", "cr")):
                    try:
                        val = Decimal(text.strip()) if text.strip() else Decimal("0")
                    except Exception:
                        val = Decimal("0")
                    if side == "dr":
                        total_dr += val
                    else:
                        total_cr += val
            totals_label.setText(f"Total Debit: {format(total_dr, 'f')}    Total Credit: {format(total_cr, 'f')}")

        lines_table.itemChanged.connect(lambda *_: _update_totals())
        _update_totals()

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        # -------------------------
        # Collect raw input and hand it straight to the ledger. No amount
        # parsing, no balance check, no negative/zero check, no minimum-line
        # count here -- record_transaction is the sole authority on all of
        # that, and handles bad input (unparseable text, NaN/Infinity, too
        # few lines) gracefully with a clear message. The only things
        # checked here are genuine UI-completeness concerns: did the dialog
        # actually have a working account selector in every row.
        # -------------------------
        lines_data = []
        for i in range(lines_table.rowCount()):
            acc_widget = lines_table.cellWidget(i, COL_ACCOUNT)
            if acc_widget is None:
                QMessageBox.warning(self, "Error", f"Row {i + 1} has no account selector -- this shouldn't happen.")
                return

            account = acc_widget.currentData()
            if not account:
                # Only reachable if the ledger currently has zero accounts to
                # choose from -- worth a clear message immediately rather
                # than a confusing round-trip for something this basic.
                QMessageBox.warning(self, "No accounts available", "Create at least one account before adding transactions.")
                return

            dr_item = lines_table.item(i, COL_DEBIT)
            cr_item = lines_table.item(i, COL_CREDIT)
            dr_text = dr_item.text().strip() if dr_item is not None else "0"
            cr_text = cr_item.text().strip() if cr_item is not None else "0"

            lines_data.append((account, dr_text, cr_text))

        new_date = dlg_date.date().toString("yyyy-MM-dd")
        new_desc = dlg_desc.text().strip()

        ok, msg = self.gl.record_transaction(new_date, new_desc, lines_data)
        if not ok:
            QMessageBox.warning(self, "Transaction Not Added", msg)
            return

        self.statusBar().showMessage("Transaction updated.", 3000)
        self.apply_transactions_tab_filters()
        self.apply_transaction_filters()
        self.refresh_overview()
        self.refresh_accounts_table()


    def delete_selected_transaction(self):
        selected = self.tx_table2.selectedItems()
        if not selected:
            QMessageBox.information(self, "No selection", "Please select a transaction to delete.")
            return

        reply = QMessageBox.question(
            self,
            "Delete transaction",
            "Are you sure you want to delete this transaction?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        row = selected[0].row()
        if not hasattr(self, "_tx_table_id_map"):
            return
        tx_id = self._tx_table_id_map.get(row)
        if tx_id is None:
            QMessageBox.warning(self, "Error", "Could not find transaction ID for selected row.")
            return

        ok, msg = self.gl.delete_transaction(tx_id)
        if ok:
            QMessageBox.information(self, "Success", "Transaction deleted.")
            self.apply_transactions_tab_filters()
            self.apply_transaction_filters()
        else:
            QMessageBox.warning(self, "Error", msg)
