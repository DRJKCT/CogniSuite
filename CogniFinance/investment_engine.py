# investment_engine.py
# For CogniFinance -- Investments module (traditional securities)
#
# Fully separate from crypto_engine.py by deliberate architectural
# decision (see the Investment & Crypto planning brief): crypto's
# messier feature surface -- wallets vs. exchange accounts, on-chain
# transfers, staking/airdrop income -- never touches this file, and
# this file's simpler buy/sell/dividend model never gets bent out of
# shape to accommodate crypto. The only thing shared between the two
# engines is lot_matcher.py (pure FIFO/specific-lot math) and the fact
# that both post through gl.record_transaction() like everything else.
#
# v1 scope (deliberately minimal -- see open questions in the planning
# brief, resolved here for v1 so building could start):
#   - Buy, sell, cash dividends. That's it.
#   - Corporate actions (splits, spin-offs, mergers, DRIP) are NOT
#     handled in v1. Calling attention to that explicitly rather than
#     silently mishandling one if it comes up -- see NotImplementedError
#     in record_stock_split() below, which exists as a landing spot for
#     that decision rather than leaving it undiscoverable.
#
# This module has zero jurisdiction knowledge. It tracks lots, applies
# whichever cost-basis method the user selected for a given holding,
# and computes realized gains/losses correctly. Which categorization a
# jurisdiction's tax filing wants is the template layer's job
# (tax_manager.py / tax_calculator.py), not this file's. See the
# planning brief for why FIFO is not a safe universal default (UK/
# Canada/France all require something else).

import json
import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Tuple, Optional

from lot_matcher import Lot, consume_fifo, consume_specific_lots, LotMatcherError

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")

# Report-line / account-name constants this engine posts to. The Asset
# report line for a given holding is chosen by the caller from
# ledger.VALID_REPORT_LINES_BY_TYPE["Asset"] at add_holding() time (e.g.
# "Investment — Equities", "Investment — Bonds", "Investment — Mutual
# Funds") -- those entries must exist in ledger.py's
# REPORT_LINE_METADATA before this engine is used; see the accompanying
# patch. The realized-gains/losses/dividend accounts below are fixed
# account names (Revenue/Expense) this engine manages on its own.
REALIZED_GAINS_ACCOUNT = "Realized Gains — Investments"
REALIZED_GAINS_REPORT_LINE = "Investment — Realized Gains"
REALIZED_LOSSES_ACCOUNT = "Realized Losses — Investments"
REALIZED_LOSSES_REPORT_LINE = "Investment — Realized Losses"
DIVIDEND_INCOME_ACCOUNT = "Dividend Income — Investments"
DIVIDEND_INCOME_REPORT_LINE = "Investment — Dividend Income"

VALID_COST_BASIS_METHODS = ("FIFO", "SPECIFIC")

NO_LEDGER_TX = 0

class InvestmentEngineError(ValueError):
    pass


@dataclass
class Holding:
    id: int
    symbol: str
    account_name: str
    cost_basis_method: str


