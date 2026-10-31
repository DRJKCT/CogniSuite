"""
Audit log tab for CogniFinance.

Standalone QWidget that uses AuditLogReader for data access. Designed
to be dropped into the main tab widget with minimal wiring in ui_main.py.

audit_tab.py ver 0.2
for CogniFinance ver 0.9.67

Tables here are *display surfaces*: they render state, they never accept
in-place edits. Mutations in CogniFinance happen via actions (buttons,
dialogs), not by typing into a cell — see ui_helpers.setup_display_table
for the contract. This is separate from append-only-ness, which is
enforced at the ledger layer (sql_transaction_history is insert-only and
hash-chained).
"""
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QGroupBox,
    QFormLayout, QDateEdit, QComboBox, QLineEdit, QDialog,
    QDialogButtonBox, QAbstractItemView, QMessageBox,
)
from PyQt6.QtCore import Qt, QDate
from PyQt6.QtGui import QColor

from audit import AuditLogReader
from ui_helpers import (
    apply_column_layout,
    set_header_tooltip,
    setup_display_table,
)


# ---------------------------------------------------------------------------
# Column layouts — single source of truth per table.
# ---------------------------------------------------------------------------

_AUDIT_TABLE_HEADERS = ["When", "Type", "Tx Date", "Description", "Reason", "Summary"]
_HISTORY_DIALOG_HEADERS = ["When", "Type", "Description", "Reason", "Summary"]
_SINGLE_ENTRY_LINE_HEADERS = ["Account", "Debit", "Credit"]

_REASON_TOOLTIP = (
    "User-supplied reason recorded when a transaction is edited. "
    "Empty for 'created' and 'voided' entries."
)


