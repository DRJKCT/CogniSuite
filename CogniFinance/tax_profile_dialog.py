# tax_profile_dialog.py
"""
Dialog for editing a tax profile section: the mapping from the user's
chart-of-accounts entries to the tax buckets declared by the currently
loaded template set.

Uses Ctrl/Shift multi-select rather than checkboxes -- selection is
transient ("these are the rows I'm acting on"), not persistent state.
The category_map dict holds the actual persistent state.
"""
from typing import Dict, List, Any

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QComboBox, QDialogButtonBox,
    QMessageBox, QGroupBox, QListWidget, QCheckBox
)
from PyQt6.QtCore import Qt, QLocale
from PyQt6.QtGui import QFont

from ledger import GeneralLedger
from tax_profile import TaxProfileManager
from tax_parser import ResolvedTemplateSet, display_label, pretty_name

# ---------------------------------------------------------------------------
# category_map entries are either an account name (adds to the bucket) or
# {"account": name, "sign": -1} (subtracts: expenses, realized losses). These
# helpers keep that shape in one place; the dialog below only calls them.
# ---------------------------------------------------------------------------
def entry_account(entry) -> str:
    return entry["account"] if isinstance(entry, dict) else entry


def entry_sign(entry) -> int:
    return entry.get("sign", 1) if isinstance(entry, dict) else 1


def add_accounts_to_map(category_map: Dict[str, list], bucket: str, accounts, subtract: bool) -> int:
    """Append accounts not already in the bucket; returns how many were added."""
    present = {entry_account(e) for e in category_map.get(bucket, [])}
    added = 0
    for acct in accounts:
        if acct in present:
            continue
        category_map.setdefault(bucket, []).append({"account": acct, "sign": -1} if subtract else acct)
        present.add(acct)
        added += 1
    return added


def remove_account_from_map(category_map: Dict[str, list], bucket: str, acct: str) -> bool:
    entries = category_map.get(bucket, [])
    for entry in entries:
        if entry_account(entry) == acct:
            entries.remove(entry)
            return True
    return False


def toggle_sign_in_map(category_map: Dict[str, list], bucket: str, acct: str) -> bool:
    entries = category_map.get(bucket, [])
    for i, entry in enumerate(entries):
        if entry_account(entry) == acct:
            entries[i] = acct if entry_sign(entry) == -1 else {"account": acct, "sign": -1}
            return True
    return False

