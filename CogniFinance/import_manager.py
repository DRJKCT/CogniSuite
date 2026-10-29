# import_manager.py
"""
import_manager.py ver 1.0
For CogniFinance ver 1.9
Encapsulates all import/export logic, background thread management,
progress dialogs, and the Suggested Matches review UI.
"""
import os
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from PyQt6.QtWidgets import (
    QWidget, QFileDialog, QMessageBox, QProgressDialog, QDialog,
    QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QHeaderView,
    QComboBox, QHBoxLayout, QDialogButtonBox, QTreeWidget, QTreeWidgetItem, QPushButton, QTreeWidgetItemIterator
)
from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtGui import QColor

from ledger import GeneralLedger, MERCHANT_MEMORY_PROMOTE_THRESHOLD, MERCHANT_MEMORY_SUGGEST_THRESHOLD
from pdf_import import PDFImportThread, normalize_statement_date
from ofx_import import OFXImportThread, record_merchant_learning_for_last_transaction, lines_with_fx
from merchant_normalization import normalize_merchant

logger = logging.getLogger(__name__)

class SuggestedMatchesDialog(QDialog):
    """Dialog for reviewing fuzzy matches between imported and existing transactions."""
    def __init__(self, matches: List[Dict[str, Any]], ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review Suggested Matches")
        self.resize(1100, 650)
        self.matches = matches
        self.ledger = ledger

        self.is_dark_mode = getattr(parent, 'is_dark_mode', False)

        layout = QVBoxLayout(self)
        info = QLabel(
            "The following imported transactions closely match existing entries.\n"
            "Expand each imported transaction to compare details and choose an action."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # 1. CREATE THE TREE WIDGET (6 Columns)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(["Status", "Date", "Description", "Amount", "Account", "Action"])

        # 2. CONFIGURE THE HEADER
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(0, 120)  # Status (Dot + Text)
        self.tree.setColumnWidth(1, 100)  # Date
        self.tree.setColumnWidth(2, 360)  # Description
        self.tree.setColumnWidth(3, 125)  # Amount
        self.tree.setColumnWidth(4, 160)  # Account
        self.tree.setColumnWidth(5, 70)  # Action (Dropdown)

        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)

        layout.addWidget(self.tree)

        self._populate_tree()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _create_tier_indicator(self, tier: str, tooltip: str) -> QWidget:
        """Restored: Creates the colored dot indicator for match confidence."""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        label = QLabel()
        label.setFixedSize(18, 18)
        label.setToolTip(tooltip)

        if tier == "green":
            label.setStyleSheet("background-color: #10B981; border-radius: 9px;")
        elif tier == "yellow":
            label.setStyleSheet("background-color: #f9a825; border-radius: 9px;")
        else:
            label.setStyleSheet("background-color: #c62828; border-radius: 9px;")

        layout.addWidget(label)
        return container

    def _create_confidence_tag(self, merchant_key: str, merchant_display: str, category: str) -> QWidget:
        """
        Parent-row Account cell, extended to also carry merchant-memory
        confidence -- point 9's default "color-coded cell/tag" view.
        Reuses the dupe-dot's green/yellow palette but as a background
        tint on THIS cell rather than a dot, and at a different screen
        position than the Status column's dot -- so nothing is ambiguous
        about which "confidence" a given colored element refers to.
        Hover reveals the read-only numbers; click opens the full
        interactive view (rating + promote), since rating controls need
        to be reliably clickable in a way a hover tooltip can't support.
        """
        # Match the parent row's existing teal/dark background so the
        # embedded widget doesn't paint over it with its own default
        # palette -- setBackground() on the QTreeWidgetItem itself has no
        # effect on a widget placed via setItemWidget.
        row_bg = "#22262E" if self.is_dark_mode else "#e0f7fa"
        row_fg = "#F3F4F6" if self.is_dark_mode else "#111827"

        container = QWidget()
        container.setStyleSheet(f"background-color: {row_bg};")
        layout = QHBoxLayout(container)
        layout.setContentsMargins(4, 2, 4, 2)

        if self.is_simple_mode:
            pooled = self._pooled_pattern(merchant_display, category)
            pattern = pooled
        else:
            pattern = self.ledger.get_pattern(merchant_key, category) if merchant_key else None

        btn = QPushButton(category)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)

        if pattern is None:
            # Below the occurrence floor, blacklisted, or never seen --
            # nothing to show, plain untinted text per point 4.
            btn.setStyleSheet(
                f"text-align: left; border: none; background: transparent; color: {row_fg};"
            )
            btn.setEnabled(False)
        else:
            if pattern.get("final_confidence") is not None:
                # Pro mode: granular confidence decimal (model artifact).
                if pattern["final_confidence"] >= MERCHANT_MEMORY_PROMOTE_THRESHOLD:
                    tint = "rgba(16, 185, 129, 60)"
                elif pattern["final_confidence"] >= MERCHANT_MEMORY_SUGGEST_THRESHOLD:
                    tint = "rgba(249, 168, 37, 60)"
                else:
                    tint = "transparent"
                tooltip_text = (
                    f"{pattern['occurrences']} of {pattern['count_total']} times for this merchant "
                    f"({pattern.get('consistency_ratio', 0) * 100:.0f}% consistent)\n"
                    f"Confidence: {pattern['final_confidence'] * 100:.0f}%\n"
                    f"Click to rate or promote to a rule."
                )
            else:
                # Simple mode: counts the user is entitled to see,
                # never just a colour. A colour alone would look
                # identical on a 2-of-2 and a 20-of-20 pattern.
                tint = "rgba(16, 185, 129, 60)" if pattern.get("rating") == "green" else (
                    "rgba(249, 168, 37, 60)" if pattern.get("rating") == "yellow" else "transparent"
                )
                tooltip_text = (
                    f"Categorized as {category} {pattern['occurrences']} of "
                    f"{pattern['count_total']} times.\n"
                    f"Click to review."
                )

            btn.setStyleSheet(
                f"text-align: left; border: none; border-radius: 4px; "
                f"background-color: {tint}; padding: 2px 6px; color: {row_fg};"
            )
            btn.setToolTip(tooltip_text)
            btn.clicked.connect(lambda checked=False, mk=merchant_key, md=merchant_display, cat=category: self._expand_pattern(mk, md, cat))

        layout.addWidget(btn)
        return container

    def _pooled_patterns(self):
        if not hasattr(self, '_pooled_cache') or self._pooled_cache is None:
            self._pooled_cache = {}
            for row in self.ledger.get_merchant_patterns(mode="simple"):
                self._pooled_cache[(row["merchant_display"], row["category"])] = row
        return self._pooled_cache

    def _pooled_pattern(self, merchant_display: str, category: str):
        return self._pooled_patterns().get((merchant_display, category))

    def _expand_pattern(self, merchant_key: str, merchant_display: str, category: str):
        if self.is_simple_mode:
            self._expand_pattern_simple(merchant_key, merchant_display, category)
        else:
            self._expand_pattern_pro(merchant_key, category)

    def _expand_pattern_pro(self, merchant_key: str, category: str):
        """
        Click-to-expand view -- point 9 chose click over hover here
        specifically because rating controls need to be stable/clickable.
        Shows the actual numbers plus Green/Yellow/Red and, once
        confidence clears the higher bar, Promote to Rule.
        """
        pattern = self.ledger.get_pattern(merchant_key, category)
        if pattern is None:
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Merchant Pattern")
        layout = QVBoxLayout(dlg)

        info = QLabel(
            f"<b>{merchant_key}</b> &rarr; {category}<br><br>"
            f"Seen {pattern['occurrences']} of {pattern['count_total']} times for this merchant "
            f"({pattern['consistency_ratio'] * 100:.0f}% consistent)<br>"
            f"Current rating: {pattern['rating'].capitalize()}<br>"
            f"Confidence: {pattern['final_confidence'] * 100:.0f}%"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        layout.addWidget(info)

        btn_row = QHBoxLayout()
        btn_green = QPushButton("Green (vouch)")
        btn_yellow = QPushButton("Yellow (neutral)")
        btn_red = QPushButton("Red (wrong, blacklist)")
        btn_row.addWidget(btn_green)
        btn_row.addWidget(btn_yellow)
        btn_row.addWidget(btn_red)
        layout.addLayout(btn_row)

        btn_promote = QPushButton("Promote to Rule…")
        btn_promote.setEnabled(pattern["can_promote"])
        if not pattern["can_promote"]:
            btn_promote.setToolTip(f"Needs {MERCHANT_MEMORY_PROMOTE_THRESHOLD * 100:.0f}% confidence to promote.")
        layout.addWidget(btn_promote)

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(dlg.reject)
        layout.addWidget(close_box)

        def do_rate(rating: str):
            ok, msg = self.ledger.rate_pattern(merchant_key, category, rating, mode="pro")
            if ok:
                dlg.accept()
                self._populate_tree()  # refresh tags so the new confidence shows immediately
            else:
                QMessageBox.warning(self, "Rating failed", msg)

        def do_reject():
            ok, msg = self.ledger.reject_pattern(merchant_key, category, mode="pro")
            if ok:
                dlg.accept()
                self._populate_tree()
            else:
                QMessageBox.warning(self, "Blacklist failed", msg)

        def do_promote():
            defaults = self.ledger.build_promotion_rule_defaults(merchant_key, category, mode="pro")
            if not defaults:
                QMessageBox.warning(self, "Promote failed", "Pattern not found.")
                return
            ok, msg = self.ledger.add_category_rule(
                defaults["keyword"], defaults["category"],
                defaults["case_sensitive"], defaults["enabled"],
            )
            if not ok:
                QMessageBox.warning(self, "Promote failed", msg)
                return
            new_rule = self.ledger.category_rules[-1]  # add_category_rule just appended it
            self.ledger.promote_pattern(merchant_key, category, new_rule.id, mode="pro")
            dlg.accept()
            QMessageBox.information(self, "Promoted", f"Created a Category Rule for '{defaults['keyword']}'.")

        btn_green.clicked.connect(lambda: do_rate("green"))
        btn_yellow.clicked.connect(lambda: do_rate("yellow"))
        btn_red.clicked.connect(do_reject)
        btn_promote.clicked.connect(do_promote)

        dlg.exec()

    def _expand_pattern_simple(self, merchant_key: str, merchant_display: str, category: str):
        pooled = self._pooled_pattern(merchant_display, category)
        if pooled is None:
            return
        scope = pooled.get("pooled_count", 1)
        mode = simple_mode.ledger_mode(True)

        dlg = QDialog(self)
        dlg.setWindowTitle("Merchant Pattern")
        layout = QVBoxLayout(dlg)

        info = QLabel(
            f"<b>{merchant_display}</b> &rarr; {category}<br><br>"
            f"Categorized as {category} {pooled['occurrences']} of "
            f"{pooled['count_total']} times."
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        layout.addWidget(info)

        # Disclosure: the user must know the scope before they click.
        if scope > 1:
            scope_label = QLabel(
                f"Your answer applies to all {scope} locations of {merchant_display}."
            )
        else:
            scope_label = QLabel("Your answer applies to this one location.")
        scope_label.setWordWrap(True)
        scope_label.setStyleSheet("color: #6B7280; font-size: 11px;")
        layout.addWidget(scope_label)

        btn_row = QHBoxLayout()
        btn_green = QPushButton("Looks right")
        btn_green.setToolTip(f"Remember {merchant_display} as {category}.")
        btn_red = QPushButton("Not right")
        btn_red.setToolTip(f"Stop suggesting {merchant_display} as {category}.")
        btn_row.addWidget(btn_green)
        btn_row.addWidget(btn_red)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Advanced controls (Pro verbs) behind a reveal.
        advanced = QWidget()
        advanced_layout = QVBoxLayout(advanced)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        adv_label = QLabel(
            "Advanced: set a neutral rating, create a category rule, "
            "or rate a single location separately."
        )
        adv_label.setWordWrap(True)
        adv_label.setStyleSheet("color: #6B7280; font-size: 11px;")
        advanced_layout.addWidget(adv_label)

        adv_btn_row = QHBoxLayout()
        btn_yellow = QPushButton("Yellow (neutral)")
        btn_promote = QPushButton("Promote to Rule…")
        btn_promote.setEnabled(pooled.get("can_promote", False))
        if not pooled.get("can_promote", False):
            btn_promote.setToolTip(
                f"Needs {MERCHANT_MEMORY_PROMOTE_THRESHOLD * 100:.0f}% confidence to promote."
            )
        adv_btn_row.addWidget(btn_yellow)
        adv_btn_row.addWidget(btn_promote)
        adv_btn_row.addStretch()
        advanced_layout.addLayout(adv_btn_row)

        # Un-pool: the individual granular locations behind the pool.
        if scope > 1:
            advanced_layout.addWidget(QLabel("Locations behind this pattern:"))
            locations = QTreeWidget()
            locations.setColumnCount(3)
            locations.setHeaderLabels(["Location", "Seen", "Rate this one"])
            locations.setRootIsDecorated(False)
            locations.setHeaderHidden(True)
            keys = sorted(pooled.get("merchant_keys", []))
            for key in keys:
                child = QTreeWidgetItem(locations)
                child.setText(0, key)
                detail = self.ledger.get_pattern(key, category)
                child.setText(
                    1,
                    f"{detail['occurrences']} of {detail['count_total']}"
                    if detail else "—",
                )
                btn_one = QPushButton("Rate this one…")
                btn_one.clicked.connect(
                    lambda _checked=False, k=key: self._expand_pattern_pro(k, category)
                )
                locations.setItemWidget(child, 2, btn_one)
            advanced_layout.addWidget(locations)

        advanced.setVisible(False)
        layout.addWidget(advanced)

        btn_advanced = QPushButton("Show advanced")
        btn_advanced.setFlat(True)
        btn_advanced.setStyleSheet("text-align: left; color: #10B981; padding: 2px;")
        layout.addWidget(btn_advanced)

        def toggle_advanced():
            showing = not advanced.isVisible()
            advanced.setVisible(showing)
            btn_advanced.setText("Hide advanced" if showing else "Show advanced")

        btn_advanced.clicked.connect(toggle_advanced)

        close_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_box.rejected.connect(dlg.reject)
        layout.addWidget(close_box)

        def do_rate(rating: str):
            ok, msg = self.ledger.rate_pattern(merchant_key, category, rating, mode=mode)
            if ok:
                dlg.accept()
                self._pooled_cache = None
                self._populate_tree()
                # After-the-fact disclosure: confirm the scope.
                QMessageBox.information(
                    self, "Remembered",
                    f"{merchant_display} is now remembered as {category}"
                    + (f" across {scope} locations." if scope > 1 else ".")
                    + "\n\nTo change a single location, open this pattern "
                      "and choose 'Show advanced'.",
                )
            else:
                QMessageBox.warning(self, "Rating failed", msg)

        def do_reject():
            ok, msg = self.ledger.reject_pattern(merchant_key, category, mode=mode)
            if ok:
                dlg.accept()
                self._pooled_cache = None
                self._populate_tree()
                QMessageBox.information(
                    self, "Removed",
                    f"{merchant_display} will no longer be suggested as "
                    f"{category}" + (f" ({scope} locations)." if scope > 1 else ".")
                    + "\n\nThis can be undone in Category Rules -> "
                      "Blacklisted Patterns, one location at a time.",
                )
            else:
                QMessageBox.warning(self, "Blacklist failed", msg)

        def do_promote():
            defaults = self.ledger.build_promotion_rule_defaults(merchant_key, category, mode=mode)
            if not defaults:
                QMessageBox.warning(self, "Promote failed", "Pattern not found.")
                return
            ok, msg = self.ledger.add_category_rule(
                defaults["keyword"], defaults["category"],
                defaults["case_sensitive"], defaults["enabled"],
            )
            if not ok:
                QMessageBox.warning(self, "Promote failed", msg)
                return
            new_rule = self.ledger.category_rules[-1]
            self.ledger.promote_pattern(merchant_key, category, new_rule.id, mode=mode)
            dlg.accept()
            self._pooled_cache = None
            self._populate_tree()
            QMessageBox.information(
                self, "Promoted",
                f"Created a Category Rule for '{defaults['keyword']}'. It "
                f"applies to every transaction from {merchant_display}.",
            )

        btn_green.clicked.connect(lambda: do_rate("green"))
        btn_red.clicked.connect(do_reject)
        btn_yellow.clicked.connect(lambda: do_rate("yellow"))
        btn_promote.clicked.connect(do_promote)

        dlg.exec()

    def _populate_tree(self):
        self.tree.clear()
        from collections import defaultdict
        from ui_helpers import get_transaction_display_amount

        # Group matches by the imported transaction's unique properties
        grouped = defaultdict(list)
        for m in self.matches:
            p_tx = m["parsed_tx"]
            key = (p_tx.get("date"), p_tx.get("description"), str(p_tx.get("amount", 0)))
            grouped[key].append(m)

        for key, match_list in grouped.items():
            p_tx = match_list[0]["parsed_tx"]

            # --- PARENT ROW: The Imported Transaction ---
            parent_item = QTreeWidgetItem(self.tree)
            parent_item.setText(0, "▼ IMPORTED")
            parent_item.setText(1, p_tx.get("date", ""))
            parent_item.setText(2, p_tx.get("description", ""))

            # Safely get amount
            amount = p_tx.get("amount", 0)
            if (amount == 0 or amount is None) and "lines" in p_tx:
                try:
                    amount = sum(float(line[1]) for line in p_tx["lines"] if len(line) >= 3)
                except Exception:
                    amount = 0

            parent_item.setText(3, f"${abs(amount):.2f}")

            category = p_tx.get("contra_account", "Unknown")
            merchant_key, merchant_display = normalize_merchant(p_tx.get("description", ""))
            self.tree.setItemWidget(parent_item, 4, self._create_confidence_tag(merchant_key, merchant_display, category))

            # Style parent to stand out (Teal background)
            font = parent_item.font(0)
            font.setBold(True)
            if self.is_dark_mode:
                bg_color = QColor("#22262E")  # Dark mode Surface 2
                fg_color = QColor("#F3F4F6")  # Dark mode off-white text
            else:
                bg_color = QColor("#e0f7fa")  # Light mode teal
                fg_color = QColor("#111827")  # Light mode dark gray text

            for col in range(6):
                parent_item.setFont(col, font)
                parent_item.setBackground(col, bg_color)
                parent_item.setForeground(col, fg_color) # Ensures text is always readable


            parent_item.setExpanded(True)

            # --- CHILD ROWS: Potential Duplicates ---
            for m in match_list:
                existing_tx = next((t for t in self.ledger.transactions if t.id == m["pending_id"]), None)
                if not existing_tx:
                    continue

                child_item = QTreeWidgetItem(parent_item)

                tier = m.get("tier", "yellow")
                date_diff = m.get("date_diff_days", 0)
                merchant_sim = m.get("merchant_similarity", 0.0)
                merchant_pct = f"{merchant_sim * 100:.0f}%"

                # 1. Col 0: Status (Restored Colored Dot)
                if tier == "green":
                    tooltip = f"GREEN MATCH\nExact amount, Date diff: {date_diff}d, Merchant: {merchant_pct}"
                else:
                    reasons = []
                    if date_diff > 1: reasons.append(f"Date is {date_diff} days apart")
                    if merchant_sim < 0.80: reasons.append(f"Merchant name similarity is only {merchant_pct}")
                    reason_text = ", ".join(reasons) if reasons else "Borderline match"
                    tooltip = f"YELLOW MATCH\nExact amount, but: {reason_text}"

                dot_widget = self._create_tier_indicator(tier, tooltip)
                self.tree.setItemWidget(child_item, 0, dot_widget)

                # 2. Cols 1-4: Data
                child_item.setText(1, existing_tx.date)
                child_item.setText(2, existing_tx.description)

                tx_amount = get_transaction_display_amount(self.ledger, existing_tx)
                child_item.setText(3, f"${abs(float(tx_amount)):.2f}")

                acc_name = existing_tx.lines[0].account_name if existing_tx.lines else "Unknown"
                child_item.setText(4, acc_name)

                # 3. Col 5: Action (Restored Dropdown)
                combo = QComboBox()
                combo.addItems(["Confirm (Merge)", "Reject (Import as New)"])
                # Default selection based on confidence
                combo.setCurrentIndex(0 if tier == "green" else 1)

                self.tree.setItemWidget(child_item, 5, combo)

                # Store reference to the match data on the item for retrieval later
                child_item.setData(0, Qt.ItemDataRole.UserRole, m)

    def get_confirmations(self):
        """Returns list of match dicts where user selected 'Confirm (Merge)'."""
        confirmations = []
        # Iterate all items in the tree
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            item = iterator.value()
            # Check if it's a child item (has a parent) and has our data
            if item.parent() is not None and item.data(0, Qt.ItemDataRole.UserRole):
                combo = self.tree.itemWidget(item, 5)
                if combo and combo.currentText() == "Confirm (Merge)":
                    confirmations.append(item.data(0, Qt.ItemDataRole.UserRole))
            iterator += 1
        return confirmations

    def get_rejections(self):
        """Returns list of parsed_tx dicts where user selected 'Reject (Import as New)'."""
        rejections = []
        iterator = QTreeWidgetItemIterator(self.tree)
        while iterator.value():
            item = iterator.value()
            if item.parent() is not None and item.data(0, Qt.ItemDataRole.UserRole):
                combo = self.tree.itemWidget(item, 5)
                if combo and combo.currentText() == "Reject (Import as New)":
                    match_data = item.data(0, Qt.ItemDataRole.UserRole)
                    rejections.append(match_data["parsed_tx"])
            iterator += 1
        return rejections


class ImportManager(QObject):
    """Manages all import/export operations, background threads, and progress dialogs."""
    data_imported = pyqtSignal()

    def __init__(self, ledger: GeneralLedger, parent: QWidget):
        super().__init__()
        self.gl = ledger
        self.parent = parent
        self.progress_dialog = None
        self.import_thread = None

    def start_pdf_import(self, bank: Optional[str], statement_type: str):
        files, _ = QFileDialog.getOpenFileNames(self.parent, "Import Bank Statement PDF", "", "PDF Files (*.pdf);;All Files (*)")
        if not files: return

        for name, t in [("Credit Card", "Liability"), ("Uncategorized Expense", "Expense"), ("checking Account", "Asset")]:
            if name not in self.gl.accounts:
                self.gl.create_account(name, t)

        self.progress_dialog = QProgressDialog("Importing PDF Statements...", "Cancel", 0, 100, self.parent)
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setAutoClose(True)
        self.progress_dialog.setMinimumDuration(500)
        self.progress_dialog.show()

        self.import_thread = PDFImportThread(files=files, ledger=self.gl, bank=bank, statement_type=statement_type)
        self.import_thread.progress_signal.connect(self._update_progress)
        self.import_thread.finished_signal.connect(self._on_pdf_finished)
        self.import_thread.error_signal.connect(self._on_import_error)
        self.import_thread.finished.connect(self.import_thread.deleteLater)

        if hasattr(self.parent, 'btn_import_pdf'):
            self.parent.btn_import_pdf.setEnabled(False)
        self.import_thread.start()

    def start_ofx_import(self):
        files, _ = QFileDialog.getOpenFileNames(self.parent, "Import OFX/QFX Bank Feed", "", "Bank Feeds (*.ofx *.qfx);;All Files (*)")
        if not files: return

        self.progress_dialog = QProgressDialog("Importing OFX/QFX...", "Cancel", 0, 100, self.parent)
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setAutoClose(True)
        self.progress_dialog.setMinimumDuration(500)
        self.progress_dialog.show()

        self.import_thread = OFXImportThread(files, self.gl)
        self.import_thread.progress_signal.connect(self._update_progress)
        self.import_thread.finished_signal.connect(self._on_ofx_finished)
        self.import_thread.error_signal.connect(self._on_import_error)
        self.import_thread.finished.connect(self.import_thread.deleteLater)
        self.import_thread.start()

    def export_ledger_csv(self):
        path, _ = QFileDialog.getSaveFileName(self.parent, "Export Ledger CSV", "", "CSV Files (*.csv);;All Files (*)")
        if not path: return
        try:
            self.gl.export_journal_csv(path)
            QMessageBox.information(self.parent, "Export complete", f"Ledger exported to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self.parent, "Export failed", str(e))

    def import_ledger_csv(self):
        files, _ = QFileDialog.getOpenFileNames(self.parent, "Import Ledger CSV", "", "CSV Files (*.csv);;All Files (*)")
        if not files: return
        total = 0
        for path in files:
            try:
                n = self.gl.import_ledger_csv(path)
                total += n
            except Exception as e:
                QMessageBox.warning(self.parent, "Import error", f"Failed to import {os.path.basename(path)}:\n{e}")
        QMessageBox.information(self.parent, "Import complete", f"Imported {total} transactions from CSV.")
        self.data_imported.emit()

    def _update_progress(self, percent: int, message: str):
        dialog = self.progress_dialog
        if dialog is not None:
            try:
                dialog.setValue(percent)
                dialog.setLabelText(message)
            except (RuntimeError, AttributeError):
                # RuntimeError catches "wrapped C++ object is deleted"
                # AttributeError catches if it somehow becomes None
                pass

    def _on_import_error(self, error_message: str):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        if hasattr(self.parent, 'btn_import_pdf'):
            self.parent.btn_import_pdf.setEnabled(True)
        QMessageBox.critical(self.parent, "Import Error", error_message)

    def _on_pdf_finished(self, imported_count: int, review: dict):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        if hasattr(self.parent, 'btn_import_pdf'):
            self.parent.btn_import_pdf.setEnabled(True)

        skipped = review.get("skipped_lines", [])
        mismatches = review.get("balance_mismatches", [])
        unverified = review.get("unverified_count", 0)
        suggested_matches = review.get("suggested_matches", [])

        if suggested_matches:
            dialog = SuggestedMatchesDialog(suggested_matches, self.gl, self.parent)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._process_confirmed_matches(dialog.get_confirmations())
                rejected_count = self._process_rejected_matches(dialog.get_rejections(), source="pdf")
                imported_count += rejected_count

        summary = f"Imported {imported_count} transactions successfully."
        needs_review = bool(skipped or mismatches or unverified)

        if needs_review:
            parts = []
            if skipped: parts.append(f"{len(skipped)} line(s) could not be parsed as transactions")
            if mismatches: parts.append(f"{len(mismatches)} transaction(s) failed a balance sanity check")
            if unverified: parts.append(f"{unverified} transaction(s) could not be balance-checked")
            summary += "\n\nHowever, " + "; ".join(parts) + ". Please review before trusting this import."

            box = QMessageBox(self.parent)
            box.setIcon(QMessageBox.Icon.Warning if (skipped or mismatches) else QMessageBox.Icon.Information)
            box.setWindowTitle("Import Complete - Review Needed")
            box.setText(summary)

            detail_lines = []
            if skipped:
                detail_lines.append("=== Lines that could not be parsed ===")
                for item in skipped[:50]: detail_lines.append(f"[{item['file']}] {item['reason']}\n    {item['line']}")
                if len(skipped) > 50: detail_lines.append(f"... and {len(skipped) - 50} more")
                detail_lines.append("")
            if mismatches:
                detail_lines.append("=== Transactions that failed the balance check ===")
                for item in mismatches[:50]: detail_lines.append(f"[{item['file']}] {item['date']} - {item['description']}: {item['detail']}")
                if len(mismatches) > 50: detail_lines.append(f"... and {len(mismatches) - 50} more")
            if detail_lines: box.setDetailedText("\n".join(detail_lines))

            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            box.exec()
        else:
            QMessageBox.information(self.parent, "Success", summary)

        self.data_imported.emit()

    def _on_ofx_finished(self, imported_count: int, review: dict):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None

        suggested_matches = review.get("suggested_matches", [])
        total_skipped = review.get("skipped_count", 0)
        new_accounts = review.get("new_accounts", [])

        if new_accounts:
            names = ", ".join([f"{n} ({t})" for n, t in new_accounts])
            logger.info(f"OFX Import: Auto-created accounts: {names}")

        if suggested_matches:
            dialog = SuggestedMatchesDialog(suggested_matches, self.gl, self.parent)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self._process_confirmed_matches(dialog.get_confirmations())
                rejected_count = self._process_rejected_matches(dialog.get_rejections(), source="ofx")
                imported_count += rejected_count

        summary_msg = f"Successfully imported {imported_count} transaction(s).\n"
        if total_skipped > 0: summary_msg += f"Skipped/Rejected by Ledger: {total_skipped}.\n"
        summary_msg += "\nNote: Transactions are currently categorized as 'Uncategorized'. Please review them in the Transactions tab or set up Category Rules."
        QMessageBox.information(self.parent, "Import Complete", summary_msg)

        self.data_imported.emit()

    def _process_confirmed_matches(self, confirmations: List[Dict[str, Any]]):
        for conf in confirmations:
            pending_id = conf["pending_id"]
            p_tx = conf["parsed_tx"]
            match_type = conf.get("match_type", "mobile_to_bank")
            bank_date = p_tx.get("date")
            if "statement_balance" in p_tx:
                bank_date = normalize_statement_date(bank_date) or datetime.now().strftime("%Y-%m-%d")
            bank_desc = p_tx.get("description", "")
            tx = next((t for t in self.gl.transactions if t.id == pending_id), None)
            if tx:
                old_imp_id = tx.imported_id
                # l.fx keeps a foreign-currency line's native amount and rate through the edit.
                existing_lines = [(l.id, l.account_name, l.dr, l.cr, l.fx) for l in tx.lines]
                ok, msg = self.gl.edit_transaction(pending_id, bank_date, bank_desc, existing_lines)
                if ok:
                    new_tx = next((t for t in self.gl.transactions if t.imported_id == old_imp_id), None)
                    if new_tx:
                        self.gl.mark_reconciled(new_tx.id, True)
                    if match_type == "bank_to_bank":
                        duplicate_imp_id = p_tx.get("imported_id")
                        if duplicate_imp_id:
                            duplicate_tx = next((t for t in self.gl.transactions if t.imported_id == duplicate_imp_id), None)
                            if duplicate_tx:
                                self.gl.void_transaction(duplicate_tx.id)
                else:
                    logger.warning(f"Failed to merge match {pending_id}: {msg}")

    def _process_rejected_matches(self, rejected_txs: List[Dict[str, Any]], source: str):
        imported = 0
        for tx in rejected_txs:
            date_str = tx.get("date")
            if source == "pdf":
                date_str = normalize_statement_date(date_str) or datetime.now().strftime("%Y-%m-%d")
            desc = tx.get("description", "")
            lines = lines_with_fx(tx)
            imp_id = tx.get("imported_id")
            if not imp_id and source == "pdf":
                amount = tx.get("amount", 0)
                imp_id = f"rejected::{date_str}::{desc[:40]}::{amount}"
            ok, msg = self.gl.record_transaction(date_str, desc, lines, imported_id=imp_id, persist=True)
            if ok:
                imported += 1
                record_merchant_learning_for_last_transaction(
                    self.gl, desc,
                    merchant_key=tx.get("merchant_key"),
                    merchant_display=tx.get("merchant_display"),
                    category_suggested=tx.get("category_suggested"),
                )
                if source == "pdf" and tx.get("statement_balance") is not None:
                    self.gl.record_statement_balance(tx["contra_account"], date_str, tx["statement_balance"], source="rejected_match")
        return imported
