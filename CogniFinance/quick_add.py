"""
quick_add.py ver 0.1
For CogniFinance ver 1.9

The quick-add form used by both Simple Mode Home and Money, so there is
one implementation of "add a transaction the easy way".

It posts through GeneralLedger.post_simple_transaction, the same call
Pro Mode's Add/Edit screen uses, so every ledger rule applies equally.

One category list covers three cases, and the form says which before
the click:
    an expense account  -> an expense paid from the chosen account
    a revenue account   -> income received into the chosen account
    an account under "Transfer to" -> money moved between accounts
"""
from datetime import datetime
from typing import Callable, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QLineEdit,
                             QComboBox, QPushButton)

import home_data
import ledger_messages
from simple_inputs import parse_amount
from simple_theme import LIGHT, fmt_money


class QuickAddBody(QWidget):
    data_modified = pyqtSignal()

    def __init__(self, ledger, parent=None, now_fn: Optional[Callable] = None):
        super().__init__(parent)
        self.ledger = ledger
        self._now_fn = now_fn or datetime.now
        self._theme = LIGHT

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.payee_edit = QLineEdit()
        self.payee_edit.setPlaceholderText("Who was it with?")
        self.category_combo = QComboBox()
        self.amount_edit = QLineEdit()
        self.amount_edit.setPlaceholderText("0.00")
        self.account_combo = QComboBox()
        for caption, widget in (("Payee", self.payee_edit),
                                ("Category or transfer", self.category_combo),
                                ("Amount", self.amount_edit),
                                ("Account", self.account_combo)):
            label = QLabel(caption)
            label.setObjectName("FieldLabel")
            layout.addWidget(label)
            layout.addWidget(widget)

        self.disclosure_label = QLabel()
        self.disclosure_label.setObjectName("Muted")
        self.disclosure_label.setWordWrap(True)
        layout.addWidget(self.disclosure_label)

        self.add_button = QPushButton("Add")
        self.add_button.setObjectName("PrimaryButton")
        self.add_button.clicked.connect(self._on_add)
        layout.addWidget(self.add_button)

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.category_combo.currentIndexChanged.connect(self._update_disclosure)
        self.account_combo.currentIndexChanged.connect(self._update_disclosure)
        self.refresh_choices()

    def set_theme(self, theme: dict):
        self._theme = theme

    # ------------------------------------------------------------ choices

    def refresh_choices(self):
        keep_cat = self.category_combo.currentData()
        keep_acc = self.account_combo.currentData()
        self.category_combo.blockSignals(True)
        self.account_combo.blockSignals(True)

        self.category_combo.clear()
        self.category_combo.addItem("Select category\u2026", None)
        for name in sorted(self.ledger.accounts, key=str.lower):
            if self.ledger.accounts[name].account_type in ("Expense", "Revenue"):
                self.category_combo.addItem(name, name)
        everyday, _total = home_data.everyday_accounts(self.ledger, 10_000)
        if everyday:
            self.category_combo.insertSeparator(self.category_combo.count())
            self.category_combo.addItem("Transfer to\u2026", None)
            heading = self.category_combo.count() - 1
            self.category_combo.model().item(heading).setEnabled(False)
            for row in everyday:
                self.category_combo.addItem(row.name, row.name)

        self.account_combo.clear()
        for row in everyday:
            self.account_combo.addItem(row.name, row.name)

        for combo, keep in ((self.category_combo, keep_cat), (self.account_combo, keep_acc)):
            idx = combo.findData(keep) if keep else -1
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)
        self._update_disclosure()

    def _kind(self, category: str) -> str:
        return self.ledger.accounts[category].account_type

    def _update_disclosure(self, *_):
        """Say, before the click, what Add will record."""
        category = self.category_combo.currentData()
        account = self.account_combo.currentData()
        if not category or not account:
            self.disclosure_label.setText("")
            return
        kind = self._kind(category)
        if kind == "Expense":
            self.disclosure_label.setText(f"Records an expense paid from {account}.")
        elif kind == "Revenue":
            self.disclosure_label.setText(f"Records income received into {account}.")
        else:
            self.disclosure_label.setText(f"Moves money from {account} to {category}.")

    def _set_status(self, text: str, error: bool):
        color = self._theme["negative"] if error else self._theme["accent"]
        self.status_label.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.status_label.setText(text)

    # ---------------------------------------------------------------- add

    def _on_add(self):
        payee = self.payee_edit.text().strip()
        category = self.category_combo.currentData()
        account = self.account_combo.currentData()
        if not payee:
            return self._set_status("Enter who the transaction was with.", True)
        if not category:
            return self._set_status("Choose a category or a transfer.", True)
        if not account:
            return self._set_status("Choose an account.", True)
        try:
            amount = parse_amount(self.amount_edit.text())
        except ValueError as problem:
            return self._set_status(str(problem), True)

        kind = self._kind(category)
        if kind == "Revenue":
            credit, debit = category, account        # income: category gives, account receives
        else:
            credit, debit = account, category        # expense or transfer: account gives
        when = self._now_fn().date().isoformat()
        ok, message = self.ledger.post_simple_transaction(when, payee, credit, debit, amount)
        if not ok:
            return self._set_status(ledger_messages.for_simple_mode(message), True)
        self.payee_edit.clear()
        self.amount_edit.clear()
        self._set_status(f"Added {payee}: {fmt_money(amount)}", False)
        self.data_modified.emit()