class AuditTab(QWidget):
    """Paginated, filterable view of the ledger's transaction history."""

    PAGE_SIZE = 100
    TYPE_MAP = {0: None, 1: "created", 2: "updated", 3: "deleted"}
    TYPE_COLORS = {"created": "#007a00", "updated": "#d68910", "deleted": "#b00000"}

    def __init__(self, db_path: str, dek_hex: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.reader = AuditLogReader(db_path, dek_hex)
        self._page = 0
        self._total = 0
        self._rows_cache: list = []  # current page's rows, for double-click detail
        self._init_ui()
        self.refresh()

    # ------------------------------------------------------------------ UI
    def _init_ui(self):
        layout = QVBoxLayout(self)

        info = QLabel(
            "Every create, edit, and delete in the ledger is recorded here. "
            "Each entry shows the state of the transaction *before* that change."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # Filters
        filter_box = QGroupBox("Filters")
        flayout = QFormLayout()
        today = QDate.currentDate()
        self.start_edit = QDateEdit(); self.start_edit.setCalendarPopup(True)
        self.start_edit.setDate(QDate(today.year(), today.month(), 1))
        self.end_edit = QDateEdit(); self.end_edit.setCalendarPopup(True)
        self.end_edit.setDate(today)
        self.type_combo = QComboBox()
        self.type_combo.addItems(["All changes", "Created", "Updated", "Deleted"])
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search description or account name...")
        self.search_edit.setClearButtonEnabled(True)

        flayout.addRow("From:", self.start_edit)
        flayout.addRow("To:", self.end_edit)
        flayout.addRow("Change type:", self.type_combo)
        flayout.addRow("Search:", self.search_edit)
        filter_box.setLayout(flayout)
        layout.addWidget(filter_box)

        btn_row = QHBoxLayout()
        btn_apply = QPushButton("Apply Filters")
        btn_apply.clicked.connect(self._reset_page)
        btn_row.addWidget(btn_apply)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Table — display surface, row-selectable (double-click opens detail)
        self.table = QTableWidget()
        apply_column_layout(self.table, _AUDIT_TABLE_HEADERS)
        setup_display_table(self.table, selection="row")
        self._setup_headers()
        set_header_tooltip(self.table, _AUDIT_TABLE_HEADERS, "Reason", _REASON_TOOLTIP)
        self.table.doubleClicked.connect(self._show_detail_for_index)
        layout.addWidget(self.table)

        # Pagination
        pager = QHBoxLayout()
        self.prev_btn = QPushButton("← Newer"); self.prev_btn.clicked.connect(self._prev_page)
        self.page_label = QLabel("")
        self.next_btn = QPushButton("Older →"); self.next_btn.clicked.connect(self._next_page)
        pager.addStretch()
        pager.addWidget(self.prev_btn)
        pager.addWidget(self.page_label)
        pager.addWidget(self.next_btn)
        pager.addStretch()
        layout.addLayout(pager)

    def _setup_headers(self):
        """Column-sizing configuration on top of the display-surface contract."""
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setHighlightSections(False)
        header.setSectionsClickable(True)

    # ------------------------------------------------------------------ Filters
    def _filter_params(self):
        start = self.start_edit.date().toString("yyyy-MM-dd") or None
        end = self.end_edit.date().toString("yyyy-MM-dd") or None
        change_type = self.TYPE_MAP.get(self.type_combo.currentIndex())
        search = self.search_edit.text().strip() or None
        return start, end, change_type, search

    def _reset_page(self):
        self._page = 0
        self.refresh()

    def _prev_page(self):
        if self._page > 0:
            self._page -= 1
            self.refresh()

    def _next_page(self):
        if (self._page + 1) * self.PAGE_SIZE < self._total:
            self._page += 1
            self.refresh()

    # ------------------------------------------------------------------ Data
    def refresh(self):
        start, end, change_type, search = self._filter_params()
        offset = self._page * self.PAGE_SIZE

        rows = self.reader.get_audit_log(
            start_date=start, end_date=end, change_type=change_type,
            search=search, limit=self.PAGE_SIZE, offset=offset,
        )
        self._total = self.reader.count_audit_log(
            start_date=start, end_date=end, change_type=change_type, search=search,
        )
        self._rows_cache = rows

        self.table.setRowCount(len(rows))
        for row_idx, entry in enumerate(rows):
            self.table.setItem(row_idx, 0, QTableWidgetItem(entry["changed_at"] or ""))

            type_item = QTableWidgetItem(entry["change_type"].capitalize())
            color = self.TYPE_COLORS.get(entry["change_type"])
            if color:
                type_item.setForeground(QColor(color))
            self.table.setItem(row_idx, 1, type_item)

            self.table.setItem(row_idx, 2, QTableWidgetItem(entry["date"] or ""))
            self.table.setItem(row_idx, 3, QTableWidgetItem(entry["description"] or ""))
            self.table.setItem(row_idx, 4, QTableWidgetItem(entry.get("change_reason") or ""))
            # Column 5 exists now (six headers), so this lands where it should.
            self.table.setItem(row_idx, 5, QTableWidgetItem(self._summarize_lines(entry["lines"])))

        total_pages = max(1, (self._total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page_label.setText(f"Page {self._page + 1} of {total_pages}  ({self._total} entries)")
        self.prev_btn.setEnabled(self._page > 0)
        self.next_btn.setEnabled((self._page + 1) * self.PAGE_SIZE < self._total)

    # ------------------------------------------------------------------ Helpers
    @staticmethod
    def _summarize_lines(lines) -> str:
        parts = []
        for line in (lines or []):
            dr = line.get("dr", "0")
            cr = line.get("cr", "0")
            amount = dr if dr and float(dr) > 0 else cr
            parts.append(f"{line.get('account_name', '?')} ${amount}")
        return ", ".join(parts)

    # ------------------------------------------------------------------ Dialogs
    def _show_detail_for_index(self, index):
        row = index.row()
        if row < len(self._rows_cache):
            self._show_single_entry_dialog(self._rows_cache[row])

    def show_history_for_tx_id(self, tx_id: int):
        """Public entry point so other tabs (e.g. Transactions) can open
        the history dialog for a specific transaction."""
        entries = self.reader.get_history_for_tx(tx_id)
        if not entries:
            QMessageBox.information(self, "No history", "This transaction has no recorded history.")
            return
        self._show_multi_entry_dialog(f"Transaction #{tx_id} — History", entries)

    def _show_single_entry_dialog(self, entry):
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Audit Entry — {entry['change_type'].capitalize()}")
        dlg.resize(600, 450)
        layout = QVBoxLayout(dlg)

        header = QFormLayout()
        header.addRow("Change type:", QLabel(f"<b>{entry['change_type'].capitalize()}</b>"))
        header.addRow("Changed at:", QLabel(entry["changed_at"] or ""))
        header.addRow("Transaction date:", QLabel(entry["date"] or ""))
        header.addRow("Description:", QLabel(entry["description"] or ""))
        header.addRow("Change reason:", QLabel(entry.get("change_reason") or "-"))
        layout.addLayout(header)

        note = QLabel(
            "This snapshot shows the transaction's state "
            + ("<b>before</b> the edit/deletion."
               if entry["change_type"] in ("updated", "deleted")
               else "as it was first entered.")
            + " Audit snapshots are read-only."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        lines_table = QTableWidget()
        apply_column_layout(lines_table, _SINGLE_ENTRY_LINE_HEADERS)
        # Pure readout: no edit triggers, no editable delegate, no selection.
        setup_display_table(
            lines_table,
            selection="none",
            alternating_rows=False,
        )
        lines_table.horizontalHeader().setStretchLastSection(True)

        lines_table.setRowCount(len(entry["lines"]))
        for i, line in enumerate(entry["lines"]):
            lines_table.setItem(i, 0, QTableWidgetItem(line.get("account_name", "")))
            lines_table.setItem(i, 1, QTableWidgetItem(f"{line.get('dr', '0.00')}"))
            lines_table.setItem(i, 2, QTableWidgetItem(f"{line.get('cr', '0.00')}"))
        layout.addWidget(lines_table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.exec()

    def _show_multi_entry_dialog(self, title: str, entries):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(750, 500)
        layout = QVBoxLayout(dlg)

        table = QTableWidget()
        apply_column_layout(table, _HISTORY_DIALOG_HEADERS)
        setup_display_table(table, selection="row")
        table.horizontalHeader().setStretchLastSection(True)
        # Tooltip targets THIS dialog's table, looked up by header name —
        # previously it was attached to self.table (the wrong table) at a
        # hard-coded index (column 4 = Summary, not Reason).
        set_header_tooltip(table, _HISTORY_DIALOG_HEADERS, "Reason", _REASON_TOOLTIP)

        table.setRowCount(len(entries))
        for i, entry in enumerate(entries):
            table.setItem(i, 0, QTableWidgetItem(entry["changed_at"] or ""))
            type_item = QTableWidgetItem(entry["change_type"].capitalize())
            color = self.TYPE_COLORS.get(entry["change_type"])
            if color:
                type_item.setForeground(QColor(color))
            table.setItem(i, 1, type_item)
            table.setItem(i, 2, QTableWidgetItem(entry["description"] or ""))
            table.setItem(i, 3, QTableWidgetItem(entry.get("change_reason") or ""))
            table.setItem(i, 4, QTableWidgetItem(self._summarize_lines(entry["lines"])))
        layout.addWidget(table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.exec()
