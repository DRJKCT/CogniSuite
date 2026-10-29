# crypto_engine.py
# For CogniFinance -- Crypto module
#
# Fully separate from investment_engine.py by deliberate architectural
# decision (see the Investment & Crypto planning brief): this file
# carries crypto's messier feature surface -- wallet vs. exchange-
# account distinction, on-chain transfers, per-wallet lot tracking
# (US filers must track cost basis per-wallet/per-account since 2025,
# not pooled) -- without that complexity leaking into
# investment_engine.py, and without investment's simpler model
# constraining this one. The only things shared between the two
# engines are lot_matcher.py (pure FIFO/specific-lot math) and posting
# through gl.record_transaction() like everything else.
#
# v1 scope (see open question #1 in the planning brief):
#   - Buy, sell, transfer between wallets.
#   - Staking/reward/airdrop income via record_staking_reward(): income
#     at fair-market-value on receipt, which becomes the new lot's
#     cost basis. The caller supplies the FMV; this engine never looks
#     one up itself.
#   - NOT handled: a stake/unstake operation (the "Crypto — Staked"
#     report line only classifies a holding), LP/yield positions, and
#     zero-value receipts.
#
# Zero jurisdiction knowledge here either -- same reasoning as
# investment_engine.py. Per-wallet lot tracking in v1 is a deliberate
# choice that happens to match current US rules, not this module
# claiming to know the rules; other jurisdictions' pooling rules are
# the template layer's problem to reconcile against these lots.

import json
import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Tuple, Optional

from lot_matcher import Lot, consume_fifo, consume_specific_lots, LotMatcherError
from currency import to_base, parse_decimal, normalize_currency

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")

REALIZED_GAINS_REPORT_LINE = "Crypto — Realized Gains"
REALIZED_LOSSES_ACCOUNT = "Realized Losses — Crypto"
REALIZED_LOSSES_REPORT_LINE = "Crypto — Realized Losses"
REALIZED_GAINS_ACCOUNT = "Realized Gains — Crypto"
STAKING_INCOME_ACCOUNT = "Staking Income — Crypto"
STAKING_INCOME_REPORT_LINE = "Crypto — Staking Income"

# Staking, protocol rewards and airdrops all post to the one income
# account (the report line is the template layer's grouping handle);
# `kind` only labels the ledger description.
VALID_REWARD_KINDS = ("staking", "reward", "airdrop")

VALID_COST_BASIS_METHODS = ("FIFO", "SPECIFIC")


class CryptoEngineError(ValueError):
    pass


@dataclass
class Wallet:
    id: int
    name: str
    kind: str  # "wallet" or "exchange_account" -- descriptive only, no behavior differs yet


@dataclass
class Holding:
    """One (asset, wallet) pair -- e.g. BTC held in "Coinbase" is a
    different Holding from BTC held in "Ledger Nano", each with its own
    lots. This is what per-wallet cost-basis tracking requires."""
    id: int
    asset: str
    wallet_id: int
    account_name: str
    cost_basis_method: str


