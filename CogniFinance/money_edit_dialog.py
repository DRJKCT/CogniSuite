"""
money_edit_dialog.py ver 0.1
For CogniFinance ver 0.9.67

One transaction, opened from the Money screen.

Simple and transfer transactions can be corrected here: the dialog
builds the same (line_id, account, debit, credit) lines Pro's editor
produces and calls GeneralLedger.edit_transaction. Anything with more
lines (splits) is shown read-only, with a button to the full editor.

The ledger is append-only, so both actions say what they really do
before the click: a correction replaces the transaction with a new one
and keeps the original in the audit log; voiding adds a reversing entry
and erases nothing.

If the ledger will refuse the change (reconciled, period locked, managed
by Investments or Crypto) the dialog says so up front and disables the
actions, with advice that has a way forward in Simple Mode.
"""
from datetime import datetime
from decimal import Decimal
from typing import Callable, Optional

from PyQt6.QtCore import QDate, Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
    QDateEdit, QPushButton, QMessageBox,
)

import home_data
import ledger_messages
import money_data
from ledger import ZERO
from simple_inputs import parse_amount
from simple_theme import fmt_money


def blocked_reason(ledger, tx) -> Optional[str]:
    """Why the ledger will refuse to change this transaction, if it will."""
    if getattr(tx, "reconciled", False):
        return ledger_messages.RECONCILED_SIMPLE
    message = ledger.period_lock_violation(tx.date, [l.account_name for l in tx.lines])
    if message:
        return ledger_messages.for_simple_mode(message)
    check = getattr(ledger, "_investment_crypto_lock_message", None)
    if callable(check) and tx.id is not None:
        try:
            return check(tx.id)
        except Exception:
            return None
    return None


