# budgets_tab.py
"""
budgets_tab.py ver 2.0
For CogniFinance ver 1.95
Envelope-based budgeting UI. Drives the ledger's ZBB/envelope engine
(assign/unassign/move + income plan + rollover policies) and the
rollover math in budget_engine.py.

Public surface used by ui_main.py (kept stable through this rewrite):
  - refresh_budget_list()
  - refresh_account_dropdown()  (no-op hook)
  - data_modified signal emitted on any persistent change
"""
from decimal import Decimal, InvalidOperation
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox, QFormLayout,
    QComboBox, QDateEdit, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QDialog, QMessageBox, QDialogButtonBox,
    QAbstractItemView, QCheckBox
)
from PyQt6.QtCore import Qt, QDate, pyqtSignal
from PyQt6.QtGui import QColor, QFont

from ledger import GeneralLedger, ZERO
from ui_helpers import add_refresh_row, setup_table_headers, money_item
from budget_engine import (
    derive_expected_income,
    envelope_available,
    ready_to_assign,
    net_assignment_in_month,
)


INCOME_SOURCE_LABELS = [
    ("Last Month actuals", "last_month"),
    ("3-Month average", "avg_3mo"),
    ("6-Month average", "avg_6mo"),
    ("Manual", "manual"),
]


