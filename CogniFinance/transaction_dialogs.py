"""
transaction_dialogs.py ver 1.0
For CogniFinance ver 1.9
Encapsulates the Add and Edit transaction dialogs, including the
NumericDelegate and dynamic line-table management.
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QDateEdit, QLineEdit,
    QGroupBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QPushButton, QHBoxLayout, QLabel, QMessageBox, QStyledItemDelegate,
    QDialogButtonBox
)
from PyQt6.QtCore import Qt, QDate, QTimer
from PyQt6.QtGui import QDoubleValidator
from decimal import Decimal
from datetime import datetime
from ledger import GeneralLedger
from ui_helpers import create_account_combo, stretch_account_column

class _NumericDelegate(QStyledItemDelegate):
    """Constrains input to valid numeric values for Debit/Credit columns."""
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

class AddTransactionDialog(QDialog):
    COL_ACCOUNT = 0
    COL_DEBIT = 1
    COL_CREDIT = 2
    COL_DELETE = 3

    def __init__(self, ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.ledger = ledger
        self.transaction_data = None
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("Add Transaction")
        self.resize(700, 600)
        self.setMinimumSize(500, 400)

        dlg_layout = QVBoxLayout(self)
        form = QFormLayout()
        self.dlg_date = QDateEdit()
        self.dlg_date.setCalendarPopup(True)
        self.dlg_date.setDate(QDate.currentDate())
        self.dlg_desc = QLineEdit()
        form.addRow("Date:", self.dlg_date)
        form.addRow("Description:", self.dlg_desc)
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

        self.lines_table = QTableWidget()
        self.lines_table.setColumnCount(4)
        self.lines_table.setHorizontalHeaderLabels(["Account", "Debit", "Credit", ""])
        self.lines_table.setRowCount(0)
        self.lines_table.setSortingEnabled(False)

        header = self.lines_table.horizontalHeader()
        header.setSectionResizeMode(self.COL_DEBIT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_CREDIT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_DELETE, QHeaderView.ResizeMode.Fixed)
        self.lines_table.setColumnWidth(self.COL_DELETE, 120)
        header.setSectionResizeMode(self.COL_ACCOUNT, QHeaderView.ResizeMode.Interactive)

        stretch_account_column(self.lines_table, self.COL_ACCOUNT, [self.COL_DEBIT, self.COL_CREDIT, self.COL_DELETE])

        num_delegate = _NumericDelegate(self.lines_table)
        self.lines_table.setItemDelegateForColumn(self.COL_DEBIT, num_delegate)
        self.lines_table.setItemDelegateForColumn(self.COL_CREDIT, num_delegate)
        lines_layout.addWidget(self.lines_table)

        totals_layout = QHBoxLayout()
        self.totals_label = QLabel("Total Debit: 0.00    Total Credit: 0.00")
        totals_layout.addWidget(self.totals_label)
        totals_layout.addStretch()
        lines_layout.addLayout(totals_layout)

        add_btn = QPushButton("Add Line")
        add_btn.clicked.connect(lambda: self.create_line_row())
        lines_layout.addWidget(add_btn)
        dlg_layout.addWidget(lines_box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        dlg_layout.addWidget(buttons)

        self.create_line_row()
        self.create_line_row()
        self._update_totals()

    def create_line_row(self, account=None, debit="0.00", credit="0.00"):
        r = self.lines_table.rowCount()
        self.lines_table.insertRow(r)
        acc_combo = create_account_combo(self.ledger, account)
        self.lines_table.setCellWidget(r, self.COL_ACCOUNT, acc_combo)

        dr_item = QTableWidgetItem(str(debit))
        dr_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lines_table.setItem(r, self.COL_DEBIT, dr_item)

        cr_item = QTableWidgetItem(str(credit))
        cr_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lines_table.setItem(r, self.COL_CREDIT, cr_item)

        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(lambda: self._delete_row(del_btn))
        self.lines_table.setCellWidget(r, self.COL_DELETE, del_btn)

        self.lines_table.itemChanged.connect(lambda *_: self._update_totals())

    def _delete_row(self, btn):
        for rr in range(self.lines_table.rowCount()):
            if self.lines_table.cellWidget(rr, self.COL_DELETE) is btn:
                self.lines_table.removeRow(rr)
                self._update_totals()
                return

    def _update_totals(self):
        total_dr = Decimal("0")
        total_cr = Decimal("0")
        for rr in range(self.lines_table.rowCount()):
            dr_item = self.lines_table.item(rr, self.COL_DEBIT)
            cr_item = self.lines_table.item(rr, self.COL_CREDIT)
            for text, side in ((dr_item.text() if dr_item else "0", "dr"), (cr_item.text() if cr_item else "0", "cr")):
                try:
                    val = Decimal(text.strip()) if text.strip() else Decimal("0")
                except Exception:
                    val = Decimal("0")
                if side == "dr": total_dr += val
                else: total_cr += val
        self.totals_label.setText(f"Total Debit: {format(total_dr, 'f')}    Total Credit: {format(total_cr, 'f')}")

    def validate_and_accept(self):
        lines_data = []
        for i in range(self.lines_table.rowCount()):
            acc_widget = self.lines_table.cellWidget(i, self.COL_ACCOUNT)
            if acc_widget is None:
                QMessageBox.warning(self, "Error", f"Row {i + 1} has no account selector.")
                return
            account = acc_widget.currentData()
            if not account:
                QMessageBox.warning(self, "No accounts available", "Create at least one account before adding transactions.")
                return

            dr_item = self.lines_table.item(i, self.COL_DEBIT)
            cr_item = self.lines_table.item(i, self.COL_CREDIT)
            dr_text = dr_item.text().strip() if dr_item is not None else "0"
            cr_text = cr_item.text().strip() if cr_item is not None else "0"
            lines_data.append((account, dr_text, cr_text))

        self.transaction_data = (
            self.dlg_date.date().toString("yyyy-MM-dd"),
            self.dlg_desc.text().strip(),
            lines_data
        )
        super().accept()

    def get_data(self):
        return self.transaction_data


class EditTransactionDialog(QDialog):
    COL_ACCOUNT = 0
    COL_DEBIT = 1
    COL_CREDIT = 2
    COL_LINEID = 3
    COL_DELETE = 4

    def __init__(self, ledger: GeneralLedger, tx, parent=None):
        super().__init__(parent)
        self.ledger = ledger
        self.tx = tx
        self.transaction_data = None
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("Edit Transaction")
        self.resize(700, 600)
        self.setMinimumSize(500, 400)

        dlg_layout = QVBoxLayout(self)
        form = QFormLayout()
        self.dlg_date = QDateEdit()
        self.dlg_date.setCalendarPopup(True)
        try:
            dt = datetime.strptime(self.tx.date[:10], "%Y-%m-%d")
            self.dlg_date.setDate(QDate(dt.year, dt.month, dt.day))
        except Exception:
            self.dlg_date.setDate(QDate.currentDate())

        self.dlg_desc = QLineEdit()
        self.dlg_desc.setText(self.tx.description or "")
        form.addRow("Date:", self.dlg_date)
        form.addRow("Description:", self.dlg_desc)
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

        self.lines_table = QTableWidget()
        self.lines_table.setColumnCount(5)
        self.lines_table.setHorizontalHeaderLabels(["Account", "Debit", "Credit", "LineId", ""])
        self.lines_table.setColumnHidden(self.COL_LINEID, True)
        self.lines_table.setRowCount(0)
        self.lines_table.setSortingEnabled(False)

        header = self.lines_table.horizontalHeader()
        header.setSectionResizeMode(self.COL_DEBIT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_CREDIT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.COL_DELETE, QHeaderView.ResizeMode.Fixed)
        self.lines_table.setColumnWidth(self.COL_DELETE, 120)
        header.setSectionResizeMode(self.COL_ACCOUNT, QHeaderView.ResizeMode.Interactive)

        stretch_account_column(self.lines_table, self.COL_ACCOUNT, [self.COL_DEBIT, self.COL_CREDIT, self.COL_DELETE])

        num_delegate = _NumericDelegate(self.lines_table)
        self.lines_table.setItemDelegateForColumn(self.COL_DEBIT, num_delegate)
        self.lines_table.setItemDelegateForColumn(self.COL_CREDIT, num_delegate)
        lines_layout.addWidget(self.lines_table)

        totals_layout = QHBoxLayout()
        self.totals_label = QLabel("Total Debit: 0.00    Total Credit: 0.00")
        totals_layout.addWidget(self.totals_label)
        totals_layout.addStretch()
        lines_layout.addLayout(totals_layout)

        add_btn = QPushButton("Add Line")
        add_btn.clicked.connect(lambda: self.create_line_row())
        lines_layout.addWidget(add_btn)
        dlg_layout.addWidget(lines_box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        dlg_layout.addWidget(buttons)

        for line in self.tx.lines:
            self.create_line_row(line.account_name, line.dr, line.cr, line.id)

        self._update_totals()

    def create_line_row(self, account=None, debit="0.00", credit="0.00", line_id=""):
        r = self.lines_table.rowCount()
        self.lines_table.insertRow(r)
        acc_combo = create_account_combo(self.ledger, account)
        self.lines_table.setCellWidget(r, self.COL_ACCOUNT, acc_combo)

        dr_item = QTableWidgetItem(str(debit))
        dr_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lines_table.setItem(r, self.COL_DEBIT, dr_item)

        cr_item = QTableWidgetItem(str(credit))
        cr_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lines_table.setItem(r, self.COL_CREDIT, cr_item)

        line_id_item = QTableWidgetItem(str(line_id) if line_id not in (None, "") else "")
        line_id_item.setFlags(line_id_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.lines_table.setItem(r, self.COL_LINEID, line_id_item)

        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(lambda: self._delete_row(del_btn))
        self.lines_table.setCellWidget(r, self.COL_DELETE, del_btn)

        self.lines_table.itemChanged.connect(lambda *_: self._update_totals())

    def _delete_row(self, btn):
        for rr in range(self.lines_table.rowCount()):
            if self.lines_table.cellWidget(rr, self.COL_DELETE) is btn:
                self.lines_table.removeRow(rr)
                self._update_totals()
                return

    def _update_totals(self):
        total_dr = Decimal("0")
        total_cr = Decimal("0")
        for rr in range(self.lines_table.rowCount()):
            dr_item = self.lines_table.item(rr, self.COL_DEBIT)
            cr_item = self.lines_table.item(rr, self.COL_CREDIT)
            for text, side in ((dr_item.text() if dr_item else "0", "dr"), (cr_item.text() if cr_item else "0", "cr")):
                try:
                    val = Decimal(text.strip()) if text.strip() else Decimal("0")
                except Exception:
                    val = Decimal("0")
                if side == "dr": total_dr += val
                else: total_cr += val
        self.totals_label.setText(f"Total Debit: {format(total_dr, 'f')}    Total Credit: {format(total_cr, 'f')}")

    def validate_and_accept(self):
        new_lines = []
        for i in range(self.lines_table.rowCount()):
            acc_widget = self.lines_table.cellWidget(i, self.COL_ACCOUNT)
            if acc_widget is None:
                QMessageBox.warning(self, "Error", f"Row {i + 1} has no account selector.")
                return
            account = acc_widget.currentData()
            if not account:
                QMessageBox.warning(self, "No accounts available", "Create at least one account before editing transactions.")
                return

            dr_item = self.lines_table.item(i, self.COL_DEBIT)
            cr_item = self.lines_table.item(i, self.COL_CREDIT)
            line_id_item = self.lines_table.item(i, self.COL_LINEID)

            dr_text = dr_item.text().strip() if dr_item is not None else "0"
            cr_text = cr_item.text().strip() if cr_item is not None else "0"
            line_id_text = line_id_item.text().strip() if line_id_item is not None else ""
            line_id = int(line_id_text) if line_id_text else None

            new_lines.append((line_id, account, dr_text, cr_text))

        self.transaction_data = (
            self.dlg_date.date().toString("yyyy-MM-dd"),
            self.dlg_desc.text().strip(),
            new_lines
        )
        super().accept()

    def get_data(self):
        return self.transaction_data
