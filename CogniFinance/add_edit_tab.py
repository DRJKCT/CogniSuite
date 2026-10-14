"""
add_edit_tab.py ver 1.3
For CogniFinance ver 1.9
Encapsulates the Add/Edit tab UI.
Features side-by-side forms and a vertical splitter for maximum data display.
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QComboBox, QDateEdit, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QMessageBox, QSplitter, QHeaderView
)
from PyQt6.QtCore import Qt, QDate, QTimer, pyqtSignal
from decimal import Decimal
from ledger import GeneralLedger
from ui_helpers import add_refresh_row, setup_table_headers, get_transaction_display_amount
from transaction_dialogs import EditTransactionDialog
import logging

logger = logging.getLogger(__name__)

class AddEditTab(QWidget):
    history_requested = pyqtSignal(int)
    data_modified = pyqtSignal()

    def __init__(self, ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.gl = ledger
        self._tx_table_id_map = {}
        self._init_ui()
        self.refresh_data()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        add_refresh_row(layout, self.refresh_data)

        # ======================================================================
        # MAIN VERTICAL SPLITTER: Controls (top) vs Data Table (bottom)
        # ======================================================================
        main_splitter = QSplitter(Qt.Orientation.Vertical)

        # --- Top Container for Forms and Action Buttons ---
        top_container = QWidget()
        top_layout = QVBoxLayout(top_container)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(5)

        # --- Side-by-Side Forms ---
        forms_layout = QHBoxLayout()
        forms_layout.setSpacing(10)

        # Left Box: Add Transaction
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

        btn_add = QPushButton("Post Transaction")
        btn_add.clicked.connect(self.post_quick_transaction)
        flayout.addRow(btn_add)

        form_box.setLayout(flayout)
        forms_layout.addWidget(form_box)

        # Right Box: Filters
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

        btn_apply = QPushButton("Apply Filters")
        btn_apply.clicked.connect(self.refresh_data)
        flay2.addRow(btn_apply)

        filter_box.setLayout(flay2)
        forms_layout.addWidget(filter_box)

        top_layout.addLayout(forms_layout)

        # --- Action Buttons (Edit, Delete, History) ---
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
        top_layout.addLayout(btn_edit_row)

        main_splitter.addWidget(top_container)

        # ======================================================================
        # DATA TABLE
        # ======================================================================
        self.tx_table2 = QTableWidget()
        self.tx_table2.setColumnCount(5)
        self.tx_table2.setHorizontalHeaderLabels(["Date", "Description", "From", "To", "Amount"])

        header = self.tx_table2.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        # 2. Set a hard minimum width (e.g., 80px).
        # The user can drag them wider, but they can NEVER be squished smaller than this.
        header.setMinimumSectionSize(80)

        # 3. Set the initial starting widths.
        # The user can drag them from these starting points.
        header.resizeSection(0, 100)  # Date
        header.resizeSection(1, 500)  # Description (Give it plenty of room to start)
        header.resizeSection(2, 150)  # From
        header.resizeSection(3, 150)  # To
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.tx_table2.doubleClicked.connect(self.edit_selected_transaction)

        main_splitter.addWidget(self.tx_table2)

        # Configure splitter behavior: Table gets all the stretch factor
        main_splitter.setStretchFactor(0, 0) # Top container does not stretch
        main_splitter.setStretchFactor(1, 1) # Table stretches to fill window

        layout.addWidget(main_splitter)

        # ======================================================================
        # FIX: Dynamically set the initial splitter position
        # ======================================================================
        # Instead of guessing a hardcoded pixel size (like 200px), we wait for
        # the initial layout pass to finish, then ask the top_container for its
        # natural height (sizeHint) and set the splitter exactly there + 10px.
        QTimer.singleShot(0, lambda: main_splitter.setSizes([top_container.sizeHint().height() + 10, 1000]))

    def refresh_account_dropdown(self):
        """Updates the from/to account dropdowns when accounts are added/renamed/deleted."""
        current_from = self.tx_from.currentData()
        current_to = self.tx_to.currentData()
        current_filter = self.tx_filter_account.currentData()

        self.tx_from.clear()
        self.tx_to.clear()
        self.tx_filter_account.clear()
        self.tx_filter_account.addItem("All accounts", "")

        for name in sorted(self.gl.accounts.keys()):
            self.tx_from.addItem(name, name)
            self.tx_to.addItem(name, name)
            self.tx_filter_account.addItem(name, name)

        if current_from:
            idx = self.tx_from.findData(current_from)
            if idx >= 0: self.tx_from.setCurrentIndex(idx)
        if current_to:
            idx = self.tx_to.findData(current_to)
            if idx >= 0: self.tx_to.setCurrentIndex(idx)
        if current_filter:
            idx = self.tx_filter_account.findData(current_filter)
            if idx >= 0: self.tx_filter_account.setCurrentIndex(idx)

    def refresh_data(self):
        """Fetches and displays transactions based on current filters."""
        start = self.tx_filter_start.date().toString("yyyy-MM-dd") or None
        end = self.tx_filter_end.date().toString("yyyy-MM-dd") or None
        account = self.tx_filter_account.currentData() or None

        txs = self.gl.get_transactions(start_date=start, end_date=end, account=account)
        self.tx_table2.setRowCount(len(txs))
        self._tx_table_id_map = {}

        for row, tx in enumerate(txs):
            self.tx_table2.setItem(row, 0, QTableWidgetItem(tx.date))
            self.tx_table2.setItem(row, 1, QTableWidgetItem(tx.description))

            from_acc = ""
            to_acc = ""
            if tx.lines:
                for line in tx.lines:
                    # If the line has a credit, money came FROM this account
                    if line.cr > 0 and not from_acc:
                        from_acc = line.account_name
                    # If the line has a debit, money went TO this account
                    elif line.dr > 0 and not to_acc:
                        to_acc = line.account_name

            self.tx_table2.setItem(row, 2, QTableWidgetItem(from_acc))
            self.tx_table2.setItem(row, 3, QTableWidgetItem(to_acc))

            amount = get_transaction_display_amount(self.gl, tx)
            self.tx_table2.setItem(row, 4, QTableWidgetItem(f"{amount:,.2f}"))

            if tx.id is not None:
                self._tx_table_id_map[row] = tx.id

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
            self.refresh_data()
            self.data_modified.emit()
        else:
            logger.warning(f"Quick transaction failed: {msg}")
            QMessageBox.warning(self, "Error", msg)

    def edit_selected_transaction(self):
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

        dlg = EditTransactionDialog(self.gl, tx, self)
        if dlg.exec() == dlg.DialogCode.Accepted:
            new_date, new_desc, new_lines = dlg.get_data()
            ok, msg = self.gl.edit_transaction(tx_id, new_date, new_desc, new_lines)
            if not ok:
                QMessageBox.warning(self, "Transaction Not Updated", msg)
                return
            self.refresh_data()
            self.data_modified.emit()

    def delete_selected_transaction(self):
        selected = self.tx_table2.selectedItems()
        if not selected:
            QMessageBox.information(self, "No selection", "Please select a transaction to delete.")
            return

        reply = QMessageBox.question(
            self, "Delete transaction", "Are you sure you want to delete this transaction?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        row = selected[0].row()
        tx_id = self._tx_table_id_map.get(row)
        if tx_id is None:
            QMessageBox.warning(self, "Error", "Could not find transaction ID for selected row.")
            return

        ok, msg = self.gl.void_transaction(tx_id)
        if ok:
            QMessageBox.information(self, "Success", "Transaction deleted.")
            self.refresh_data()
            self.data_modified.emit()
        else:
            QMessageBox.warning(self, "Error", msg)

    def _show_selected_tx_history(self):
        selected = self.tx_table2.selectedItems()
        if not selected:
            QMessageBox.information(self, "No selection", "Please select a transaction.")
            return

        tx_id = self._tx_table_id_map.get(selected[0].row())
        if tx_id is not None:
            self.history_requested.emit(tx_id)
