"""
plans_dialogs.py ver 0.1
For CogniFinance ver 1.9

The write side of the Plans screen. Each dialog says what it will do
before the click and calls the same GeneralLedger method Pro Mode uses:

    assign / take back  -> assign_to_envelope / unassign_from_envelope
    move / cover        -> move_budget_money (both rows written together)
    add envelope        -> upsert_budget_envelope (carry_forward by default)
    change link         -> upsert_budget_envelope (policy left as it is)

The ledger's refusal messages are shown as the ledger wrote them.
"""
from decimal import Decimal
from typing import Optional

from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                             QLineEdit, QComboBox, QPushButton)

import ledger_messages
import plans_data
from ledger import ZERO
from simple_inputs import parse_amount
from simple_theme import fmt_money


class _Base(QDialog):
    def __init__(self, ledger, summary, month: str, parent=None):
        super().__init__(parent)
        self.ledger, self.summary, self.month = ledger, summary, month
        self.changed = False
        self.setMinimumWidth(380)
        self._layout = QVBoxLayout(self)
        self._layout.setSpacing(8)

    def _field(self, caption: str, widget):
        label = QLabel(caption)
        label.setObjectName("FieldLabel")
        self._layout.addWidget(label)
        self._layout.addWidget(widget)

    def _footer(self, action_text: str):
        self.note = QLabel()
        self.note.setObjectName("Muted")
        self.note.setWordWrap(True)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self._layout.addWidget(self.note)
        self._layout.addWidget(self.status)
        row = QHBoxLayout()
        self.action_button = QPushButton(action_text)
        self.action_button.setObjectName("PrimaryButton")
        self.action_button.clicked.connect(self._on_action)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        row.addWidget(self.action_button)
        row.addStretch()
        row.addWidget(cancel)
        self._layout.addLayout(row)

    def _fail(self, text: str):
        self.status.setStyleSheet("color: #B91C1C; font-size: 12px;")
        self.status.setText(ledger_messages.for_simple_mode(text))

    def _envelope_combo(self, envelopes, current: Optional[str] = None, show_available=False):
        combo = QComboBox()
        for env in envelopes:
            text = env.category if not show_available else f"{env.category}  ({fmt_money(env.available)} available)"
            combo.addItem(text, env.category)
        if current:
            combo.setCurrentIndex(max(0, combo.findData(current)))
        return combo

    def _finish(self, ok: bool, message: str):
        if not ok:
            return self._fail(message)
        self.changed = True
        self.accept()


class AssignDialog(_Base):
    """Put money into an envelope from Left to assign, or take it back."""

    def __init__(self, ledger, summary, month, parent=None, envelope=None, take_back=False):
        super().__init__(ledger, summary, month, parent)
        self.take_back = take_back
        self.setWindowTitle("Take money back" if take_back else "Assign money")
        self.envelope_combo = self._envelope_combo(summary.envelopes, envelope)
        self.amount_edit = QLineEdit()
        self.amount_edit.setPlaceholderText("0.00")
        self._field("Envelope", self.envelope_combo)
        self._field("Amount", self.amount_edit)
        self._footer("Take back" if take_back else "Assign")
        self.envelope_combo.currentIndexChanged.connect(self._update_note)
        self.amount_edit.textChanged.connect(self._update_note)
        self._update_note()

    def _update_note(self, *_):
        name = self.envelope_combo.currentData() or "the envelope"
        try:
            amount = parse_amount(self.amount_edit.text())
        except ValueError:
            amount = None
        shown = fmt_money(amount) if amount is not None else "the amount"
        if self.take_back:
            self.note.setText(f"Returns {shown} from {name} to Left to assign.")
        else:
            self.note.setText(f"Moves {shown} from Left to assign into {name}.")

    def _on_action(self):
        name = self.envelope_combo.currentData()
        if not name:
            return self._fail("Choose an envelope.")
        try:
            amount = parse_amount(self.amount_edit.text())
        except ValueError as problem:
            return self._fail(str(problem))
        call = self.ledger.unassign_from_envelope if self.take_back else self.ledger.assign_to_envelope
        self._finish(*call(self.month, name, amount))


