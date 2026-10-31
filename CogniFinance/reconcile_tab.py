# reconcile_tab.py
"""
reconcile_tab.py ver 1.1
For CogniFinance ver 0.9.67
Encapsulates the Reconciliation tab UI, including the unreconciled
transaction tree view and the reconciliation marking logic.
ver 1.1: Finish Reconciliation now locks the account through the statement
end date (see period_lock.py); locked rows can no longer be toggled.
"""
import itertools
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox,
    QComboBox, QDateEdit, QLineEdit, QAbstractItemView
)
from PyQt6.QtCore import Qt, QDate, QItemSelectionModel, QTimer
from PyQt6.QtGui import QColor, QKeyEvent

from ledger import GeneralLedger
from ui_helpers import add_refresh_row, get_transaction_display_amount
from report_engine import ReportEngine

@dataclass
class ErrorAnalysis:
    delta: Decimal
    transpositions: list = field(default_factory=list)   # list[tuple[Transaction, Decimal]]
    sign_reversals: list = field(default_factory=list)   # list[Transaction]

    @property
    def has_candidates(self) -> bool:
        return bool(self.transpositions or self.sign_reversals)


class ReconcileTab(QWidget):
    def __init__(self, ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.gl = ledger
        self.is_dark_mode = False
        self.is_simple_mode = False
        self._nav_anchor = None

        self._typeahead_buffer = ""
        self._typeahead_timer = QTimer(self)
        self._typeahead_timer.setSingleShot(True)
        self._typeahead_timer.setInterval(1500)  # 1.5 seconds to reset
        self._typeahead_timer.timeout.connect(self._clear_typeahead)

        # Error-finding (transposition / sign-reversal) state. Rebuilt fresh
        # each time _find_errors() runs; cleared implicitly whenever
        # refresh_data() rebuilds the tree, since flags shouldn't survive
        # a change to the underlying data.
        self._flagged_items = []
        self._flagged_cursor = -1

        self._init_ui()
        self._populate_account_dropdown()
        self.refresh_data()

    def set_dark_mode(self, is_dark: bool):
        self.is_dark_mode = is_dark
        self.refresh_data()

    def set_simple_mode(self, simple: bool):
        """Simple Mode: one row per transaction.

        Pro Mode shows every journal line under each transaction (the
        Dr/Cr against each account). That is the double-entry view, and
        it is what a bookkeeper needs. Simple Mode keeps the facts a
        person reconciles by -- when, what, which category, how much,
        is it ticked -- and drops the accounting detail. Nothing is lost:
        the lines stay in the ledger, and Pro Mode still shows them.

        What Simple Mode does NOT do is replace the workflow with a
        quick mark-as-reconciled. The period lock is a storage-level
        guarantee: finishing locks the account, and locked rows can no
        longer be edited, deleted, or un-reconciled. A shortcut that
        skipped the statement balance would freeze a period that never
        tied out, with no diagnostic available later.
        """
        simple = bool(simple)
        if simple == self.is_simple_mode:
            return
        self.is_simple_mode = simple
        self._apply_column_layout()
        self.refresh_data()

    def _apply_column_layout(self):
        if self.is_simple_mode:
            headers = ["Date", "Description", "Category", "Amount", "Reconciled"]
        else:
            headers = ["ID", "Date", "Description / Account",
                       "Imported ID / Amount", "Reconciled"]
        self.reconcile_tree.setColumnCount(len(headers))
        self.reconcile_tree.setHeaderLabels(headers)

    def _simple_category_label(self, tx) -> str:
        """The account names for this transaction, excluding the
        reconciled account itself."""
        account = self._current_account()
        names = []
        for line in tx.lines:
            if line.account_name != account and line.account_name not in names:
                names.append(line.account_name)
        return ", ".join(names) or account or ""

    def _init_ui(self):
        layout = QVBoxLayout(self)
        add_refresh_row(layout, self.refresh_data)

        # --- Statement session controls ---
        session_box = QGroupBox("Statement")
        session_layout = QHBoxLayout(session_box)

        session_layout.addWidget(QLabel("Account:"))
        self.account_combo = QComboBox()
        self.account_combo.currentTextChanged.connect(self.refresh_data)
        session_layout.addWidget(self.account_combo)

        session_layout.addWidget(QLabel("From:"))
        self.start_date_edit = QDateEdit(calendarPopup=True)
        self.start_date_edit.setDate(QDate.currentDate().addMonths(-1))
        self.start_date_edit.dateChanged.connect(self.refresh_data)
        session_layout.addWidget(self.start_date_edit)

        session_layout.addWidget(QLabel("To:"))
        self.end_date_edit = QDateEdit(calendarPopup=True)
        self.end_date_edit.setDate(QDate.currentDate())
        self.end_date_edit.dateChanged.connect(self.refresh_data)
        session_layout.addWidget(self.end_date_edit)

        session_layout.addWidget(QLabel("Statement ending balance:"))
        self.ending_balance_edit = QLineEdit()
        self.ending_balance_edit.setPlaceholderText("0.00")
        self.ending_balance_edit.textChanged.connect(self._update_difference)
        session_layout.addWidget(self.ending_balance_edit)

        layout.addWidget(session_box)

        info = QLabel(
            "Select a row and press Space (or click) to toggle reconciled. "
            "Shift+Up/Down selects a range. Ctrl+Enter finishes once the "
            "difference reaches $0.00. If it doesn't, Ctrl+E scans for likely "
            "transposition or sign-reversal errors and flags candidates "
            "(F4 jumps between flagged rows)."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        lock_row = QHBoxLayout()
        self.lock_label = QLabel("")
        lock_row.addWidget(self.lock_label)
        self.reopen_btn = QPushButton("Reopen Last Period")
        self.reopen_btn.setEnabled(False)
        self.reopen_btn.clicked.connect(self._reopen_last_period)
        lock_row.addWidget(self.reopen_btn)
        lock_row.addStretch()
        layout.addLayout(lock_row)

        reconcile_box = QGroupBox("Statement Transactions")
        box_layout = QVBoxLayout(reconcile_box)

        self.reconcile_tree = QTreeWidget()
        self.reconcile_tree.setColumnCount(5)
        self.reconcile_tree.setHeaderLabels(["ID", "Date", "Description / Account", "Imported ID / Amount", "Reconciled"])
        self.reconcile_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        header = self.reconcile_tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setHighlightSections(False)

        self.reconcile_tree.setRootIsDecorated(True)
        self.reconcile_tree.setAlternatingRowColors(True)
        self.reconcile_tree.installEventFilter(self)
        box_layout.addWidget(self.reconcile_tree)

        btn_row = QHBoxLayout()
        btn_toggle = QPushButton("Toggle Reconciled (Space)")
        btn_toggle.clicked.connect(self._toggle_selected)
        btn_row.addWidget(btn_toggle)

        self.diff_label = QLabel("Cleared: $0.00  |  Statement: $0.00  |  Difference: $0.00")
        self.diff_label.setStyleSheet("font-weight: bold;")
        btn_row.addWidget(self.diff_label)
        btn_row.addStretch()

        self.find_errors_btn = QPushButton("Find Errors (Ctrl+E)")
        self.find_errors_btn.setEnabled(False)
        self.find_errors_btn.clicked.connect(self._find_errors)
        btn_row.addWidget(self.find_errors_btn)

        self.finish_btn = QPushButton("Finish Reconciliation (Ctrl+Enter)")
        self.finish_btn.setEnabled(False)
        self.finish_btn.clicked.connect(self._finish_reconciliation)
        btn_row.addWidget(self.finish_btn)

        self.btn_export_pdf = QPushButton("Export PDF")
        self.btn_export_pdf.clicked.connect(self._export_reconciliation_pdf)
        btn_row.addWidget(self.btn_export_pdf)

        box_layout.addLayout(btn_row)
        layout.addWidget(reconcile_box)

    def _populate_account_dropdown(self):
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        self.account_combo.addItems(sorted(self.gl.accounts.keys()))
        self.account_combo.blockSignals(False)

    def _current_account(self):
        return self.account_combo.currentText() or None

    def refresh_data(self):
        account = self._current_account()

        # Remember what was selected/focused before the rebuild, by transaction
        # id -- rebuilding the tree creates brand-new QTreeWidgetItem objects,
        # so Qt can't preserve selection across a clear() on its own.
        selected_ids = set()
        for item in self.reconcile_tree.selectedItems():
            top = item
            while top.parent() is not None:
                top = top.parent()
            tx_id = top.data(0, Qt.ItemDataRole.UserRole)
            if tx_id is not None:
                selected_ids.add(tx_id)

        current_item = self.reconcile_tree.currentItem()
        current_tx_id = None
        if current_item is not None:
            top = current_item
            while top.parent() is not None:
                top = top.parent()
            current_tx_id = top.data(0, Qt.ItemDataRole.UserRole)

        scroll_pos = self.reconcile_tree.verticalScrollBar().value()

        # Any prior "Find Errors" flags reference items that clear() is
        # about to destroy; drop the references rather than call
        # _clear_flags() (which would touch the doomed items' style).
        self._flagged_items = []
        self._flagged_cursor = -1

        self.reconcile_tree.clear()
        self._update_lock_status()
        if not account:
            self._update_difference()
            return

        start_date = self.start_date_edit.date().toString("yyyy-MM-dd")
        end_date = self.end_date_edit.date().toString("yyyy-MM-dd")
        txs = self.gl.get_transactions_for_account(account, start_date, end_date)

        restored_current = None
        for tx in txs:
            item = QTreeWidgetItem(self.reconcile_tree)
            item.setData(0, Qt.ItemDataRole.UserRole, tx.id)
            if self.is_simple_mode:
                item.setText(0, tx.date)
                item.setText(1, tx.description)
                item.setText(2, self._simple_category_label(tx))
                amount = get_transaction_display_amount(self.gl, tx)
                item.setText(3, f"{amount:,.2f}")
            else:
                item.setText(0, str(tx.id))
                item.setText(1, tx.date)
                item.setText(2, tx.description)
                item.setText(3, tx.imported_id or "")
            if self.gl.is_tx_locked(tx):
                item.setText(4, "Yes (locked)" if tx.reconciled else "No (locked)")
                item.setToolTip(4, f"Period locked through {self.gl.locked_through(account)}")
            else:
                item.setText(4, "Yes" if tx.reconciled else "No")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsSelectable)

            if tx.id in selected_ids:
                item.setSelected(True)
            if tx.id == current_tx_id:
                restored_current = item

            if not self.is_simple_mode:
                for line in tx.lines:
                    sub_item = QTreeWidgetItem(item)
                    sub_item.setText(2, line.account_name)
                    dr_text = f"Dr: {line.dr:.2f}" if line.dr > 0 else ""
                    cr_text = f"Cr: {line.cr:.2f}" if line.cr > 0 else ""
                    sub_item.setText(3, f"{dr_text}  {cr_text}".strip())

                    sub_bg = QColor("#22262E") if self.is_dark_mode else QColor("#f0f4f8")
                    sub_fg = QColor("#F3F4F6") if self.is_dark_mode else QColor("#111827")
                    for col in range(5):
                        sub_item.setBackground(col, sub_bg)
                        sub_item.setForeground(col, sub_fg)
                    sub_item.setFlags(sub_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)

        if restored_current is not None:
            self.reconcile_tree.setCurrentItem(restored_current)

        if self.is_simple_mode:
            self.reconcile_tree.collapseAll()
        else:
            self.reconcile_tree.expandAll()
        self.reconcile_tree.verticalScrollBar().setValue(scroll_pos)
        self._update_difference()

        self.reconcile_tree.setFocus() # Auto Focus for Type Ahead

        # Ensure there is a current item selected so type-ahead has a valid starting index
        top_items = self._top_level_items()
        if top_items and restored_current is None:
            self.reconcile_tree.setCurrentItem(top_items[0])

    def _top_level_items(self):
        return [self.reconcile_tree.topLevelItem(i) for i in range(self.reconcile_tree.topLevelItemCount())]

    def _toggle_selected(self):
        account = self._current_account()
        if not account:
            return
        selected = self.reconcile_tree.selectedItems()
        if not selected:
            QMessageBox.information(self, "No selection", "Please select one or more transaction rows.")
            return

        seen_tx_ids = set()
        locked_skipped = 0
        for item in selected:
            while item.parent() is not None:
                item = item.parent()
            tx_id = item.data(0, Qt.ItemDataRole.UserRole)
            if tx_id is None or tx_id in seen_tx_ids:
                continue
            seen_tx_ids.add(tx_id)
            tx = next((t for t in self.gl.transactions if t.id == tx_id), None)
            if tx is None:
                continue
            if self.gl.is_tx_locked(tx):
                locked_skipped += 1
                continue
            self.gl.mark_reconciled(tx_id, not tx.reconciled)

        self.refresh_data()
        if locked_skipped:
            QMessageBox.information(
                self, "Period Locked",
                f"{locked_skipped} selected transaction(s) are in a locked period and were not changed. "
                "Use \"Reopen Last Period\" if a correction is needed."
            )

    def _update_difference(self):
        account = self._current_account()
        end_date = self.end_date_edit.date().toString("yyyy-MM-dd") if account else None

        cleared = self.gl.cleared_balance_as_of(account, end_date) if account else Decimal("0.00")

        try:
            statement_balance = Decimal(self.ending_balance_edit.text() or "0")
        except InvalidOperation:
            statement_balance = None

        if statement_balance is None:
            self.diff_label.setText(f"Cleared: ${cleared:.2f}  |  Statement: (invalid)  |  Difference: --")
            self.diff_label.setStyleSheet("font-weight: bold; color: #c0392b;")
            self.finish_btn.setEnabled(False)
            self.find_errors_btn.setEnabled(False)
            return

        difference = statement_balance - cleared
        self.diff_label.setText(
            f"Cleared: ${cleared:.2f}  |  Statement: ${statement_balance:.2f}  |  Difference: ${difference:.2f}"
        )
        is_balanced = abs(difference) < Decimal("0.005")
        color = "#1b5e20" if is_balanced else "#c0392b"
        self.diff_label.setStyleSheet(f"font-weight: bold; color: {color};")
        self.finish_btn.setEnabled(is_balanced)
        self.find_errors_btn.setEnabled(not is_balanced)

    def _account_amount(self, tx, account: str) -> Decimal:
        """
        Signed contribution of this transaction to the given account, using
        the same normal-balance convention as GeneralLedger.cleared_balance_as_of:
        Asset/Expense accounts are dr-normal (dr - cr), Liability/Equity/
        Revenue accounts are cr-normal (cr - dr).
        """
        acc = self.gl.accounts.get(account)
        is_dr_normal = acc is not None and acc.NORMAL_BALANCES[acc.account_type] == "DR"
        total = Decimal("0.00")
        found = False
        for line in tx.lines:
            if line.account_name == account:
                total += (line.dr - line.cr) if is_dr_normal else (line.cr - line.dr)
                found = True
        return total if found else Decimal("0.00")

    def _find_error_candidates(self, account: str, delta: Decimal, as_of_date: str) -> ErrorAnalysis:
        """
        Pure, read-only diagnostic. Given the current out-of-balance delta,
        looks for a single reconciled transaction whose amount could explain
        it via digit transposition (any two-digit swap, off by a multiple of
        9) or a sign reversal (off by exactly 2x the amount). Does not touch
        ledger.py and never mutates transaction state.

        Only considers transactions that actually feed cleared_balance_as_of
        for this account/date -- i.e. reconciled AND dated on or before
        as_of_date -- so candidates are always ones that could plausibly be
        responsible for the difference currently shown.
        """
        analysis = ErrorAnalysis(delta=delta)
        if delta == 0:
            return analysis

        reconciled_txs = [
            tx for tx in self.gl.transactions
            if tx.reconciled and tx.date <= as_of_date
            and any(line.account_name == account for line in tx.lines)
        ]

        if delta % 9 == 0:
            for tx in reconciled_txs:
                amt = abs(self._account_amount(tx, account))
                if amt == 0:
                    continue
                sign, digits, exponent = amt.as_tuple()
                digits = list(digits)
                for i, j in itertools.combinations(range(len(digits)), 2):
                    if digits[i] == digits[j]:
                        continue
                    swapped = digits[:]
                    swapped[i], swapped[j] = swapped[j], swapped[i]
                    new_amt = Decimal((sign, tuple(swapped), exponent))
                    corrected_delta = new_amt - amt   # signed change to this tx's contribution
                    if corrected_delta == -delta:
                        analysis.transpositions.append((tx, new_amt))

        if delta % 2 == 0:
            target = abs(delta) / 2
            for tx in reconciled_txs:
                if abs(self._account_amount(tx, account)) == target:
                    analysis.sign_reversals.append(tx)

        return analysis

    def _clear_flags(self):
        # Top-level rows never get an explicit background/foreground of their
        # own (they rely on Qt's default palette + alternating-row coloring),
        # so "un-flagging" means resetting to an empty brush, not picking a
        # theme color -- otherwise this would override alternating-row
        # coloring for that row from then on.
        for item in self._flagged_items:
            for col in range(5):
                item.setBackground(col, QColor())
                item.setForeground(col, QColor())
            item.setToolTip(4, "")
        self._flagged_items = []
        self._flagged_cursor = -1

    def _apply_flags(self, tx_ids_to_notes: dict):
        """tx_ids_to_notes: {tx_id: note_text} for rows to highlight."""
        flag_bg = QColor("#7A4B00") if self.is_dark_mode else QColor("#FFF3CD")
        flag_fg = QColor("#FFE9B3") if self.is_dark_mode else QColor("#664D03")

        for item in self._top_level_items():
            tx_id = item.data(0, Qt.ItemDataRole.UserRole)
            note = tx_ids_to_notes.get(tx_id)
            if note is None:
                continue
            for col in range(5):
                item.setBackground(col, flag_bg)
                item.setForeground(col, flag_fg)
            item.setToolTip(4, note)
            self._flagged_items.append(item)

    def _find_errors(self):
        account = self._current_account()
        if not account:
            return

        self._clear_flags()

        try:
            statement_balance = Decimal(self.ending_balance_edit.text() or "0")
        except InvalidOperation:
            return
        end_date = self.end_date_edit.date().toString("yyyy-MM-dd")
        cleared = self.gl.cleared_balance_as_of(account, end_date)
        delta = statement_balance - cleared

        if delta == 0:
            return

        analysis = self._find_error_candidates(account, delta, end_date)

        notes = {}
        for tx, new_amt in analysis.transpositions:
            notes[tx.id] = f"Possible transposition: entered as this amount, statement suggests ${new_amt:.2f}"
        for tx in analysis.sign_reversals:
            notes[tx.id] = notes.get(tx.id, "") + " Possible sign reversal (Dr/Cr flipped)."

        if notes:
            self._apply_flags(notes)
            self._flagged_cursor = -1
            self._jump_to_next_flagged()
            msg = f"Out of balance by ${delta:.2f} — "
            parts = []
            if analysis.transpositions:
                parts.append(f"{len(analysis.transpositions)} possible transposition(s)")
            if analysis.sign_reversals:
                parts.append(f"{len(analysis.sign_reversals)} possible sign reversal(s)")
            msg += ", ".join(parts) + ". Flagged below."
        elif delta % 9 == 0 or delta % 2 == 0:
            msg = (
                f"Out of balance by ${delta:.2f} — this matches a transposition or "
                "sign-reversal pattern, but no single reconciled transaction matches "
                "exactly. Could be more than one error, or an unreconciled item."
            )
        else:
            msg = (
                f"Out of balance by ${delta:.2f} — doesn't match a transposition or "
                "sign-reversal signature. Look for a missing or duplicate entry instead."
            )

        QMessageBox.information(self, "Find Errors", msg)

    def _jump_to_next_flagged(self):
        if not self._flagged_items:
            return
        self._flagged_cursor = (self._flagged_cursor + 1) % len(self._flagged_items)
        item = self._flagged_items[self._flagged_cursor]
        self._select_and_scroll(item)

    def _update_lock_status(self):
        account = self._current_account()
        through = self.gl.locked_through(account) if account else None
        if through:
            self.lock_label.setText(f"\U0001F512 {account} is locked through {through}")
            self.lock_label.setStyleSheet("font-weight: bold; color: #1b5e20;")
        else:
            self.lock_label.setText("Period not locked")
            self.lock_label.setStyleSheet("color: gray;")
        self.reopen_btn.setEnabled(bool(through))

    def _finish_reconciliation(self):
        account = self._current_account()
        if not account:
            return
        end_qdate = self.end_date_edit.date()
        end_date = end_qdate.toString("yyyy-MM-dd")

        already = self.gl.locked_through(account)
        if already and end_date <= already:
            QMessageBox.information(
                self, "Already Locked",
                f"{account} is already locked through {already}."
            )
            return

        # Transactions inside the range being locked that were never
        # reconciled will be frozen too, so say so up front.
        unreconciled = [
            tx for tx in self.gl.transactions
            if not tx.reconciled and tx.date <= end_date
            and (not already or tx.date > already)
            and any(line.account_name == account for line in tx.lines)
        ]

        lines = [f"Reconcile {account} and lock all transactions through {end_date}?"]
        if end_qdate.day() != end_qdate.daysInMonth():
            lines.append(
                f"\nNote: {end_date} is not a month end. Everything on or before "
                "that date will be locked."
            )
        if unreconciled:
            lines.append(
                f"\n{len(unreconciled)} transaction(s) in this period are still "
                "unreconciled and will be locked as-is."
            )
        lines.append("\nLocked transactions cannot be edited, deleted, or un-reconciled.")

        answer = QMessageBox.question(
            self, "Finish and Lock Period", "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        ok, msg = self.gl.lock_period(account, end_date)
        self.refresh_data()
        if not ok:
            QMessageBox.critical(self, "Lock Failed", msg)
            return
        QMessageBox.information(
            self, "Reconciliation Complete",
            f"{account} reconciled and locked through {end_date}."
        )

    def _reopen_last_period(self):
        account = self._current_account()
        through = self.gl.locked_through(account) if account else None
        if not through:
            return
        answer = QMessageBox.question(
            self, "Reopen Period",
            f"Reopen the most recent locked month for {account}?\n\n"
            f"The lock currently runs through {through}. Transactions in that "
            "month become editable again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        ok, msg = self.gl.reopen_last_period(account)
        self.refresh_data()
        if not ok:
            QMessageBox.critical(self, "Reopen Failed", msg)

    def eventFilter(self, obj, event):
        if (obj is self.reconcile_tree or obj is self.reconcile_tree.viewport()) and \
           isinstance(event, QKeyEvent) and event.type() == event.Type.KeyPress:

            if event.key() == Qt.Key.Key_Escape:
                self._typeahead_buffer = ""
                self._typeahead_timer.stop()
                return True

            if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_F3): #Next match with Tab or F3
                if self._typeahead_buffer:
                    self._find_next_match()
                    return True
            if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                extend = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                self._navigate_top_level(event.key(), extend)
                return True
            if event.key() == Qt.Key.Key_Space:
                self._toggle_selected()
                return True
            if event.key() == Qt.Key.Key_Return and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                if self.finish_btn.isEnabled():
                    self._finish_reconciliation()
                return True
            if event.key() == Qt.Key.Key_E and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                if self.find_errors_btn.isEnabled():
                    self._find_errors()
                return True
            if event.key() == Qt.Key.Key_F4:
                self._jump_to_next_flagged()
                return True
            if event.key() == Qt.Key.Key_Right:
                item = self.reconcile_tree.currentItem()
                if item is not None and item.childCount() > 0:
                    item.setExpanded(True)
                    return True
            if event.key() == Qt.Key.Key_Left:
                item = self.reconcile_tree.currentItem()
                if item is not None and item.childCount() > 0:
                    item.setExpanded(False)
                    return True

            if event.key() == Qt.Key.Key_Backspace:
                if self._typeahead_buffer:
                    self._typeahead_buffer = self._typeahead_buffer[:-1]
                    self._typeahead_timer.start()
                    if self._typeahead_buffer:
                        self._jump_to_typeahead_match()
                    return True

            # Capture printable characters (letters, digits, punctuation)
            # Exclude Space (handled above) and ensure no Ctrl/Alt modifiers are active
            text = event.text()
            if text and text.isprintable() and text != ' ':
                if event.modifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ShiftModifier):
                    self._typeahead_buffer += text
                    self._typeahead_timer.start()
                    self._jump_to_typeahead_match()
                    return True

        return super().eventFilter(obj, event)

    def _navigate_top_level(self, key, extend_selection: bool):
        top_items = self._top_level_items()
        if not top_items:
            return

        current = self.reconcile_tree.currentItem()
        ref = current
        while ref is not None and ref.parent() is not None:
            ref = ref.parent()

        if ref is None:
            new_index = 0
        else:
            idx = top_items.index(ref)
            new_index = idx + 1 if key == Qt.Key.Key_Down else idx - 1
            new_index = max(0, min(new_index, len(top_items) - 1))

        new_item = top_items[new_index]

        if extend_selection:
            if self._nav_anchor is None:
                self._nav_anchor = ref if ref is not None else new_item
            anchor_idx = top_items.index(self._nav_anchor)
            lo, hi = sorted((anchor_idx, new_index))
            self.reconcile_tree.clearSelection()
            for i in range(lo, hi + 1):
                top_items[i].setSelected(True)
        else:
            self._nav_anchor = new_item
            self.reconcile_tree.clearSelection()
            new_item.setSelected(True)

        # NoUpdate: move keyboard focus/current-item without letting Qt's own
        # current-item-changed handling clobber the selection we just set above.
        self.reconcile_tree.setCurrentItem(new_item, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.reconcile_tree.scrollToItem(new_item)

    def _clear_typeahead(self):
        """Clears the type-ahead buffer after the timeout expires."""
        self._typeahead_buffer = ""

    def _select_and_scroll(self, item):
        """Selects the matched item and scrolls it into view without disrupting selection logic."""
        self.reconcile_tree.clearSelection()
        item.setSelected(True)
        # NoUpdate prevents Qt's default current-item-changed handling from clobbering our state
        self.reconcile_tree.setCurrentItem(item, 0, QItemSelectionModel.SelectionFlag.NoUpdate)
        self.reconcile_tree.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtTop)

    def _search_from_index(self, start_idx: int) -> bool:
        """Core search logic. Searches for the buffer starting from a specific index, wrapping around."""
        top_items = self._top_level_items()
        if not top_items or not self._typeahead_buffer:
            return False

        is_amount_search = all(c.isdigit() or c in ('.', '-', ',') for c in self._typeahead_buffer)

        # Create a wrapped search order: from start_idx to end, then 0 to start_idx
        search_order = list(range(start_idx, len(top_items))) + list(range(0, start_idx))

        for i in search_order:
            parent_item = top_items[i]

            if is_amount_search:
                # Search child rows for the amount
                for child_idx in range(parent_item.childCount()):
                    child_item = parent_item.child(child_idx)
                    if self._typeahead_buffer in child_item.text(3):
                        self._select_and_scroll(parent_item)
                        return True
            else:
                # Search parent row for description (mode-aware column)
                desc_col = 1 if self.is_simple_mode else 2
                if self._typeahead_buffer.lower() in parent_item.text(desc_col).lower():
                    self._select_and_scroll(parent_item)
                    return True

        return False

    def _jump_to_typeahead_match(self):
        """Called when typing. Starts searching from the item BELOW the current one."""
        current = self.reconcile_tree.currentItem()
        start_idx = 0
        if current:
            top_current = current
            while top_current.parent() is not None:
                top_current = top_current.parent()
            top_items = self._top_level_items()
            if top_current in top_items:
                start_idx = top_items.index(top_current) + 1

        self._search_from_index(start_idx)

    def _find_next_match(self):
        """Called when pressing Tab/F3. Finds the NEXT occurrence of the current buffer."""
        current = self.reconcile_tree.currentItem()
        start_idx = 0
        if current:
            top_current = current
            while top_current.parent() is not None:
                top_current = top_current.parent()
            top_items = self._top_level_items()
            if top_current in top_items:
                start_idx = top_items.index(top_current) + 1

        self._search_from_index(start_idx)

        # Keep the search timer alive so the user can keep pressing Tab
        self._typeahead_timer.start()
    def _export_reconciliation_pdf(self):
        from PyQt6.QtWidgets import QFileDialog

        account = self._current_account()
        if not account:
            QMessageBox.warning(self, "No Account", "Please select an account first.")
            return

        end_date_str = self.end_date_edit.date().toString("yyyy-MM-dd")
        default_name = f"Reconciliation_{account}_{end_date_str}.pdf"

        path, _ = QFileDialog.getSaveFileName(self, "Save Reconciliation Report", default_name, "PDF Files (*.pdf)")
        if not path:
            return

        try:
            statement_bal = Decimal(self.ending_balance_edit.text() or "0")
            start_date_str = self.start_date_edit.date().toString("yyyy-MM-dd")

            ReportEngine.generate_reconciliation_pdf(
                ledger=self.gl,
                account_name=account,
                start_date=start_date_str,
                end_date=end_date_str,
                statement_balance=statement_bal,
                output_path=path
            )
            QMessageBox.information(self, "Success", f"Report saved to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", str(e))
