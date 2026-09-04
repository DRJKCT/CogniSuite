# ui_helpers.py
from PyQt6.QtWidgets import (
    QTableWidget, QHeaderView, QHBoxLayout, QPushButton,
    QLineEdit, QAbstractItemView, QTableWidgetItem, QStyledItemDelegate, QComboBox, QStyle
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

def setup_table_headers(table: QTableWidget, fractions=None):
    """Configures table headers for interactive resizing and optional fractional widths."""
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(True)
    header.setHighlightSections(False)
    header.setSectionsClickable(True)
    table.verticalHeader().setVisible(False)

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

# Add these imports to the top of ui_helpers.py if not already present:
# from PyQt6.QtWidgets import QStyledItemDelegate, QComboBox, QStyle
# from PyQt6.QtGui import QColor, QStandardItemModel, QStandardItem
# from PyQt6.QtCore import Qt, QTimer

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