class MoveDialog(_Base):
    """Move money between two envelopes, or cover an overspent one."""

    def __init__(self, ledger, summary, month, parent=None, target=None):
        super().__init__(ledger, summary, month, parent)
        self.setWindowTitle("Cover overspending" if target else "Move money")
        envelopes = summary.envelopes
        covering = None
        if target:
            covering = next((e for e in envelopes if e.category == target), None)
            sources = plans_data.cover_sources(summary, target)
        else:
            sources = list(envelopes)
        self.from_combo = self._envelope_combo(sources, show_available=True)
        self.to_combo = self._envelope_combo(envelopes, target)
        self.amount_edit = QLineEdit()
        self.amount_edit.setPlaceholderText("0.00")
        if covering is not None:
            gap = plans_data.shortfall(covering)
            if gap > ZERO:
                self.amount_edit.setText(f"{gap:.2f}")
        self._field("Move from", self.from_combo)
        self._field("Move to", self.to_combo)
        self._field("Amount", self.amount_edit)
        self._footer("Move")
        for widget in (self.from_combo, self.to_combo):
            widget.currentIndexChanged.connect(self._update_note)
        self.amount_edit.textChanged.connect(self._update_note)
        self._update_note()
        if target and not sources:
            self._fail("No other envelope has money available to move. "
                       "Assign more money first.")
            self.action_button.setEnabled(False)

    def _update_note(self, *_):
        source = self.from_combo.currentData() or "the first envelope"
        target = self.to_combo.currentData() or "the second envelope"
        try:
            shown = fmt_money(parse_amount(self.amount_edit.text()))
        except ValueError:
            shown = "the amount"
        self.note.setText(f"Moves {shown} from {source} to {target}. "
                          "Left to assign does not change.")

    def _on_action(self):
        source, target = self.from_combo.currentData(), self.to_combo.currentData()
        if not source or not target:
            return self._fail("Choose both envelopes.")
        if source == target:
            return self._fail("Choose two different envelopes.")
        try:
            amount = parse_amount(self.amount_edit.text())
        except ValueError as problem:
            return self._fail(str(problem))
        self._finish(*self.ledger.move_budget_money(source, target, amount, self.month))


class AddEnvelopeDialog(_Base):
    """A new envelope that tracks one expense account."""

    def __init__(self, ledger, summary, month, parent=None):
        super().__init__(ledger, summary, month, parent)
        self.setWindowTitle("Add envelope")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Groceries")
        self.account_combo = QComboBox()
        for name in plans_data.free_expense_accounts(ledger):
            self.account_combo.addItem(name, name)
        self._field("Name", self.name_edit)
        self._field("Spending account it tracks", self.account_combo)
        self._footer("Add envelope")
        self.note.setText("Spending in this account comes out of the envelope. "
                          "Unspent money carries over to the next month.")
        if not self.account_combo.count():
            self._fail("Every expense account already has an envelope.")
            self.action_button.setEnabled(False)
        self.account_combo.currentIndexChanged.connect(self._suggest_name)

    def _suggest_name(self, *_):
        if not self.name_edit.text().strip() and self.account_combo.currentData():
            self.name_edit.setPlaceholderText(self.account_combo.currentData())

    def _on_action(self):
        name = self.name_edit.text().strip() or (self.account_combo.currentData() or "")
        linked = self.account_combo.currentData()
        problem = plans_data.validate_new_envelope(self.ledger, name, linked)
        if problem:
            return self._fail(problem)
        self._finish(*self.ledger.upsert_budget_envelope(name, linked, "carry_forward", True))


class ChangeLinkDialog(_Base):
    """Point an existing envelope at a different spending account."""

    def __init__(self, ledger, summary, month, parent=None, envelope=None):
        super().__init__(ledger, summary, month, parent)
        self.setWindowTitle("Change what an envelope tracks")
        self.envelope_combo = self._envelope_combo(summary.envelopes, envelope)
        self.account_combo = QComboBox()
        self._field("Envelope", self.envelope_combo)
        self._field("Spending account it tracks", self.account_combo)
        self._footer("Save")
        self.note.setText("Spending history follows the account, so this envelope's "
                          "Spent and Available can change for past months too.")
        self.envelope_combo.currentIndexChanged.connect(self._fill_accounts)
        self._fill_accounts()

    def _current(self):
        name = self.envelope_combo.currentData()
        return next((e for e in self.summary.envelopes if e.category == name), None)

    def _fill_accounts(self, *_):
        env = self._current()
        self.account_combo.clear()
        if env is None:
            return
        for name in plans_data.free_expense_accounts(
                self.ledger, keep=env.linked_account, exclude_category=env.category):
            self.account_combo.addItem(name, name)
        if env.linked_account:
            self.account_combo.setCurrentIndex(max(0, self.account_combo.findData(env.linked_account)))

    def _on_action(self):
        env = self._current()
        linked = self.account_combo.currentData()
        if env is None or not linked:
            return self._fail("Choose an envelope and an account.")
        if linked in plans_data.linked_accounts(self.ledger, exclude_category=env.category):
            return self._fail(f"{linked} is already tracked by another envelope.")
        self._finish(*self.ledger.upsert_budget_envelope(env.category, linked, env.policy, True))
