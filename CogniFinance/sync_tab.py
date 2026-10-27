# sync_tab.py
"""
sync_tab.py ver 1.0
For CogniFinance ver 1.9
Encapsulates the Mobile Sync tab UI, server lifecycle, QR rendering,
and post-sync data processing.
"""
import os
import io
import json
import threading
import urllib.error
import urllib.request
import logging
import time
import qrcode

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QLabel, QPushButton, QTextEdit, QDialog, QLineEdit, QComboBox,
    QDialogButtonBox, QMessageBox, QSizePolicy
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QPixmap, QImage

from ledger import GeneralLedger
from desktop_sync_server import CogniSyncServer
from protocol import make_pairing_code

logger = logging.getLogger(__name__)

class SyncTab(QWidget):
    """Self-contained Mobile Sync tab."""
    data_synced = pyqtSignal()

    def __init__(self, ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.gl = ledger
        self.sync_server = None
        self.sync_thread = None
        self.sync_status_timer = None
        self.sync_timeout_timer = None
        self.sync_qr_pixmap = None
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        server_box = QGroupBox("Mobile Sync")
        form = QFormLayout()
        self.sync_state_label = QLabel("Stopped")
        self.sync_state_label.setFont(QFont("Monospace", 10))
        form.addRow("Server:", self.sync_state_label)
        self.sync_address_label = QLabel("—")
        form.addRow("Address:", self.sync_address_label)
        self.sync_pairing_label = QLabel("—")
        self.sync_pairing_label.setFont(QFont("Monospace", 14))
        form.addRow("Pairing code:", self.sync_pairing_label)
        self.sync_fingerprint_label = QLabel("—")
        self.sync_fingerprint_label.setFont(QFont("Monospace", 9))
        form.addRow("Desktop fingerprint:", self.sync_fingerprint_label)
        server_box.setLayout(form)
        layout.addWidget(server_box)

        controls = QHBoxLayout()
        self.sync_start_button = QPushButton("Start Mobile Sync")
        self.sync_start_button.clicked.connect(self.start_mobile_sync)
        controls.addWidget(self.sync_start_button)
        self.sync_stop_button = QPushButton("Stop Sync Server")
        self.sync_stop_button.clicked.connect(self.stop_sync)
        self.sync_stop_button.setEnabled(False)
        controls.addWidget(self.sync_stop_button)
        controls.addStretch()
        layout.addLayout(controls)

        qr_box = QGroupBox("Scan this QR code with the Android companion")
        qr_layout = QHBoxLayout(qr_box)
        self.sync_qr_label = QLabel("Start Mobile Sync to generate a QR code.")
        self.sync_qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sync_qr_label.setMinimumSize(0, 0)
        self.sync_qr_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        qr_layout.addWidget(self.sync_qr_label, 1)
        layout.addWidget(qr_box, 3)

        status_box = QGroupBox("Sync Status")
        status_layout = QVBoxLayout(status_box)
        self.sync_handshake_label = QLabel("Handshake: —")
        self.sync_peer_label = QLabel("Phone identity: —")
        self.sync_last_cursor_label = QLabel("Last sync cursor: —")
        self.sync_accounts_label = QLabel("Unknown accounts: none")
        self.sync_held_label = QLabel("Held transactions: 0")
        for widget in (self.sync_handshake_label, self.sync_peer_label, self.sync_last_cursor_label, self.sync_accounts_label, self.sync_held_label):
            status_layout.addWidget(widget)
        layout.addWidget(status_box)

        account_row = QHBoxLayout()
        self.sync_create_account_button = QPushButton("Create Missing Account(s)")
        self.sync_create_account_button.clicked.connect(self.create_sync_accounts)
        self.sync_create_account_button.setEnabled(False)
        account_row.addWidget(self.sync_create_account_button)
        self.sync_reprocess_button = QPushButton("Reprocess Held Transactions")
        self.sync_reprocess_button.clicked.connect(self.reprocess_sync_transactions)
        self.sync_reprocess_button.setEnabled(False)
        account_row.addWidget(self.sync_reprocess_button)
        account_row.addStretch()
        layout.addLayout(account_row)

        self.sync_log = QTextEdit()
        self.sync_log.setReadOnly(True)
        self.sync_log.setFont(QFont("Monospace", 9))
        self.sync_log.setPlaceholderText("Sync events will appear here.")
        layout.addWidget(self.sync_log, 1)

    # ==========================================
    # Public API (called by parent)
    # ==========================================
    def set_start_button_enabled(self, enabled: bool):
        """Allows the parent to disable the sync button during audits."""
        self.sync_start_button.setEnabled(enabled)
        if enabled:
            self.sync_start_button.setToolTip("Start Mobile Sync")
        else:
            self.sync_start_button.setToolTip("Sync temporarily locked: Background system audit in progress (~1 min).")

    def stop_sync(self):
        """Public teardown method. Safe to call on shutdown or restore."""
        self._stop_mobile_sync_internal(from_server=False)

    # ==========================================
    # Internal Sync Logic
    # ==========================================
    def _append_sync_log(self, message: str):
        self.sync_log.append(message)
        # Note: statusBar access requires parent, so we emit a signal or just log it.
        # For simplicity, we'll just append to the text box.
        logger.info(message)

    def _render_sync_qr(self, payload: dict):
        try:
            qr = qrcode.QRCode(version=None, box_size=8, border=4)
            qr.add_data(json.dumps(payload, separators=(",", ":"), sort_keys=True))
            qr.make(fit=True)
            image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            qimage = QImage.fromData(buffer.getvalue(), "PNG")
            pixmap = QPixmap.fromImage(qimage)
            self.sync_qr_pixmap = pixmap
            self._resize_sync_qr()
        except Exception as e:
            logger.warning(f"QR render failed: {e}")

    def _resize_sync_qr(self):
        if self.sync_qr_pixmap is None or self.sync_qr_pixmap.isNull():
            return
        available = self.sync_qr_label.size()
        margin = 12
        width = max(1, available.width() - margin * 2)
        height = max(1, available.height() - margin * 2)
        size = min(width, height)
        self.sync_qr_label.setPixmap(
            self.sync_qr_pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_sync_qr()

    def start_mobile_sync(self):
        if self.sync_server is not None:
            return
        try:
            pairing_code = make_pairing_code()
            # We need a way to trigger the backup on sync complete.
            # We'll pass a lambda that emits a signal, or just rely on the parent.
            # Actually, the original code called self._create_safety_backup("post_sync").
            # We will emit data_synced which the parent can use to trigger a backup if needed,
            # or we can just pass a dummy callback. Let's pass a lambda that does nothing
            # and let the parent handle post-sync backups via the signal.
            server = CogniSyncServer(self.gl, pairing_code, on_sync_completed=lambda: None)
            self.sync_server = server
            thread = threading.Thread(target=server.serve_forever, name="CogniSyncServer", daemon=True)
            thread.start()
        except Exception as e:
            QMessageBox.critical(self, "Mobile Sync", f"Could not start the sync server:\n{e}")
            return

        self.sync_thread = thread
        bootstrap = server.bootstrap_data
        self.sync_state_label.setText("Running — waiting for Android")
        self.sync_address_label.setText(f"http://{bootstrap['desktop_ip']}:{bootstrap['port']}")
        self.sync_pairing_label.setText(pairing_code)
        self.sync_fingerprint_label.setText(bootstrap.get("desktop_fingerprint", "—"))
        self._render_sync_qr(bootstrap)
        self.sync_start_button.setEnabled(False)
        self.sync_stop_button.setEnabled(True)
        self.sync_log.clear()
        self._append_sync_log("Sync server started.")
        self._append_sync_log("Scan the QR code with the Android companion and enter the pairing code.")

        self.sync_status_timer = QTimer(self)
        self.sync_status_timer.setInterval(1000)
        self.sync_status_timer.timeout.connect(self.poll_mobile_sync_status)
        self.sync_status_timer.start()
        self.poll_mobile_sync_status()

        self.sync_timeout_timer = QTimer(self)
        self.sync_timeout_timer.setSingleShot(True)
        self.sync_timeout_timer.timeout.connect(self._force_stop_sync_timeout)
        self.sync_timeout_timer.start(5 * 60 * 1000)

    def _force_stop_sync_timeout(self):
        if self.sync_server is not None:
            self._append_sync_log("Sync server auto-closed: 5-minute inactivity timeout reached.")
            self._stop_mobile_sync_internal(from_server=True)

    def _sync_request(self, path: str):
        if self.sync_server is None:
            return None
        ctx = self.sync_server.sync_context
        url = f"http://127.0.0.1:{self.sync_server.server_port}{path}"
        body = json.dumps({"session_id": ctx.session_id}).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ctx.http_token}",
        })
        try:
            with urllib.request.urlopen(request, timeout=0.5) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            return None

    def poll_mobile_sync_status(self):
        if self.sync_server is None:
            return
        result = self._sync_request("/api/status")
        if result is None:
            if self.sync_thread is not None and not self.sync_thread.is_alive():
                self._append_sync_log("Sync completed successfully. Server shutting down.")
                self._stop_mobile_sync_internal(from_server=True)
            return

        handshake = bool(result.get("handshake_confirmed"))
        reconnect = bool(result.get("reconnect_available"))
        peer = result.get("peer_fingerprint") or "none"
        unknown = result.get("unknown_accounts") or []
        held = int(result.get("held_transaction_count", 0) or 0)
        cursor = result.get("last_sync_cursor") or "—"

        if handshake:
            self.sync_state_label.setText("Connected — handshake confirmed")
        elif reconnect:
            self.sync_state_label.setText("Running — reconnect available")
        else:
            self.sync_state_label.setText("Running — waiting for Android")

        self.sync_handshake_label.setText(f"Handshake: {'confirmed' if handshake else 'not confirmed'}")
        self.sync_peer_label.setText(f"Phone identity: {peer}")
        self.sync_last_cursor_label.setText(f"Last sync cursor: {cursor}")
        self.sync_accounts_label.setText("Unknown accounts: " + (", ".join(unknown) if unknown else "none"))
        self.sync_held_label.setText(f"Held transactions: {held}")
        self.sync_create_account_button.setEnabled(bool(unknown))
        self.sync_reprocess_button.setEnabled(held > 0)

    def create_sync_accounts(self):
        if self.sync_server is None:
            return
        ctx = self.sync_server.sync_context
        with ctx._state_lock:
            names = sorted(ctx.unknown_accounts)
        for name in names:
            dialog = QDialog(self)
            dialog.setWindowTitle("Create Missing Account")
            form = QFormLayout(dialog)
            name_edit = QLineEdit(name)
            type_combo = QComboBox()
            for account_type in ("Asset", "Liability", "Equity", "Revenue", "Expense"):
                type_combo.addItem(account_type)
            form.addRow("Account:", name_edit)
            form.addRow("Type:", type_combo)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            form.addRow(buttons)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                continue
            account_name = name_edit.text().strip()
            if not account_name:
                continue
            ok, message = ctx.create_account(account_name, type_combo.currentText())
            if ok:
                self._append_sync_log(message)
                self.data_synced.emit() # Notify parent to refresh UI
            else:
                QMessageBox.warning(self, "Create Account", message)
        self.poll_mobile_sync_status()

    def reprocess_sync_transactions(self):
        if self.sync_server is None:
            return
        try:
            result = self.sync_server.sync_context.reprocess_held()
        except Exception as e:
            QMessageBox.critical(self, "Reprocess Held Transactions", str(e))
            return
        posted = result.get("posted", [])
        still_missing = result.get("still_missing", [])
        self._append_sync_log(f"Reprocessed {len(posted)} held transaction(s)." + (f" Still missing: {', '.join(still_missing)}." if still_missing else ""))
        self.data_synced.emit() # Notify parent to refresh UI
        self.poll_mobile_sync_status()

    def _stop_mobile_sync_internal(self, from_server: bool = False):
        server = self.sync_server
        if server is None and not from_server:
            return

        for attr in ("sync_timeout_timer", "sync_status_timer"):
            timer = getattr(self, attr, None)
            if timer is not None:
                try:
                    timer.stop()
                    timer.deleteLater()
                except RuntimeError:
                    # "wrapped C++ object is deleted" — Qt tore it down before
                    # us. Nothing left to stop.
                    pass
                finally:
                    setattr(self, attr, None)

        if server:
            try:
                server.sync_context.request_shutdown()
                if self.sync_thread is not None and self.sync_thread.is_alive():
                    server.shutdown()
                server.server_close()
            except Exception as e:
                if not from_server:
                    self._append_sync_log(f"Error stopping sync server: {e}")

        self.sync_server = None
        self.sync_thread = None

        # Reset UI
        self.sync_state_label.setText("Stopped")
        self.sync_address_label.setText("—")
        self.sync_pairing_label.setText("—")
        self.sync_fingerprint_label.setText("—")
        self.sync_qr_label.clear()
        self.sync_qr_label.setText("Start Mobile Sync to generate a QR code.")
        self.sync_handshake_label.setText("Handshake: —")
        self.sync_peer_label.setText("Phone identity: —")
        self.sync_last_cursor_label.setText("Last sync cursor: —")
        self.sync_accounts_label.setText("Unknown accounts: none")
        self.sync_held_label.setText("Held transactions: 0")
        self.sync_start_button.setEnabled(True)
        self.sync_stop_button.setEnabled(False)
        self.sync_create_account_button.setEnabled(False)
        self.sync_reprocess_button.setEnabled(False)

        if not from_server:
            self._append_sync_log("Sync server stopped.")
