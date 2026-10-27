# investment_import_manager.py
# For CogniFinance -- background-thread/progress-dialog orchestration
# for investment & crypto CSV imports.
#
# Deliberately its own small file rather than added to import_manager.py:
# investment/crypto code should stay cleanly excludable from the core
# app (see the planning brief's "rejected/deferred" note on a stripped
# privacy build), and import_manager.py is squarely a core file. This
# mirrors ImportManager's own shape closely on purpose --
# QProgressDialog setup (modal, autoclose, 500ms minimum duration),
# thread signal wiring, deleteLater on finish -- so it behaves the same
# way to a user as every other import in the app, without the two
# files needing to know about each other.

import logging
from typing import Optional

from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtWidgets import QWidget, QFileDialog, QMessageBox, QProgressDialog

from csv_transaction_import import (
    ColumnMapping, PosterFn, TransactionCSVImportThread, CSVColumnMappingDialog,
)

logger = logging.getLogger(__name__)


class InvestmentImportManager(QObject):
    """One instance can serve both the Investments and Crypto sub-tabs
    -- it holds no engine-specific state, only the dialog/thread
    plumbing. Each call to start_csv_import() supplies its own
    `poster`, already bound (by the caller) to the right engine, cash
    account, and (for crypto) wallet."""

    data_imported = pyqtSignal()

    def __init__(self, parent: QWidget):
        super().__init__()
        self.parent = parent
        self.progress_dialog: Optional[QProgressDialog] = None
        self.import_thread: Optional[TransactionCSVImportThread] = None

    def start_csv_import(self, poster: PosterFn, dialog_caption: str = "Import Transactions CSV"):
        """Opens a file picker, then a column-mapping dialog for
        whatever the user picked (see CSVColumnMappingDialog -- this is
        the deliberate answer to not having a fixed target format),
        then runs the import in the background with a progress dialog,
        same as start_ofx_import()."""
        path, _ = QFileDialog.getOpenFileName(self.parent, dialog_caption, "", "CSV Files (*.csv);;All Files (*)")
        if not path:
            return

        mapping_dialog = CSVColumnMappingDialog(path, self.parent)
        if mapping_dialog.exec() != CSVColumnMappingDialog.DialogCode.Accepted:
            return
        mapping = mapping_dialog.get_mapping()
        if mapping is None:
            return

        self.progress_dialog = QProgressDialog("Importing transactions...", "Cancel", 0, 100, self.parent)
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setAutoClose(True)
        self.progress_dialog.setMinimumDuration(500)
        self.progress_dialog.show()

        self.import_thread = TransactionCSVImportThread(path, mapping, poster)
        self.import_thread.progress_signal.connect(self._update_progress)
        self.import_thread.finished_signal.connect(self._on_finished)
        self.import_thread.error_signal.connect(self._on_error)
        self.import_thread.finished.connect(self.import_thread.deleteLater)
        self.import_thread.start()

    def _update_progress(self, percent: int, message: str):
        dialog = self.progress_dialog
        if dialog is not None:
            try:
                dialog.setValue(percent)
                dialog.setLabelText(message)
            except (RuntimeError, AttributeError):
                pass

    def _on_finished(self, imported_count: int, review: dict):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None

        skipped = review.get("skipped", [])
        errors = review.get("errors", [])

        summary = f"Imported {imported_count} transaction(s)."
        if skipped or errors:
            parts = []
            if skipped:
                parts.append(f"{len(skipped)} row(s) skipped during parsing")
            if errors:
                parts.append(f"{len(errors)} row(s) rejected while posting")
            summary += "\n\nHowever, " + "; ".join(parts) + ". See details below."

            box = QMessageBox(self.parent)
            box.setIcon(QMessageBox.Icon.Warning if errors else QMessageBox.Icon.Information)
            box.setWindowTitle("Import Complete — Review Needed")
            box.setText(summary)
            detail_lines = []
            if skipped:
                detail_lines.append("=== Rows skipped during parsing ===")
                detail_lines.extend(skipped[:50])
                if len(skipped) > 50:
                    detail_lines.append(f"... and {len(skipped) - 50} more")
                detail_lines.append("")
            if errors:
                detail_lines.append("=== Rows rejected while posting ===")
                detail_lines.extend(errors[:50])
                if len(errors) > 50:
                    detail_lines.append(f"... and {len(errors) - 50} more")
            box.setDetailedText("\n".join(detail_lines))
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            box.exec()
        else:
            QMessageBox.information(self.parent, "Success", summary)

        self.data_imported.emit()

    def _on_error(self, error_message: str):
        if self.progress_dialog:
            self.progress_dialog.close()
            self.progress_dialog = None
        QMessageBox.critical(self.parent, "Import Error", error_message)