class ProfileEditDialog(QDialog):
    def __init__(
        self,
        ledger: GeneralLedger,
        template_set: ResolvedTemplateSet,
        section_name: str = "Default",
        parent=None,
    ):
        super().__init__(parent)
        self._lang = QLocale().name()
        self.setWindowTitle(self.tr("Edit Tax Profile Section"))
        self.resize(700, 600)

        self.gl = ledger
        self.ts = template_set
        self.section_name = section_name
        self.profile_manager = TaxProfileManager(ledger)

        existing = self.profile_manager.load_section(section_name) or {}
        self.category_map: Dict[str, List[str]] = existing.get("category_map", {})
        self.user_inputs: Dict[str, Any] = existing.get("user_inputs", {})
        self.skipped_inputs: List[str] = existing.get("skipped_inputs", [])

        self._init_ui()
        self._populate()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        info = QLabel(
            self.tr(
                "Map your accounts to the tax buckets declared by the currently "
                "loaded template set. Section: <b>{section}</b>"
            ).format(section=self.section_name)
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        bucket_box = QGroupBox(self.tr("Bucket Assignments"))
        bucket_layout = QVBoxLayout(bucket_box)

        bucket_hint = QLabel(
            self.tr(
                "Select account rows (Ctrl or Shift for multiple) to remove them or "
                "switch them between adding and subtracting. Accounts add to the bucket "
                "unless marked '\u2212' (e.g. expenses and realized losses, which subtract)."
            )
        )
        bucket_hint.setWordWrap(True)
        bucket_hint.setStyleSheet("color: #6B7280; font-size: 11px;")
        bucket_layout.addWidget(bucket_hint)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([self.tr("Bucket / Account"), ""])
        self.tree.setColumnCount(2)
        self.tree.header().setSectionResizeMode(0, self.tree.header().ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, self.tree.header().ResizeMode.ResizeToContents)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.tree.setSelectionBehavior(QTreeWidget.SelectionBehavior.SelectRows)
        bucket_layout.addWidget(self.tree)

        bucket_actions = QHBoxLayout()
        add_account_btn = QPushButton(self.tr("Add Account to Selected Bucket"))
        add_account_btn.clicked.connect(self._add_account_to_selected_bucket)
        remove_account_btn = QPushButton(self.tr("Remove Selected Accounts"))
        remove_account_btn.clicked.connect(self._remove_selected_accounts)
        bucket_actions.addWidget(add_account_btn)
        bucket_actions.addWidget(remove_account_btn)
        toggle_btn = QPushButton(self.tr("Toggle Add / Subtract"))
        toggle_btn.clicked.connect(self._toggle_selected_signs)
        bucket_actions.addWidget(toggle_btn)
        bucket_actions.addStretch()
        bucket_layout.addLayout(bucket_actions)

        layout.addWidget(bucket_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save_and_close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _bucket_label(self, bucket_name: str) -> str:
        return display_label(self.ts.all_buckets.get(bucket_name), pretty_name(bucket_name), self._lang)

    def _populate(self):
        self.tree.clear()
        bold_font = QFont()
        bold_font.setBold(True)

        for bucket_name in sorted(self.ts.all_buckets.keys()):
            bucket_item = QTreeWidgetItem(self.tree)
            bucket_item.setText(0, self._bucket_label(bucket_name))
            bucket_item.setFont(0, bold_font)
            bucket_item.setData(0, Qt.ItemDataRole.UserRole, ("bucket", bucket_name))

            for entry in self.category_map.get(bucket_name, []):
                child = QTreeWidgetItem(bucket_item)
                child.setText(0, ("\u2212 " + acct) if entry_sign(entry) == -1 else acct)
                child.setData(0, Qt.ItemDataRole.UserRole, ("account", bucket_name, acct))

            bucket_item.setExpanded(True)

    def _add_account_to_selected_bucket(self):
        selected = self.tree.selectedItems()
        if not selected:
            QMessageBox.information(self, self.tr("No bucket selected"), self.tr("Select a bucket row first."))
            return

        item = selected[0]
        data = item.data(0, Qt.ItemDataRole.UserRole)
        # If an account row is selected, walk up to its parent bucket.
        if data and data[0] == "account":
            item = item.parent()
            data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data or data[0] != "bucket":
            QMessageBox.information(self, self.tr("Select a bucket"), self.tr("Pick the bucket row first."))
            return

        bucket_name = data[1]
        currently_mapped = {entry_account(e) for e in self.category_map.get(bucket_name, [])}
        available = sorted(a for a in self.gl.accounts.keys() if a not in currently_mapped)

        if not available:
            QMessageBox.information(
                self, self.tr("No accounts available"),
                self.tr("All accounts are already mapped to '{bucket}'.").format(
                    bucket=self._bucket_label(bucket_name)
                )
            )
            return

        picker = QDialog(self)
        picker.setWindowTitle(self.tr("Add accounts to '{bucket}'").format(bucket=self._bucket_label(bucket_name)))
        picker.resize(420, 480)
        picker_layout = QVBoxLayout(picker)
        picker_layout.addWidget(QLabel(self.tr("Select one or more accounts (Ctrl or Shift):")))

        account_list = QListWidget()
        account_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        for acct in available:
            account_list.addItem(acct)
        picker_layout.addWidget(account_list)

        subtract_box = QCheckBox(self.tr("Subtract from this bucket (expenses, realized losses)"))
        picker_layout.addWidget(subtract_box)

        def _suggest_subtract():
            # Expense accounts almost always subtract from an income bucket.
            names = [i.text() for i in account_list.selectedItems()]
            subtract_box.setChecked(bool(names) and all(
                getattr(self.gl.accounts.get(n), "account_type", None) == "Expense" for n in names
            ))
        account_list.itemSelectionChanged.connect(_suggest_subtract)

        picker_buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        picker_buttons.accepted.connect(picker.accept)
        picker_buttons.rejected.connect(picker.reject)
        picker_layout.addWidget(picker_buttons)

        if picker.exec() != QDialog.DialogCode.Accepted:
            return

        chosen = [i.text() for i in account_list.selectedItems()]
        if chosen and add_accounts_to_map(self.category_map, bucket_name, chosen, subtract_box.isChecked()):
            self._populate()

    def _remove_selected_accounts(self):
        removed = False
        for item in self.tree.selectedItems():
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if not data or data[0] != "account":
                continue
            _, bucket_name, acct = data
            if remove_account_from_map(self.category_map, bucket_name, acct):
                removed = True
        if removed:
            self._populate()

    def _toggle_selected_signs(self):
        changed = False
        for item in self.tree.selectedItems():
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data and data[0] == "account":
                _, bucket_name, acct = data
                changed = toggle_sign_in_map(self.category_map, bucket_name, acct) or changed
        if changed:
            self._populate()

    def _save_and_close(self):
        cleaned = {k: v for k, v in self.category_map.items() if v}
        ok = self.profile_manager.save_section(
            self.section_name,
            category_map=cleaned,
            user_inputs=self.user_inputs,
            skipped_inputs=self.skipped_inputs,
        )
        if not ok:
            QMessageBox.warning(self, self.tr("Save Failed"),
                                self.tr("Could not write the profile section to the database."))
            return
        self.accept()
