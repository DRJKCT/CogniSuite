# investments_tab.py
# For CogniFinance -- the "Investments & Crypto" tab.
#
# One shared tab, split internally into two sub-tabs, each calling its
# own engine -- avoids a single tab file becoming a dumping ground
# where investment and crypto logic tangle together (see the planning
# brief's UI-structure decision).
#
# NOTE ON INTEGRATION: this file follows the constructor/signal
# convention observed in ui_main.py (a tab takes `gl` and exposes a
# `data_modified` pyqtSignal that the main window connects to
# `_refresh_all_data`, the same way BudgetsTab and AddEditTab do). I
# did not have ui_helpers.py / transaction_dialogs.py available, so
# this uses plain Qt widgets throughout rather than the app's shared
# `money_item`/`create_account_combo` helpers -- swapping those in
# where they exist would make this look more consistent with the rest
# of the app and is a reasonable follow-up, not required for this to
# work.

import logging
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QLineEdit, QComboBox, QPushButton, QDateEdit, QCheckBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox, QDialog,
    QDialogButtonBox, QTabWidget, QFileDialog, QAbstractItemView,
)
from PyQt6.QtCore import Qt, QDate, pyqtSignal

from investment_engine import InvestmentEngine, VALID_COST_BASIS_METHODS as INV_COST_METHODS
from crypto_engine import CryptoEngine, VALID_COST_BASIS_METHODS as CRYPTO_COST_METHODS, VALID_REWARD_KINDS
from investment_import_manager import InvestmentImportManager
import price_fetch
import price_history
import network_call_log
from ledger import REPORT_LINE_METADATA

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")


def _decimal_from_field(text: str, field_name: str) -> Decimal:
    text = (text or "").strip().replace(",", "")
    if not text:
        raise ValueError(f"{field_name} is required.")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{field_name} must be a number.")
    return value


def _report_lines_for(subcategory: str) -> List[str]:
    return [rl for rl, (acc_type, subcat) in REPORT_LINE_METADATA.items()
            if acc_type == "Asset" and subcat == subcategory]


# =====================================================================
# Shared price-refresh panel (tier 2 of the pricing approach)
# =====================================================================

class NetworkCallLogDialog(QDialog):
    """Read-only view of network_call_log.py's recent entries -- the
    trust artifact: 'I clicked Refresh, here's exactly what that
    triggered.'"""

    def __init__(self, gl, parent=None):
        super().__init__(parent)
        self.gl = gl
        self.setWindowTitle("Network Call Log — Price Refresh")
        self.resize(640, 360)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Every price-fetch call this app has made, most recent first. "
            "No account data, balances, or identifiers are ever logged here."
        ))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Time (UTC)", "Source", "Symbols", "Outcome", "Detail"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self._reload()

    def _reload(self):
        rows = network_call_log.get_recent(self.gl, limit=200)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, key in enumerate(("timestamp", "source", "symbols", "outcome", "detail")):
                item = QTableWidgetItem(str(row.get(key, "")))
                if key == "outcome" and row.get(key) == "failure":
                    item.setForeground(Qt.GlobalColor.red)
                self.table.setItem(r, c, item)


class PriceRefreshPanel(QWidget):
    """The opt-in, manual-trigger-only price refresh control. Firm
    rules enforced here: unchecked (disabled) on first load every time
    unless the user has explicitly enabled it before (state persists
    via price_fetch.py's own settings table, not a UI-only toggle);
    the refresh buttons only ever fire from a direct click; nothing in
    this class runs on a timer or on tab-show."""

    prices_updated = pyqtSignal()

    def __init__(self, gl, get_stock_symbols, get_crypto_symbols, parent=None):
        super().__init__(parent)
        self.gl = gl
        self._get_stock_symbols = get_stock_symbols
        self._get_crypto_symbols = get_crypto_symbols

        box = QGroupBox("Live Price Lookup (optional, network required)")
        layout = QVBoxLayout(box)

        self.enable_checkbox = QCheckBox("Enable price lookups")
        self.enable_checkbox.setChecked(price_fetch.is_enabled(self.gl))
        self.enable_checkbox.toggled.connect(self._on_toggle_enabled)
        layout.addWidget(self.enable_checkbox)

        disclosure = QLabel(price_fetch.DISCLOSURE_TEXT)
        disclosure.setWordWrap(True)
        disclosure.setStyleSheet("color: #555; font-size: 11px;")
        layout.addWidget(disclosure)

        btn_row = QHBoxLayout()
        self.refresh_stocks_btn = QPushButton("Refresh Stock Prices")
        self.refresh_stocks_btn.clicked.connect(lambda: self._do_refresh("stock"))
        self.refresh_crypto_btn = QPushButton("Refresh Crypto Prices")
        self.refresh_crypto_btn.clicked.connect(lambda: self._do_refresh("crypto"))
        self.view_log_btn = QPushButton("View Network Call Log")
        self.view_log_btn.clicked.connect(self._show_log)
        btn_row.addWidget(self.refresh_stocks_btn)
        btn_row.addWidget(self.refresh_crypto_btn)
        btn_row.addWidget(self.view_log_btn)
        layout.addLayout(btn_row)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)

        self._update_button_states()

    def _on_toggle_enabled(self, checked: bool):
        price_fetch.set_enabled(self.gl, checked)
        self._update_button_states()

    def _update_button_states(self):
        enabled = self.enable_checkbox.isChecked()
        self.refresh_stocks_btn.setEnabled(enabled)
        self.refresh_crypto_btn.setEnabled(enabled)

    def _do_refresh(self, asset_kind: str):
        symbols = self._get_stock_symbols() if asset_kind == "stock" else self._get_crypto_symbols()
        if not symbols:
            QMessageBox.information(self, "Nothing to refresh", "No holdings of this kind yet.")
            return
        try:
            results = price_fetch.refresh_prices(self.gl, symbols, asset_kind)
        except price_fetch.PriceFetchDisabledError as e:
            QMessageBox.warning(self, "Disabled", str(e))
            return
        succeeded = [s for s, err in results.items() if err is None]
        failed = {s: err for s, err in results.items() if err is not None}
        msg = f"Updated: {', '.join(succeeded) if succeeded else 'none'}."
        if failed:
            msg += "\n\nFailed:\n" + "\n".join(f"  {s}: {err}" for s, err in failed.items())
        QMessageBox.information(self, "Price Refresh", msg)
        self.prices_updated.emit()

    def _show_log(self):
        NetworkCallLogDialog(self.gl, self).exec()


