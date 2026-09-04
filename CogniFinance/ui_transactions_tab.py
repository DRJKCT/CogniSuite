# ui_transactions_tab.py
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QDateEdit, QComboBox, QLineEdit,
    QGroupBox, QFormLayout, QHBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem
)
from PyQt6.QtCore import QDate
from decimal import Decimal
from ledger import GeneralLedger
from ui_helpers import add_refresh_row, setup_table_headers, fit_table_columns, get_transaction_display_amount

class TransactionsTab(QWidget):
    def __init__(self, ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.gl = ledger
        self._init_ui()
        self.refresh_data()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        # Filters
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

        add_refresh_row(layout, self.refresh_data, self.filter_search)

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
        btn_apply.clicked.connect(self.refresh_data)
        btn_row.addWidget(btn_apply)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Table
        self.tx_table = QTableWidget()
        self.tx_table.setColumnCount(4)
        self.tx_table.setHorizontalHeaderLabels(["Date", "Description", "Accounts", "Amount"])
        setup_table_headers(self.tx_table, [0.11, 0.42, 0.31, 0.16])
        layout.addWidget(self.tx_table)

    def refresh_data(self):
        """Fetches and displays transactions based on current filters."""
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
            amount = get_transaction_display_amount(self.gl, tx)
            self.tx_table.setItem(row, 3, QTableWidgetItem(f"{amount:,.2f}"))

        fit_table_columns(self.tx_table)

    def refresh_account_dropdown(self):
        """Updates the account filter dropdown when accounts are added/renamed/deleted."""
        current_account = self.filter_account.currentData()
        self.filter_account.clear()
        self.filter_account.addItem("All accounts", "")
        for name in sorted(self.gl.accounts.keys()):
            self.filter_account.addItem(name, name)

        # Restore previous selection if it still exists
        if current_account:
            idx = self.filter_account.findData(current_account)
            if idx >= 0:
                self.filter_account.setCurrentIndex(idx)
