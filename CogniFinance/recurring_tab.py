"""
recurring_tab.py
Review surface for the Recurring Charges feature.

Three tables, mirroring the shape of the Category Rules tab:
  1. Detected candidates -- staged by recurring_engine, waiting for a
     human to confirm or dismiss them.
  2. Confirmed patterns -- what the user has vouched for.
  3. Blacklisted -- dismissed false positives, individually reversible.

This is a confirmation surface, not a consumer. It does not pre-fund
budgets, forecast anything, or drive any other feature: it only does
the one thing the draft -> confirm lifecycle needs a place for, namely
a human deciding which recurring claims are real.
"""
from decimal import Decimal, InvalidOperation
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGroupBox, QFormLayout,
    QComboBox, QDateEdit, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QMessageBox
)
from PyQt6.QtCore import Qt, QDate

from ledger import GeneralLedger
from ui_helpers import add_refresh_row, setup_display_table, money_item
from recurring_engine import INTERVALS


INTERVAL_LABELS = [
    ("Weekly", "weekly"),
    ("Monthly", "monthly"),
    ("Quarterly", "quarterly"),
    ("Annual", "annual"),
]


class RecurringTab(QWidget):
    """Widget for reviewing detected recurring charges."""

    def __init__(self, ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.gl = ledger
        self._init_ui()
        self.refresh_tables()

    # ==================================================================
    # UI construction
    # ==================================================================

    def _init_ui(self):
        layout = QVBoxLayout(self)
        add_refresh_row(layout, self.refresh_tables)

        intro = QLabel(
            "Recurring charges are detected from your posted transactions and "
            "staged here for review. Confirming one promotes it to a pattern; "
            "dismissing one blacklists it so it is not suggested again."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(self._build_candidates_box())
        layout.addWidget(self._build_patterns_box())
        layout.addWidget(self._build_blacklist_box())
        layout.addWidget(self._build_declare_box())

    def _build_candidates_box(self) -> QGroupBox:
        box = QGroupBox("Detected Candidates")
        box_layout = QVBoxLayout(box)

        self.candidate_table = QTableWidget()
        self.candidate_table.setColumnCount(8)
        self.candidate_table.setHorizontalHeaderLabels(
            ["Merchant", "Category", "Interval", "Expected Amount",
             "Amount Variance", "Next Expected", "Occurrences", "Confidence"]
        )
        setup_display_table(self.candidate_table, selection="row")
        box_layout.addWidget(self.candidate_table)

        btn_row = QHBoxLayout()
        btn_confirm = QPushButton("Confirm Pattern")
        btn_confirm.clicked.connect(self.confirm_selected)
        btn_dismiss = QPushButton("Dismiss (Not Recurring)")
        btn_dismiss.clicked.connect(self.dismiss_selected)
        btn_row.addWidget(btn_confirm)
        btn_row.addWidget(btn_dismiss)
        btn_row.addStretch()
        box_layout.addLayout(btn_row)
        return box

    def _build_patterns_box(self) -> QGroupBox:
        box = QGroupBox("Confirmed Patterns")
        box_layout = QVBoxLayout(box)

        self.pattern_table = QTableWidget()
        self.pattern_table.setColumnCount(9)
        self.pattern_table.setHorizontalHeaderLabels(
            ["Merchant", "Category", "Interval", "Expected Amount",
             "Next Expected", "Occurrences", "Confidence", "Source", "Rating"]
        )
        setup_display_table(self.pattern_table, selection="row")
        box_layout.addWidget(self.pattern_table)

        btn_row = QHBoxLayout()
        btn_green = QPushButton("Rate Green")
        btn_green.clicked.connect(self.rate_green_selected)
        btn_reject = QPushButton("Reject (Not Recurring)")
        btn_reject.clicked.connect(self.reject_selected)
        btn_row.addWidget(btn_green)
        btn_row.addWidget(btn_reject)
        btn_row.addStretch()
        box_layout.addLayout(btn_row)
        return box

    def _build_blacklist_box(self) -> QGroupBox:
        box = QGroupBox("Blacklisted")
        box_layout = QVBoxLayout(box)

        self.blacklist_table = QTableWidget()
        self.blacklist_table.setColumnCount(4)
        self.blacklist_table.setHorizontalHeaderLabels(
            ["Merchant", "Category", "Interval", "Blacklisted At"]
        )
        setup_display_table(self.blacklist_table, selection="row")
        box_layout.addWidget(self.blacklist_table)

        btn_row = QHBoxLayout()
        btn_unblacklist = QPushButton("Un-blacklist Selected")
        btn_unblacklist.clicked.connect(self.unblacklist_selected)
        btn_row.addWidget(btn_unblacklist)
        btn_row.addStretch()
        box_layout.addLayout(btn_row)
        return box

    def _build_declare_box(self) -> QGroupBox:
        """
        The user-declared path. The detector needs a cadence to work
        with, so a subscription that started last month is invisible to
        it; this is how the user says "I know this one recurs".
        """
        box = QGroupBox("Declare a Recurring Charge")
        form = QFormLayout()

        self.declare_merchant = QLineEdit()
        self.declare_merchant.setPlaceholderText("e.g. Netflix, Rent, Hydro")

        self.declare_category = QComboBox()
        self._populate_category_combo()

        self.declare_interval = QComboBox()
        for label, value in INTERVAL_LABELS:
            self.declare_interval.addItem(label, value)

        self.declare_amount = QLineEdit()
        self.declare_amount.setPlaceholderText("e.g. 15.00")

        self.declare_next_date = QDateEdit()
        self.declare_next_date.setCalendarPopup(True)
        self.declare_next_date.setDisplayFormat("yyyy-MM-dd")
        self.declare_next_date.setDate(QDate.currentDate())

        form.addRow("Merchant:", self.declare_merchant)
        form.addRow("Category:", self.declare_category)
        form.addRow("Interval:", self.declare_interval)
        form.addRow("Amount:", self.declare_amount)
        form.addRow("Next Expected:", self.declare_next_date)

        btn_row = QHBoxLayout()
        btn_declare = QPushButton("Declare")
        btn_declare.clicked.connect(self.declare_pattern)
        btn_row.addWidget(btn_declare)
        btn_row.addStretch()
        box_layout = QVBoxLayout()
        box_layout.addLayout(form)
        box_layout.addLayout(btn_row)
        box.setLayout(box_layout)
        return box

    def _populate_category_combo(self):
        """Expense accounts only -- the detector looks at charges, not income."""
        self.declare_category.clear()
        for name in sorted(self.gl.accounts.keys()):
            account = self.gl.accounts[name]
            if account.account_type == "Expense":
                self.declare_category.addItem(name, name)

    # ==================================================================
    # Refresh
    # ==================================================================

    def refresh_tables(self):
        self._refresh_candidates()
        self._refresh_patterns()
        self._refresh_blacklist()

    def _refresh_candidates(self):
        drafts = self.gl.get_recurring_drafts()
        self.candidate_table.setRowCount(len(drafts))
        for row, draft in enumerate(drafts):
            key_item = QTableWidgetItem(draft["merchant_display"])
            key_item.setData(
                Qt.ItemDataRole.UserRole,
                (draft["merchant_key"], draft["category"], draft["interval"]),
            )
            if draft.get("evidence_dates"):
                key_item.setToolTip("Charges: " + ", ".join(draft["evidence_dates"]))
            self.candidate_table.setItem(row, 0, key_item)
            self.candidate_table.setItem(row, 1, QTableWidgetItem(draft["category"]))
            self.candidate_table.setItem(row, 2, QTableWidgetItem(draft["interval"].title()))
            self.candidate_table.setItem(row, 3, money_item(draft["expected_amount"]))
            self.candidate_table.setItem(row, 4, money_item(draft["amount_variance"]))
            self.candidate_table.setItem(row, 5, QTableWidgetItem(draft["expected_next_date"] or "—"))
            self.candidate_table.setItem(row, 6, QTableWidgetItem(str(draft["occurrences"])))
            self.candidate_table.setItem(row, 7, QTableWidgetItem(f"{draft['confidence'] * 100:.0f}%"))

    def _refresh_patterns(self):
        patterns = self.gl.get_recurring_patterns()
        self.pattern_table.setRowCount(len(patterns))
        for row, pattern in enumerate(patterns):
            key_item = QTableWidgetItem(pattern["merchant_display"])
            key_item.setData(
                Qt.ItemDataRole.UserRole,
                (pattern["merchant_key"], pattern["category"], pattern["interval"]),
            )
            self.pattern_table.setItem(row, 0, key_item)
            self.pattern_table.setItem(row, 1, QTableWidgetItem(pattern["category"]))
            self.pattern_table.setItem(row, 2, QTableWidgetItem(pattern["interval"].title()))
            self.pattern_table.setItem(row, 3, money_item(pattern["expected_amount"]))
            self.pattern_table.setItem(row, 4, QTableWidgetItem(pattern["expected_next_date"] or "—"))
            self.pattern_table.setItem(row, 5, QTableWidgetItem(str(pattern["occurrences"])))
            self.pattern_table.setItem(row, 6, QTableWidgetItem(f"{pattern['confidence'] * 100:.0f}%"))
            self.pattern_table.setItem(row, 7, QTableWidgetItem(
                "Declared" if pattern["user_declared"] else "Detected"))
            self.pattern_table.setItem(row, 8, QTableWidgetItem(pattern["rating"].title()))

    def _refresh_blacklist(self):
        entries = self.gl.get_recurring_blacklist()
        self.blacklist_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            key_item = QTableWidgetItem(entry["merchant_key"])
            key_item.setData(
                Qt.ItemDataRole.UserRole,
                (entry["merchant_key"], entry["category"], entry["interval"]),
            )
            self.blacklist_table.setItem(row, 0, key_item)
            self.blacklist_table.setItem(row, 1, QTableWidgetItem(entry["category"]))
            self.blacklist_table.setItem(row, 2, QTableWidgetItem(entry["interval"].title()))
            self.blacklist_table.setItem(row, 3, QTableWidgetItem(entry["blacklisted_at"] or ""))

    # ==================================================================
    # Selection
    # ==================================================================

    def _selected_key(self, table) -> Optional[tuple]:
        selected = table.selectedItems()
        if not selected:
            return None
        item = table.item(selected[0].row(), 0)
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    # ==================================================================
    # Actions
    # ==================================================================

    def confirm_selected(self):
        key = self._selected_key(self.candidate_table)
        if not key:
            QMessageBox.information(self, "No selection",
                                    "Please select a candidate to confirm.")
            return
        merchant_key, category, interval = key
        ok, msg = self.gl.confirm_recurring_draft(merchant_key, category, interval)
        if ok:
            self.refresh_tables()
        else:
            QMessageBox.warning(self, "Confirm failed", msg)

    def dismiss_selected(self):
        key = self._selected_key(self.candidate_table)
        if not key:
            QMessageBox.information(self, "No selection",
                                    "Please select a candidate to dismiss.")
            return
        merchant_key, category, interval = key
        ok, msg = self.gl.reject_recurring(merchant_key, category, interval)
        if ok:
            self.refresh_tables()
        else:
            QMessageBox.warning(self, "Dismiss failed", msg)

    def rate_green_selected(self):
        key = self._selected_key(self.pattern_table)
        if not key:
            QMessageBox.information(self, "No selection",
                                    "Please select a pattern to rate.")
            return
        merchant_key, category, interval = key
        ok, msg = self.gl.rate_recurring_pattern(merchant_key, category, interval, "green")
        if ok:
            self.refresh_tables()
        else:
            QMessageBox.warning(self, "Rating failed", msg)

    def reject_selected(self):
        key = self._selected_key(self.pattern_table)
        if not key:
            QMessageBox.information(self, "No selection",
                                    "Please select a pattern to reject.")
            return
        merchant_key, category, interval = key
        ok, msg = self.gl.reject_recurring(merchant_key, category, interval)
        if ok:
            self.refresh_tables()
        else:
            QMessageBox.warning(self, "Reject failed", msg)

    def unblacklist_selected(self):
        key = self._selected_key(self.blacklist_table)
        if not key:
            QMessageBox.information(self, "No selection",
                                    "Please select a blacklisted pattern to reverse.")
            return
        merchant_key, category, interval = key
        ok, msg = self.gl.unblacklist_recurring(merchant_key, category, interval)
        if ok:
            self.refresh_tables()
        else:
            QMessageBox.warning(self, "Un-blacklist failed", msg)

    def declare_pattern(self):
        merchant = self.declare_merchant.text().strip()
        category = self.declare_category.currentData()
        interval = self.declare_interval.currentData()
        amount_text = self.declare_amount.text().strip()
        next_date = self.declare_next_date.date().toString("yyyy-MM-dd")

        if not merchant or not category or not interval:
            QMessageBox.warning(self, "Missing details",
                                "Merchant, category and interval are all required.")
            return
        try:
            amount = Decimal(amount_text)
        except (InvalidOperation, ValueError):
            QMessageBox.warning(self, "Invalid amount", "Amount must be a number.")
            return

        ok, msg = self.gl.declare_recurring_pattern(merchant, category, interval, amount, next_date)
        if ok:
            self.declare_merchant.clear()
            self.declare_amount.clear()
            self.refresh_tables()
        else:
            QMessageBox.warning(self, "Declare failed", msg)
