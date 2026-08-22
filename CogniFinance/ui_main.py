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
    QComboBox, QDateEdit, QGroupBox, QDialog, QDialogButtonBox, QSizePolicy, QAbstractItemView
)
from PyQt6.QtCore import Qt, QDate, QTimer
from PyQt6.QtGui import QColor, QFont, QPixmap, QImage
from datetime import datetime

from ledger import GeneralLedger, Budget, ZERO
from parser_integration import parse_statement_to_transactions
from tax_manager import TaxTemplateManager
from pdf_import import PDFImportThread
from decimal import Decimal

from desktop_sync_server import CogniSyncServer
from protocol import make_pairing_code
from log_tab import LogTab
from audit_tab import AuditTab
from tax_tab import TaxTab
from ofx_import import parse_ofx_to_ledger_transactions
from integrity_checker import IntegrityChecker, should_run_integrity_check, save_cooldown_timestamp

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

logger = logging.getLogger(__name__)

# ui_main.py v0.2 - automatic data-view refresh

class BudgetApp(QMainWindow):
    def __init__(self, db_path: str, passphrase: str):
        super().__init__()
        self.gl = GeneralLedger(db_path, passphrase)
        self._tx_table_id_map = {}
        self.rule_edit_id = None

        # Mobile sync state. The sync server is deliberately ephemeral: it is
        # created when the user starts pairing/sync and stopped when the phone
        # sends /api/shutdown or the user stops it from the UI.
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
        self.tab_accounts = QWidget()
        self.tab_transactions = QWidget()
        self.tab_transactions2 = QWidget()
        self.tab_budgets = QWidget()
        self.tab_charts = QWidget()
        self.tab_reports = QWidget()
        self.tab_reconcile = QWidget()
        self.tab_balance = QWidget()
        self.tab_rules = QWidget()
        self.tab_taxes = TaxTab(self.gl)
        self.tab_sync = QWidget()
        self.tab_log = LogTab()
        self.tab_audit = AuditTab(db_path=self.gl.db_path, passphrase=self.gl.passphrase)

        self.tabs.addTab(self.tab_overview, "Overview")
        self.tabs.addTab(self.tab_accounts, "Accounts")
        self.tabs.addTab(self.tab_transactions, "Transactions")
        self.tabs.addTab(self.tab_transactions2, "Add/Edit")
        self.tabs.addTab(self.tab_budgets, "Budgets")
        self.tabs.addTab(self.tab_charts, "Charts")
        self.tabs.addTab(self.tab_reports, "Reports")
        self.tabs.addTab(self.tab_reconcile, "Reconciliation")
        self.tabs.addTab(self.tab_balance, "Balance Sheet")
        self.tabs.addTab(self.tab_rules, "Category Rules")
        self.tabs.addTab(self.tab_taxes, "Taxes")
        self.tabs.addTab(self.tab_sync, "Mobile Sync")
        self.tabs.addTab(self.tab_log, "Logs")
        self.tabs.addTab(self.tab_audit, "Audit Log")

        self._init_overview()
        self._init_accounts()
        self._init_transactions()
        self._init_transactions_tab()
        self._init_budgets()
        self._init_charts()
        self._init_reports_tab()
        self._init_reconcile()
        self._init_balance_sheet()
        self._init_category_rules()
        self._init_sync_tab()

        logging.getLogger().addHandler(self.tab_log.log_handler)
        # Data-view tabs refresh automatically when they become active.
        # Input/action tabs are intentionally excluded so navigation never
        # resets an in-progress form or operation.

        # --- NEW: Schedule background integrity checks WITH A DELAY ---
        self.integrity_checker = None

        # Wait 5 seconds (5000ms) after the UI is fully drawn before starting.
        # This ensures the user can interact with the app immediately upon launch.
        QTimer.singleShot(5000, self._schedule_integrity_check)

        # Data-view tabs refresh automatically...
        self.tabs.currentChanged.connect(self._refresh_current_tab)

    def _schedule_integrity_check(self):
        """Checks the cooldown and triggers the background test runner if due."""
        db_dir = os.path.dirname(self.gl.db_path)

        if should_run_integrity_check(db_dir, cooldown_days=7):
            # Show a non-blocking message to the user
            QMessageBox.information(
                self,
                "Weekly Integrity Check",
                "CogniFinance is running its weekly mathematical integrity checks in the background.\n\n"
                "This verifies that your ledger's accounting equation and audit trails are perfectly balanced.\n\n"
                "The app might be slightly slower for the next minute while these tests run. "
                "You can view the live progress in the 'Logs' tab."
            )

            main_dir = os.path.dirname(os.path.abspath(__file__))
            test_dir = os.path.join(main_dir, "test")

            # Start the QProcess-based checker
            self.integrity_checker = IntegrityChecker(test_dir)
            self.integrity_checker.log_output.connect(self._log_integrity_message)
            self.integrity_checker.finished_signal.connect(self._on_integrity_finished)
            self.integrity_checker.start()

    def _log_integrity_message(self, message: str):
        """Routes the pytest output directly into the Log Tab."""
        import logging
        # We use the root logger so it hits the QtLogHandler and goes to the UI
        if "PASSED" in message or "SUCCESS" in message:
            logging.info(message)
        elif "FAILED" in message or "ERROR" in message or "CRITICAL" in message:
            logging.error(message)
        else:
            logging.debug(message)

    def _on_integrity_finished(self, success: bool, summary: str):
        """Called when the background thread finishes."""
        db_dir = os.path.dirname(self.gl.db_path)

        if success:
            # Only update the cooldown if the tests actually passed
            save_cooldown_timestamp(db_dir)
            # Optional: Show a final success popup, or just let the Log Tab handle it
            # QMessageBox.information(self, "Integrity Verified", summary)
        else:
            # CRITICAL: If it fails, we DO NOT update the cooldown, so it will
            # try again next time the app is opened, and we alert the user loudly.
            QMessageBox.critical(
                self,
                "CRITICAL LEDGER FAILURE",
                f"The weekly integrity check FAILED.\n\n"
                f"{summary}\n\n"
                "Please open the 'Logs' tab to see exactly which mathematical "
                "invariants were violated. Your data may be corrupted."
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
            self.tab_charts: self.refresh_charts,
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

        # Make sure tables in the newly shown tab get sensible widths.
        if widget is not None:
            for table in widget.findChildren(QTableWidget):
                self._fit_table_columns(table)

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
        btn_delete = QPushButton("Delete Selected")
        btn_delete.clicked.connect(self.delete_selected_account)

        btn_row.addWidget(btn_add)
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
        type_combo.setCurrentText("Expense") # Default to Expense for easy category creation

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
                self.refresh_account_combos() # Crucial: Updates all dropdowns app-wide
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

    def import_ofx_qfx(self):
        """Handles importing structured OFX/QFX bank feeds."""
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import OFX/QFX Bank Feed", "",
            "Bank Feeds (*.ofx *.qfx);;All Files (*)"
        )
        if not files:
            return

        total_imported = 0
        total_skipped = 0

        for file_path in files:
            try:
                txns, new_accounts = parse_ofx_to_ledger_transactions(file_path, self.gl)

                if new_accounts:
                    names = ", ".join([f"{n} ({t})" for n, t in new_accounts])
                    logger.info(f"OFX Import: Auto-created accounts: {names}")

                imported_count = 0
                for tx in txns:
                    ok, msg = self.gl.record_transaction(
                        date=tx["date"],
                        description=tx["description"],
                        lines_data=tx["lines"],
                        imported_id=tx["imported_id"],
                        persist=True
                    )
                    if ok:
                        imported_count += 1
                    else:
                        logger.warning(f"OFX Import: Ledger rejected transaction '{tx['description']}': {msg}")
                        total_skipped += 1

                total_imported += imported_count
                logger.info(f"OFX Import: Successfully imported {imported_count} transactions from {os.path.basename(file_path)}")

            except Exception as e:
                logger.error(f"OFX Import failed for {os.path.basename(file_path)}: {e}")
                QMessageBox.critical(self, "Import Failed", f"Error processing {os.path.basename(file_path)}:\n{e}")

        # Refresh all views
        self.apply_transaction_filters()
        self.apply_transactions_tab_filters()
        self.refresh_overview()
        self.refresh_account_combos() # Crucial: Updates dropdowns with newly created bank accounts

        QMessageBox.information(
            self,
            "Import Complete",
            f"Successfully imported {total_imported} transaction(s).\n"
            f"Skipped/Rejected: {total_skipped}.\n\n"
            "Note: Transactions are currently categorized as 'Uncategorized'. "
            "Please review them in the Transactions tab or set up Category Rules."
        )

    def _money_item(self, value, editable=False):
        """
        Create a right-aligned, non-editable table item for monetary values.
        """
        item = QTableWidgetItem(f"{value:,.2f}")
        item.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

        return item

    def _setup_table_headers(self, table: QTableWidget, fractions=None):
        """
        Make table columns user-adjustable and give them sensible default widths.

        fractions should be a list/tuple of floats, one per column.
        The last column is allowed to stretch to fill the remaining width.
        """
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

            header.sectionResized.connect(
                lambda *_: self._table_section_resized(table)
            )

            QTimer.singleShot(0, lambda: self._fit_table_columns(table))


    def _table_section_resized(self, table: QTableWidget):
        """
        Track when the user manually resizes a column so we do not override it later.
        """
        if table.property("applying_widths"):
            return

        table.setProperty("user_adjusted", True)


    def _fit_table_columns(self, table: QTableWidget):
        """
        Apply initial proportional column widths.
        """
        fractions = table.property("fractions")

        if not fractions:
            return

        if table.property("user_adjusted"):
            return

        column_count = table.columnCount()
        if column_count == 0:
            return

        total_width = table.viewport().width()
        if total_width <= 0:
            total_width = table.width()

        if total_width <= 0:
            total_width = 1000

        usable_width = max(200, total_width - 2)
        last_column = column_count - 1

        table.setProperty("applying_widths", True)

        try:
            for col in range(column_count):
                # Let the last column stretch naturally.
                if col == last_column:
                    continue

                if col < len(fractions):
                    fraction = fractions[col]
                else:
                    fraction = 1.0 / column_count

                width = max(70, int(usable_width * fraction))
                table.setColumnWidth(col, width)
        finally:
            table.setProperty("applying_widths", False)

    def _init_overview(self):
        layout = QVBoxLayout(self.tab_overview)
        self.overview_text = QTextEdit()
        self.overview_text.setReadOnly(True)
        self.overview_text.setFont(QFont("Monospace", 10))
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
        btn_row.addWidget(btn_export)
        btn_row.addWidget(btn_import)
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
            return (
                '<table width="100%" cellspacing="0" cellpadding="2">'
                + "".join(rows)
                + "</table>"
            )

        net_color = "#007a00" if report["net"] >= ZERO else "#b00000"

        month_rows = [
            amount_row("Income", report["income_total"]),
            amount_row("Expenses", report["expense_total"]),
            amount_row("Net", report["net"], bold=True, color=net_color),
        ]



        # ------------------------------------------------------------
        # Budget alerts
        # ------------------------------------------------------------
        budgets = [
            b
            for b in self.gl.get_all_budgets()
            if b.month == month
        ]

        spending = self.gl.spending_by_category(month)

        alert_rows = []

        for b in sorted(budgets, key=lambda x: x.category):
            actual = spending.get(b.category, ZERO)

            if actual > b.amount:
                over_by = actual - b.amount
                alert_rows.append(
                    amount_row(
                        f"{b.category} over budget",
                        over_by,
                        color="#b00000"
                    )
                )

        if not alert_rows:
            if budgets:
                alert_rows.append(
                    f'<tr><td style="{muted_style}">No budget overruns this month.</td></tr>'
                )
            else:
                alert_rows.append(
                    f'<tr><td style="{muted_style}">No budgets set for this month.</td></tr>'
                )
        # ------------------------------------------------------------
        # Recent transactions
        # ------------------------------------------------------------
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
            recent_rows.append(
                f'<tr><td style="{muted_style}">No transactions yet.</td></tr>'
            )

        # ------------------------------------------------------------
        # Reconciliation
        # ------------------------------------------------------------
        unreconciled_count = len(self.gl.get_unreconciled_transactions())

        reconciliation_rows = [
            f"<tr><td>{unreconciled_count} unreconciled transaction(s)</td></tr>"
        ]

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

        self._add_refresh_row(
            layout,
            self.apply_transaction_filters,
            self.filter_search
        )

        filter_box = QGroupBox("Filters")
        flayout = QFormLayout()

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
        self.tx_table.setHorizontalHeaderLabels(
            ["Date", "Description", "Accounts", "Amount"]
        )

        self._setup_table_headers(
            self.tx_table,
            [0.11, 0.42, 0.31, 0.16]
        )

        layout.addWidget(self.tx_table)

        self.apply_transaction_filters()

    def _transaction_display_amount(self, tx) -> Decimal:
        from decimal import Decimal


        amount = Decimal("0.00")

    # Prefer expense debit
        for line in tx.lines:
            acc = self.gl.accounts[line.account_name]
            if acc.account_type == "Expense" and line.dr > 0:
                amount += line.dr

        if amount > 0:
            return amount

    # Then revenue credit
        for line in tx.lines:
            acc = self.gl.accounts[line.account_name]
            if acc.account_type == "Revenue" and line.cr > 0:
                amount += line.cr

        if amount > 0:
            return amount

    # Fallback: first non-zero debit or credit
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
            amount = sum(l.dr for l in tx.lines if self.gl.accounts[l.account_name].account_type == "Expense")
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
        self.tx_table2.setHorizontalHeaderLabels(
            ["Date", "Description", "From", "To", "Amount"]
        )

        self._setup_table_headers(
            self.tx_table2,
            [0.11, 0.37, 0.18, 0.18, 0.16]
        )

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
        current_rule = self.rule_category.currentData()

        self.tx_from.clear()
        self.tx_to.clear()
        self.budget_category.clear()
        self.rule_category.clear()

        for name in account_names:
            self.tx_from.addItem(name, name)
            self.tx_to.addItem(name, name)

        expense_accounts = [
            name
            for name, acc in self.gl.accounts.items()
            if acc.account_type == "Expense"
        ]

        for name in sorted(expense_accounts):
            self.budget_category.addItem(name, name)
            self.rule_category.addItem(name, name)

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

        if current_rule:
            idx = self.rule_category.findData(current_rule)
            if idx >= 0:
                self.rule_category.setCurrentIndex(idx)

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
                from_acc = tx.lines[0].account_name
                to_acc = tx.lines[1].account_name
            else:
                from_acc = tx.lines[0].account_name if tx.lines else ""
                to_acc = ""

            self.tx_table2.setItem(row, 2, QTableWidgetItem(from_acc))
            self.tx_table2.setItem(row, 3, QTableWidgetItem(to_acc))

            amount = 0.0
            for line in tx.lines:
                acc = self.gl.accounts[line.account_name]
                if acc.account_type in ("Revenue", "Expense"):
                    amount = line.cr if acc.account_type == "Revenue" else line.dr
                    break
            self.tx_table2.setItem(row, 4, QTableWidgetItem(f"{amount:,.2f}"))

            if tx.id is not None:
                self._tx_table_id_map[row] = tx.id

    def _init_budgets(self):
        layout = QVBoxLayout(self.tab_budgets)

        self._add_refresh_row(layout, self.refresh_budget_list)

        form_box = QGroupBox("Set Monthly Budget")
        flayout = QFormLayout()

        self.budget_category = QComboBox()

        expense_accounts = [
            name
            for name, acc in self.gl.accounts.items()
            if acc.account_type == "Expense"
        ]

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
        self.budget_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.budget_table.setColumnCount(5)
        self.budget_table.setHorizontalHeaderLabels(
            ["Category", "Budget", "Actual", "Difference", "Used"]
        )

        self._setup_table_headers(self.budget_table)

        self.budget_table.setColumnWidth(0, 320)
        self.budget_table.setColumnWidth(1, 110)
        self.budget_table.setColumnWidth(2, 110)
        self.budget_table.setColumnWidth(3, 120)
        self.budget_table.setColumnWidth(4, 80)

        layout.addWidget(self.budget_table)

        self.budget_view_month.dateChanged.connect(
            lambda *_: self.refresh_budget_list()
        )

        budget_action_row = QHBoxLayout()

        if hasattr(self, "edit_selected_budget"):
            btn_edit_budget = QPushButton("Edit Selected Budget")
            btn_edit_budget.clicked.connect(self.edit_selected_budget)
            budget_action_row.addWidget(btn_edit_budget)

            self.budget_table.doubleClicked.connect(
                lambda *_: self.edit_selected_budget()
            )

        if hasattr(self, "delete_selected_budget"):
            btn_delete_budget = QPushButton("Delete Selected Budget")
            btn_delete_budget.clicked.connect(self.delete_selected_budget)
            budget_action_row.addWidget(btn_delete_budget)

        if budget_action_row.count():
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
        if hasattr(self, "budget_view_month"):
            month = self.budget_view_month.date().toString("yyyy-MM")
        else:
            month = self.budget_month.date().toString("yyyy-MM")

        budgets = [
            b
            for b in self.gl.get_all_budgets()
            if b.month == month
        ]

        actuals = self.gl.spending_by_category(month)

        self.budget_table.setRowCount(len(budgets))

        red = QColor("#b00000")
        green = QColor("#007a00")
        orange = QColor("#b06000")

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
                used_text = f"{used:.0f}%"

                used_item = QTableWidgetItem(used_text)

                if used > Decimal("100"):
                    used_item.setForeground(red)
                elif used > Decimal("90"):
                    used_item.setForeground(orange)
                else:
                    used_item.setForeground(green)
            else:
                used_item = QTableWidgetItem("Over" if actual > ZERO else "0%")
                used_item.setForeground(red if actual > ZERO else green)

            used_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )

            for item in (
                category_item,
                budget_item,
                actual_item,
                diff_item,
                used_item,
            ):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            self.budget_table.setItem(row, 0, category_item)
            self.budget_table.setItem(row, 1, budget_item)
            self.budget_table.setItem(row, 2, actual_item)
            self.budget_table.setItem(row, 3, diff_item)
            self.budget_table.setItem(row, 4, used_item)

    def _selected_budget_data(self):
        """
        Return (category, month, amount) for the selected budget row.
        """
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

        if hasattr(self, "budget_view_month"):
            month = self.budget_view_month.date().toString("yyyy-MM")
        else:
            month = self.budget_month.date().toString("yyyy-MM")

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
            QMessageBox.information(
                self,
                "No selection",
                "Please select a budget row to edit."
            )
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

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel
        )
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
            QMessageBox.warning(
                self,
                "Invalid amount",
                "Please enter a valid non-negative number."
            )
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
            QMessageBox.information(
                self,
                "No selection",
                "Please select a budget row to delete."
            )
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

    def _init_charts(self):
        layout = QVBoxLayout(self.tab_charts)
        self._add_refresh_row(layout, self.refresh_charts)

        controls = QHBoxLayout()
        self.chart_month = QDateEdit()
        self.chart_month.setCalendarPopup(True)
        self.chart_month.setDate(QDate.currentDate())

        controls.addWidget(QLabel("Month:"))
        controls.addWidget(self.chart_month)
        controls.addStretch()
        layout.addLayout(controls)

        self.figure = Figure(figsize=(6, 4), dpi=100)
        self.canvas = FigureCanvas(self.figure)
        layout.addWidget(self.canvas)

        self.refresh_charts()

    def refresh_charts(self):
        self.figure.clear()
        month = self.chart_month.date().toString("yyyy-MM")
        spending = self.gl.spending_by_category(month)

        if not spending:
            self.figure.text(0.5, 0.5, "No expense data for this month.", ha="center")
            self.canvas.draw()
            return

        ax = self.figure.add_subplot(111)
        cats = list(spending.keys())
        vals = [spending[c] for c in cats]

        ax.bar(range(len(cats)), vals)
        ax.set_xticks(range(len(cats)))
        ax.set_xticklabels(cats, rotation=30, ha="right")
        ax.set_title(f"Spending by Category – {month}")
        ax.set_ylabel("Amount ($)")

        self.canvas.draw()

    def _init_reports_tab(self):
        layout = QVBoxLayout(self.tab_reports)
        self._add_refresh_row(layout, self.refresh_monthly_report)

        month_box = QGroupBox("Month")
        mlayout = QFormLayout()

        # Get current date components once
        current_date = QDate.currentDate()
        current_year = current_date.year()
        current_month = current_date.month()

        self.report_year = QComboBox()
        current_year = QDate.currentDate().year()
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
            return (
                f'<tr><td colspan="2" style="{section_style}">'
                f"{escape(str(title))}"
                "</td></tr>"
            )

        income_rows = "".join(
            row(acc, amt, indent=1)
            for acc, amt in data["income_by_account"].items()
        )

        if not income_rows:
            income_rows = row("(none)", ZERO, indent=1)

        expense_rows = "".join(
            row(acc, amt, indent=1)
            for acc, amt in data["expense_by_account"].items()
        )

        if not expense_rows:
            expense_rows = row("(none)", ZERO, indent=1)

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

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Monthly Report",
            "",
            "Excel Files (*.xlsx);;All Files (*)",
        )
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

        self.reconcile_table = QTableWidget()
        self.reconcile_table.setColumnCount(5)
        self.reconcile_table.setHorizontalHeaderLabels(
            ["ID", "Date", "Description", "Imported ID", "Reconciled"]
        )

        self._setup_table_headers(
            self.reconcile_table,
            [0.08, 0.13, 0.52, 0.17, 0.10]
        )

        layout.addWidget(self.reconcile_table)

        btn_row = QHBoxLayout()

        btn_mark = QPushButton("Mark Selected as Reconciled")
        btn_mark.clicked.connect(self.mark_selected_reconciled)

        btn_row.addWidget(btn_mark)
        btn_row.addStretch()

        layout.addLayout(btn_row)

        self.load_unreconciled()

    def load_unreconciled(self):
        txs = self.gl.get_unreconciled_transactions()
        self.reconcile_table.setRowCount(len(txs))
        for row, tx in enumerate(txs):
            self.reconcile_table.setItem(row, 0, QTableWidgetItem(str(tx.id)))
            self.reconcile_table.setItem(row, 1, QTableWidgetItem(tx.date))
            self.reconcile_table.setItem(row, 2, QTableWidgetItem(tx.description))
            self.reconcile_table.setItem(row, 3, QTableWidgetItem(tx.imported_id or ""))
            rec_item = QTableWidgetItem("Yes" if tx.reconciled else "No")
            rec_item.setFlags(rec_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.reconcile_table.setItem(row, 4, rec_item)

    def mark_selected_reconciled(self):
        selected = self.reconcile_table.selectedItems()
        if not selected:
            QMessageBox.information(self, "No selection", "Please select a transaction row.")
            return

        row = selected[0].row()
        id_item = self.reconcile_table.item(row, 0)
        if not id_item:
            return
        try:
            tx_id = int(id_item.text())
        except ValueError:
            return

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
            return (
                f'<tr><td colspan="2" style="{section_style}">'
                f"{escape(str(title))}"
                "</td></tr>"
            )

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

        net = (
            bs["assets"]["total"]
            - bs["liabilities"]["total"]
            - bs["equity"]["total"]
        )

        html = f"""
        <html>
        <body style="font-family: 'Inter', sans-serif; font-size: 10pt;">
            <div style="font-weight: bold; font-size: 12pt;">
                BALANCE SHEET
            </div>
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
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Ledger CSV", "", "CSV Files (*.csv);;All Files (*)"
        )
        if not path:
            return
        try:
            self.gl.export_journal_csv(path)
            QMessageBox.information(self, "Export complete", f"Ledger exported to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))

    def import_ledger_csv(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import Ledger CSV", "", "CSV Files (*.csv);;All Files (*)"
        )
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
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import Bank Statement PDF", "", "PDF Files (*.pdf);;All Files (*)"
        )
        if not files:
            return

        for name, t in [
            ("Credit Card", "Liability"),
            ("Uncategorized Expense", "Expense"),
            ("checking Account", "Asset")
        ]:
            if name not in self.gl.accounts:
                self.gl.create_account(name, t)

        bank = self.import_bank.currentData()
        statement_type = self.import_statement_type.currentData() or "auto"
        # 1. Instantiate the thread
        # Important: Store it as a class attribute (self.thread) so it isn't garbage collected!
        self.import_thread = PDFImportThread(files=files, ledger=self.gl, bank=bank, statement_type=statement_type,)

        # 2. Connect the signals to UI update methods
        self.import_thread.progress_signal.connect(self.update_status_bar)
        self.import_thread.finished_signal.connect(self.on_import_success)
        self.import_thread.error_signal.connect(self.on_import_error)
        self.import_thread.finished.connect(self.import_thread.deleteLater)

        # 3. Start the thread
        self.btn_import_pdf.setEnabled(False)
        self.import_thread.start()

        # Optional: Disable the import button here so the user doesn't spam it
        # self.btn_import_pdf.setEnabled(False)

    def update_status_bar(self, perecent: int, message: str):
        """Updates UI status - runs on main thread safely."""
        self.statusBar().showMessage(message)

    def on_import_success(self, imported_count: int, review: dict):
        """Handles the parsed data - runs on main thread safely."""
        self.btn_import_pdf.setEnabled(True)

        skipped = review.get("skipped_lines", [])
        mismatches = review.get("balance_mismatches", [])
        unverified = review.get("unverified_count", 0)
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
        """Displays errors - runs on main thread safely."""
        # self.btn_import_pdf.setEnabled(True)
        self.btn_import_pdf.setEnabled(True)
        QMessageBox.critical(self, "Import Error", error_message)

    def _init_sync_tab(self):
        """Desktop UI for the ephemeral Cogni-Transfer LAN sync server."""
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
        """Fit the QR code completely inside its available display area."""
        pixmap = getattr(self, "sync_qr_pixmap", None)
        if pixmap is None or pixmap.isNull():
            return

        available = self.sync_qr_label.size()
        margin = 12
        width = max(1, available.width() - margin * 2)
        height = max(1, available.height() - margin * 2)
        size = min(width, height)

        # Keep the QR square and never scale it beyond the actual label.
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
            server = CogniSyncServer(self.gl, pairing_code)
            thread = threading.Thread(
                target=server.serve_forever,
                name="CogniSyncServer",
                daemon=True,
            )
            thread.start()
        except Exception as e:
            QMessageBox.critical(self, "Mobile Sync", f"Could not start the sync server:\n{e}")
            return

        self.sync_server = server
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

        self.sync_handshake_label.setText(
            f"Handshake: {'confirmed' if handshake else 'not confirmed'}"
        )
        self.sync_peer_label.setText(f"Phone identity: {peer}")
        self.sync_last_cursor_label.setText(f"Last sync cursor: {cursor}")
        self.sync_accounts_label.setText(
            "Unknown accounts: " + (", ".join(unknown) if unknown else "none")
        )
        self.sync_held_label.setText(f"Held transactions: {held}")
        self.sync_create_account_button.setEnabled(bool(unknown))
        self.sync_reprocess_button.setEnabled(held > 0)

        if handshake and self.sync_state_label.text() == "Connected — handshake confirmed":
            pass

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
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
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

    def _run_silent_auto_backup(self):
        """
        Creates a timestamped encrypted backup in ~/BudgetData/backups/
        and automatically prunes backups older than 7 days to prevent
        disk bloat. Runs silently; if it fails, it logs a warning but
        does NOT block the app from closing.
        """
        try:
            # Create a dedicated backups folder next to the main DB
            backup_dir = os.path.join(os.path.dirname(self.gl.db_path), "backups")
            os.makedirs(backup_dir, exist_ok=True)

            # Generate a timestamped filename
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            backup_path = os.path.join(backup_dir, f"auto_backup_{timestamp}.db")

            # --- CRITICAL: Flush WAL to main DB before backing up ---
            self.gl.checkpoint()

            # Call the existing ledger method directly!
            self.gl.create_encrypted_backup(backup_path)

            # --- Prune old backups (keep last 7 days) ---
            cutoff_time = time.time() - (30 * 86400)  # 7 days in seconds
            for filename in os.listdir(backup_dir):
                file_path = os.path.join(backup_dir, filename)
                if os.path.isfile(file_path) and os.path.getmtime(file_path) < cutoff_time:
                    try:
                        os.remove(file_path)
                    except OSError:
                        pass  # Best effort cleanup
        except Exception as e:
            # CRITICAL: We catch everything here. If the backup fails
            # (e.g., disk full, permissions issue), we just log it.
            # We MUST NOT show a popup or raise an error here, because
            # the user is trying to close the app and a popup would
            # block the exit process.
            import logging
            logging.warning(f"Silent auto-backup on close failed: {e}")

    def closeEvent(self, event):
        # 1. Run the auto-backup BEFORE closing the DB connection
        self._run_silent_auto_backup()

        # 2. Shutdown mobile sync
        self.stop_mobile_sync()
        try:
            self.gl.close()
        except Exception:
            pass
        super().closeEvent(event)

    def _init_category_rules(self):
        layout = QVBoxLayout(self.tab_rules)

        form_box = QGroupBox("Add / Edit Category Rule")
        flayout = QFormLayout()

        self.rule_keyword = QLineEdit()
        self.rule_keyword.setPlaceholderText("e.g. T&T, STARBUCKS, GROCERY")

        self.rule_category = QComboBox()

        expense_accounts = [
            name
            for name, acc in self.gl.accounts.items()
            if acc.account_type == "Expense"
        ]

        for name in sorted(expense_accounts):
            self.rule_category.addItem(name, name)

        self.rule_case = QComboBox()
        self.rule_case.addItem("Case insensitive", False)
        self.rule_case.addItem("Case sensitive", True)

        self.rule_enabled = QComboBox()
        self.rule_enabled.addItem("Enabled", True)
        self.rule_enabled.addItem("Disabled", False)

        flayout.addRow("Keyword:", self.rule_keyword)
        flayout.addRow("Category:", self.rule_category)
        flayout.addRow("Case match:", self.rule_case)
        flayout.addRow("Status:", self.rule_enabled)

        form_box.setLayout(flayout)
        layout.addWidget(form_box)

        btn_row = QHBoxLayout()

        self.rule_edit_id = None

        btn_add = QPushButton("Add Rule")
        btn_add.clicked.connect(self.add_category_rule)

        btn_update = QPushButton("Update Selected")
        btn_update.clicked.connect(self.update_category_rule)

        btn_delete = QPushButton("Delete Selected")
        btn_delete.clicked.connect(self.delete_category_rule)

        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_update)
        btn_row.addWidget(btn_delete)
        btn_row.addStretch()

        layout.addLayout(btn_row)

        self.rules_table = QTableWidget()
        self.rules_table.setColumnCount(5)
        self.rules_table.setHorizontalHeaderLabels(
            ["ID", "Keyword", "Category", "Case", "Enabled"]
        )

        self._setup_table_headers(
            self.rules_table,
            [0.08, 0.38, 0.32, 0.11, 0.11]
        )

        self.rules_table.itemSelectionChanged.connect(self.on_rule_selected)

        layout.addWidget(self.rules_table)

        self.refresh_rules_table()

    def refresh_rules_table(self):
        rules = self.gl.get_category_rules()
        self.rules_table.setRowCount(len(rules))
        for row, r in enumerate(rules):
            self.rules_table.setItem(row, 0, QTableWidgetItem(str(r.id)))
            self.rules_table.setItem(row, 1, QTableWidgetItem(r.keyword))
            self.rules_table.setItem(row, 2, QTableWidgetItem(r.category))
            self.rules_table.setItem(row, 3, QTableWidgetItem("Yes" if r.case_sensitive else "No"))
            self.rules_table.setItem(row, 4, QTableWidgetItem("Yes" if r.enabled else "No"))

    def on_rule_selected(self):
        selected = self.rules_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        id_item = self.rules_table.item(row, 0)
        if not id_item:
            return
        try:
            rule_id = int(id_item.text())
        except ValueError:
            return

        rule = next((r for r in self.gl.category_rules if r.id == rule_id), None)
        if not rule:
            return

        self.rule_edit_id = rule_id
        self.rule_keyword.setText(rule.keyword)

        idx = self.rule_category.findData(rule.category)
        if idx >= 0:
            self.rule_category.setCurrentIndex(idx)

        idx = self.rule_case.findData(rule.case_sensitive)
        if idx >= 0:
            self.rule_case.setCurrentIndex(idx)

        idx = self.rule_enabled.findData(rule.enabled)
        if idx >= 0:
            self.rule_enabled.setCurrentIndex(idx)

    def add_category_rule(self):
        keyword = self.rule_keyword.text().strip()
        category = self.rule_category.currentData()
        case_sensitive = self.rule_case.currentData()
        enabled = self.rule_enabled.currentData()

        if not keyword or not category:
            QMessageBox.warning(self, "Invalid rule", "Keyword and category are required.")
            return

        ok, msg = self.gl.add_category_rule(keyword, category, case_sensitive, enabled)
        if ok:
            QMessageBox.information(self, "Rule added", msg)
            self.refresh_rules_table()
            self.rule_keyword.clear()
        else:
            QMessageBox.warning(self, "Rule error", msg)

    def update_category_rule(self):
        if self.rule_edit_id is None:
            QMessageBox.information(self, "No selection", "Please select a rule to update.")
            return

        keyword = self.rule_keyword.text().strip()
        category = self.rule_category.currentData()
        case_sensitive = self.rule_case.currentData()
        enabled = self.rule_enabled.currentData()

        if not keyword or not category:
            QMessageBox.warning(self, "Invalid rule", "Keyword and category are required.")
            return

        ok, msg = self.gl.update_category_rule(
            self.rule_edit_id, keyword, category, case_sensitive, enabled
        )
        if ok:
            QMessageBox.information(self, "Rule updated", msg)
            self.refresh_rules_table()
        else:
            QMessageBox.warning(self, "Rule error", msg)

    def delete_category_rule(self):
        if self.rule_edit_id is None:
            QMessageBox.information(self, "No selection", "Please select a rule to delete.")
            return

        reply = QMessageBox.question(
            self,
            "Delete rule",
            "Are you sure you want to delete this rule?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        ok, msg = self.gl.delete_category_rule(self.rule_edit_id)
        if ok:
            QMessageBox.information(self, "Rule deleted", msg)
            self.refresh_rules_table()
            self.rule_keyword.clear()
            self.rule_edit_id = None
        else:
            QMessageBox.warning(self, "Rule error", msg)

    def edit_selected_transaction(self):
        selected = self.tx_table2.selectedItems()
        if not selected:
            QMessageBox.information(self, "No selection", "Please select a transaction to edit.")
            return

        row = selected[0].row()
        tx_id = self._tx_table_id_map.get(row)

        if tx_id is None:
            QMessageBox.warning(self, "Error", "Could not find transaction ID for selected row.")
            return

        tx = next((t for t in self.gl.transactions if t.id == tx_id), None)

        if not tx:
            QMessageBox.warning(self, "Error", "Transaction not found.")
            return

        if len(tx.lines) != 2:
            QMessageBox.warning(
                self,
                "Editing not supported",
                "Only simple two-line transactions can be edited here.",
            )
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Transaction")
        dlg_layout = QVBoxLayout(dlg)

        form = QFormLayout()

        dlg_date = QDateEdit()
        dlg_date.setCalendarPopup(True)
        from datetime import datetime
        try:
            dt = datetime.strptime(tx.date[:10], "%Y-%m-%d")
            dlg_date.setDate(QDate(dt.year, dt.month, dt.day))
        except Exception:
            dlg_date.setDate(QDate.currentDate())

        dlg_desc = QLineEdit()
        dlg_desc.setText(tx.description)

        amount = Decimal("0.00")
        acc_name = None
        for line in tx.lines:
            if line.dr > ZERO:
                amount = line.dr
                acc_name = line.account_name
                break
            if line.cr > ZERO:
                amount = line.cr
                acc_name = line.account_name
                break

        dlg_amount = QLineEdit()
        dlg_amount.setText(f"{amount:.2f}")

        form.addRow("Date:", dlg_date)
        form.addRow("Description:", dlg_desc)
        form.addRow("Account:", QLabel(acc_name or "(none)"))
        form.addRow("Amount:", dlg_amount)

        dlg_layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        dlg_layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        new_date = dlg_date.date().toString("yyyy-MM-dd")
        new_desc = dlg_desc.text().strip()
        new_amt_str = dlg_amount.text().strip()

        try:
            new_amt = Decimal(new_amt_str)
            if new_amt <= 0:
                raise ValueError()
        except ValueError:
            QMessageBox.warning(self, "Invalid amount", "Amount must be a positive number.")
            return

        new_lines = []
        for line in tx.lines:
            acc = self.gl.accounts[line.account_name]
            if acc.account_type in ("Revenue", "Expense"):
                if acc.account_type == "Revenue":
                    new_lines.append((line.account_name, 0.0, new_amt))
                else:
                    new_lines.append((line.account_name, new_amt, 0.0))
                break
            else:
                new_lines.append((line.account_name, line.dr, line.cr))

        ok, msg = self.gl.update_transaction(tx_id, new_date, new_desc, new_lines)
        if ok:
            QMessageBox.information(self, "Success", "Transaction updated.")
            self.apply_transactions_tab_filters()
            self.apply_transaction_filters()
        else:
            QMessageBox.warning(self, "Error", msg)

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
        if reply != QMessageBox.Yes:
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