class InvestmentEngine:
    """Wraps a GeneralLedger connection with per-security lot tracking.

    Every dollar movement still posts through gl.record_transaction()
    -- this class never writes a dr/cr amount anywhere else, so the
    ledger's own balancing/validation/period-lock logic still applies
    to every investment transaction. Quantities and per-unit cost basis
    (concepts the plain ledger has no notion of) live in this module's
    own tables, in the same encrypted database file, following the
    same "own tables, own connection" pattern as the rest of the app.
    """

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
                CREATE TABLE IF NOT EXISTS sql_investment_holdings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT UNIQUE,
                    account_name TEXT UNIQUE,
                    cost_basis_method TEXT DEFAULT 'FIFO'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sql_investment_lots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    holding_id INTEGER NOT NULL,
                    date_acquired TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    cost_basis_per_unit TEXT NOT NULL,
                    acquiring_tx_id INTEGER,
                    FOREIGN KEY(holding_id) REFERENCES sql_investment_holdings(id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sql_investment_realized (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    holding_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    cost_basis TEXT NOT NULL,
                    proceeds TEXT NOT NULL,
                    realized_gain TEXT NOT NULL,
                    disposing_tx_id INTEGER,
                    FOREIGN KEY(holding_id) REFERENCES sql_investment_holdings(id)
                )
            """)
            # Append-only undo log: one row per buy/sell/dividend,
            # written BEFORE the lot table is mutated, so a later
            # void_last_transaction() can restore exactly what was
            # there before. Never edited after insert, only ever
            # marked undone=1 (same "append-only" ethos as the
            # ledger's own void_transaction/edit_transaction) --
            # that's what lets a second undo correctly see past an
            # already-undone entry to the one before it.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sql_investment_tx_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    holding_id INTEGER NOT NULL,
                    tx_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    lots_before TEXT NOT NULL,
                    undone INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(holding_id) REFERENCES sql_investment_holdings(id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sql_investment_corporate_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    holding_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    action TEXT NOT NULL,
                    numerator TEXT NOT NULL,
                    denominator TEXT NOT NULL,
                    log_id INTEGER,
                    undone INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(holding_id) REFERENCES sql_investment_holdings(id)
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def _ensure_shared_accounts_exist(self):
        """Create the fixed Revenue/Expense accounts this engine needs,
        idempotently. Per-holding Asset accounts are created explicitly
        via add_holding() instead, since those need a caller-chosen
        symbol and report line."""
        for name, acc_type, report_line in (
            (REALIZED_GAINS_ACCOUNT, "Revenue", REALIZED_GAINS_REPORT_LINE),
            (REALIZED_LOSSES_ACCOUNT, "Expense", REALIZED_LOSSES_REPORT_LINE),
            (DIVIDEND_INCOME_ACCOUNT, "Revenue", DIVIDEND_INCOME_REPORT_LINE),
        ):
            if name not in self.gl.accounts:
                ok, msg = self.gl.create_account(name, acc_type, report_line)
                if not ok:
                    logger.warning(f"InvestmentEngine could not create '{name}': {msg}")

    # ---------------------------------------------------------------
    # Holdings
    # ---------------------------------------------------------------

    def add_holding(self, symbol: str, account_name: str, report_line: str,
                     cost_basis_method: str = "FIFO") -> Tuple[bool, str]:
        """Register a new security holding. Creates the backing Asset
        account (via the ledger, so it obeys the ledger's own
        report_line validation) and this engine's holding row together.
        """
        symbol = symbol.strip().upper()
        if cost_basis_method not in VALID_COST_BASIS_METHODS:
            return False, f"Invalid cost basis method '{cost_basis_method}'."
        if self.get_holding(symbol) is not None:
            return False, f"Holding '{symbol}' already exists."

        ok, msg = self.gl.create_account(account_name, "Asset", report_line)
        if not ok:
            return False, f"Could not create holding account: {msg}"

        conn, cur = self.gl._connect()
        try:
            cur.execute(
                "INSERT INTO sql_investment_holdings (symbol, account_name, cost_basis_method) VALUES (?, ?, ?)",
                (symbol, account_name, cost_basis_method),
            )
            conn.commit()
            return True, f"Holding '{symbol}' added."
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            conn.close()

    def get_holding(self, symbol: str) -> Optional[Holding]:
        symbol = symbol.strip().upper()
        conn, cur = self.gl._connect()
        try:
            cur.execute(
                "SELECT id, symbol, account_name, cost_basis_method FROM sql_investment_holdings WHERE symbol = ?",
                (symbol,),
            )
            row = cur.fetchone()
            return Holding(*row) if row else None
        finally:
            conn.close()

    def list_holdings(self) -> List[Holding]:
        conn, cur = self.gl._connect()
        try:
            cur.execute("SELECT id, symbol, account_name, cost_basis_method FROM sql_investment_holdings")
            return [Holding(*row) for row in cur.fetchall()]
        finally:
            conn.close()

    def _open_lots(self, cur, holding_id: int) -> List[Lot]:
        cur.execute(
            "SELECT id, date_acquired, quantity, cost_basis_per_unit FROM sql_investment_lots WHERE holding_id = ?",
            (holding_id,),
        )
        return [
            Lot(id=str(r[0]), date_acquired=r[1], quantity=Decimal(r[2]), cost_basis_per_unit=Decimal(r[3]))
            for r in cur.fetchall()
        ]

    def get_open_lots(self, symbol: str) -> List[Lot]:
        holding = self.get_holding(symbol)
        if holding is None:
            raise InvestmentEngineError(f"Unknown holding '{symbol}'.")
        conn, cur = self.gl._connect()
        try:
            return self._open_lots(cur, holding.id)
        finally:
            conn.close()

    def get_quantity_held(self, symbol: str) -> Decimal:
        return sum((lot.quantity for lot in self.get_open_lots(symbol)), ZERO)

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

    def _log_undo_snapshot(self, cur, holding_id: int, tx_id: Optional[int], action: str, lots_before: List[Lot]) -> Optional[int]:
        """Returns the new log row's id (None if nothing was logged).
        Pass tx_id=NO_LEDGER_TX for actions that touch only the lot
        table (splits) -- they still need an undo entry so LIFO undo
        ordering stays correct relative to buys/sells around them."""
        if tx_id is None:
            # No transaction id to reverse later means nothing for
            # void_last_transaction to act on -- don't log a dead entry.
            return None
        cur.execute(
            "INSERT INTO sql_investment_tx_log (holding_id, tx_id, action, lots_before) VALUES (?, ?, ?, ?)",
            (holding_id, tx_id, action, self._serialize_lots(lots_before)),
        )
        return cur.lastrowid
    # ---------------------------------------------------------------
    # Buy / Sell / Dividends
    # ---------------------------------------------------------------

    def buy(self, symbol: str, date: str, quantity: Decimal, price_per_unit: Decimal,
            cash_account: str, fees: Decimal = ZERO, description: Optional[str] = None) -> Tuple[bool, str]:
        """Record a purchase: money out of `cash_account`, units and
        cost basis into the holding's lot table. Fees are folded into
        the lot's cost basis (standard treatment; not separately
        deductible against the same disposal)."""
        holding = self.get_holding(symbol)
        if holding is None:
            return False, f"Unknown holding '{symbol}'. Call add_holding() first."
        if quantity <= ZERO:
            return False, "Quantity must be positive."
        if price_per_unit < ZERO or fees < ZERO:
            return False, "Price and fees cannot be negative."

        total_cost = (quantity * price_per_unit) + fees
        desc = description or f"Buy {quantity} {symbol} @ {price_per_unit}"

        ok, msg = self.gl.record_transaction(
            date, desc,
            [(holding.account_name, total_cost, ZERO), (cash_account, ZERO, total_cost)],
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
                "INSERT INTO sql_investment_lots (holding_id, date_acquired, quantity, cost_basis_per_unit, acquiring_tx_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (holding.id, date, str(quantity), str(cost_basis_per_unit), tx.id if tx else None),
            )
            conn.commit()
            return True, f"Bought {quantity} {symbol}."
        except Exception as e:
            conn.rollback()
            # Ledger side already posted -- surface this loudly rather
            # than leaving a silent quantity/dollar mismatch.
            logger.error(f"InvestmentEngine: ledger posted buy for '{symbol}' but lot insert failed: {e}")
            return False, f"Ledger entry posted but lot tracking failed: {e}"
        finally:
            conn.close()

    def sell(self, symbol: str, date: str, quantity: Decimal, price_per_unit: Decimal,
              cash_account: str, fees: Decimal = ZERO,
              specific_selections: Optional[List[Tuple[str, Decimal]]] = None,
              description: Optional[str] = None) -> Tuple[bool, str]:
        """Record a disposal. Consumes open lots per the holding's
        cost-basis method (or `specific_selections`, an explicit
        [(lot_id, quantity), ...] override for a SPECIFIC-method
        holding), posts the cash/asset/gain-or-loss entry, then
        persists the updated lot state and a realized-gain log row."""
        holding = self.get_holding(symbol)
        if holding is None:
            return False, f"Unknown holding '{symbol}'."
        if quantity <= ZERO:
            return False, "Quantity must be positive."
        if price_per_unit < ZERO or fees < ZERO:
            return False, "Price and fees cannot be negative."

        conn, cur = self.gl._connect()
        try:
            open_lots = self._open_lots(cur, holding.id)
        finally:
            conn.close()

        # Fees reduce net proceeds per unit; keeps consumption math in
        # dollars-per-unit terms consistent with how buy() folds fees
        # into cost basis per unit.
        net_proceeds_per_unit = price_per_unit - (fees / quantity)

        try:
            if specific_selections is not None:
                match = consume_specific_lots(open_lots, specific_selections, net_proceeds_per_unit)
            elif holding.cost_basis_method == "FIFO":
                match = consume_fifo(open_lots, quantity, net_proceeds_per_unit)
            else:
                return False, (
                    f"Holding '{symbol}' uses SPECIFIC-lot identification; "
                    f"specific_selections is required."
                )
        except LotMatcherError as e:
            return False, str(e)

        # Round proceeds and cost basis to cents FIRST (matching the
        # ledger's own money() convention), then derive gain as their
        # difference -- rather than rounding all three independently.
        # Fees rarely divide evenly across quantity (e.g. $4.95 fee /
        # 15 units), so match.total_cost_basis and
        # match.total_realized_gain can carry more than 2 decimal
        # places; rounding gain separately from proceeds/cost_basis
        # can drift a cent from what the other two lines sum to,
        # which the ledger's balance check correctly rejects.
        cost_basis = match.total_cost_basis.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        gross_proceeds = ((quantity * price_per_unit) - fees).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        gain = gross_proceeds - cost_basis

        lines = [
            (cash_account, gross_proceeds, ZERO),
            (holding.account_name, ZERO, cost_basis),
        ]
        if gain > ZERO:
            lines.append((REALIZED_GAINS_ACCOUNT, ZERO, gain))
        elif gain < ZERO:
            lines.append((REALIZED_LOSSES_ACCOUNT, -gain, ZERO))

        desc = description or f"Sell {quantity} {symbol} @ {price_per_unit}"
        ok, msg = self.gl.record_transaction(date, desc, lines)
        if not ok:
            return False, msg

        tx = self.gl.get_last_transaction()

        conn, cur = self.gl._connect()
        try:
            # Log the pre-disposal snapshot (open_lots, captured above
            # before any consumption math ran) before mutating anything,
            # so void_last_transaction() can restore exactly this.
            self._log_undo_snapshot(cur, holding.id, tx.id if tx else None, "sell", open_lots)
            # Replace this holding's lot rows wholesale with the
            # post-disposal state. Simplest correct approach -- avoids
            # juggling which original row id survives a partial
            # consumption vs. a full one.
            cur.execute("DELETE FROM sql_investment_lots WHERE holding_id = ?", (holding.id,))
            for lot in match.remaining_lots:
                cur.execute(
                    "INSERT INTO sql_investment_lots (holding_id, date_acquired, quantity, cost_basis_per_unit, acquiring_tx_id) "
                    "VALUES (?, ?, ?, ?, NULL)",
                    (holding.id, lot.date_acquired, str(lot.quantity), str(lot.cost_basis_per_unit)),
                )
            cur.execute(
                "INSERT INTO sql_investment_realized (holding_id, date, quantity, cost_basis, proceeds, realized_gain, disposing_tx_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (holding.id, date, str(quantity), str(cost_basis), str(gross_proceeds), str(gain), tx.id if tx else None),
            )
            conn.commit()
            return True, f"Sold {quantity} {symbol} (realized {'gain' if gain >= ZERO else 'loss'} {abs(gain)})."
        except Exception as e:
            conn.rollback()
            logger.error(f"InvestmentEngine: ledger posted sale for '{symbol}' but lot update failed: {e}")
            return False, f"Ledger entry posted but lot tracking failed: {e}"
        finally:
            conn.close()

    def record_dividend(self, symbol: str, date: str, amount: Decimal, cash_account: str,
                         description: Optional[str] = None) -> Tuple[bool, str]:
        """Cash dividend only -- money in, no lot/cost-basis effect.
        For a dividend that was reinvested instead of paid out, use
        record_dividend_reinvestment()."""
        holding = self.get_holding(symbol)
        if holding is None:
            return False, f"Unknown holding '{symbol}'."
        if amount <= ZERO:
            return False, "Dividend amount must be positive."
        desc = description or f"Dividend: {symbol}"
        ok, msg = self.gl.record_transaction(
            date, desc,
            [(cash_account, amount, ZERO), (DIVIDEND_INCOME_ACCOUNT, ZERO, amount)],
        )
        if not ok:
            return False, msg

        # No lot-table effect, so lots_before is just the current
        # (unchanged) state -- logged anyway so void_last_transaction()
        # can still reverse the ledger side in the correct LIFO order
        # relative to any buys/sells on this same holding.
        tx = self.gl.get_last_transaction()
        conn, cur = self.gl._connect()
        try:
            lots_before = self._open_lots(cur, holding.id)
            self._log_undo_snapshot(cur, holding.id, tx.id if tx else None, "dividend", lots_before)
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"InvestmentEngine: ledger posted dividend for '{symbol}' but undo-log insert failed: {e}")
            # Not fatal to the dividend itself -- it posted successfully
            # and has no lot-table state to desync. Only consequence is
            # this specific dividend won't be undoable via
            # void_last_transaction(); surfaced via the return message.
            return True, f"Dividend recorded, but it won't be undoable via Void Last Transaction ({e})."
        finally:
            conn.close()
        return True, f"Dividend of {amount} recorded for {symbol}."

    def _fetch_tx_for_undo(self, tx_id: int):
        """Reads the original transaction's CURRENT state directly --
        date, description, reconciled flag, is_voided flag, and lines
        -- so void_last_transaction() can build a correct offsetting
        entry and enforce the same reconciled/period-lock protections
        the ledger's own void/edit methods enforce, without calling
        those methods (see the note on why: ledger.py's
        void_transaction() currently double-reverses balances rather
        than zeroing them out -- marks the original excluded AND posts
        a flipped entry, when only one of those should happen. Flagged
        separately for a core-ledger fix; this engine works around it
        by never calling void_transaction() itself)."""
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
            cur.execute("SELECT account_name, dr, cr FROM sql_transaction_lines WHERE tx_id = ?", (tx_id,))
            # Stored dr/cr are already money-formatted strings from
            # whichever record_transaction() call originally wrote them;
            # Decimal(...) here just parses them back, no re-rounding needed.
            lines = [(acc, Decimal(dr) if dr else ZERO, Decimal(cr) if cr else ZERO) for acc, dr, cr in cur.fetchall()]
            return {"date": date, "description": description, "reconciled": bool(reconciled),
                    "is_voided": bool(is_voided), "lines": lines}
        finally:
            conn.close()

    def void_last_transaction(self, symbol: str) -> Tuple[bool, str]:
        """Reverses the most recent not-yet-undone buy/sell/dividend/DRIP/split for
        this holding, strictly in LIFO order -- if a newer transaction
        exists for the same holding, that one must be undone first.
        This is the ONLY supported way to remove or correct an
        investment-engine transaction; the generic ledger's
        void_transaction()/edit_transaction() refuse these transactions
        outright (see ledger.py's _investment_crypto_lock_message) to
        prevent quantity/cost-basis from silently desyncing from the
        dollar amounts posted to the ledger.

        Reverses the dollar posting by recording a plain, ordinary
        offsetting entry (dr/cr swapped from the original) via
        record_transaction() -- the same well-tested mechanism
        buy()/sell()/record_dividend() already use -- rather than
        calling the ledger's void_transaction(), which has a known bug
        (see module-level note) that would double the reversal instead
        of zeroing it out. The original transaction is left exactly as
        it was (not marked voided); the offsetting entry cancels it
        out arithmetically, and both remain visible in the ledger --
        consistent with the app's append-only ethos.

        Still respects reconciliation and period locks exactly as a
        direct void would: refuses if the original is reconciled (must
        be unreconciled first) or falls in a locked period. Then
        restores the lot table to the snapshot taken immediately
        before that action. If the dollar reversal fails for any
        reason, the lot table is left untouched.
        """
        holding = self.get_holding(symbol)
        if holding is None:
            return False, f"Unknown holding '{symbol}'."

        conn, cur = self.gl._connect()
        try:
            cur.execute(
                "SELECT id, tx_id, action, lots_before FROM sql_investment_tx_log "
                "WHERE holding_id = ? AND undone = 0 ORDER BY id DESC LIMIT 1",
                (holding.id,),
            )
            row = cur.fetchone()
        finally:
            conn.close()

        if row is None:
            return False, f"No undoable transactions recorded for '{symbol}'."
        log_id, tx_id, action, lots_before_json = row

        if tx_id != NO_LEDGER_TX:
            original = self._fetch_tx_for_undo(tx_id)
            if original is None:
                return False, f"Could not find the original transaction (id {tx_id}) to reverse."
            if original["is_voided"]:
                return False, "The original transaction has already been voided outside this undo feature; check it manually."
            if original["reconciled"]:
                return False, "Cannot undo a reconciled transaction. Unreconcile it first."

            lock_msg = self.gl.period_lock_violation(original["date"], [acc for acc, _, _ in original["lines"]])
            if lock_msg:
                return False, lock_msg

            offsetting_lines = [(acc, cr, dr) for acc, dr, cr in original["lines"]]
            ok, msg = self.gl.record_transaction(
                original["date"], f"UNDO: {original['description']}", offsetting_lines,
            )
            if not ok:
                return False, f"Could not reverse the ledger entry: {msg}"
        # else: a split -- no dollars were posted, so there is no
        # ledger entry to reverse; only the lot table needs restoring.

        conn, cur = self.gl._connect()
        try:
            if action in ("buy", "sell", "drip", "split"):
                lots_before = self._deserialize_lots(lots_before_json)
                cur.execute("DELETE FROM sql_investment_lots WHERE holding_id = ?", (holding.id,))
                for lot in lots_before:
                    cur.execute(
                        "INSERT INTO sql_investment_lots (holding_id, date_acquired, quantity, cost_basis_per_unit, acquiring_tx_id) "
                        "VALUES (?, ?, ?, ?, NULL)",
                        (holding.id, lot.date_acquired, str(lot.quantity), str(lot.cost_basis_per_unit)),
                    )
            if action == "sell":
                cur.execute("DELETE FROM sql_investment_realized WHERE disposing_tx_id = ?", (tx_id,))
            if action == "split":
                cur.execute("UPDATE sql_investment_corporate_actions SET undone = 1 WHERE log_id = ?", (log_id,))
            cur.execute("UPDATE sql_investment_tx_log SET undone = 1 WHERE id = ?", (log_id,))
            conn.commit()
            return True, f"Reversed the most recent {action} for '{symbol}'."
        except Exception as e:
            conn.rollback()
            logger.error(f"InvestmentEngine: ledger reversal succeeded for '{symbol}' but lot restore failed: {e}")
            return False, (
                f"The ledger entry was reversed, but restoring lot tracking failed ({e}). "
                f"Check this holding's lots manually before trusting its cost basis."
            )
        finally:
            conn.close()

    # ---------------------------------------------------------------
    # Corporate actions & reinvestment
    # ---------------------------------------------------------------

    def record_dividend_reinvestment(self, symbol: str, date: str, amount: Decimal,
                                      quantity: Decimal,
                                      description: Optional[str] = None) -> Tuple[bool, str]:
        """A dividend that was reinvested rather than paid out (DRIP).

        Two things happen at once, which is why this isn't just buy():
          1. The dividend is income -- credited to the same Dividend
             Income account a cash dividend uses.
          2. The reinvested shares become a NEW lot dated `date`, with
             cost basis equal to the dividend amount (the income already
             recognised is what was "spent" on them).
        No cash account is touched: money never left the brokerage.

        `amount` is the dividend as reported by the broker; `quantity`
        is the (possibly fractional) shares it bought. The implied
        price is amount / quantity. Basis is the dividend amount
        itself, so the ledger and lot table agree to the cent.
        """
        holding = self.get_holding(symbol)
        if holding is None:
            return False, f"Unknown holding '{symbol}'."
        if amount <= ZERO:
            return False, "Dividend amount must be positive."
        if quantity <= ZERO:
            return False, "Reinvested quantity must be positive."

        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if amount <= ZERO:
            return False, "Dividend amount rounds to zero."
        desc = description or f"Dividend reinvested: {quantity} {symbol}"
        ok, msg = self.gl.record_transaction(
            date, desc,
            [(holding.account_name, amount, ZERO), (DIVIDEND_INCOME_ACCOUNT, ZERO, amount)],
        )
        if not ok:
            return False, msg

        tx = self.gl.get_last_transaction()
        cost_basis_per_unit = amount / quantity

        conn, cur = self.gl._connect()
        try:
            lots_before = self._open_lots(cur, holding.id)
            self._log_undo_snapshot(cur, holding.id, tx.id if tx else None, "drip", lots_before)
            cur.execute(
                "INSERT INTO sql_investment_lots (holding_id, date_acquired, quantity, cost_basis_per_unit, acquiring_tx_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (holding.id, date, str(quantity), str(cost_basis_per_unit), tx.id if tx else None),
            )
            conn.commit()
            return True, f"Reinvested dividend of {amount} into {quantity} {symbol}."
        except Exception as e:
            conn.rollback()
            logger.error(f"InvestmentEngine: ledger posted DRIP for '{symbol}' but lot insert failed: {e}")
            return False, f"Ledger entry posted but lot tracking failed: {e}"
        finally:
            conn.close()

    def record_stock_split(self, symbol: str, date: str, new_shares: Decimal,
                            old_shares: Decimal) -> Tuple[bool, str]:
        """Apply a split of `new_shares` for every `old_shares` held
        (2-for-1 is new_shares=2, old_shares=1; a 1-for-10 reverse
        split is new_shares=1, old_shares=10).

        Each affected lot's quantity is multiplied by the ratio and its
        cost-basis-per-unit divided by it, so TOTAL cost basis is
        unchanged and nothing is a taxable event. Lot acquisition
        dates are untouched (FIFO order and holding period survive).

        `date` is the effective date. Only lots acquired BEFORE it are
        adjusted; a lot acquired on or after it is already in
        post-split units. If any sale on or after `date` has already
        been recorded, the split is refused: those sales consumed lots
        in the wrong units and must be undone first.

        Posts nothing to the ledger -- no dollars move. Undoable via
        void_last_transaction() like everything else. Fractional
        shares from a reverse split are kept as-is; cash-in-lieu is
        not modelled in v1.
        """
        holding = self.get_holding(symbol)
        if holding is None:
            return False, f"Unknown holding '{symbol}'."
        if new_shares <= ZERO or old_shares <= ZERO:
            return False, "Both sides of the split ratio must be positive."
        if new_shares == old_shares:
            return False, "A split ratio of 1-for-1 changes nothing."
        ratio = new_shares / old_shares

        conn, cur = self.gl._connect()
        try:
            open_lots = self._open_lots(cur, holding.id)
            cur.execute(
                "SELECT 1 FROM sql_investment_realized WHERE holding_id = ? AND date >= ? LIMIT 1",
                (holding.id, date),
            )
            later_sale = cur.fetchone() is not None
            # A split posts nothing to the ledger, so nothing else can
            # catch the same split being entered twice (e.g. the same
            # CSV imported twice) -- and a second one would compound.
            cur.execute(
                "SELECT 1 FROM sql_investment_corporate_actions "
                "WHERE holding_id = ? AND date = ? AND action = 'split' AND undone = 0 LIMIT 1",
                (holding.id, date),
            )
            duplicate = cur.fetchone() is not None
        finally:
            conn.close()

        if duplicate:
            return False, (
                f"A split for '{symbol}' on {date} is already recorded. If that was a mistake, "
                f"undo it first (Undo box), then record the correct one."
            )

        if later_sale:
            return False, (
                f"'{symbol}' already has a sale recorded on or after {date}. Those sales used "
                f"pre-split lots; undo them first, record the split, then re-enter them."
            )
        affected = [l for l in open_lots if l.date_acquired < date]
        if not affected:
            return False, f"No open lots of '{symbol}' were acquired before {date}, so there is nothing to split."

        conn, cur = self.gl._connect()
        try:
            log_id = self._log_undo_snapshot(cur, holding.id, NO_LEDGER_TX, "split", open_lots)
            for lot in affected:
                cur.execute(
                    "UPDATE sql_investment_lots SET quantity = ?, cost_basis_per_unit = ? WHERE id = ?",
                    (str(lot.quantity * ratio), str(lot.cost_basis_per_unit / ratio), int(lot.id)),
                )
            cur.execute(
                "INSERT INTO sql_investment_corporate_actions (holding_id, date, action, numerator, denominator, log_id) "
                "VALUES (?, ?, 'split', ?, ?, ?)",
                (holding.id, date, str(new_shares), str(old_shares), log_id),
            )
            conn.commit()
            return True, f"Applied {new_shares}-for-{old_shares} split to {len(affected)} lot(s) of {symbol}."
        except Exception as e:
            conn.rollback()
            logger.error(f"InvestmentEngine: split for '{symbol}' failed: {e}")
            return False, f"Split failed; no lots were changed: {e}"
        finally:
            conn.close()

    def get_corporate_actions(self, symbol: str) -> List[dict]:
        holding = self.get_holding(symbol)
        if holding is None:
            raise InvestmentEngineError(f"Unknown holding '{symbol}'.")
        conn, cur = self.gl._connect()
        try:
            cur.execute(
                "SELECT date, action, numerator, denominator FROM sql_investment_corporate_actions "
                "WHERE holding_id = ? AND undone = 0 ORDER BY date, id",
                (holding.id,),
            )
            return [{"date": r[0], "action": r[1], "new_shares": Decimal(r[2]), "old_shares": Decimal(r[3])}
                    for r in cur.fetchall()]
        finally:
            conn.close()

    # ---------------------------------------------------------------
    # Reporting
    # ---------------------------------------------------------------

    def get_holdings_summary(self) -> List[dict]:
        """One row per holding: symbol, quantity held, average cost
        basis per unit, and total cost basis -- the building blocks a
        UI table or the (future, deferred) combined portfolio view
        needs. Does not fetch live prices; that's price_fetch.py's job,
        kept deliberately separate."""
        summary = []
        for holding in self.list_holdings():
            lots = self.get_open_lots(holding.symbol)
            qty = sum((l.quantity for l in lots), ZERO)
            cost_basis = sum((l.quantity * l.cost_basis_per_unit for l in lots), ZERO)
            avg_cost = (cost_basis / qty) if qty > ZERO else ZERO
            summary.append({
                "symbol": holding.symbol,
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
                "SELECT h.symbol, r.date, r.quantity, r.cost_basis, r.proceeds, r.realized_gain "
                "FROM sql_investment_realized r JOIN sql_investment_holdings h ON r.holding_id = h.id"
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
                    "symbol": row[0], "date": row[1], "quantity": Decimal(row[2]),
                    "cost_basis": Decimal(row[3]), "proceeds": Decimal(row[4]),
                    "realized_gain": Decimal(row[5]),
                }
                for row in cur.fetchall()
            ]
        finally:
            conn.close()
