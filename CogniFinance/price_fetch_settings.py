"""
price_fetch_settings.py
For CogniFinance -- the settings UI for the opt-in online price refresh.

A self-contained QGroupBox: drop it into the Settings tab (or anywhere
that has the ledger handle `gl`). It only talks to price_fetch.py's
public settings functions and never makes a network call itself.

Changes here apply immediately (enable flag, provider choice, key
save/clear) rather than waiting on the tab's Save button, because they
change what leaves the machine and should take effect exactly when the
user acts. The API key is never held in a widget attribute after saving
and is never displayed back.
"""
import html

from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QFormLayout, QHBoxLayout, QLabel, QCheckBox,
    QComboBox, QLineEdit, QPushButton, QWidget, QMessageBox,
)
from PyQt6.QtCore import Qt

import price_fetch

_NONE_LABEL = "(none selected)"


class PriceFetchSettingsGroup(QGroupBox):
    def __init__(self, gl, parent=None):
        super().__init__("Online Price Refresh (optional)", parent)
        self.gl = gl
        self._loading = False
        self._build_ui()
        self.reload()

    # ------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Off by default. Prices are only fetched when you click Refresh -- never "
            "automatically. Leave this off to stay fully local and enter prices manually. "
            "You bring your own provider account and API key."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.enable_check = QCheckBox("Enable online price refresh (manual button only)")
        self.enable_check.toggled.connect(self._on_enable_toggled)
        layout.addWidget(self.enable_check)

        form = QFormLayout()

        self.stock_combo = QComboBox()
        self.stock_combo.currentIndexChanged.connect(self._on_stock_changed)
        form.addRow("Stock provider:", self.stock_combo)

        # Key row (shown only for providers that need a key)
        self.key_widget = QWidget()
        key_row = QHBoxLayout(self.key_widget)
        key_row.setContentsMargins(0, 0, 0, 0)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Paste your API key")
        self.key_edit.returnPressed.connect(self._on_save_key)
        self.save_key_btn = QPushButton("Save key")
        self.save_key_btn.clicked.connect(self._on_save_key)
        self.clear_key_btn = QPushButton("Clear key")
        self.clear_key_btn.clicked.connect(self._on_clear_key)
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(self.save_key_btn)
        key_row.addWidget(self.clear_key_btn)
        self.key_label = QLabel("API key:")
        form.addRow(self.key_label, self.key_widget)

        self.crypto_combo = QComboBox()
        self.crypto_combo.currentIndexChanged.connect(self._on_crypto_changed)
        form.addRow("Crypto provider:", self.crypto_combo)

        layout.addLayout(form)

        self.key_status = QLabel()
        self.key_status.setWordWrap(True)
        layout.addWidget(self.key_status)

        self.provider_note = QLabel()
        self.provider_note.setWordWrap(True)
        self.provider_note.setTextFormat(Qt.TextFormat.RichText)
        self.provider_note.setOpenExternalLinks(True)
        layout.addWidget(self.provider_note)

        self.disclosure = QLabel()
        self.disclosure.setWordWrap(True)
        layout.addWidget(self.disclosure)

    # ------------------------------------------------------------
    def reload(self):
        """Re-read everything from the database (call after external changes)."""
        self._loading = True
        try:
            self.enable_check.setChecked(price_fetch.is_enabled(self.gl))
            self._fill_combo(self.stock_combo, "stock", with_none=True)
            self._fill_combo(self.crypto_combo, "crypto", with_none=False)
        except Exception as e:
            self._warn(f"Could not load price refresh settings: {e}")
        finally:
            self._loading = False
        self._refresh_dependent()

    def _fill_combo(self, combo: QComboBox, kind: str, with_none: bool):
        combo.clear()
        if with_none:
            combo.addItem(_NONE_LABEL, None)
        for p in price_fetch.providers_for(kind):
            combo.addItem(p.label, p.id)
        selected = price_fetch.get_provider_id(self.gl, kind)
        idx = combo.findData(selected)
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _refresh_dependent(self):
        pid = self.stock_combo.currentData()
        provider = price_fetch.PROVIDERS.get(pid)
        needs_key = bool(provider and provider.needs_key)

        self.key_label.setVisible(needs_key)
        self.key_widget.setVisible(needs_key)
        self.key_status.setVisible(needs_key)

        if needs_key:
            try:
                saved = price_fetch.has_api_key(self.gl, pid)
            except Exception:
                saved = False
            self.key_status.setText(
                f"A key is saved for {provider.label}." if saved
                else f"No key saved for {provider.label} yet."
            )
            self.clear_key_btn.setEnabled(saved)

        if provider is not None:
            note = html.escape(provider.note)
            if provider.signup_url:
                url = html.escape(provider.signup_url, quote=True)
                note += f' <a href="{url}">Get a key</a>'
            self.provider_note.setText(note)
        else:
            self.provider_note.setText("")

        try:
            self.disclosure.setText(
                price_fetch.get_disclosure_text(self.gl, "stock")
                + "\n\n"
                + price_fetch.get_disclosure_text(self.gl, "crypto")
            )
        except Exception:
            self.disclosure.setText(price_fetch.DISCLOSURE_TEXT)

    # ------------------------------------------------------------
    def _warn(self, message: str):
        QMessageBox.warning(self, "Price Refresh Settings", message)

    def _on_enable_toggled(self, checked: bool):
        if self._loading:
            return
        if checked:
            # Opting in requires seeing exactly what will be sent where.
            text = (
                price_fetch.get_disclosure_text(self.gl, "stock")
                + "\n\n"
                + price_fetch.get_disclosure_text(self.gl, "crypto")
                + "\n\nEnable online price refresh?"
            )
            answer = QMessageBox.question(self, "Enable online price refresh", text)
            if answer != QMessageBox.StandardButton.Yes:
                self._set_checkbox_silently(False)
                return
        try:
            price_fetch.set_enabled(self.gl, checked)
        except Exception as e:
            self._warn(f"Could not save that setting: {e}")
            self._set_checkbox_silently(not checked)

    def _set_checkbox_silently(self, value: bool):
        self._loading = True
        try:
            self.enable_check.setChecked(value)
        finally:
            self._loading = False

    def _on_stock_changed(self, _index: int):
        if self._loading:
            return
        self._apply_provider("stock", self.stock_combo.currentData())

    def _on_crypto_changed(self, _index: int):
        if self._loading:
            return
        self._apply_provider("crypto", self.crypto_combo.currentData())

    def _apply_provider(self, kind: str, provider_id):
        try:
            if provider_id is None:
                price_fetch.clear_provider_id(self.gl, kind)
            else:
                price_fetch.set_provider_id(self.gl, kind, provider_id)
        except Exception as e:
            self._warn(f"Could not save the provider choice: {e}")
        self.key_edit.clear()
        self._refresh_dependent()

    def _on_save_key(self):
        pid = self.stock_combo.currentData()
        provider = price_fetch.PROVIDERS.get(pid)
        if provider is None or not provider.needs_key:
            return
        try:
            price_fetch.set_api_key(self.gl, pid, self.key_edit.text())
        except ValueError as e:
            self._warn(str(e))
            return
        except Exception as e:
            self._warn(f"Could not save the key: {e}")
            return
        self.key_edit.clear()  # never keep the key in the widget
        self._refresh_dependent()

    def _on_clear_key(self):
        pid = self.stock_combo.currentData()
        if pid is None:
            return
        try:
            price_fetch.clear_api_key(self.gl, pid)
        except Exception as e:
            self._warn(f"Could not clear the key: {e}")
            return
        self.key_edit.clear()
        self._refresh_dependent()
