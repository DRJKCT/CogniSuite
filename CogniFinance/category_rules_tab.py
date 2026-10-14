"""
category_rules_tab.py ver 0.1
For CogniFinance ver 1.8
Dedicated widget for managing category rules.
"""
from typing import Optional
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QComboBox, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QGroupBox, QMessageBox
)
from PyQt6.QtCore import Qt
from ledger import GeneralLedger


class CategoryRulesTab(QWidget):
    """Widget for managing category auto-assignment rules."""

    def __init__(self, ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.gl = ledger
        self.rule_edit_id = None
        self._init_ui()
        self.refresh_rules_table()
        self.refresh_blacklist_table()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        form_box = QGroupBox("Add / Edit Category Rule")
        flayout = QFormLayout()

        self.rule_keyword = QLineEdit()
        self.rule_keyword.setPlaceholderText("e.g. T&T, STARBUCKS, GROCERY")

        self.rule_category = QComboBox()
        self._populate_category_combo()

        self.rule_case = QComboBox()
        self.rule_case.addItem("Case insensitive", False)
        self.rule_case.addItem("Case sensitive", True)

        self.rule_enabled = QComboBox()
        self.rule_enabled.addItem("Enabled", True)
        self.rule_enabled.addItem("Disabled", False)

        flayout.addRow("Keyword:", self.rule_keyword)
        flayout.addRow("Category:", self.rule_category)
        flayout.addRow("Case match:", self.rule_case)
        flayout.addRow("Status:", self.rule_enabled)

        form_box.setLayout(flayout)
        layout.addWidget(form_box)

        btn_row = QHBoxLayout()
        btn_add = QPushButton("Add Rule")
        btn_add.clicked.connect(self.add_category_rule)

        btn_update = QPushButton("Update Selected")
        btn_update.clicked.connect(self.update_category_rule)

        btn_delete = QPushButton("Delete Selected")
        btn_delete.clicked.connect(self.delete_category_rule)

        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_update)
        btn_row.addWidget(btn_delete)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.rules_table = QTableWidget()
        self.rules_table.setColumnCount(5)
        self.rules_table.setHorizontalHeaderLabels(["ID", "Keyword", "Category", "Case", "Enabled"])

        header = self.rules_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setHighlightSections(False)
        self.rules_table.verticalHeader().setVisible(False)
        self.rules_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.rules_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        self.rules_table.itemSelectionChanged.connect(self.on_rule_selected)

        layout.addWidget(self.rules_table)

        # --- Blacklisted Patterns ---
        # Point 6: blacklisting must be visible and reversible in the UI.
        # This is also the one place Simple Mode pooling could otherwise
        # silently blacklist a granular pattern the user never
        # individually saw -- this list always shows the real, granular
        # (merchant_key, category) rows, regardless of whether the Red
        # rating that created them came from a pooled Simple Mode view.
        blacklist_box = QGroupBox("Blacklisted Patterns")
        blacklist_layout = QVBoxLayout()

        self.blacklist_table = QTableWidget()
        self.blacklist_table.setColumnCount(3)
        self.blacklist_table.setHorizontalHeaderLabels(["Merchant", "Category", "Blacklisted At"])
        bl_header = self.blacklist_table.horizontalHeader()
        bl_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        bl_header.setStretchLastSection(True)
        bl_header.setHighlightSections(False)
        self.blacklist_table.verticalHeader().setVisible(False)
        self.blacklist_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.blacklist_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        blacklist_layout.addWidget(self.blacklist_table)

        bl_btn_row = QHBoxLayout()
        btn_unblacklist = QPushButton("Un-blacklist Selected")
        btn_unblacklist.clicked.connect(self.unblacklist_selected)
        bl_btn_row.addWidget(btn_unblacklist)
        bl_btn_row.addStretch()
        blacklist_layout.addLayout(bl_btn_row)

        blacklist_box.setLayout(blacklist_layout)
        layout.addWidget(blacklist_box)

    def _populate_category_combo(self):
        """Populate the category dropdown with all accounts."""
        self.rule_category.clear()
        for name in sorted(self.gl.accounts.keys()):
            self.rule_category.addItem(name, name)

    def refresh_rules_table(self):
        """Refresh the rules table from the ledger."""
        rules = self.gl.get_category_rules()
        self.rules_table.setRowCount(len(rules))
        for row, r in enumerate(rules):
            self.rules_table.setItem(row, 0, QTableWidgetItem(str(r.id)))
            self.rules_table.setItem(row, 1, QTableWidgetItem(r.keyword))
            self.rules_table.setItem(row, 2, QTableWidgetItem(r.category))
            self.rules_table.setItem(row, 3, QTableWidgetItem("Yes" if r.case_sensitive else "No"))
            self.rules_table.setItem(row, 4, QTableWidgetItem("Yes" if r.enabled else "No"))

    def on_rule_selected(self):
        """Handle rule selection in the table."""
        selected = self.rules_table.selectedItems()
        if not selected:
            return
        row = selected[0].row()
        id_item = self.rules_table.item(row, 0)
        if not id_item:
            return
        try:
            rule_id = int(id_item.text())
        except ValueError:
            return

        rule = next((r for r in self.gl.category_rules if r.id == rule_id), None)
        if not rule:
            return

        self.rule_edit_id = rule_id
        self.rule_keyword.setText(rule.keyword)

        idx = self.rule_category.findData(rule.category)
        if idx >= 0:
            self.rule_category.setCurrentIndex(idx)

        idx = self.rule_case.findData(rule.case_sensitive)
        if idx >= 0:
            self.rule_case.setCurrentIndex(idx)

        idx = self.rule_enabled.findData(rule.enabled)
        if idx >= 0:
            self.rule_enabled.setCurrentIndex(idx)

    def refresh_blacklist_table(self):
        """Refresh the Blacklisted Patterns table from the ledger."""
        entries = self.gl.get_merchant_blacklist()
        self.blacklist_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self.blacklist_table.setItem(row, 0, QTableWidgetItem(entry["merchant_key"]))
            self.blacklist_table.setItem(row, 1, QTableWidgetItem(entry["category"]))
            self.blacklist_table.setItem(row, 2, QTableWidgetItem(entry["blacklisted_at"] or ""))

    def unblacklist_selected(self):
        """Reverse the selected blacklist entry -- always single-row/granular (point 6)."""
        selected = self.blacklist_table.selectedItems()
        if not selected:
            QMessageBox.information(self, "No selection", "Please select a blacklisted pattern to reverse.")
            return
        row = selected[0].row()
        merchant_key = self.blacklist_table.item(row, 0).text()
        category = self.blacklist_table.item(row, 1).text()

        ok, msg = self.gl.unblacklist_pattern(merchant_key, category)
        if ok:
            self.refresh_blacklist_table()
        else:
            QMessageBox.warning(self, "Un-blacklist failed", msg)

    def add_category_rule(self):
        """Add a new category rule."""
        keyword = self.rule_keyword.text().strip()
        category = self.rule_category.currentData()
        case_sensitive = self.rule_case.currentData()
        enabled = self.rule_enabled.currentData()

        if not keyword or not category:
            QMessageBox.warning(self, "Invalid rule", "Keyword and category are required.")
            return

        ok, msg = self.gl.add_category_rule(keyword, category, case_sensitive, enabled)
        if ok:
            QMessageBox.information(self, "Rule added", msg)
            self.refresh_rules_table()
            self.rule_keyword.clear()
        else:
            QMessageBox.warning(self, "Rule error", msg)

    def update_category_rule(self):
        """Update the selected category rule."""
        if self.rule_edit_id is None:
            QMessageBox.information(self, "No selection", "Please select a rule to update.")
            return

        keyword = self.rule_keyword.text().strip()
        category = self.rule_category.currentData()
        case_sensitive = self.rule_case.currentData()
        enabled = self.rule_enabled.currentData()

        if not keyword or not category:
            QMessageBox.warning(self, "Invalid rule", "Keyword and category are required.")
            return

        ok, msg = self.gl.update_category_rule(self.rule_edit_id, keyword, category, case_sensitive, enabled)
        if ok:
            QMessageBox.information(self, "Rule updated", msg)
            self.refresh_rules_table()
        else:
            QMessageBox.warning(self, "Rule error", msg)

    def delete_category_rule(self):
        """Delete the selected category rule."""
        if self.rule_edit_id is None:
            QMessageBox.information(self, "No selection", "Please select a rule to delete.")
            return

        reply = QMessageBox.question(
            self,
            "Delete rule",
            "Are you sure you want to delete this rule?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        ok, msg = self.gl.delete_category_rule(self.rule_edit_id)
        if ok:
            QMessageBox.information(self, "Rule deleted", msg)
            self.refresh_rules_table()
            self.rule_keyword.clear()
            self.rule_edit_id = None
        else:
            QMessageBox.warning(self, "Rule error", msg)
