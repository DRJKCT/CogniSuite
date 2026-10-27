# ui_helpers.py
from PyQt6.QtWidgets import (
    QTableWidget, QHeaderView, QHBoxLayout, QPushButton,
    QLineEdit, QAbstractItemView, QTableWidgetItem, QStyledItemDelegate,
    QComboBox, QStyle, QTreeWidget
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QStandardItemModel, QStandardItem
from decimal import Decimal
from typing import Optional


def add_refresh_row(layout, callback, search_line: Optional[QLineEdit] = None):
    """Add the standard refresh control used by ledger-backed views."""
    row = QHBoxLayout()
    row.addStretch()
    if search_line is not None:
        clear_button = QPushButton("Clear Search")
        clear_button.clicked.connect(lambda *_: search_line.clear())
        row.addWidget(clear_button)
    button = QPushButton("Refresh")
    button.clicked.connect(callback)
    row.addWidget(button)
    layout.addLayout(row)
    return button


# ---------------------------------------------------------------------------
# Display-surface vs. input-surface contract
# ---------------------------------------------------------------------------
#
# In CogniFinance, mutations are always initiated through actions
# (buttons, dialogs, keyboard shortcuts), never through in-place cell
# editing. A table's job is to *display* state; inputs live in forms and
# dialogs. This is orthogonal to whether the underlying data is
# mutable: the Accounts tab mutates accounts, but not by letting you
# type into a cell; the Audit tab never mutates anything, but neither
# does its table let you type into a cell. Both are display surfaces.
#
# `setup_display_table` makes that contract explicit in one place, so a
# table that should be a display surface can't accidentally accept
# in-place edits (as the audit single-entry dialog's lines table did).
#
# `in_place_editing=True` is reserved for the rare case where a cell
# genuinely *is* an input. Nothing in the app currently uses it, and
# setting it should be a deliberate, reviewable design decision rather
# than a config tweak.

_SELECTION_MODES = {
    "row":      QAbstractItemView.SelectionMode.ExtendedSelection,
    "single":   QAbstractItemView.SelectionMode.SingleSelection,
    "none":     QAbstractItemView.SelectionMode.NoSelection,
}


def setup_display_table(
    table,
    *,
    selection: str = "row",
    in_place_editing: bool = False,
    alternating_rows: bool = True,
):
    """
    Configure a QTableWidget or QTreeWidget as a display surface.

    table:
        The QTableWidget / QTreeWidget to configure.
    selection:
        "row"    — whole-row selection (tables you act on)
        "single" — single-row selection
        "none"   — no selection (pure computed readouts)
    in_place_editing:
        Reserved. Leave False (the default) for every table in this app.
    alternating_rows:
        Turn zebra striping on/off.
    """
    if not in_place_editing:
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(_SELECTION_MODES.get(selection, _SELECTION_MODES["row"]))
    table.setAlternatingRowColors(alternating_rows)

    # Both QTableWidget and QTreeWidget expose verticalHeader; hide the
    # per-row numbers, which carry no meaning here.
    vheader = getattr(table, "verticalHeader", None)
    if callable(vheader):
        vheader().setVisible(False)


# ---------------------------------------------------------------------------
# Column-layout helper (single source of truth for header lists)
# ---------------------------------------------------------------------------

def apply_column_layout(table, headers):
    """
    Set column count and header labels from a single header list.

    Fixes the class of bug where setColumnCount(N) and the length of the
    label list disagree: Qt silently truncates extra labels and silently
    accepts setItem(row, col) writes past the real column count, so the
    mismatch never surfaces as an error. Deriving both from one list
    means they cannot disagree.
    """
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(headers)


def set_header_tooltip(table, headers, column_name: str, tooltip: str):
    """
    Attach a tooltip to a named column. Looks the column up by header
    name rather than by index, so adding/reordering columns can't
    silently point the tooltip at the wrong place.
    """
    try:
        index = headers.index(column_name)
    except ValueError:
        return
    item = table.horizontalHeaderItem(index)
    if item is not None:
        item.setToolTip(tooltip)


# ---------------------------------------------------------------------------
# Header configuration for interactive resizing / fractional widths
# ---------------------------------------------------------------------------

def setup_table_headers(table: QTableWidget, fractions=None):
    """
    Legacy entry point kept for existing callers. Applies the
    display-surface contract, then configures interactive resizing and
    optional fractional widths.

    New code should call setup_display_table() for the contract and
    (optionally) fit_table_columns() for sizing; this wrapper remains so
    existing call sites don't need to change at once.
    """
    setup_display_table(table)

    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(True)
    header.setHighlightSections(False)
    header.setSectionsClickable(True)

    if fractions:
        table.setProperty("fractions", tuple(fractions))
        table.setProperty("user_adjusted", False)
        table.setProperty("applying_widths", False)
        header.sectionResized.connect(lambda *_: _table_section_resized(table))
        QTimer.singleShot(0, lambda: fit_table_columns(table))


def _table_section_resized(table: QTableWidget):
    if table.property("applying_widths"):
        return
    table.setProperty("user_adjusted", True)


def fit_table_columns(table: QTableWidget):
    """Applies fractional widths to table columns if the user hasn't manually resized them."""
    fractions = table.property("fractions")
    if not fractions or table.property("user_adjusted"):
        return
    column_count = table.columnCount()
    if column_count == 0:
        return
    total_width = table.viewport().width() or table.width() or 1000
    usable_width = max(200, total_width - 2)
    last_column = column_count - 1
    table.setProperty("applying_widths", True)
    try:
        for col in range(column_count):
            if col == last_column:
                continue
            fraction = fractions[col] if col < len(fractions) else 1.0 / column_count
            width = max(70, int(usable_width * fraction))
            table.setColumnWidth(col, width)
    finally:
        table.setProperty("applying_widths", False)


def money_item(value, editable=False):
    """Create a right-aligned, non-editable table item for monetary values."""
    item = QTableWidgetItem(f"{value:,.2f}")
    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    if not editable:
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


def get_transaction_display_amount(ledger, tx) -> Decimal:
    """
    Determines the primary display amount for a transaction.
    Prefers Expenses, then Revenue, then falls back to the first non-zero line.
    """
    amount = Decimal("0.00")
    for line in tx.lines:
        acc = ledger.accounts.get(line.account_name)
        if acc and acc.account_type == "Expense" and line.dr > 0:
            amount += line.dr
    if amount > 0:
        return amount

    for line in tx.lines:
        acc = ledger.accounts.get(line.account_name)
        if acc and acc.account_type == "Revenue" and line.cr > 0:
            amount += line.cr
    if amount > 0:
        return amount

    for line in tx.lines:
        if line.dr > 0:
            return line.dr
        if line.cr > 0:
            return line.cr

    return Decimal("0.00")


# ---------------------------------------------------------------------------
# Account combo helpers
# ---------------------------------------------------------------------------

ACCOUNT_COLORS = {
    "Asset": "#2e7d32",
    "Liability": "#333333",
    "Equity": "#1565c0",
    "Revenue": "#6a1b9a",
    "Expense": "#c62828"
}


class AccountColorDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(option.rect, QColor("#e8f0fe"))
        else:
            painter.fillRect(option.rect, option.palette.base())

        brush = index.data(Qt.ItemDataRole.ForegroundRole)
        text_color = QColor("#000000")
        if brush is not None:
            c = brush.color() if hasattr(brush, 'color') else brush
            if isinstance(c, QColor):
                text_color = c

        painter.setPen(text_color)
        font = option.font
        font.setBold(True)
        painter.setFont(font)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        painter.drawText(
            option.rect.adjusted(8, 0, -8, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            text
        )
        painter.restore()


def create_account_combo(ledger, account_name=None):
    combo = QComboBox()
    delegate = AccountColorDelegate(combo)
    combo.setItemDelegate(delegate)
    combo.view().setItemDelegate(delegate)

    model = QStandardItemModel()
    account_names = sorted(ledger.accounts.keys())
    for name in account_names:
        acc = ledger.accounts.get(name)
        acc_type = acc.account_type if acc else "Expense"
        color_hex = ACCOUNT_COLORS.get(acc_type, "#000000")
        item = QStandardItem(name)
        item.setData(name, Qt.ItemDataRole.UserRole)
        item.setForeground(QColor(color_hex))
        model.appendRow(item)
    combo.setModel(model)

    if account_name is not None:
        for i in range(model.rowCount()):
            if model.item(i).data(Qt.ItemDataRole.UserRole) == account_name:
                combo.setCurrentIndex(i)
                break

    def _update_text_color(index):
        if index >= 0:
            item = model.item(index)
            if item:
                color = item.foreground().color()
                combo.setStyleSheet(f"QComboBox {{ color: {color.name()}; font-weight: bold; }}")

    combo.currentIndexChanged.connect(_update_text_color)
    _update_text_color(combo.currentIndex())
    return combo


def stretch_account_column(table: QTableWidget, account_col: int, other_cols: list):
    """Dynamically stretches the account column to fill remaining space."""
    def _stretch():
        total_width = table.viewport().width()
        other_width = sum(table.columnWidth(i) for i in other_cols)
        account_width = total_width - other_width - 15
        if account_width > 150:
            table.setColumnWidth(account_col, account_width)
    QTimer.singleShot(0, _stretch)


# ---------------------------------------------------------------------------
# Simple Mode: shared reversible progressive-disclosure affordance
# ---------------------------------------------------------------------------
#
# Simple Mode hides specialist controls. The contract (in simple_mode.py)
# requires that whatever is hidden stays one click away. This class owns
# the mechanics -- which widgets are specialist controls, and what the
# reveal looks like, belongs to the tab.
#
# In Pro Mode the button disappears entirely and the tab's own visibility
# rules (e.g. _update_controls_visibility) take over again, so Pro
# Mode looks exactly as it did before Simple Mode existed.

class AdvancedDisclosure:
    def __init__(self, parent, widgets, label="Show advanced", on_restore=None):
        self.widgets = list(widgets)
        self.expanded = False
        self.on_restore = on_restore
        self.button = QPushButton(label)
        self.button.setFlat(True)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        # High-contrast style so the reveal link is always readable,
        # regardless of the parent background (light/dark theme).
        self.button.setStyleSheet(
            "text-align: left; color: #047857; background-color: transparent; "
            "padding: 4px 8px; font-weight: bold; border: none; "
            "min-width: 140px;"
        )
        self.button.clicked.connect(self.toggle)
        self.button.setVisible(False)

    def apply_simple(self, simple: bool):
        if simple:
            self.expanded = False
            for w in self.widgets:
                w.setVisible(False)
            self.button.setVisible(True)
            self.button.setText("Show advanced" if not self.expanded else "Hide advanced")
        else:
            self.button.setVisible(False)
            if self.on_restore:
                self.on_restore()

    def toggle(self):
        self.expanded = not self.expanded
        for w in self.widgets:
            w.setVisible(self.expanded)
        self.button.setText("Hide advanced" if self.expanded else "Show advanced")

    @property
    def is_expanded(self) -> bool:
        return self.expanded