class MoneyEditDialog(QDialog):
    def __init__(self, ledger, tx, parent=None,
                 full_editor_fn: Optional[Callable] = None):
        super().__init__(parent)
        self.ledger = ledger
        self.tx = tx
        self.kind = money_data.edit_kind(ledger, tx)
        self.changed = False
        self._full_editor_fn = full_editor_fn
        self.setWindowTitle("Transaction")
        self.setMinimumWidth(420)
        self._build()

    # ------------------------------------------------------------------ ui

    def _label(self, text, muted=True):
        label = QLabel(text)
        if muted:
            label.setObjectName("Muted")
        label.setWordWrap(True)
        return label

    def _line_of(self, kind_of_account):
        for line in self.tx.lines:
            if self.ledger.accounts[line.account_name].account_type in kind_of_account:
                return line
        return None

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self.blocked = blocked_reason(self.ledger, self.tx)
        if self.blocked:
            self.banner = self._label(self.blocked, muted=False)
            self.banner.setStyleSheet("color: #B91C1C; font-weight: 600;")
            layout.addWidget(self.banner)

        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        parsed = QDate.fromString(self.tx.date[:10], "yyyy-MM-dd")
        self.date_edit.setDate(parsed if parsed.isValid() else QDate.currentDate())
        self.payee_edit = QLineEdit(self.tx.description)
        self.amount_edit = QLineEdit()
        self.category_combo = QComboBox()
        self.account_combo = QComboBox()
        self.from_combo = QComboBox()
        self.to_combo = QComboBox()

        everyday, _n = home_data.everyday_accounts(self.ledger, 10_000)
        names = [row.name for row in everyday]

        if self.kind == money_data.KIND_SIMPLE:
            cat_line = self._line_of(("Expense", "Revenue"))
            acct_line = self._line_of(home_data.EVERYDAY_TYPES)
            self._cat_is_debit = cat_line.dr > ZERO
            amount = cat_line.dr if self._cat_is_debit else cat_line.cr
            same_type = self.ledger.accounts[cat_line.account_name].account_type
            for name in sorted(self.ledger.accounts, key=str.lower):
                if self.ledger.accounts[name].account_type == same_type:
                    self.category_combo.addItem(name, name)
            self.category_combo.setCurrentIndex(max(0, self.category_combo.findData(cat_line.account_name)))
            for name in names + ([acct_line.account_name] if acct_line.account_name not in names else []):
                self.account_combo.addItem(name, name)
            self.account_combo.setCurrentIndex(max(0, self.account_combo.findData(acct_line.account_name)))
            fields = (("Date", self.date_edit), ("Payee", self.payee_edit),
                      ("Category", self.category_combo), ("Amount", self.amount_edit),
                      ("Account", self.account_combo))
        elif self.kind == money_data.KIND_TRANSFER:
            source = next(l for l in self.tx.lines if l.cr > ZERO)
            target = next(l for l in self.tx.lines if l.dr > ZERO)
            amount = target.dr
            for combo, current in ((self.from_combo, source.account_name),
                                   (self.to_combo, target.account_name)):
                for name in names + ([current] if current not in names else []):
                    combo.addItem(name, name)
                combo.setCurrentIndex(max(0, combo.findData(current)))
            fields = (("Date", self.date_edit), ("Description", self.payee_edit),
                      ("From", self.from_combo), ("To", self.to_combo),
                      ("Amount", self.amount_edit))
        else:
            amount = None
            fields = ()
            layout.addWidget(self._label(
                f"{self.tx.date}  \u00b7  {self.tx.description}", muted=False))
            layout.addWidget(self._label(
                "This transaction has several parts, so it is shown here as it was "
                "recorded. Use the full editor to change it."))
            for line in self.tx.lines:
                side = (f"debit {fmt_money(line.dr)}" if line.dr > ZERO
                        else f"credit {fmt_money(line.cr)}")
                layout.addWidget(self._label(f"{line.account_name}: {side}", muted=False))

        if amount is not None:
            self.amount_edit.setText(f"{amount:.2f}")
        for caption, widget in fields:
            layout.addWidget(self._label(caption))
            layout.addWidget(widget)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.save_note = self._label(
            "Saving replaces this transaction with a corrected one. "
            "The original stays in the audit log.")
        self.void_note = self._label(
            "Voiding adds a reversing entry. Nothing is erased.")
        row = QHBoxLayout()
        self.save_button = QPushButton("Save changes")
        self.save_button.setObjectName("PrimaryButton")
        self.save_button.clicked.connect(self._on_save)
        self.void_button = QPushButton("Void")
        self.void_button.setObjectName("SecondaryButton")
        self.void_button.clicked.connect(self._on_void)
        self.full_button = QPushButton("Full editor\u2026")
        self.full_button.setObjectName("SecondaryButton")
        self.full_button.clicked.connect(self._on_full_editor)
        close = QPushButton("Close")
        close.setObjectName("SecondaryButton")
        close.clicked.connect(self.reject)
        editable = self.kind != money_data.KIND_COMPLEX
        if editable:
            layout.addWidget(self.save_note)
        layout.addWidget(self.void_note)
        if editable:
            row.addWidget(self.save_button)
        row.addWidget(self.void_button)
        row.addWidget(self.full_button)
        row.addStretch()
        row.addWidget(close)
        layout.addLayout(row)

        if self.blocked:
            for button in (self.save_button, self.void_button, self.full_button):
                button.setEnabled(False)
        self.full_button.setVisible(self._full_editor_fn is not None)

    # --------------------------------------------------------------- actions

    def _set_status(self, text, error=True):
        color = "#B91C1C" if error else "#059669"
        self.status_label.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.status_label.setText(text)

    def _new_lines(self, amount: Decimal):
        if self.kind == money_data.KIND_SIMPLE:
            category = self.category_combo.currentData()
            account = self.account_combo.currentData()
            if self._cat_is_debit:
                return [(None, category, amount, ZERO), (None, account, ZERO, amount)]
            return [(None, category, ZERO, amount), (None, account, amount, ZERO)]
        source, target = self.from_combo.currentData(), self.to_combo.currentData()
        if source == target:
            raise ValueError("Choose two different accounts.")
        return [(None, source, ZERO, amount), (None, target, amount, ZERO)]

    def _on_save(self):
        description = self.payee_edit.text().strip()
        if not description:
            return self._set_status("Enter a description.")
        try:
            amount = parse_amount(self.amount_edit.text())
            lines = self._new_lines(amount)
        except ValueError as problem:
            return self._set_status(str(problem))
        when = self.date_edit.date().toString("yyyy-MM-dd")
        ok, message = self.ledger.edit_transaction(self.tx.id, when, description, lines)
        if not ok:
            return self._set_status(ledger_messages.for_simple_mode(message))
        self.changed = True
        self.accept()

    def _on_void(self):
        answer = QMessageBox.question(
            self, "Void transaction",
            "Void this transaction?\n\nThis adds a reversing entry. Nothing is "
            "erased, and both entries stay in the audit log.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        ok, message = self.ledger.void_transaction(self.tx.id)
        if not ok:
            return self._set_status(ledger_messages.for_simple_mode(message))
        self.changed = True
        self.accept()

    def _on_full_editor(self):
        if self._full_editor_fn and self._full_editor_fn(self.tx):
            self.changed = True
            self.accept()
