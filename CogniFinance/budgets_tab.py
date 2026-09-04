# budgets_tab.py
"""
budgets_tab.py ver 1.0
For CogniFinance ver 1.9
Encapsulates the Budgets tab UI, including budget creation, editing,
deletion, and the actuals vs. budget color-coded table.
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox, QFormLayout,
    QComboBox, QDateEdit, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QDialog, QMessageBox, QDialogButtonBox
)
from PyQt6.QtCore import Qt, QDate
from PyQt6.QtGui import QColor
from decimal import Decimal
from ledger import GeneralLedger, ZERO
from ui_helpers import add_refresh_row, setup_table_headers, money_item

class BudgetsTab(QWidget):
    def __init__(self, ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.gl = ledger
        self._init_ui()
        self.refresh_budget_list()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        add_refresh_row(layout, self.refresh_budget_list)

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
        setup_table_headers(self.budget_table)
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

    def refresh_account_dropdown(self):
        """Updates the category dropdown when accounts are added/renamed/deleted."""
        current_category = self.budget_category.currentData()
        self.budget_category.clear()
        expense_accounts = [name for name, acc in self.gl.accounts.items() if acc.account_type == "Expense"]
        for name in sorted(expense_accounts):
            self.budget_category.addItem(name, name)
        if current_category:
            idx = self.budget_category.findData(current_category)
            if idx >= 0:
                self.budget_category.setCurrentIndex(idx)

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
            QMessageBox.warning(self, "Budget error", msg)

    def refresh_budget_list(self):
        month = self.budget_view_month.date().toString("yyyy-MM")
        budgets = [b for b in self.gl.get_all_budgets() if b.month == month]
        actuals = self.gl.spending_by_category(month)
        self.budget_table.setRowCount(len(budgets))
        red, green, orange = QColor("#b00000"), QColor("#007a00"), QColor("#b06000")
        for row, b in enumerate(budgets):
            actual = actuals.get(b.category, ZERO)
            diff = b.amount - actual
            category_item = QTableWidgetItem(b.category)

            budget_item = money_item(b.amount)
            budget_item.setData(Qt.ItemDataRole.UserRole, float(b.amount))
            actual_item = money_item(actual)
            diff_item = money_item(diff)

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
        month = self.budget_view_month.date().toString("yyyy-MM")
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
