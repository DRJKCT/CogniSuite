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
    QComboBox, QHBoxLayout, QDialogButtonBox
)
from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtGui import QColor

from ledger import GeneralLedger
from pdf_import import PDFImportThread, normalize_statement_date
from ofx_import import OFXImportThread

logger = logging.getLogger(__name__)

class SuggestedMatchesDialog(QDialog):
    """Dialog for reviewing fuzzy matches between imported and existing transactions."""
    def __init__(self, matches: List[Dict[str, Any]], ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review Suggested Mobile Matches")
        self.resize(950, 550)
        self.matches = matches
        self.ledger = ledger
        self.user_actions = {}

        layout = QVBoxLayout(self)
        info = QLabel(
            "The following imported transactions closely match existing unreconciled entries.\n"
            "<b>Confirm</b>: Consolidate into a single transaction and mark as reconciled.\n"
            "<b>Reject</b>: Ignore the match and import the new line as a separate transaction."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["New Date", "New Description", "Existing Description", "Status", "Date Diff", "Merchant Match", "Action"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        for i, m in enumerate(matches):
            self.table.insertRow(i)
            p_tx = m["parsed_tx"]
            self.table.setItem(i, 0, QTableWidgetItem(p_tx.get("date", "")))
            self.table.setItem(i, 1, QTableWidgetItem(p_tx.get("description", "")))

            existing_tx = next((t for t in self.ledger.transactions if t.id == m["pending_id"]), None)
            existing_desc = existing_tx.description if existing_tx else "Unknown"
            self.table.setItem(i, 2, QTableWidgetItem(existing_desc))

            date_diff = m.get("date_diff_days", 0)
            merchant_sim = m.get("merchant_similarity", 0.0)
            merchant_pct = f"{merchant_sim * 100:.0f}%"

            if m["tier"] == "green":
                tooltip = f"GREEN MATCH\nExact amount, Date diff: {date_diff}d, Merchant: {merchant_pct}"
            else:
                reasons = []
                if date_diff > 1: reasons.append(f"Date is {date_diff} days apart")
                if merchant_sim < 0.80: reasons.append(f"Merchant name similarity is only {merchant_pct}")
                reason_text = ", ".join(reasons) if reasons else "Borderline match"
                tooltip = f"YELLOW MATCH\nExact amount, but: {reason_text}"

            tier_widget = self._create_tier_indicator(m["tier"], tooltip)
            self.table.setCellWidget(i, 3, tier_widget)

            diff_item = QTableWidgetItem(f"{date_diff} days")
            diff_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 4, diff_item)

            match_item = QTableWidgetItem(merchant_pct)
            match_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if merchant_sim >= 0.80: match_item.setForeground(QColor("#2e7d32"))
            elif merchant_sim >= 0.60: match_item.setForeground(QColor("#f57c00"))
            else: match_item.setForeground(QColor("#c62828"))
            self.table.setItem(i, 5, match_item)

            combo = QComboBox()
            combo.addItems(["Confirm (Merge)", "Reject (Import as New)"])
            combo.setCurrentIndex(0 if m["tier"] == "green" else 1)
            combo.currentIndexChanged.connect(lambda idx, row=i: self._on_action_changed(row, idx))
            self.table.setCellWidget(i, 6, combo)
            self.user_actions[i] = "confirm" if combo.currentIndex() == 0 else "reject"

        layout.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _create_tier_indicator(self, tier: str, tooltip: str) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel()
        label.setFixedSize(18, 18)
        label.setToolTip(tooltip)
        if tier == "green": label.setStyleSheet("background-color: #2e7d32; border-radius: 9px;")
        elif tier == "yellow": label.setStyleSheet("background-color: #f9a825; border-radius: 9px;")
        else: label.setStyleSheet("background-color: #c62828; border-radius: 9px;")
        layout.addWidget(label)
        return container

    def _on_action_changed(self, row, index):
        self.user_actions[row] = "confirm" if index == 0 else "reject"

    def get_confirmations(self):
        return [self.matches[i] for i, action in self.user_actions.items() if action == "confirm"]

    def get_rejections(self):
        return [self.matches[i]["parsed_tx"] for i, action in self.user_actions.items() if action == "reject"]


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
                existing_lines = [(l.id, l.account_name, l.dr, l.cr) for l in tx.lines]
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
            lines = tx.get("lines", [])
            imp_id = tx.get("imported_id")
            if not imp_id and source == "pdf":
                amount = tx.get("amount", 0)
                imp_id = f"rejected::{date_str}::{desc[:40]}::{amount}"
            ok, msg = self.gl.record_transaction(date_str, desc, lines, imported_id=imp_id, persist=True)
            if ok:
                imported += 1
                if source == "pdf" and tx.get("statement_balance") is not None:
                    self.gl.record_statement_balance(tx["contra_account"], date_str, tx["statement_balance"], source="rejected_match")
        return imported