class CryptoEngine:
    """Wraps a GeneralLedger connection with per-(asset, wallet) lot
    tracking. Every dollar movement posts through
    gl.record_transaction(); quantities and per-unit cost basis live in
    this module's own tables, same pattern as investment_engine.py."""

    def __init__(self, gl):
        self.gl = gl
        self._init_db()
        self._ensure_shared_accounts_exist()

    # ---------------------------------------------------------------
    # Schema
    # ---------------------------------------------------------------

    def _init_db(self):
        conn, cur = self.gl._connect()
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sql_crypto_wallets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    kind TEXT DEFAULT 'wallet'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sql_crypto_holdings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset TEXT NOT NULL,
                    wallet_id INTEGER NOT NULL,
                    account_name TEXT UNIQUE,
                    cost_basis_method TEXT DEFAULT 'FIFO',
                    UNIQUE(asset, wallet_id),
                    FOREIGN KEY(wallet_id) REFERENCES sql_crypto_wallets(id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sql_crypto_lots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    holding_id INTEGER NOT NULL,
                    date_acquired TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    cost_basis_per_unit TEXT NOT NULL,
                    acquiring_tx_id INTEGER,
                    FOREIGN KEY(holding_id) REFERENCES sql_crypto_holdings(id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sql_crypto_realized (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    holding_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    cost_basis TEXT NOT NULL,
                    proceeds TEXT NOT NULL,
                    realized_gain TEXT NOT NULL,
                    disposing_tx_id INTEGER,
                    FOREIGN KEY(holding_id) REFERENCES sql_crypto_holdings(id)
                )
            """)
            # Append-only undo log, same role as investment_engine.py's
            # sql_investment_tx_log -- see void_last_transaction() for
            # buy/sell. transfer rows ARE logged here too (so the
            # ledger-level guard in ledger.py still protects them from
            # being voided/edited through the generic Transactions tab)
            # even though void_last_transaction() itself refuses to
            # auto-undo a transfer in v1 -- a transfer spans two
            # holdings, and safely restoring both sides' lot state
            # together (with correct LIFO ordering checked
            # independently per holding) is a real scope decision, not
            # something to guess at under this task's time budget.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sql_crypto_tx_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    holding_id INTEGER NOT NULL,
                    tx_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    lots_before TEXT NOT NULL,
                    undone INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(holding_id) REFERENCES sql_crypto_holdings(id)
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _ensure_shared_accounts_exist(self):
        for name, acc_type, report_line in (
            (REALIZED_GAINS_ACCOUNT, "Revenue", REALIZED_GAINS_REPORT_LINE),
            (REALIZED_LOSSES_ACCOUNT, "Expense", REALIZED_LOSSES_REPORT_LINE),
            (STAKING_INCOME_ACCOUNT, "Revenue", STAKING_INCOME_REPORT_LINE),
        ):
            if name not in self.gl.accounts:
                ok, msg = self.gl.create_account(name, acc_type, report_line)
                if not ok:
                    logger.warning(f"CryptoEngine could not create '{name}': {msg}")

    # ---------------------------------------------------------------
    # Multi-currency
    #
    # Rules (same in both engines):
    #   * Lots, cost basis, proceeds and realized gains are ALWAYS held
    #     in the ledger's BASE currency. lot_matcher never sees anything else.
    #   * Price and fees passed to buy()/sell(), and the amount passed to
    #     record_dividend(), are in the CASH ACCOUNT's own currency (what
    #     actually left or arrived). If that account is foreign, the amount
    #     is converted to base at the rate for the transaction date (or an
    #     explicit fx_rate=), and the cash leg posts through the ledger's
    #     FxInfo so the native amount and rate are stored on the line.
    #   * Holding accounts stay in the base currency, carried at cost.
    # ---------------------------------------------------------------

    def _cash_leg(self, cash_account, native_amount, date, fx_rate=None, fx_rate_source=None):
        """(base_amount, fx) for the cash side of a trade. fx is None --
        and the amount is returned untouched -- for a base-currency or
        unknown account (the ledger reports a missing account itself).
        Raises ValueError (e.g. no rate on file) for the caller to return."""
        acc = self.gl.accounts.get(cash_account)
        if acc is None or acc.currency == self.gl.base_currency:
            return native_amount, None
        return self.gl.convert_to_base(cash_account, native_amount, date, fx_rate, fx_rate_source)

    def _amount_to_base(self, amount, currency, date, fx_rate=None):
        """Convert an amount that has no cash account to carry its currency
        (a reinvested dividend, a reward's fair market value). Returns
        (base_amount, note); note is text for the ledger description so the
        native amount and rate stay visible. currency=None means base."""
        if not currency:
            return amount, ""
        code = normalize_currency(currency)
        if code == self.gl.base_currency:
            return amount, ""
        if fx_rate is not None:
            rate = parse_decimal(fx_rate, "rate")
            if rate <= ZERO:
                raise ValueError("Rate must be greater than zero.")
        else:
            found = self.gl.get_fx_rate(code, self.gl.base_currency, date)
            if found is None:
                raise ValueError(
                    f"No {code}->{self.gl.base_currency} rate on or before {date}. "
                    "Add one with set_fx_rate() or pass fx_rate=."
                )
            rate = found.rate
        return to_base(amount, rate), f" [{amount} {code} @ {rate}]"

    # ---------------------------------------------------------------
    # Wallets
    # ---------------------------------------------------------------

    def add_wallet(self, name: str, kind: str = "wallet") -> Tuple[bool, str]:
        if kind not in ("wallet", "exchange_account"):
            return False, "kind must be 'wallet' or 'exchange_account'."
        conn, cur = self.gl._connect()
        try:
            cur.execute("INSERT INTO sql_crypto_wallets (name, kind) VALUES (?, ?)", (name, kind))
            conn.commit()
            return True, f"Wallet '{name}' added."
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    def get_wallet(self, name: str) -> Optional[Wallet]:
        conn, cur = self.gl._connect()
        try:
            cur.execute("SELECT id, name, kind FROM sql_crypto_wallets WHERE name = ?", (name,))
            row = cur.fetchone()
            return Wallet(*row) if row else None
        finally:
            conn.close()

    def list_wallets(self) -> List[Wallet]:
        conn, cur = self.gl._connect()
        try:
            cur.execute("SELECT id, name, kind FROM sql_crypto_wallets")
            return [Wallet(*row) for row in cur.fetchall()]
        finally:
            conn.close()

    # ---------------------------------------------------------------
    # Holdings (asset held in a specific wallet)
    # ---------------------------------------------------------------

    def add_holding(self, asset: str, wallet_name: str, account_name: str, report_line: str,
                     cost_basis_method: str = "FIFO") -> Tuple[bool, str]:
        asset = asset.strip().upper()
        wallet = self.get_wallet(wallet_name)
        if wallet is None:
            return False, f"Unknown wallet '{wallet_name}'. Call add_wallet() first."
        if cost_basis_method not in VALID_COST_BASIS_METHODS:
            return False, f"Invalid cost basis method '{cost_basis_method}'."
        if self.get_holding(asset, wallet_name) is not None:
            return False, f"Holding '{asset}' in wallet '{wallet_name}' already exists."

        ok, msg = self.gl.create_account(account_name, "Asset", report_line)
        if not ok:
            return False, f"Could not create holding account: {msg}"

        conn, cur = self.gl._connect()
        try:
            cur.execute(
                "INSERT INTO sql_crypto_holdings (asset, wallet_id, account_name, cost_basis_method) VALUES (?, ?, ?, ?)",
                (asset, wallet.id, account_name, cost_basis_method),
            )
            conn.commit()
            return True, f"Holding '{asset}' in '{wallet_name}' added."
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    def get_holding(self, asset: str, wallet_name: str) -> Optional[Holding]:
        asset = asset.strip().upper()
        wallet = self.get_wallet(wallet_name)
        if wallet is None:
            return None
        conn, cur = self.gl._connect()
        try:
            cur.execute(
                "SELECT id, asset, wallet_id, account_name, cost_basis_method FROM sql_crypto_holdings "
                "WHERE asset = ? AND wallet_id = ?",
                (asset, wallet.id),
            )
            row = cur.fetchone()
            return Holding(*row) if row else None
        finally:
            conn.close()

    def list_holdings(self) -> List[Holding]:
        conn, cur = self.gl._connect()
        try:
            cur.execute("SELECT id, asset, wallet_id, account_name, cost_basis_method FROM sql_crypto_holdings")
            return [Holding(*row) for row in cur.fetchall()]
        finally:
            conn.close()

    def _open_lots(self, cur, holding_id: int) -> List[Lot]:
        cur.execute(
            "SELECT id, date_acquired, quantity, cost_basis_per_unit FROM sql_crypto_lots WHERE holding_id = ?",
            (holding_id,),
        )
        return [
            Lot(id=str(r[0]), date_acquired=r[1], quantity=Decimal(r[2]), cost_basis_per_unit=Decimal(r[3]))
            for r in cur.fetchall()
        ]

    def get_open_lots(self, asset: str, wallet_name: str) -> List[Lot]:
        holding = self.get_holding(asset, wallet_name)
        if holding is None:
            raise CryptoEngineError(f"Unknown holding '{asset}' in wallet '{wallet_name}'.")
        conn, cur = self.gl._connect()
        try:
            return self._open_lots(cur, holding.id)
        finally:
            conn.close()

    # ---------------------------------------------------------------
    # Undo log (append-only -- see void_last_transaction())
    # ---------------------------------------------------------------

    @staticmethod
    def _serialize_lots(lots: List[Lot]) -> str:
        return json.dumps([
            {"date_acquired": l.date_acquired, "quantity": str(l.quantity),
             "cost_basis_per_unit": str(l.cost_basis_per_unit)}
            for l in lots
        ])

    @staticmethod
    def _deserialize_lots(raw: str) -> List[Lot]:
        data = json.loads(raw)
        return [
            Lot(id="restored", date_acquired=d["date_acquired"],
                quantity=Decimal(d["quantity"]), cost_basis_per_unit=Decimal(d["cost_basis_per_unit"]))
            for d in data
        ]

    def _log_undo_snapshot(self, cur, holding_id: int, tx_id: Optional[int], action: str, lots_before: List[Lot]):
        if tx_id is None:
            return
        cur.execute(
            "INSERT INTO sql_crypto_tx_log (holding_id, tx_id, action, lots_before) VALUES (?, ?, ?, ?)",
            (holding_id, tx_id, action, self._serialize_lots(lots_before)),
        )

    # ---------------------------------------------------------------
    # Buy / Sell / Transfer
    # ---------------------------------------------------------------

    def buy(self, asset: str, wallet_name: str, date: str, quantity: Decimal, price_per_unit: Decimal,
            cash_account: str, fees: Decimal = ZERO, description: Optional[str] = None,
            fx_rate: Optional[Decimal] = None, fx_rate_source: Optional[str] = None) -> Tuple[bool, str]:
        # price_per_unit and fees are in the CASH ACCOUNT's currency; a foreign
        # account is converted to base at the rate for `date` (or fx_rate).
        holding = self.get_holding(asset, wallet_name)
        if holding is None:
            return False, f"Unknown holding '{asset}' in wallet '{wallet_name}'. Call add_holding() first."
        if quantity <= ZERO:
            return False, "Quantity must be positive."
        if price_per_unit < ZERO or fees < ZERO:
            return False, "Price and fees cannot be negative."

        total_native = (quantity * price_per_unit) + fees
        try:
            total_cost, fx = self._cash_leg(cash_account, total_native, date, fx_rate, fx_rate_source)
        except ValueError as e:
            return False, str(e)
        ccy = f" {fx.currency}" if fx else ""
        desc = description or f"Buy {quantity} {asset} ({wallet_name}) @ {price_per_unit}{ccy}"

        ok, msg = self.gl.record_transaction(
            date, desc,
            [(holding.account_name, total_cost, ZERO), (cash_account, ZERO, total_cost, fx)],
        )
        if not ok:
            return False, msg

        tx = self.gl.get_last_transaction()
        cost_basis_per_unit = total_cost / quantity

        conn, cur = self.gl._connect()
        try:
            lots_before = self._open_lots(cur, holding.id)
            self._log_undo_snapshot(cur, holding.id, tx.id if tx else None, "buy", lots_before)
            cur.execute(
                "INSERT INTO sql_crypto_lots (holding_id, date_acquired, quantity, cost_basis_per_unit, acquiring_tx_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (holding.id, date, str(quantity), str(cost_basis_per_unit), tx.id if tx else None),
            )
            conn.commit()
            return True, f"Bought {quantity} {asset} into '{wallet_name}'."
        except Exception as e:
            conn.rollback()
            logger.error(f"CryptoEngine: ledger posted buy for '{asset}' but lot insert failed: {e}")
            return False, f"Ledger entry posted but lot tracking failed: {e}"
        finally:
            conn.close()

    def sell(self, asset: str, wallet_name: str, date: str, quantity: Decimal, price_per_unit: Decimal,
              cash_account: str, fees: Decimal = ZERO,
              specific_selections: Optional[List[Tuple[str, Decimal]]] = None,
              description: Optional[str] = None,
              fx_rate: Optional[Decimal] = None, fx_rate_source: Optional[str] = None) -> Tuple[bool, str]:
        # price_per_unit and fees are in the CASH ACCOUNT's currency. A foreign
        # account's proceeds are converted to base at the rate for `date` (or
        # fx_rate); the gain is computed entirely in base against base cost.
        holding = self.get_holding(asset, wallet_name)
        if holding is None:
            return False, f"Unknown holding '{asset}' in wallet '{wallet_name}'."
        if quantity <= ZERO:
            return False, "Quantity must be positive."
        if price_per_unit < ZERO or fees < ZERO:
            return False, "Price and fees cannot be negative."

        conn, cur = self.gl._connect()
        try:
            open_lots = self._open_lots(cur, holding.id)
        finally:
            conn.close()

        gross_native = (quantity * price_per_unit) - fees
        try:
            gross_base, fx = self._cash_leg(cash_account, gross_native, date, fx_rate, fx_rate_source)
        except ValueError as e:
            return False, str(e)
        if fx is not None:
            net_proceeds_per_unit = gross_base / quantity
        else:
            net_proceeds_per_unit = price_per_unit - (fees / quantity)

        try:
            if specific_selections is not None:
                match = consume_specific_lots(open_lots, specific_selections, net_proceeds_per_unit)
            elif holding.cost_basis_method == "FIFO":
                match = consume_fifo(open_lots, quantity, net_proceeds_per_unit)
            else:
                return False, (
                    f"Holding '{asset}' in '{wallet_name}' uses SPECIFIC-lot identification; "
                    f"specific_selections is required."
                )
        except LotMatcherError as e:
            return False, str(e)

        # Round proceeds and cost basis to cents FIRST (matching the
        # ledger's own money() convention), then derive gain as their
        # difference -- see investment_engine.py's sell() for why
        # rounding all three independently can drift the books a cent
        # out of balance when fees don't divide evenly across quantity.
        cost_basis = match.total_cost_basis.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        gross_proceeds = gross_base.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        gain = gross_proceeds - cost_basis

        lines = [
            (cash_account, gross_proceeds, ZERO, fx),
            (holding.account_name, ZERO, cost_basis),
        ]
        if gain > ZERO:
            lines.append((REALIZED_GAINS_ACCOUNT, ZERO, gain))
        elif gain < ZERO:
            lines.append((REALIZED_LOSSES_ACCOUNT, -gain, ZERO))

        desc = description or f"Sell {quantity} {asset} ({wallet_name}) @ {price_per_unit}{' ' + fx.currency if fx else ''}"
        ok, msg = self.gl.record_transaction(date, desc, lines)
        if not ok:
            return False, msg

        tx = self.gl.get_last_transaction()

        conn, cur = self.gl._connect()
        try:
            self._log_undo_snapshot(cur, holding.id, tx.id if tx else None, "sell", open_lots)
            cur.execute("DELETE FROM sql_crypto_lots WHERE holding_id = ?", (holding.id,))
            for lot in match.remaining_lots:
                cur.execute(
                    "INSERT INTO sql_crypto_lots (holding_id, date_acquired, quantity, cost_basis_per_unit, acquiring_tx_id) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (holding.id, lot.date_acquired, str(lot.quantity), str(lot.cost_basis_per_unit)),
                )
            cur.execute(
                "INSERT INTO sql_crypto_realized (holding_id, date, quantity, cost_basis, proceeds, realized_gain, disposing_tx_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (holding.id, date, str(quantity), str(cost_basis), str(gross_proceeds), str(gain), tx.id if tx else None),
            )
            conn.commit()
            return True, f"Sold {quantity} {asset} from '{wallet_name}' (realized {'gain' if gain >= ZERO else 'loss'} {abs(gain)})."
        except Exception as e:
            conn.rollback()
            logger.error(f"CryptoEngine: ledger posted sale for '{asset}' but lot update failed: {e}")
            return False, f"Ledger entry posted but lot tracking failed: {e}"
        finally:
            conn.close()

    def transfer(self, asset: str, date: str, quantity: Decimal, from_wallet: str, to_wallet: str,
                 network_fee: Decimal = ZERO, fee_paid_from_holding: bool = True,
                 description: Optional[str] = None) -> Tuple[bool, str]:
        """Move units between wallets you own. Not a disposal -- no
        gain/loss, lots move with their original cost basis and
        acquisition date intact (this matters: a same-day transfer
        should never reset someone's long-term holding period).

        A network fee, if any, is itself a small disposal of `asset`
        (you're paying gas in-kind) when `fee_paid_from_holding` is
        True -- handled as a FIFO consumption of just the fee amount,
        booked as a realized gain/loss like any other disposal, before
        the remaining quantity transfers fee-free. If the fee was paid
        from a separate cash/fiat balance instead, set
        fee_paid_from_holding=False and pass network_fee=0 here (record
        that cash payment yourself via record_transaction)."""
        from_holding = self.get_holding(asset, from_wallet)
        to_holding = self.get_holding(asset, to_wallet)
        if from_holding is None:
            return False, f"Unknown holding '{asset}' in wallet '{from_wallet}'."
        if to_holding is None:
            return False, f"Unknown holding '{asset}' in wallet '{to_wallet}'. Call add_holding() first."
        if quantity <= ZERO:
            return False, "Quantity must be positive."
        if network_fee < ZERO:
            return False, "Network fee cannot be negative."

        conn, cur = self.gl._connect()
        try:
            open_lots = self._open_lots(cur, from_holding.id)
        finally:
            conn.close()

        fee_qty = network_fee if fee_paid_from_holding else ZERO
        total_needed = quantity + fee_qty
        available = sum((l.quantity for l in open_lots), ZERO)
        if total_needed > available:
            return False, f"Cannot transfer {quantity} plus {fee_qty} fee; only {available} available in '{from_wallet}'."

        # Consume the fee portion first (FIFO; a network fee is small
        # and jurisdictions don't offer a specific-lot choice for gas),
        # then carry the transferred portion's lots over unchanged.
        try:
            if fee_qty > ZERO:
                fee_match = consume_fifo(open_lots, fee_qty, ZERO)  # gas has no proceeds; whole cost basis is a loss
                lots_after_fee = fee_match.remaining_lots
            else:
                fee_match = None
                lots_after_fee = open_lots
            transfer_match = consume_fifo(lots_after_fee, quantity, ZERO)  # proceeds_per_unit unused: no disposal booked for these
        except LotMatcherError as e:
            return False, str(e)

        desc = description or f"Transfer {quantity} {asset}: {from_wallet} → {to_wallet}"
        gas_loss = -fee_match.total_realized_gain if fee_match else ZERO  # cost basis given up as gas, expressed as a positive loss amount

        lines = []
        if gas_loss > ZERO:
            lines.append((REALIZED_LOSSES_ACCOUNT, gas_loss, ZERO))
            lines.append((from_holding.account_name, ZERO, gas_loss))
        # The transferred quantity's dollar value moves from one Asset
        # account to another at its existing cost basis -- no gain/loss.
        transferred_value = transfer_match.total_cost_basis
        if transferred_value > ZERO:
            lines.append((to_holding.account_name, transferred_value, ZERO))
            lines.append((from_holding.account_name, ZERO, transferred_value))

        if not lines:
            return False, "Nothing to transfer (zero quantity and zero fee)."

        ok, msg = self.gl.record_transaction(date, desc, lines)
        if not ok:
            return False, msg

        tx = self.gl.get_last_transaction()

        conn, cur = self.gl._connect()
        try:
            # Log both sides' pre-transfer snapshots under the same
            # tx_id -- purely so the ledger-level guard protects this
            # transaction from being voided/edited through the generic
            # Transactions tab. void_last_transaction() explicitly
            # refuses to auto-undo a "transfer" entry (see its
            # docstring); these log rows exist for the guard, not to
            # promise an undo path.
            to_holding_lots_before = self._open_lots(cur, to_holding.id)
            self._log_undo_snapshot(cur, from_holding.id, tx.id if tx else None, "transfer", open_lots)
            self._log_undo_snapshot(cur, to_holding.id, tx.id if tx else None, "transfer", to_holding_lots_before)
            # from_wallet: replace with whatever's left after fee + transfer
            cur.execute("DELETE FROM sql_crypto_lots WHERE holding_id = ?", (from_holding.id,))
            for lot in transfer_match.remaining_lots:
                cur.execute(
                    "INSERT INTO sql_crypto_lots (holding_id, date_acquired, quantity, cost_basis_per_unit, acquiring_tx_id) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (from_holding.id, lot.date_acquired, str(lot.quantity), str(lot.cost_basis_per_unit)),
                )
            # to_wallet: add the transferred lots, preserving original
            # acquisition dates and cost basis per unit.
            for c in transfer_match.consumptions:
                cur.execute(
                    "INSERT INTO sql_crypto_lots (holding_id, date_acquired, quantity, cost_basis_per_unit, acquiring_tx_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (to_holding.id, c.date_acquired, str(c.quantity), str(c.cost_basis_per_unit), tx.id if tx else None),
                )
            if fee_match:
                cur.execute(
                    "INSERT INTO sql_crypto_realized (holding_id, date, quantity, cost_basis, proceeds, realized_gain, disposing_tx_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (from_holding.id, date, str(fee_qty), str(fee_match.total_cost_basis), "0.00",
                     str(fee_match.total_realized_gain), tx.id if tx else None),
                )
            conn.commit()
            return True, f"Transferred {quantity} {asset}: {from_wallet} → {to_wallet}."
        except Exception as e:
            conn.rollback()
            logger.error(f"CryptoEngine: ledger posted transfer for '{asset}' but lot update failed: {e}")
            return False, f"Ledger entry posted but lot tracking failed: {e}"
        finally:
            conn.close()

    def _fetch_tx_for_undo(self, tx_id: int):
        """See InvestmentEngine._fetch_tx_for_undo() -- identical logic,
        duplicated rather than shared, consistent with this module's
        overall "fully separate from investment_engine.py" design.
        Reads the original transaction's current state directly rather
        than calling the ledger's void_transaction(), which has a known
        balance-doubling bug (flagged separately for a core-ledger fix)."""
        conn, cur = self.gl._connect()
        try:
            cur.execute(
                "SELECT date, description, reconciled, is_voided FROM sql_transactions WHERE id = ?",
                (tx_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            date, description, reconciled, is_voided = row
            cur.execute(self.gl._LINE_SELECT_SQL, (tx_id,))
            # Lines come back as ledger TransactionLine objects, so a foreign-
            # currency cash leg keeps its native amount and rate -- the undo
            # reverses it at exactly the rate it was posted at.
            lines = [self.gl._line_from_row(r) for r in cur.fetchall()]
            return {"date": date, "description": description, "reconciled": bool(reconciled),
                    "is_voided": bool(is_voided), "lines": lines}
        finally:
            conn.close()

    def void_last_transaction(self, asset: str, wallet_name: str) -> Tuple[bool, str]:
        """Reverses the most recent not-yet-undone buy/sell/reward for this
        (asset, wallet) holding, strictly in LIFO order -- see
        InvestmentEngine.void_last_transaction() for the full rationale,
        including why this posts a plain offsetting entry via
        record_transaction() rather than calling the ledger's
        void_transaction() (which currently double-reverses balances
        instead of zeroing them out -- see the module-level note).

        Transfers are deliberately NOT auto-undoable in v1: a transfer
        spans two holdings, and safely restoring both sides together
        (each with its own independent LIFO constraint -- what if the
        destination wallet had unrelated activity after the transfer
        landed?) is a real scope decision, not something to guess at.
        If the most recent entry for this holding is a transfer, this
        refuses with a clear message rather than silently skipping to
        an older entry (which would violate LIFO) or guessing at a
        two-sided reversal.
        """
        holding = self.get_holding(asset, wallet_name)
        if holding is None:
            return False, f"Unknown holding '{asset}' in wallet '{wallet_name}'."

        conn, cur = self.gl._connect()
        try:
            cur.execute(
                "SELECT id, tx_id, action, lots_before FROM sql_crypto_tx_log "
                "WHERE holding_id = ? AND undone = 0 ORDER BY id DESC LIMIT 1",
                (holding.id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()

        if row is None:
            return False, f"No undoable transactions recorded for '{asset}' in '{wallet_name}'."
        log_id, tx_id, action, lots_before_json = row

        if action == "transfer":
            return False, (
                "The most recent transaction for this holding is a transfer, which can't "
                "be auto-undone yet. Record a manual offsetting transfer instead."
            )

        original = self._fetch_tx_for_undo(tx_id)
        if original is None:
            return False, f"Could not find the original transaction (id {tx_id}) to reverse."
        if original["is_voided"]:
            return False, "The original transaction has already been voided outside this undo feature; check it manually."
        if original["reconciled"]:
            return False, "Cannot undo a reconciled transaction. Unreconcile it first."

        lock_msg = self.gl.period_lock_violation(original["date"], [l.account_name for l in original["lines"]])
        if lock_msg:
            return False, lock_msg

        offsetting_lines = [(l.account_name, l.cr, l.dr, l.fx) for l in original["lines"]]
        ok, msg = self.gl.record_transaction(
            original["date"], f"UNDO: {original['description']}", offsetting_lines,
        )
        if not ok:
            return False, f"Could not reverse the ledger entry: {msg}"

        conn, cur = self.gl._connect()
        try:
            lots_before = self._deserialize_lots(lots_before_json)
            cur.execute("DELETE FROM sql_crypto_lots WHERE holding_id = ?", (holding.id,))
            for lot in lots_before:
                cur.execute(
                    "INSERT INTO sql_crypto_lots (holding_id, date_acquired, quantity, cost_basis_per_unit, acquiring_tx_id) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (holding.id, lot.date_acquired, str(lot.quantity), str(lot.cost_basis_per_unit)),
                )
            if action == "sell":
                cur.execute("DELETE FROM sql_crypto_realized WHERE disposing_tx_id = ?", (tx_id,))
            cur.execute("UPDATE sql_crypto_tx_log SET undone = 1 WHERE id = ?", (log_id,))
            conn.commit()
            return True, f"Reversed the most recent {action} for '{asset}' in '{wallet_name}'."
        except Exception as e:
            conn.rollback()
            logger.error(f"CryptoEngine: ledger reversal succeeded for '{asset}'/'{wallet_name}' but lot restore failed: {e}")
            return False, (
                f"The ledger entry was reversed, but restoring lot tracking failed ({e}). "
                f"Check this holding's lots manually before trusting its cost basis."
            )
        finally:
            conn.close()

    def record_staking_reward(self, asset: str, wallet_name: str, date: str, quantity: Decimal,
                               fmv_per_unit: Decimal, kind: str = "staking",
                               description: Optional[str] = None,
                               currency: Optional[str] = None,
                               fx_rate: Optional[Decimal] = None) -> Tuple[bool, str]:
        """Record crypto received as a reward (staking, protocol
        reward, airdrop) into an existing (asset, wallet) holding.

        Most jurisdictions that tax crypto treat this as ordinary
        income at fair market value on the date received, and that
        same value becomes the cost basis of the received units for
        any later disposal. So this posts income AND opens a lot:
            Dr <holding account>   income
            Cr Staking Income      income
        and adds a lot dated `date` with cost basis per unit =
        income / quantity. No cash account is involved.

        `fmv_per_unit` is supplied by the caller (typed in, or taken
        from imported price history) -- this engine does no lookups.
        Income is rounded to cents FIRST, and the lot's per-unit
        basis is derived from that rounded figure, so the ledger and
        the lot table always agree to the cent.

        Not supported in v1: zero-value receipts (a worthless airdrop
        has no income to post and no ledger entry to hang an undo
        on), and any jurisdiction-specific treatment -- that is the
        tax template layer's job, same as for every other action.
        """
        if kind not in VALID_REWARD_KINDS:
            return False, f"kind must be one of {VALID_REWARD_KINDS}."
        holding = self.get_holding(asset, wallet_name)
        if holding is None:
            return False, f"Unknown holding '{asset}' in wallet '{wallet_name}'. Call add_holding() first."
        if quantity <= ZERO:
            return False, "Quantity must be positive."
        if fmv_per_unit <= ZERO:
            return False, (
                "Fair market value per unit must be positive. Zero-value receipts "
                "aren't supported in v1."
            )

        # `currency` (optional) is the currency fmv_per_unit is quoted in. No cash
        # account exists to carry it, so it is converted to base here and the native
        # figures are kept in the description. Omitted = already in base.
        try:
            income, fx_note = self._amount_to_base(quantity * fmv_per_unit, currency, date, fx_rate)
        except ValueError as e:
            return False, str(e)
        income = income.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if income <= ZERO:
            return False, "The received amount is worth less than one cent at that price; nothing to record."
        desc = description or f"{kind.capitalize()} received: {quantity} {asset} ({wallet_name}) @ {fmv_per_unit}{fx_note}"

        ok, msg = self.gl.record_transaction(
            date, desc,
            [(holding.account_name, income, ZERO), (STAKING_INCOME_ACCOUNT, ZERO, income)],
        )
        if not ok:
            return False, msg

        tx = self.gl.get_last_transaction()
        cost_basis_per_unit = income / quantity

        conn, cur = self.gl._connect()
        try:
            lots_before = self._open_lots(cur, holding.id)
            self._log_undo_snapshot(cur, holding.id, tx.id if tx else None, "reward", lots_before)
            cur.execute(
                "INSERT INTO sql_crypto_lots (holding_id, date_acquired, quantity, cost_basis_per_unit, acquiring_tx_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (holding.id, date, str(quantity), str(cost_basis_per_unit), tx.id if tx else None),
            )
            conn.commit()
            return True, f"Recorded {kind} of {quantity} {asset} into '{wallet_name}' (income {income})."
        except Exception as e:
            conn.rollback()
            logger.error(f"CryptoEngine: ledger posted {kind} for '{asset}' but lot insert failed: {e}")
            return False, f"Ledger entry posted but lot tracking failed: {e}"
        finally:
            conn.close()

    # ---------------------------------------------------------------
    # Reporting
    # ---------------------------------------------------------------

    def get_holdings_summary(self) -> List[dict]:
        summary = []
        wallets_by_id = {w.id: w.name for w in self.list_wallets()}
        for holding in self.list_holdings():
            conn, cur = self.gl._connect()
            try:
                lots = self._open_lots(cur, holding.id)
            finally:
                conn.close()
            qty = sum((l.quantity for l in lots), ZERO)
            cost_basis = sum((l.quantity * l.cost_basis_per_unit for l in lots), ZERO)
            avg_cost = (cost_basis / qty) if qty > ZERO else ZERO
            summary.append({
                "asset": holding.asset,
                "wallet": wallets_by_id.get(holding.wallet_id, "?"),
                "account_name": holding.account_name,
                "cost_basis_method": holding.cost_basis_method,
                "quantity_held": qty,
                "average_cost_per_unit": avg_cost,
                "total_cost_basis": cost_basis,
            })
        return summary

    def get_realized_gains(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> List[dict]:
        conn, cur = self.gl._connect()
        try:
            query = (
                "SELECT h.asset, w.name, r.date, r.quantity, r.cost_basis, r.proceeds, r.realized_gain "
                "FROM sql_crypto_realized r "
                "JOIN sql_crypto_holdings h ON r.holding_id = h.id "
                "JOIN sql_crypto_wallets w ON h.wallet_id = w.id"
            )
            clauses, params = [], []
            if start_date:
                clauses.append("r.date >= ?")
                params.append(start_date)
            if end_date:
                clauses.append("r.date <= ?")
                params.append(end_date)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY r.date"
            cur.execute(query, params)
            return [
                {
                    "asset": row[0], "wallet": row[1], "date": row[2], "quantity": Decimal(row[3]),
                    "cost_basis": Decimal(row[4]), "proceeds": Decimal(row[5]),
                    "realized_gain": Decimal(row[6]),
                }
                for row in cur.fetchall()
            ]
        finally:
            conn.close()
