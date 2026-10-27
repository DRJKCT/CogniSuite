"""
simple_money_tab.py ver 0.1
For CogniFinance ver 1.9

Simple Mode's Money screen. One place for what Pro splits across the
Add/Edit and Transactions tabs: add something, find something, fix or
void something, and bring in a bank file.

Same engine as Pro. The list comes from GeneralLedger.get_transactions,
adds go through post_simple_transaction, corrections through
edit_transaction and voids through void_transaction. It does less, not
differently: no multi-account filters, no bulk actions, no history
view. Those stay on the full Transactions screen.
"""
from datetime import datetime
from typing import Callable, Dict, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QLineEdit, QComboBox, QPushButton, QScrollArea, QMessageBox,
)

import home_data
import ledger_messages
import money_data
from money_edit_dialog import MoneyEditDialog
from quick_add import QuickAddBody
from simple_theme import LIGHT, DARK, fmt_money, stylesheet

PAGE_SIZE = 50


def _default_amount_fn() -> Callable:
    try:
        from ui_helpers import get_transaction_display_amount
        return get_transaction_display_amount
    except Exception:
        from simple_home_tab import _fallback_amount
        return _fallback_amount


class TxRow(QFrame):
    """One transaction. The whole row is the button."""
    clicked = pyqtSignal(object)

    def __init__(self, row: "money_data.TxRowData", theme: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("TxRow")
        self.tx_id = row.tx_id
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)

        left = QVBoxLayout()
        left.setSpacing(0)
        title = QLabel(row.description)
        title.setStyleSheet("font-weight: 600;")
        sub = QLabel(f"{row.category}  \u00b7  {row.account}" if row.account else row.category)
        sub.setObjectName("Muted")
        left.addWidget(title)
        left.addWidget(sub)
        layout.addLayout(left, 1)

        for text in self._badges(row):
            badge = QLabel(text)
            badge.setObjectName("Badge")
            layout.addWidget(badge)

        amount = QLabel(fmt_money(row.amount, signed=True))
        color = theme["accent"] if row.amount > 0 else (theme["negative"] if row.amount < 0 else theme["muted"])
        amount.setStyleSheet(f"color: {color}; font-weight: 600;")
        amount.setMinimumWidth(96)
        amount.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(amount)

        for child in self.findChildren(QLabel):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    @staticmethod
    def _badges(row):
        badges = []
        if row.is_split:
            badges.append("Split")
        if row.reconciled:
            badges.append("Reconciled")
        if row.locked:
            badges.append("Locked")
        return badges

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.tx_id)
        super().mouseReleaseEvent(event)


