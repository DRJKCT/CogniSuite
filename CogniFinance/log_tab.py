# log_tab.py
import os
import logging
from datetime import datetime
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit,
    QComboBox, QPushButton, QLabel, QFileDialog, QMessageBox
)
from PyQt6.QtCore import QObject, pyqtSignal, Qt
from PyQt6.QtGui import QFont, QColor

# =============================================================================
# THREAD-SAFE LOG BRIDGE
# =============================================================================
class _LogSignal(QObject):
    """Signal to safely pass log strings from background threads to the main UI thread."""
    new_log = pyqtSignal(str)

class QtLogHandler(logging.Handler):
    """
    Captures Python logging output and routes it to the Qt UI.
    Because the sync server runs in a background thread, we CANNOT update
    the QPlainTextEdit directly. We must emit a Qt signal, which Qt will
    safely queue for the main UI thread to process.
    """
    def __init__(self):
        super().__init__()
        self.signal = _LogSignal()
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
            datefmt="%H:%M:%S"
        ))

    def emit(self, record):
        try:
            msg = self.format(record)
            if "hmac check failed" in msg or "error decrypting page" in msg or "sqlcipher_codec_ctx_set_error" in msg:
                return
            self.signal.new_log.emit(msg)
        except Exception:
            self.handleError(record)

# =============================================================================
# THE LOG TAB UI
# =============================================================================
class LogTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        # Instantiate the handler and expose it so the main app can attach it
        self.log_handler = QtLogHandler()
        self.log_handler.setLevel(logging.INFO)

        self._init_ui()

        # Connect the thread-safe signal to our UI update slot
        self.log_handler.signal.new_log.connect(self._append_log)

    def _init_ui(self):
        layout = QVBoxLayout(self)

        info_label = QLabel(
            "Live application and sync logs. Use this to debug sync failures, "
            "category mismatches, or connection issues without digging through files."
        )
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        # --- Controls ---
        controls = QHBoxLayout()

        controls.addWidget(QLabel("Minimum Level:"))
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.log_level_combo.setCurrentText("INFO")
        self.log_level_combo.currentTextChanged.connect(self._change_log_level)
        controls.addWidget(self.log_level_combo)

        btn_clear = QPushButton("Clear Logs")
        btn_clear.clicked.connect(self._clear_logs)
        controls.addWidget(btn_clear)

        btn_save = QPushButton("Save Logs to File...")
        btn_save.clicked.connect(self._save_logs)
        controls.addWidget(btn_save)

        controls.addStretch()
        layout.addLayout(controls)

        # --- Log Display Area ---
        self.log_text_area = QPlainTextEdit()
        self.log_text_area.setReadOnly(True)
        self.log_text_area.setMaximumBlockCount(5000) # Prevent memory bloat
        self.log_text_area.setFont(QFont("Monospace", 9))
        layout.addWidget(self.log_text_area)

    # --- Slots ---
    def _append_log(self, message: str):
        """Appends a new log line and auto-scrolls to the bottom."""
        self.log_text_area.appendPlainText(message)
        scrollbar = self.log_text_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _change_log_level(self, level_text: str):
        """Dynamically changes the logging threshold."""
        level = getattr(logging, level_text, logging.INFO)
        self.log_handler.setLevel(level)

    def _clear_logs(self):
        self.log_text_area.clear()

    def _save_logs(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Logs",
            f"cognifinance_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "Text Files (*.txt);;All Files (*)"
        )
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log_text_area.toPlainText())
            QMessageBox.information(self, "Saved", "Logs saved successfully.")