# =====================================================================
# Investments sub-tab
# =====================================================================

class InvestmentsSubTab(QWidget):
    data_modified = pyqtSignal()

    def __init__(self, gl, parent=None):
        super().__init__(parent)
        self.gl = gl
        self.engine = InvestmentEngine(gl)
        self.import_manager = InvestmentImportManager(self)
        self.import_manager.data_imported.connect(self._on_csv_imported)

        layout = QVBoxLayout(self)

        self.holdings_table = QTableWidget(0, 6)
        self.holdings_table.setHorizontalHeaderLabels(
            ["Symbol", "Qty Held", "Avg Cost/Unit", "Cost Basis", "Last Price", "Method"]
        )
        self.holdings_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.holdings_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(QLabel("Holdings"))
        layout.addWidget(self.holdings_table)

        forms_row = QHBoxLayout()
        forms_row.addWidget(self._build_add_holding_box())
        forms_row.addWidget(self._build_buy_box())
        forms_row.addWidget(self._build_sell_box())
        forms_row.addWidget(self._build_dividend_box())
        forms_row.addWidget(self._build_csv_import_box())
        forms_row.addWidget(self._build_undo_box())
        layout.addLayout(forms_row)

        corp_row = QHBoxLayout()
        corp_row.addWidget(self._build_drip_box())
        corp_row.addWidget(self._build_split_box())
        corp_row.addStretch()
        layout.addLayout(corp_row)

        self.realized_table = QTableWidget(0, 5)
        self.realized_table.setHorizontalHeaderLabels(["Symbol", "Date", "Quantity", "Cost Basis", "Realized Gain"])
        self.realized_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.realized_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(QLabel("Realized Gains / Losses"))
        layout.addWidget(self.realized_table)

        self.refresh_all()

    # ---- forms ----

    def _build_add_holding_box(self) -> QGroupBox:
        box = QGroupBox("Add Holding")
        form = QFormLayout(box)
        self.ah_symbol = QLineEdit()
        self.ah_account_name = QLineEdit()
        self.ah_report_line = QComboBox()
        self.ah_report_line.addItems(_report_lines_for("Investments"))
        self.ah_method = QComboBox()
        self.ah_method.addItems(INV_COST_METHODS)
        form.addRow("Symbol:", self.ah_symbol)
        form.addRow("Account name:", self.ah_account_name)
        form.addRow("Report line:", self.ah_report_line)
        form.addRow("Cost-basis method:", self.ah_method)
        btn = QPushButton("Add Holding")
        btn.clicked.connect(self._on_add_holding)
        form.addRow(btn)
        return box

    def _build_buy_box(self) -> QGroupBox:
        box = QGroupBox("Buy")
        form = QFormLayout(box)
        self.buy_symbol = QComboBox()
        self.buy_date = QDateEdit(QDate.currentDate())
        self.buy_date.setCalendarPopup(True)
        self.buy_qty = QLineEdit()
        self.buy_price = QLineEdit()
        self.buy_fees = QLineEdit("0")
        self.buy_cash_account = QComboBox()
        form.addRow("Symbol:", self.buy_symbol)
        form.addRow("Date:", self.buy_date)
        form.addRow("Quantity:", self.buy_qty)
        form.addRow("Price/unit:", self.buy_price)
        form.addRow("Fees:", self.buy_fees)
        form.addRow("Cash account:", self.buy_cash_account)
        btn = QPushButton("Record Buy")
        btn.clicked.connect(self._on_buy)
        form.addRow(btn)
        return box

    def _build_sell_box(self) -> QGroupBox:
        box = QGroupBox("Sell")
        form = QFormLayout(box)
        self.sell_symbol = QComboBox()
        self.sell_date = QDateEdit(QDate.currentDate())
        self.sell_date.setCalendarPopup(True)
        self.sell_qty = QLineEdit()
        self.sell_price = QLineEdit()
        self.sell_fees = QLineEdit("0")
        self.sell_cash_account = QComboBox()
        form.addRow("Symbol:", self.sell_symbol)
        form.addRow("Date:", self.sell_date)
        form.addRow("Quantity:", self.sell_qty)
        form.addRow("Price/unit:", self.sell_price)
        form.addRow("Fees:", self.sell_fees)
        form.addRow("Cash account:", self.sell_cash_account)
        note = QLabel("Specific-lot selection isn't available in this form yet; "
                       "SPECIFIC-method holdings must be sold via the engine API directly.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 10px;")
        form.addRow(note)
        btn = QPushButton("Record Sell")
        btn.clicked.connect(self._on_sell)
        form.addRow(btn)
        return box

    def _build_dividend_box(self) -> QGroupBox:
        box = QGroupBox("Dividend")
        form = QFormLayout(box)
        self.div_symbol = QComboBox()
        self.div_date = QDateEdit(QDate.currentDate())
        self.div_date.setCalendarPopup(True)
        self.div_amount = QLineEdit()
        self.div_cash_account = QComboBox()
        form.addRow("Symbol:", self.div_symbol)
        form.addRow("Date:", self.div_date)
        form.addRow("Amount:", self.div_amount)
        form.addRow("Cash account:", self.div_cash_account)
        btn = QPushButton("Record Dividend")
        btn.clicked.connect(self._on_dividend)
        form.addRow(btn)
        return box

    def _build_drip_box(self) -> QGroupBox:
        box = QGroupBox("Dividend Reinvested (DRIP)")
        form = QFormLayout(box)
        note = QLabel(
            "For a dividend that bought more shares instead of paying "
            "cash. Records the dividend as income and adds the new "
            "shares as a lot whose cost basis is the dividend amount."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 10px;")
        form.addRow(note)
        self.drip_symbol = QComboBox()
        self.drip_date = QDateEdit(QDate.currentDate())
        self.drip_date.setCalendarPopup(True)
        self.drip_amount = QLineEdit()
        self.drip_quantity = QLineEdit()
        form.addRow("Symbol:", self.drip_symbol)
        form.addRow("Date:", self.drip_date)
        form.addRow("Dividend amount:", self.drip_amount)
        form.addRow("Shares received:", self.drip_quantity)
        btn = QPushButton("Record Reinvested Dividend")
        btn.clicked.connect(self._on_drip)
        form.addRow(btn)
        return box

    def _build_split_box(self) -> QGroupBox:
        box = QGroupBox("Stock Split")
        form = QFormLayout(box)
        note = QLabel(
            "Enter the ratio as new shares for old shares: 2-for-1 is "
            "2 and 1; a 1-for-10 reverse split is 1 and 10. Total cost "
            "basis is unchanged. Record the split BEFORE any sales on or "
            "after its effective date."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 10px;")
        form.addRow(note)
        self.split_symbol = QComboBox()
        self.split_date = QDateEdit(QDate.currentDate())
        self.split_date.setCalendarPopup(True)
        self.split_new = QLineEdit()
        self.split_old = QLineEdit()
        form.addRow("Symbol:", self.split_symbol)
        form.addRow("Effective date:", self.split_date)
        form.addRow("New shares:", self.split_new)
        form.addRow("For old shares:", self.split_old)
        btn = QPushButton("Record Split")
        btn.clicked.connect(self._on_split)
        form.addRow(btn)
        return box

    def _build_csv_import_box(self) -> QGroupBox:
        box = QGroupBox("Import CSV")
        form = QFormLayout(box)
        note = QLabel(
            "Works with any brokerage export -- you'll map its columns "
            "after picking the file. Unseen symbols are auto-added as "
            "new holdings using the settings below."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 10px;")
        form.addRow(note)
        self.csv_cash_account = QComboBox()
        self.csv_report_line = QComboBox()
        self.csv_report_line.addItems(_report_lines_for("Investments"))
        self.csv_method = QComboBox()
        self.csv_method.addItems(INV_COST_METHODS)
        form.addRow("Cash account:", self.csv_cash_account)
        form.addRow("New-holding report line:", self.csv_report_line)
        form.addRow("New-holding cost method:", self.csv_method)
        btn = QPushButton("Import CSV...")
        btn.clicked.connect(self._on_import_csv)
        form.addRow(btn)
        return box

    def _build_undo_box(self) -> QGroupBox:
        box = QGroupBox("Undo")
        form = QFormLayout(box)
        note = QLabel(
            "Reverses the most recent buy/sell/dividend/reinvestment/split "
            "for the selected holding. Must be undone in order -- if you made several "
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 10px;")
        form.addRow(note)
        self.undo_symbol = QComboBox()
        form.addRow("Holding:", self.undo_symbol)
        btn = QPushButton("Void Most Recent Transaction")
        btn.clicked.connect(self._on_void_last)
        form.addRow(btn)
        return box

    # ---- actions ----

    def _cash_account_names(self) -> List[str]:
        return sorted(name for name, acc in self.gl.accounts.items() if acc.account_type == "Asset")

    def _on_add_holding(self):
        symbol = self.ah_symbol.text().strip()
        account_name = self.ah_account_name.text().strip()
        report_line = self.ah_report_line.currentText()
        method = self.ah_method.currentText()
        if not symbol or not account_name:
            QMessageBox.warning(self, "Missing info", "Symbol and account name are required.")
            return
        ok, msg = self.engine.add_holding(symbol, account_name, report_line, cost_basis_method=method)
        self._finish_action(ok, msg)
        if ok:
            self.ah_symbol.clear()
            self.ah_account_name.clear()

    def _on_buy(self):
        try:
            symbol = self.buy_symbol.currentText()
            qty = _decimal_from_field(self.buy_qty.text(), "Quantity")
            price = _decimal_from_field(self.buy_price.text(), "Price")
            fees = _decimal_from_field(self.buy_fees.text() or "0", "Fees")
            cash_account = self.buy_cash_account.currentText()
            date = self.buy_date.date().toString("yyyy-MM-dd")
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        ok, msg = self.engine.buy(symbol, date, qty, price, cash_account, fees=fees)
        self._finish_action(ok, msg)

    def _on_sell(self):
        try:
            symbol = self.sell_symbol.currentText()
            qty = _decimal_from_field(self.sell_qty.text(), "Quantity")
            price = _decimal_from_field(self.sell_price.text(), "Price")
            fees = _decimal_from_field(self.sell_fees.text() or "0", "Fees")
            cash_account = self.sell_cash_account.currentText()
            date = self.sell_date.date().toString("yyyy-MM-dd")
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        ok, msg = self.engine.sell(symbol, date, qty, price, cash_account, fees=fees)
        self._finish_action(ok, msg)

    def _on_drip(self):
        try:
            symbol = self.drip_symbol.currentText()
            amount = _decimal_from_field(self.drip_amount.text(), "Dividend amount")
            quantity = _decimal_from_field(self.drip_quantity.text(), "Shares received")
            date = self.drip_date.date().toString("yyyy-MM-dd")
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        ok, msg = self.engine.record_dividend_reinvestment(symbol, date, amount, quantity)
        self._finish_action(ok, msg)

    def _on_split(self):
        try:
            symbol = self.split_symbol.currentText()
            new_shares = _decimal_from_field(self.split_new.text(), "New shares")
            old_shares = _decimal_from_field(self.split_old.text(), "Old shares")
            date = self.split_date.date().toString("yyyy-MM-dd")
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        ok, msg = self.engine.record_stock_split(symbol, date, new_shares, old_shares)
        self._finish_action(ok, msg)

    def _on_dividend(self):
        try:
            symbol = self.div_symbol.currentText()
            amount = _decimal_from_field(self.div_amount.text(), "Amount")
            cash_account = self.div_cash_account.currentText()
            date = self.div_date.date().toString("yyyy-MM-dd")
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        ok, msg = self.engine.record_dividend(symbol, date, amount, cash_account)
        self._finish_action(ok, msg)

    def _on_import_csv(self):
        cash_account = self.csv_cash_account.currentText()
        report_line = self.csv_report_line.currentText()
        method = self.csv_method.currentText()
        if not cash_account:
            QMessageBox.warning(self, "No cash account", "Add a cash/asset account first.")
            return

        engine = self.engine

        def poster(row):
            if row.action in ("staking", "reward", "airdrop"):
                return False, f"'{row.action}' rows aren't supported for investment CSV import (crypto only)."
            if row.action == "split":
                # No auto-created holding for a split: you can't split
                # shares you were never recorded as holding. The engine
                # reports an unknown symbol clearly.
                return engine.record_stock_split(row.symbol, row.date, row.split_new, row.split_old)
            # Auto-create the holding on first sight of a new symbol,
            # using the report line/method chosen above -- matches the
            # "generic first cut, no per-provider assumptions" approach
            # csv_transaction_import.py takes.
            if engine.get_holding(row.symbol) is None:
                ok, msg = engine.add_holding(row.symbol, f"Investment: {row.symbol}", report_line, cost_basis_method=method)
                if not ok:
                    return ok, msg
            if row.action == "buy":
                return engine.buy(row.symbol, row.date, row.quantity, row.price, cash_account, fees=row.fee)
            elif row.action == "sell":
                return engine.sell(row.symbol, row.date, row.quantity, row.price, cash_account, fees=row.fee)
            elif row.action == "dividend":
                return engine.record_dividend(row.symbol, row.date, row.price, cash_account)
            elif row.action == "drip":
                return engine.record_dividend_reinvestment(row.symbol, row.date, row.amount, row.quantity)
            return False, f"'{row.action}' rows aren't supported for investment CSV import yet."

        self.import_manager.start_csv_import(poster, dialog_caption="Import Investment Transactions CSV")

    def _on_csv_imported(self):
        self.refresh_all()
        self.data_modified.emit()

    def _finish_action(self, ok: bool, msg: str):
        if ok:
            self.refresh_all()
            self.data_modified.emit()
        else:
            QMessageBox.warning(self, "Failed", msg)

    # ---- refresh ----

    def held_symbols(self) -> List[str]:
        return [h.symbol for h in self.engine.list_holdings()]

    def _on_void_last(self):
        symbol = self.undo_symbol.currentText()
        if not symbol:
            QMessageBox.information(self, "No holding", "Add a holding first.")
            return
        ok, msg = self.engine.void_last_transaction(symbol)
        self._finish_action(ok, msg)
        if ok:
            QMessageBox.information(self, "Undone", msg)

    def refresh_all(self):
        symbols = self.held_symbols()
        for combo in (self.buy_symbol, self.sell_symbol, self.div_symbol, self.drip_symbol, self.split_symbol, self.undo_symbol):
            current = combo.currentText()
            combo.clear()
            combo.addItems(symbols)
            idx = combo.findText(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)

        cash_names = self._cash_account_names()
        for combo in (self.buy_cash_account, self.sell_cash_account, self.div_cash_account, self.csv_cash_account):
            current = combo.currentText()
            combo.clear()
            combo.addItems(cash_names)
            idx = combo.findText(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)

        summary = self.engine.get_holdings_summary()
        self.holdings_table.setRowCount(len(summary))
        for r, row in enumerate(summary):
            last_price = price_history.get_latest_price(self.gl, row["symbol"])
            values = [
                row["symbol"],
                str(row["quantity_held"]),
                str(row["average_cost_per_unit"]),
                str(row["total_cost_basis"]),
                str(last_price) if last_price is not None else "—",
                row["cost_basis_method"],
            ]
            for c, v in enumerate(values):
                self.holdings_table.setItem(r, c, QTableWidgetItem(v))

        realized = self.engine.get_realized_gains()
        self.realized_table.setRowCount(len(realized))
        for r, row in enumerate(realized):
            values = [row["symbol"], row["date"], str(row["quantity"]), str(row["cost_basis"]), str(row["realized_gain"])]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == 4 and row["realized_gain"] < ZERO:
                    item.setForeground(Qt.GlobalColor.red)
                self.realized_table.setItem(r, c, item)


# =====================================================================
# Crypto sub-tab
# =====================================================================

class CryptoSubTab(QWidget):
    data_modified = pyqtSignal()

    def __init__(self, gl, parent=None):
        super().__init__(parent)
        self.gl = gl
        self.engine = CryptoEngine(gl)
        self.import_manager = InvestmentImportManager(self)
        self.import_manager.data_imported.connect(self._on_csv_imported)

        layout = QVBoxLayout(self)

        self.holdings_table = QTableWidget(0, 7)
        self.holdings_table.setHorizontalHeaderLabels(
            ["Asset", "Wallet", "Qty Held", "Avg Cost/Unit", "Cost Basis", "Last Price", "Method"]
        )
        self.holdings_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.holdings_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(QLabel("Holdings"))
        layout.addWidget(self.holdings_table)

        forms_row1 = QHBoxLayout()
        forms_row1.addWidget(self._build_add_wallet_box())
        forms_row1.addWidget(self._build_add_holding_box())
        forms_row1.addWidget(self._build_csv_import_box())
        layout.addLayout(forms_row1)

        forms_row2 = QHBoxLayout()
        forms_row2.addWidget(self._build_buy_box())
        forms_row2.addWidget(self._build_sell_box())
        forms_row2.addWidget(self._build_transfer_box())
        forms_row2.addWidget(self._build_undo_box())
        layout.addLayout(forms_row2)

        forms_row3 = QHBoxLayout()
        forms_row3.addWidget(self._build_reward_box())
        forms_row3.addStretch()
        layout.addLayout(forms_row3)

        self.realized_table = QTableWidget(0, 6)
        self.realized_table.setHorizontalHeaderLabels(
            ["Asset", "Wallet", "Date", "Quantity", "Cost Basis", "Realized Gain"]
        )
        self.realized_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.realized_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(QLabel("Realized Gains / Losses (includes in-kind network fees)"))
        layout.addWidget(self.realized_table)

        self.refresh_all()

    # ---- forms ----

    def _build_add_wallet_box(self) -> QGroupBox:
        box = QGroupBox("Add Wallet / Exchange Account")
        form = QFormLayout(box)
        self.aw_name = QLineEdit()
        self.aw_kind = QComboBox()
        self.aw_kind.addItems(["wallet", "exchange_account"])
        form.addRow("Name:", self.aw_name)
        form.addRow("Kind:", self.aw_kind)
        btn = QPushButton("Add Wallet")
        btn.clicked.connect(self._on_add_wallet)
        form.addRow(btn)
        return box

    def _build_add_holding_box(self) -> QGroupBox:
        box = QGroupBox("Add Holding")
        form = QFormLayout(box)
        self.ah_asset = QLineEdit()
        self.ah_wallet = QComboBox()
        self.ah_account_name = QLineEdit()
        self.ah_report_line = QComboBox()
        self.ah_report_line.addItems(_report_lines_for("Digital Assets"))
        self.ah_method = QComboBox()
        self.ah_method.addItems(CRYPTO_COST_METHODS)
        form.addRow("Asset (e.g. BTC):", self.ah_asset)
        form.addRow("Wallet:", self.ah_wallet)
        form.addRow("Account name:", self.ah_account_name)
        form.addRow("Report line:", self.ah_report_line)
        form.addRow("Cost-basis method:", self.ah_method)
        btn = QPushButton("Add Holding")
        btn.clicked.connect(self._on_add_holding)
        form.addRow(btn)
        return box

    def _build_buy_box(self) -> QGroupBox:
        box = QGroupBox("Buy")
        form = QFormLayout(box)
        self.buy_asset = QComboBox()
        self.buy_wallet = QComboBox()
        self.buy_date = QDateEdit(QDate.currentDate())
        self.buy_date.setCalendarPopup(True)
        self.buy_qty = QLineEdit()
        self.buy_price = QLineEdit()
        self.buy_fees = QLineEdit("0")
        self.buy_cash_account = QComboBox()
        form.addRow("Asset:", self.buy_asset)
        form.addRow("Wallet:", self.buy_wallet)
        form.addRow("Date:", self.buy_date)
        form.addRow("Quantity:", self.buy_qty)
        form.addRow("Price/unit:", self.buy_price)
        form.addRow("Fees:", self.buy_fees)
        form.addRow("Cash account:", self.buy_cash_account)
        btn = QPushButton("Record Buy")
        btn.clicked.connect(self._on_buy)
        form.addRow(btn)
        return box

    def _build_sell_box(self) -> QGroupBox:
        box = QGroupBox("Sell")
        form = QFormLayout(box)
        self.sell_asset = QComboBox()
        self.sell_wallet = QComboBox()
        self.sell_date = QDateEdit(QDate.currentDate())
        self.sell_date.setCalendarPopup(True)
        self.sell_qty = QLineEdit()
        self.sell_price = QLineEdit()
        self.sell_fees = QLineEdit("0")
        self.sell_cash_account = QComboBox()
        form.addRow("Asset:", self.sell_asset)
        form.addRow("Wallet:", self.sell_wallet)
        form.addRow("Date:", self.sell_date)
        form.addRow("Quantity:", self.sell_qty)
        form.addRow("Price/unit:", self.sell_price)
        form.addRow("Fees:", self.sell_fees)
        form.addRow("Cash account:", self.sell_cash_account)
        btn = QPushButton("Record Sell")
        btn.clicked.connect(self._on_sell)
        form.addRow(btn)
        return box

    def _build_transfer_box(self) -> QGroupBox:
        box = QGroupBox("Transfer Between Wallets")
        form = QFormLayout(box)
        self.tr_asset = QComboBox()
        self.tr_from = QComboBox()
        self.tr_to = QComboBox()
        self.tr_date = QDateEdit(QDate.currentDate())
        self.tr_date.setCalendarPopup(True)
        self.tr_qty = QLineEdit()
        self.tr_fee = QLineEdit("0")
        self.tr_fee_from_holding = QCheckBox("Fee paid in-kind (from the transferred asset)")
        self.tr_fee_from_holding.setChecked(True)
        form.addRow("Asset:", self.tr_asset)
        form.addRow("From wallet:", self.tr_from)
        form.addRow("To wallet:", self.tr_to)
        form.addRow("Date:", self.tr_date)
        form.addRow("Quantity:", self.tr_qty)
        form.addRow("Network fee (units):", self.tr_fee)
        form.addRow(self.tr_fee_from_holding)
        btn = QPushButton("Record Transfer")
        btn.clicked.connect(self._on_transfer)
        form.addRow(btn)
        return box

    def _build_reward_box(self) -> QGroupBox:
        box = QGroupBox("Staking / Reward / Airdrop Income")
        form = QFormLayout(box)
        note = QLabel(
            "Crypto received for free. Recorded as income at fair market "
            "value on the date received, and that value becomes the cost "
            "basis of the new units. Leave FMV blank to use imported "
            "price history (you'll be asked to confirm the price used)."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 10px;")
        form.addRow(note)
        self.rw_asset = QComboBox()
        self.rw_wallet = QComboBox()
        self.rw_date = QDateEdit(QDate.currentDate())
        self.rw_date.setCalendarPopup(True)
        self.rw_kind = QComboBox()
        self.rw_kind.addItems(list(VALID_REWARD_KINDS))
        self.rw_qty = QLineEdit()
        self.rw_fmv = QLineEdit()
        form.addRow("Asset:", self.rw_asset)
        form.addRow("Wallet:", self.rw_wallet)
        form.addRow("Date received:", self.rw_date)
        form.addRow("Type:", self.rw_kind)
        form.addRow("Quantity received:", self.rw_qty)
        form.addRow("FMV per unit:", self.rw_fmv)
        btn = QPushButton("Record Income")
        btn.clicked.connect(self._on_reward)
        form.addRow(btn)
        return box

    def _build_undo_box(self) -> QGroupBox:
        box = QGroupBox("Undo")
        form = QFormLayout(box)
        note = QLabel(
            "Reverses the most recent buy/sell/reward for the selected (asset, "
            "wallet) holding, in order. Transfers can't be auto-undone "
            "yet -- record a manual offsetting transfer instead."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 10px;")
        form.addRow(note)
        self.undo_asset = QComboBox()
        self.undo_wallet = QComboBox()
        form.addRow("Asset:", self.undo_asset)
        form.addRow("Wallet:", self.undo_wallet)
        btn = QPushButton("Void Most Recent Transaction")
        btn.clicked.connect(self._on_void_last)
        form.addRow(btn)
        return box

    def _build_csv_import_box(self) -> QGroupBox:
        box = QGroupBox("Import CSV")
        form = QFormLayout(box)
        note = QLabel(
            "Works with any exchange export -- you'll map its columns "
            "after picking the file. All rows in one import apply to "
            "the single wallet/exchange account chosen below; "
            "transfer rows aren't supported via CSV import yet (no "
            "counterpart wallet in a single row) -- use the Transfer "
            "form for those."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888; font-size: 10px;")
        form.addRow(note)
        self.csv_wallet = QComboBox()
        self.csv_cash_account = QComboBox()
        self.csv_report_line = QComboBox()
        self.csv_report_line.addItems(_report_lines_for("Digital Assets"))
        self.csv_method = QComboBox()
        self.csv_method.addItems(CRYPTO_COST_METHODS)
        form.addRow("Wallet/exchange account:", self.csv_wallet)
        form.addRow("Cash account:", self.csv_cash_account)
        form.addRow("New-holding report line:", self.csv_report_line)
        form.addRow("New-holding cost method:", self.csv_method)
        btn = QPushButton("Import CSV...")
        btn.clicked.connect(self._on_import_csv)
        form.addRow(btn)
        return box

    # ---- actions ----

    def _cash_account_names(self) -> List[str]:
        return sorted(name for name, acc in self.gl.accounts.items() if acc.account_type == "Asset")

    def _on_add_wallet(self):
        name = self.aw_name.text().strip()
        kind = self.aw_kind.currentText()
        if not name:
            QMessageBox.warning(self, "Missing info", "Wallet name is required.")
            return
        ok, msg = self.engine.add_wallet(name, kind=kind)
        self._finish_action(ok, msg)
        if ok:
            self.aw_name.clear()

    def _on_add_holding(self):
        asset = self.ah_asset.text().strip()
        wallet = self.ah_wallet.currentText()
        account_name = self.ah_account_name.text().strip()
        report_line = self.ah_report_line.currentText()
        method = self.ah_method.currentText()
        if not asset or not wallet or not account_name:
            QMessageBox.warning(self, "Missing info", "Asset, wallet, and account name are required.")
            return
        ok, msg = self.engine.add_holding(asset, wallet, account_name, report_line, cost_basis_method=method)
        self._finish_action(ok, msg)
        if ok:
            self.ah_asset.clear()
            self.ah_account_name.clear()

    def _on_buy(self):
        try:
            asset = self.buy_asset.currentText()
            wallet = self.buy_wallet.currentText()
            qty = _decimal_from_field(self.buy_qty.text(), "Quantity")
            price = _decimal_from_field(self.buy_price.text(), "Price")
            fees = _decimal_from_field(self.buy_fees.text() or "0", "Fees")
            cash_account = self.buy_cash_account.currentText()
            date = self.buy_date.date().toString("yyyy-MM-dd")
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        ok, msg = self.engine.buy(asset, wallet, date, qty, price, cash_account, fees=fees)
        self._finish_action(ok, msg)

    def _latest_price_point(self, asset: str, date: str):
        """Latest imported/fetched price on or before `date`, with its
        own date and source so the user can see exactly what is being
        proposed. None if there's nothing on or before that date."""
        history = [p for p in price_history.get_price_history(self.gl, asset) if p["date"] <= date]
        return history[-1] if history else None

    def _on_reward(self):
        try:
            asset = self.rw_asset.currentText()
            wallet = self.rw_wallet.currentText()
            kind = self.rw_kind.currentText()
            qty = _decimal_from_field(self.rw_qty.text(), "Quantity")
            date = self.rw_date.date().toString("yyyy-MM-dd")
            fmv_text = self.rw_fmv.text().strip()
            fmv = _decimal_from_field(fmv_text, "FMV per unit") if fmv_text else None
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        if fmv is None:
            point = self._latest_price_point(asset, date)
            if point is None:
                QMessageBox.warning(
                    self, "No price on file",
                    f"No price for {asset} on or before {date}. Enter the fair market "
                    f"value per unit, or import price history first.",
                )
                return
            answer = QMessageBox.question(
                self, "Confirm price",
                f"No FMV entered. Use {point['price']} per unit, the latest {asset} price on "
                f"file ({point['date']}, source: {point['source']})?\n\n"
                f"This sets both the income recorded and the cost basis of the received units.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            fmv = point["price"]
        ok, msg = self.engine.record_staking_reward(asset, wallet, date, qty, fmv, kind=kind)
        self._finish_action(ok, msg)

    def _on_sell(self):
        try:
            asset = self.sell_asset.currentText()
            wallet = self.sell_wallet.currentText()
            qty = _decimal_from_field(self.sell_qty.text(), "Quantity")
            price = _decimal_from_field(self.sell_price.text(), "Price")
            fees = _decimal_from_field(self.sell_fees.text() or "0", "Fees")
            cash_account = self.sell_cash_account.currentText()
            date = self.sell_date.date().toString("yyyy-MM-dd")
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        ok, msg = self.engine.sell(asset, wallet, date, qty, price, cash_account, fees=fees)
        self._finish_action(ok, msg)

    def _on_transfer(self):
        try:
            asset = self.tr_asset.currentText()
            from_wallet = self.tr_from.currentText()
            to_wallet = self.tr_to.currentText()
            qty = _decimal_from_field(self.tr_qty.text(), "Quantity")
            fee = _decimal_from_field(self.tr_fee.text() or "0", "Network fee")
            date = self.tr_date.date().toString("yyyy-MM-dd")
        except ValueError as e:
            QMessageBox.warning(self, "Invalid input", str(e))
            return
        if from_wallet == to_wallet:
            QMessageBox.warning(self, "Invalid transfer", "From and To wallets must differ.")
            return
        ok, msg = self.engine.transfer(
            asset, date, qty, from_wallet, to_wallet,
            network_fee=fee, fee_paid_from_holding=self.tr_fee_from_holding.isChecked(),
        )
        self._finish_action(ok, msg)

    def _on_import_csv(self):
        wallet = self.csv_wallet.currentText()
        cash_account = self.csv_cash_account.currentText()
        report_line = self.csv_report_line.currentText()
        method = self.csv_method.currentText()
        if not wallet:
            QMessageBox.warning(self, "No wallet", "Add a wallet/exchange account first.")
            return
        if not cash_account:
            QMessageBox.warning(self, "No cash account", "Add a cash/asset account first.")
            return

        engine = self.engine

        def poster(row):
            if row.action in ("transfer_in", "transfer_out"):
                return False, (
                    "Transfer rows aren't supported via CSV import (no counterpart "
                    "wallet in a single row) -- use the Transfer form instead."
                )
            if row.action in ("drip", "split"):
                return False, f"'{row.action}' rows aren't supported for crypto CSV import."
            if engine.get_holding(row.symbol, wallet) is None:
                account_name = f"Crypto: {row.symbol} ({wallet})"
                ok, msg = engine.add_holding(row.symbol, wallet, account_name, report_line, cost_basis_method=method)
                if not ok:
                    return ok, msg
            if row.action == "buy":
                return engine.buy(row.symbol, wallet, row.date, row.quantity, row.price, cash_account, fees=row.fee)
            elif row.action == "sell":
                return engine.sell(row.symbol, wallet, row.date, row.quantity, row.price, cash_account, fees=row.fee)
            elif row.action in ("staking", "reward", "airdrop"):
                # row.price is the fair market value per unit (parser
                # guarantees it is positive for these actions).
                return engine.record_staking_reward(
                    row.symbol, wallet, row.date, row.quantity, row.price, kind=row.action,
                )
            return False, f"'{row.action}' rows aren't supported for crypto CSV import yet."

        self.import_manager.start_csv_import(poster, dialog_caption="Import Crypto Transactions CSV")

    def _on_csv_imported(self):
        self.refresh_all()
        self.data_modified.emit()

    def _finish_action(self, ok: bool, msg: str):
        if ok:
            self.refresh_all()
            self.data_modified.emit()
        else:
            QMessageBox.warning(self, "Failed", msg)

    # ---- refresh ----

    def held_assets(self) -> List[str]:
        return sorted({h.asset for h in self.engine.list_holdings()})

    def _on_void_last(self):
        asset = self.undo_asset.currentText()
        wallet = self.undo_wallet.currentText()
        if not asset or not wallet:
            QMessageBox.information(self, "No holding", "Add a holding first.")
            return
        ok, msg = self.engine.void_last_transaction(asset, wallet)
        self._finish_action(ok, msg)
        if ok:
            QMessageBox.information(self, "Undone", msg)

    def refresh_all(self):
        wallets = [w.name for w in self.engine.list_wallets()]
        for combo in (self.ah_wallet, self.buy_wallet, self.sell_wallet, self.tr_from, self.tr_to, self.csv_wallet, self.undo_wallet, self.rw_wallet):
            current = combo.currentText()
            combo.clear()
            combo.addItems(wallets)
            idx = combo.findText(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)

        assets = self.held_assets()
        for combo in (self.buy_asset, self.sell_asset, self.tr_asset, self.undo_asset, self.rw_asset):
            current = combo.currentText()
            combo.clear()
            combo.addItems(assets)
            idx = combo.findText(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)

        cash_names = self._cash_account_names()
        for combo in (self.buy_cash_account, self.sell_cash_account, self.csv_cash_account):
            current = combo.currentText()
            combo.clear()
            combo.addItems(cash_names)
            idx = combo.findText(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)

        summary = self.engine.get_holdings_summary()
        self.holdings_table.setRowCount(len(summary))
        for r, row in enumerate(summary):
            last_price = price_history.get_latest_price(self.gl, row["asset"])
            values = [
                row["asset"], row["wallet"], str(row["quantity_held"]),
                str(row["average_cost_per_unit"]), str(row["total_cost_basis"]),
                str(last_price) if last_price is not None else "—",
                row["cost_basis_method"],
            ]
            for c, v in enumerate(values):
                self.holdings_table.setItem(r, c, QTableWidgetItem(v))

        realized = self.engine.get_realized_gains()
        self.realized_table.setRowCount(len(realized))
        for r, row in enumerate(realized):
            values = [row["asset"], row["wallet"], row["date"], str(row["quantity"]),
                      str(row["cost_basis"]), str(row["realized_gain"])]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == 5 and row["realized_gain"] < ZERO:
                    item.setForeground(Qt.GlobalColor.red)
                self.realized_table.setItem(r, c, item)


# =====================================================================
# Outer tab: "Investments & Crypto"
# =====================================================================

class InvestmentsAndCryptoTab(QWidget):
    """Drop-in tab for BudgetApp, following the same constructor/signal
    convention as the other tabs in ui_main.py:

        self.tab_investments = InvestmentsAndCryptoTab(self.gl)
        ...
        self.stacked_widget.addWidget(self.tab_investments)
        ...
        self.tab_investments.data_modified.connect(self._refresh_all_data)

    and add "Investments & Crypto" to the `tab_names` list passed to
    CollapsibleSidebar, in the same position.
    """

    data_modified = pyqtSignal()

    def __init__(self, gl, parent=None):
        super().__init__(parent)
        self.gl = gl

        layout = QVBoxLayout(self)

        self.sub_tabs = QTabWidget()
        self.investments_subtab = InvestmentsSubTab(gl)
        self.crypto_subtab = CryptoSubTab(gl)
        self.sub_tabs.addTab(self.investments_subtab, "Investments")
        self.sub_tabs.addTab(self.crypto_subtab, "Crypto")
        layout.addWidget(self.sub_tabs)

        self.price_panel = PriceRefreshPanel(
            gl,
            get_stock_symbols=self.investments_subtab.held_symbols,
            get_crypto_symbols=self.crypto_subtab.held_assets,
        )
        layout.addWidget(self.price_panel)

        self.investments_subtab.data_modified.connect(self.data_modified)
        self.crypto_subtab.data_modified.connect(self.data_modified)
        self.price_panel.prices_updated.connect(self._on_prices_updated)

    def _on_prices_updated(self):
        self.investments_subtab.refresh_all()
        self.crypto_subtab.refresh_all()

    def refresh_all(self):
        """Called by _refresh_all_data() in ui_main.py, same as other
        tabs' refresh methods."""
        self.investments_subtab.refresh_all()
        self.crypto_subtab.refresh_all()
