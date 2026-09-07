# reconcile_tab.py
"""
reconcile_tab.py ver 1.0
For CogniFinance ver 1.9
Encapsulates the Reconciliation tab UI, including the unreconciled
transaction tree view and the reconciliation marking logic.
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QMessageBox
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

from ledger import GeneralLedger
from ui_helpers import add_refresh_row

class ReconcileTab(QWidget):
    def __init__(self, ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.gl = ledger
        self._init_ui()
        self.refresh_data()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        add_refresh_row(layout, self.refresh_data)

        info = QLabel(
            "Unreconciled transactions (usually from imported statements).\n"
            "Select a row and mark it as reconciled once it matches your ledger."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        reconcile_box = QGroupBox("Unreconciled Transactions")
        box_layout = QVBoxLayout(reconcile_box)

        self.reconcile_tree = QTreeWidget()
        self.reconcile_tree.setColumnCount(5)
        self.reconcile_tree.setHeaderLabels(["ID", "Date", "Description / Account", "Imported ID / Amount", "Reconciled"])

        header = self.reconcile_tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setHighlightSections(False)

        self.reconcile_tree.setRootIsDecorated(True)
        self.reconcile_tree.setAlternatingRowColors(True)
        box_layout.addWidget(self.reconcile_tree)

        btn_row = QHBoxLayout()
        btn_mark = QPushButton("Mark Selected as Reconciled")
        btn_mark.clicked.connect(self.mark_selected_reconciled)
        btn_row.addWidget(btn_mark)
        btn_row.addStretch()
        box_layout.addLayout(btn_row)

        layout.addWidget(reconcile_box)

    def refresh_data(self):
        """Fetches and displays all unreconciled transactions."""
        txs = self.gl.get_unreconciled_transactions()
        self.reconcile_tree.clear()

        for tx in txs:
            # --- TOP LEVEL ITEM (Main Transaction) ---
            item = QTreeWidgetItem(self.reconcile_tree)
            item.setData(0, Qt.ItemDataRole.UserRole, tx.id) # Store DB ID
            item.setText(0, str(tx.id))
            item.setText(1, tx.date)
            item.setText(2, tx.description)
            item.setText(3, tx.imported_id or "")
            item.setText(4, "Yes" if tx.reconciled else "No")

            # Make it selectable
            flags = item.flags() | Qt.ItemFlag.ItemIsSelectable
            item.setFlags(flags)

            # --- CHILD ITEMS (Split Lines) ---
            for line in tx.lines:
                sub_item = QTreeWidgetItem(item)
                sub_item.setText(2, line.account_name)

                dr_text = f"Dr: {line.dr:.2f}" if line.dr > 0 else ""
                cr_text = f"Cr: {line.cr:.2f}" if line.cr > 0 else ""
                sub_item.setText(3, f"{dr_text}  {cr_text}".strip())

                # Style sub-rows to look like indented split-lines
                sub_color = QColor("#f0f4f8")
                for col in range(5):
                    sub_item.setBackground(col, sub_color)

                # Prevent sub-rows from being selected independently
                sub_item.setFlags(sub_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)

    def mark_selected_reconciled(self):
        """Marks the currently selected top-level transaction as reconciled."""
        item = self.reconcile_tree.currentItem()
        if not item:
            QMessageBox.information(self, "No selection", "Please select a transaction row.")
            return

        # Ensure we are acting on the top-level item (in case user clicked a sub-row)
        while item.parent() is not None:
            item = item.parent()

        tx_id = item.data(0, Qt.ItemDataRole.UserRole)
        if tx_id is not None:
            self.gl.mark_reconciled(tx_id, True)
            self.refresh_data()