class SimpleMoneyTab(QWidget):
    data_modified = pyqtSignal()

    def __init__(self, ledger, parent=None, amount_fn: Optional[Callable] = None,
                 now_fn: Optional[Callable] = None,
                 import_actions: Optional[Dict[str, Callable]] = None,
                 full_editor_fn: Optional[Callable] = None):
        super().__init__(parent)
        self.ledger = ledger
        self._amount_fn = amount_fn or _default_amount_fn()
        self._now_fn = now_fn or datetime.now
        self._import_actions = dict(import_actions or {})
        self._full_editor_fn = full_editor_fn or self._open_full_editor
        self.is_dark_mode = False
        self._theme = LIGHT
        self._limit = PAGE_SIZE
        self._build()
        self.refresh()

    # ----------------------------------------------------------------- build

    def _card(self, title: str):
        frame = QFrame()
        frame.setObjectName("HomeCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        label = QLabel(title.upper())
        label.setObjectName("CardTitle")
        layout.addWidget(label)
        return frame, layout

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)
        page = QWidget()
        page.setObjectName("MoneyPage")
        scroll.setWidget(page)
        grid = QGridLayout(page)
        grid.setContentsMargins(24, 20, 24, 24)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(16)
        for column in range(12):
            grid.setColumnStretch(column, 1)

        title = QLabel("Money")
        title.setObjectName("Greeting")
        grid.addWidget(title, 0, 0, 1, 12)

        # --- the list
        list_card, self.list_layout = self._card("Transactions")
        filters = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search")
        self.search_edit.setClearButtonEnabled(True)
        self.period_combo = QComboBox()
        for label, key in money_data.PERIOD_CHOICES:
            self.period_combo.addItem(label, key)
        self.account_combo = QComboBox()
        filters.addWidget(self.search_edit, 1)
        filters.addWidget(self.period_combo)
        filters.addWidget(self.account_combo)
        self.list_layout.addLayout(filters)
        self.rows_host = QWidget()
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(0)
        self.list_layout.addWidget(self.rows_host)
        self.footer_label = QLabel()
        self.footer_label.setObjectName("Muted")
        self.footer_label.setWordWrap(True)
        self.list_layout.addWidget(self.footer_label)
        self.more_button = QPushButton(f"Show {PAGE_SIZE} more")
        self.more_button.setObjectName("SecondaryButton")
        self.more_button.clicked.connect(self._show_more)
        self.list_layout.addWidget(self.more_button)
        self.list_layout.addStretch()
        grid.addWidget(list_card, 1, 0, 2, 9)

        # --- quick add (shared with Home)
        add_card, add_layout = self._card("Add transaction")
        add_card.setMinimumWidth(230)
        self.quick_add = QuickAddBody(self.ledger, now_fn=self._now_fn)
        self.quick_add.data_modified.connect(self._after_change)
        add_layout.addWidget(self.quick_add)
        grid.addWidget(add_card, 1, 9, 1, 3)

        # --- bring in a file
        self.import_card, import_layout = self._card("Bring in transactions")
        hint = QLabel("Bank files and statements. They are checked before anything is added.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        import_layout.addWidget(hint)
        self.import_buttons = {}
        for label, action in self._import_actions.items():
            button = QPushButton(label)
            button.setObjectName("SecondaryButton")
            button.clicked.connect(lambda _=False, fn=action: fn())
            import_layout.addWidget(button)
            self.import_buttons[label] = button
        self.import_card.setVisible(bool(self._import_actions))
        grid.addWidget(self.import_card, 2, 9, 1, 3)
        grid.setRowStretch(3, 1)

        self.search_edit.textChanged.connect(self._filters_changed)
        self.period_combo.currentIndexChanged.connect(self._filters_changed)
        self.account_combo.currentIndexChanged.connect(self._filters_changed)

    # ----------------------------------------------------------------- theme

    def set_dark_mode(self, is_dark: bool):
        self.is_dark_mode = bool(is_dark)
        self._theme = DARK if is_dark else LIGHT
        self.refresh()

    def _apply_theme(self):
        self.setStyleSheet(stylesheet(self._theme, "MoneyPage"))
        self.quick_add.set_theme(self._theme)

    # ------------------------------------------------------------------ data

    def _filters_changed(self, *_):
        self._limit = PAGE_SIZE
        self._fill_list()

    def _show_more(self):
        self._limit += PAGE_SIZE
        self._fill_list()

    def refresh(self):
        self._apply_theme()
        keep = self.account_combo.currentData()
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        self.account_combo.addItem("All accounts", "")
        everyday, _n = home_data.everyday_accounts(self.ledger, 10_000)
        for row in everyday:
            self.account_combo.addItem(row.name, row.name)
        idx = self.account_combo.findData(keep) if keep else 0
        self.account_combo.setCurrentIndex(max(0, idx))
        self.account_combo.blockSignals(False)
        self.quick_add.refresh_choices()
        self._fill_list()

    def _fill_list(self):
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        data = money_data.build_money_data(
            self.ledger, self._now_fn().date(), self._amount_fn,
            period=self.period_combo.currentData() or "this_month",
            account=self.account_combo.currentData() or None,
            search=self.search_edit.text().strip() or None,
            limit=self._limit)

        if not data.days:
            empty = QLabel("No transactions match. Try a longer period or clear the search.")
            empty.setObjectName("Muted")
            empty.setWordWrap(True)
            self.rows_layout.addWidget(empty)
        for day in data.days:
            heading = QLabel(day.label.upper())
            heading.setObjectName("DayHeading")
            self.rows_layout.addWidget(heading)
            for row in day.rows:
                widget = TxRow(row, self._theme)
                widget.clicked.connect(self._open_transaction)
                self.rows_layout.addWidget(widget)

        text = f"Showing {data.shown} of {data.total} transactions"
        if data.hidden_count:
            text += (f". {data.hidden_count} voided or reversing entries are hidden; "
                     "they appear on the full Transactions screen.")
        self.footer_label.setText(text)
        self.more_button.setVisible(data.total > data.shown)

    # ---------------------------------------------------------------- actions

    def _after_change(self):
        self.refresh()
        self.data_modified.emit()

    def _find_tx(self, tx_id):
        return next((t for t in self.ledger.transactions if t.id == tx_id), None)

    def _open_transaction(self, tx_id):
        tx = self._find_tx(tx_id)
        if tx is None:
            QMessageBox.warning(self, "Not found", "That transaction could not be found.")
            return
        dialog = MoneyEditDialog(self.ledger, tx, self, full_editor_fn=self._full_editor_fn)
        dialog.setStyleSheet(stylesheet(self._theme, "MoneyDialog"))
        dialog.exec()
        if dialog.changed:
            self._after_change()

    def _open_full_editor(self, tx) -> bool:
        """Pro's own editor, for transactions Simple Mode shows read-only."""
        from transaction_dialogs import EditTransactionDialog
        dialog = EditTransactionDialog(self.ledger, tx, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return False
        new_date, new_desc, new_lines = dialog.get_data()
        ok, message = self.ledger.edit_transaction(tx.id, new_date, new_desc, new_lines)
        if not ok:
            QMessageBox.warning(self, "Transaction Not Updated",
                                ledger_messages.for_simple_mode(message))
            return False
        return True