class BudgetsTab(QWidget):
    data_modified = pyqtSignal()

    def __init__(self, ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.gl = ledger
        self._init_ui()
        self.refresh_budget_list()

    # ==================================================================
    # UI construction
    # ==================================================================
    def _init_ui(self):
        layout = QVBoxLayout(self)
        add_refresh_row(layout, self.refresh_budget_list)

        # --- Month + Ready to Assign banner ---
        header_box = QGroupBox("Budget Month")
        h_layout = QHBoxLayout(header_box)

        h_layout.addWidget(QLabel("Month:"))
        self.month_edit = QDateEdit()
        self.month_edit.setCalendarPopup(True)
        self.month_edit.setDate(QDate.currentDate())
        self.month_edit.setDisplayFormat("yyyy-MM")
        self.month_edit.dateChanged.connect(lambda *_: self.refresh_budget_list())
        h_layout.addWidget(self.month_edit)

        h_layout.addSpacing(20)
        self.ready_to_assign_label = QLabel("Ready to Assign: $0.00")
        self.ready_to_assign_label.setFont(QFont("Inter", 14, QFont.Weight.Bold))
        self.ready_to_assign_label.setStyleSheet("padding: 6px; border-radius: 6px;")
        h_layout.addWidget(self.ready_to_assign_label)
        h_layout.addStretch()
        layout.addWidget(header_box)

        # --- Income plan ---
        income_box = QGroupBox("Income Plan")
        i_layout = QFormLayout(income_box)

        self.income_source = QComboBox()
        for label, value in INCOME_SOURCE_LABELS:
            self.income_source.addItem(label, value)
        self.income_source.currentIndexChanged.connect(self._on_income_source_changed)

        self.income_derived_label = QLabel("—")
        self.income_derived_label.setStyleSheet("color: #6B7280;")

        self.income_expected = QLineEdit()
        self.income_expected.setPlaceholderText("Expected income for this month")

        btn_save_income = QPushButton("Save Income Plan")
        btn_save_income.clicked.connect(self._save_income_plan)

        i_layout.addRow("Source:", self.income_source)
        i_layout.addRow("Derived:", self.income_derived_label)
        i_layout.addRow("Expected:", self.income_expected)
        i_layout.addRow(btn_save_income)
        layout.addWidget(income_box)

        # --- Envelope table ---
        env_box = QGroupBox("Envelopes")
        e_layout = QVBoxLayout(env_box)

        self.envelope_table = QTableWidget()
        self.envelope_table.setColumnCount(6)
        self.envelope_table.setHorizontalHeaderLabels(
            ["Category", "Linked Account", "Policy", "Assigned", "Spent", "Available"]
        )
        setup_table_headers(self.envelope_table, [0.22, 0.22, 0.14, 0.14, 0.14, 0.14])
        self.envelope_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.envelope_table.doubleClicked.connect(lambda *_: self._edit_envelope())
        e_layout.addWidget(self.envelope_table)

        btn_row = QHBoxLayout()
        for label, handler in (
            ("Assign Money", self._assign_money),
            ("Unassign", self._unassign_money),
            ("Move Between Envelopes", self._move_money),
            ("Add Envelope", self._add_envelope),
            ("Edit Envelope", self._edit_envelope),
        ):
            b = QPushButton(label)
            b.clicked.connect(handler)
            btn_row.addWidget(b)
        btn_row.addStretch()
        e_layout.addLayout(btn_row)
        layout.addWidget(env_box, 1)

        # --- Month budget-ledger history ---
        hist_box = QGroupBox("This Month's Budget Activity")
        hist_layout = QVBoxLayout(hist_box)
        self.history_table = QTableWidget()
        self.history_table.setColumnCount(5)
        self.history_table.setHorizontalHeaderLabels(
            ["When", "Category", "Event", "Amount", "Note"]
        )
        setup_table_headers(self.history_table, [0.18, 0.22, 0.14, 0.14, 0.32])
        self.history_table.setMaximumHeight(180)
        hist_layout.addWidget(self.history_table)
        layout.addWidget(hist_box)

    # ==================================================================
    # Public refresh entry points (ui_main.py depends on these names)
    # ==================================================================
    def refresh_budget_list(self):
        month = self.month_edit.date().toString("yyyy-MM")

        # --- Ready to Assign ---
        rta = ready_to_assign(self.gl, month)
        rta_color = "#007a00" if rta >= ZERO else "#b00000"
        self.ready_to_assign_label.setText(f"Ready to Assign: ${rta:,.2f}")
        self.ready_to_assign_label.setStyleSheet(
            f"padding: 6px; border-radius: 6px; color: {rta_color};"
        )

        # --- Income plan ---
        plan = self.gl.get_income_plan(month)
        self.income_source.blockSignals(True)
        if plan:
            idx = self.income_source.findData(plan["income_source"])
            if idx >= 0:
                self.income_source.setCurrentIndex(idx)
        else:
            self.income_source.setCurrentIndex(0)
        self.income_source.blockSignals(False)

        self._refresh_derived_preview()
        if plan:
            self.income_expected.setText(f"{plan['expected_amount']:.2f}")
        else:
            self.income_expected.clear()

        # --- Envelopes ---
        envelopes = self.gl.get_budget_envelopes(include_inactive=False)
        self.envelope_table.setRowCount(len(envelopes))

        red = QColor("#b00000")
        green = QColor("#007a00")
        orange = QColor("#b06000")

        for row, env in enumerate(envelopes):
            category = env["category_name"]
            linked = env["linked_coa_account"] or ""
            policy = env["rollover_policy"]

            assigned = net_assignment_in_month(self.gl, category, month)
            spent = self.gl.get_category_actuals(linked, month) if linked else ZERO
            available = envelope_available(self.gl, category, month)

            cat_item = QTableWidgetItem(category)
            linked_item = QTableWidgetItem(linked or "—")
            policy_item = QTableWidgetItem(
                "Carry Forward" if policy == "carry_forward" else "Reset Monthly"
            )
            assigned_item = money_item(assigned)
            spent_item = money_item(spent)
            avail_item = money_item(available)

            if available < ZERO:
                avail_item.setForeground(red)
            elif spent > ZERO and available < spent * Decimal("0.10"):
                avail_item.setForeground(orange)
            else:
                avail_item.setForeground(green)

            for item in (cat_item, linked_item, policy_item,
                         assigned_item, spent_item, avail_item):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            self.envelope_table.setItem(row, 0, cat_item)
            self.envelope_table.setItem(row, 1, linked_item)
            self.envelope_table.setItem(row, 2, policy_item)
            self.envelope_table.setItem(row, 3, assigned_item)
            self.envelope_table.setItem(row, 4, spent_item)
            self.envelope_table.setItem(row, 5, avail_item)

        # --- History ---
        rows = self.gl.get_budget_ledger_rows(month)
        self.history_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.history_table.setItem(i, 0, QTableWidgetItem(r["created_at"] or ""))
            self.history_table.setItem(i, 1, QTableWidgetItem(r["category_name"]))
            self.history_table.setItem(i, 2, QTableWidgetItem(r["event_type"]))
            self.history_table.setItem(i, 3, money_item(r["amount"]))
            self.history_table.setItem(i, 4, QTableWidgetItem(r["note"] or ""))

    def refresh_account_dropdown(self):
        """
        ui_main.py calls this on account changes. The Budgets tab builds
        its account dropdowns fresh inside each dialog, so there's no
        persistent dropdown to refresh here. Kept as an explicit no-op
        so ui_main's hasattr() guard stays True and future expansion has
        an obvious hook.
        """
        pass

    # ==================================================================
    # Income plan
    # ==================================================================
    def _on_income_source_changed(self):
        self._refresh_derived_preview()

    def _refresh_derived_preview(self):
        month = self.month_edit.date().toString("yyyy-MM")
        source = self.income_source.currentData()
        amount, label = derive_expected_income(self.gl, month, source)
        self.income_derived_label.setText(f"${amount:,.2f} — {label}")

    def _save_income_plan(self):
        month = self.month_edit.date().toString("yyyy-MM")
        source = self.income_source.currentData()

        text = self.income_expected.text().strip().replace(",", "")
        try:
            expected = Decimal(text) if text else ZERO
            if expected < ZERO:
                raise ValueError()
        except (ValueError, InvalidOperation):
            QMessageBox.warning(self, "Invalid amount",
                                "Enter a valid non-negative number.")
            return

        if source == "manual":
            derived_amount = None
        else:
            derived_amount, _ = derive_expected_income(self.gl, month, source)
        ok, msg = self.gl.set_income_plan(month, expected, source, derived_amount)
        if ok:
            self.refresh_budget_list()
            self.data_modified.emit()
        else:
            QMessageBox.warning(self, "Income plan error", msg)

    # ==================================================================
    # Envelope assignment
    # ==================================================================
    def _selected_envelope_category(self) -> Optional[str]:
        row = self.envelope_table.currentRow()
        if row < 0:
            selected = self.envelope_table.selectedItems()
            if not selected:
                return None
            row = selected[0].row()
        item = self.envelope_table.item(row, 0)
        return item.text() if item else None

    def _prompt_amount(self, title: str, prompt: str) -> Optional[Decimal]:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg_layout = QVBoxLayout(dlg)
        dlg_layout.addWidget(QLabel(prompt))
        amount_edit = QLineEdit()
        amount_edit.setPlaceholderText("0.00")
        dlg_layout.addWidget(amount_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        dlg_layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        text = amount_edit.text().strip().replace(",", "")
        try:
            amount = Decimal(text)
            if amount <= ZERO:
                raise ValueError()
        except (ValueError, InvalidOperation):
            QMessageBox.warning(self, "Invalid amount",
                                "Amount must be a positive number.")
            return None
        return amount

    def _assign_money(self):
        category = self._selected_envelope_category()
        if not category:
            QMessageBox.information(self, "No selection",
                                    "Select an envelope to assign money to.")
            return
        month = self.month_edit.date().toString("yyyy-MM")
        amount = self._prompt_amount("Assign Money",
                                     f"Assign to '{category}' for {month}:")
        if amount is None:
            return
        ok, msg = self.gl.assign_to_envelope(month, category, amount)
        if ok:
            self.refresh_budget_list()
            self.data_modified.emit()
        else:
            QMessageBox.warning(self, "Assignment failed", msg)

    def _unassign_money(self):
        category = self._selected_envelope_category()
        if not category:
            QMessageBox.information(self, "No selection",
                                    "Select an envelope to unassign from.")
            return
        month = self.month_edit.date().toString("yyyy-MM")
        amount = self._prompt_amount(
            "Unassign Money",
            f"Return from '{category}' to Ready to Assign:",
        )
        if amount is None:
            return
        ok, msg = self.gl.unassign_from_envelope(month, category, amount)
        if ok:
            self.refresh_budget_list()
            self.data_modified.emit()
        else:
            QMessageBox.warning(self, "Unassign failed", msg)

    def _move_money(self):
        source = self._selected_envelope_category()
        if not source:
            QMessageBox.information(self, "No selection",
                                    "Select the source envelope first.")
            return
        month = self.month_edit.date().toString("yyyy-MM")

        envelopes = self.gl.get_budget_envelopes(include_inactive=False)
        destinations = [e["category_name"] for e in envelopes
                        if e["category_name"] != source]
        if not destinations:
            QMessageBox.information(self, "No destination",
                                    "No other envelopes to move money to.")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Move Between Envelopes")
        form = QFormLayout()
        dest_combo = QComboBox()
        for d in destinations:
            dest_combo.addItem(d, d)
        form.addRow("From:", QLabel(source))
        form.addRow("To:", dest_combo)
        amount_edit = QLineEdit()
        amount_edit.setPlaceholderText("0.00")
        form.addRow("Amount:", amount_edit)

        dlg_layout = QVBoxLayout(dlg)
        dlg_layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        dlg_layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        text = amount_edit.text().strip().replace(",", "")
        try:
            amount = Decimal(text)
            if amount <= ZERO:
                raise ValueError()
        except (ValueError, InvalidOperation):
            QMessageBox.warning(self, "Invalid amount",
                                "Amount must be a positive number.")
            return

        to_cat = dest_combo.currentData()
        ok, msg = self.gl.move_budget_money(source, to_cat, amount, month)
        if ok:
            self.refresh_budget_list()
            self.data_modified.emit()
        else:
            QMessageBox.warning(self, "Move failed", msg)

    # ==================================================================
    # Envelope configuration
    # ==================================================================
    def _add_envelope(self):
        existing = {e["category_name"]
                    for e in self.gl.get_budget_envelopes(include_inactive=True)}
        available_accounts = [
            name for name, acc in self.gl.accounts.items()
            if acc.account_type == "Expense" and name not in existing
        ]
        if not available_accounts:
            QMessageBox.information(
                self, "No accounts available",
                "Every Expense-type account already has an envelope. "
                "Create a new Expense account first, or edit an existing envelope.",
            )
            return
        self._envelope_dialog(None)

    def _edit_envelope(self):
        category = self._selected_envelope_category()
        if not category:
            QMessageBox.information(self, "No selection",
                                    "Select an envelope to edit.")
            return
        self._envelope_dialog(category)

    def _envelope_dialog(self, existing_category: Optional[str]):
        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Envelope" if existing_category else "Add Envelope")
        form = QFormLayout()

        # Category dropdown: locked when editing, filtered when adding
        category_combo = QComboBox()
        if existing_category:
            category_combo.addItem(existing_category, existing_category)
            category_combo.setEnabled(False)
        else:
            existing = {e["category_name"]
                        for e in self.gl.get_budget_envelopes(include_inactive=True)}
            for name, acc in sorted(self.gl.accounts.items()):
                if acc.account_type == "Expense" and name not in existing:
                    category_combo.addItem(name, name)
        form.addRow("Category:", category_combo)

        # Linked GL account
        linked_combo = QComboBox()
        linked_combo.addItem("(none)", None)
        for name, acc in sorted(self.gl.accounts.items()):
            if acc.account_type == "Expense":
                linked_combo.addItem(name, name)

        # Rollover policy
        policy_combo = QComboBox()
        policy_combo.addItem("Carry Forward", "carry_forward")
        policy_combo.addItem("Reset Monthly", "reset_monthly")

        is_active_check = QCheckBox("Active")
        is_active_check.setChecked(True)

        # Prefill when editing
        if existing_category:
            all_envs = self.gl.get_budget_envelopes(include_inactive=True)
            env = next((e for e in all_envs
                        if e["category_name"] == existing_category), None)
            if env:
                idx = linked_combo.findData(env["linked_coa_account"])
                if idx >= 0:
                    linked_combo.setCurrentIndex(idx)
                idx = policy_combo.findData(env["rollover_policy"])
                if idx >= 0:
                    policy_combo.setCurrentIndex(idx)
                is_active_check.setChecked(env["is_active"])

        form.addRow("Linked Account:", linked_combo)
        form.addRow("Rollover Policy:", policy_combo)
        form.addRow(is_active_check)

        dlg_layout = QVBoxLayout(dlg)
        dlg_layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        dlg_layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        category = category_combo.currentData()
        if not category:
            QMessageBox.warning(self, "Error", "No category selected.")
            return

        ok, msg = self.gl.upsert_budget_envelope(
            category,
            linked_combo.currentData(),
            policy_combo.currentData(),
            is_active_check.isChecked(),
        )
        if ok:
            self.refresh_budget_list()
            self.data_modified.emit()
        else:
            QMessageBox.warning(self, "Envelope error", msg)
