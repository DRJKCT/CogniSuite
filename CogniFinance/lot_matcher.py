# lot_matcher.py
# For CogniFinance -- Investment & Crypto modules
#
# Pure lot-consumption math (FIFO / specific-lot identification), factored
# out so the tax-sensitive "which units were sold, at what cost basis"
# algorithm has exactly one implementation shared by investment_engine.py
# and crypto_engine.py, rather than two copies that can silently drift.
#
# Design constraints (deliberate, per project style):
#   - Pure functions / dataclasses only. No side effects, no DB access,
#     no PyQt6, no knowledge of accounts or ledgers. Same spirit as
#     budget_engine.py: this file could be unit-tested with nothing but
#     plain Python objects.
#   - No jurisdiction knowledge. This module answers "which units, at
#     what original cost, were consumed by this disposal" -- it does not
#     know or care whether FIFO is the legally-correct method for the
#     user's jurisdiction. That decision is made by the caller (which
#     reads the user's per-holding method preference) and, downstream,
#     by the tax template layer.

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Tuple, Optional

ZERO = Decimal("0.00")


class LotMatcherError(ValueError):
    """Raised when a disposal can't be matched against available lots
    (e.g. selling more units than are held)."""


@dataclass
class Lot:
    """One acquisition lot of a security or crypto unit.

    `id` is caller-defined and opaque to this module -- typically the
    underlying ledger transaction id or a synthetic key the engine
    assigns. `quantity` is whatever remains unconsumed in this lot at
    the time matching runs (callers are expected to pass in current
    remaining quantities, not original purchase quantities).
    """
    id: str
    date_acquired: str  # ISO date "YYYY-MM-DD"
    quantity: Decimal
    cost_basis_per_unit: Decimal

    def __post_init__(self):
        if self.quantity < ZERO:
            raise LotMatcherError(f"Lot '{self.id}' has negative quantity.")
        if self.cost_basis_per_unit < ZERO:
            raise LotMatcherError(f"Lot '{self.id}' has negative cost basis.")


@dataclass
class Consumption:
    """The portion of one lot consumed by a single disposal, plus enough
    to compute realized gain/loss for that slice. `proceeds_per_unit` is
    passed in by the caller (the disposal's sale price), not derived
    here -- this module doesn't know about currencies or fees beyond
    what the caller already folded into that per-unit figure."""
    lot_id: str
    date_acquired: str
    quantity: Decimal
    cost_basis_per_unit: Decimal
    proceeds_per_unit: Decimal

    @property
    def cost_basis(self) -> Decimal:
        return self.quantity * self.cost_basis_per_unit

    @property
    def proceeds(self) -> Decimal:
        return self.quantity * self.proceeds_per_unit

    @property
    def realized_gain(self) -> Decimal:
        return self.proceeds - self.cost_basis


@dataclass
class MatchResult:
    """Result of matching a disposal against held lots."""
    consumptions: List[Consumption] = field(default_factory=list)
    remaining_lots: List[Lot] = field(default_factory=list)  # post-disposal lot state, in the SAME
                                                              # order the caller should persist it in

    @property
    def total_realized_gain(self) -> Decimal:
        return sum((c.realized_gain for c in self.consumptions), ZERO)

    @property
    def total_cost_basis(self) -> Decimal:
        return sum((c.cost_basis for c in self.consumptions), ZERO)

    @property
    def total_proceeds(self) -> Decimal:
        return sum((c.proceeds for c in self.consumptions), ZERO)


def _total_quantity(lots: List[Lot]) -> Decimal:
    return sum((lot.quantity for lot in lots), ZERO)

def _fifo_sort_key(lot: Lot):
    """Oldest first; ties on the same date broken by id, compared
    NUMERICALLY when ids are integers. Engines use integer row ids
    stored as strings, and plain string comparison would put "10"
    before "9", consuming a later-entered same-day lot first."""
    lot_id = str(lot.id)
    numeric = int(lot_id) if lot_id.isdigit() else -1
    return (lot.date_acquired, numeric, lot_id)

def consume_fifo(lots: List[Lot], quantity_to_consume: Decimal, proceeds_per_unit: Decimal) -> MatchResult:
    """Consume `quantity_to_consume` units oldest-lot-first.

    `lots` should be every currently-open lot for the holding, in any
    order -- this function sorts by date_acquired internally (ties
    broken by id for determinism). It does not mutate the input list or
    its Lot objects; callers persist `MatchResult.remaining_lots`.
    """
    if quantity_to_consume <= ZERO:
        raise LotMatcherError("Quantity to consume must be positive.")

    available = _total_quantity(lots)
    if quantity_to_consume > available:
        raise LotMatcherError(
            f"Cannot consume {quantity_to_consume} units; only {available} available."
        )

    ordered = sorted(lots, key=_fifo_sort_key)
    remaining_to_consume = quantity_to_consume
    consumptions: List[Consumption] = []
    remaining_lots: List[Lot] = []

    for lot in ordered:
        if remaining_to_consume <= ZERO:
            remaining_lots.append(lot)
            continue

        take = min(lot.quantity, remaining_to_consume)
        if take > ZERO:
            consumptions.append(Consumption(
                lot_id=lot.id,
                date_acquired=lot.date_acquired,
                quantity=take,
                cost_basis_per_unit=lot.cost_basis_per_unit,
                proceeds_per_unit=proceeds_per_unit,
            ))
            remaining_to_consume -= take

        leftover_qty = lot.quantity - take
        if leftover_qty > ZERO:
            remaining_lots.append(Lot(lot.id, lot.date_acquired, leftover_qty, lot.cost_basis_per_unit))
        # else: lot fully consumed, dropped from remaining_lots

    return MatchResult(consumptions=consumptions, remaining_lots=remaining_lots)


def consume_specific_lots(
    lots: List[Lot],
    selections: List[Tuple[str, Decimal]],
    proceeds_per_unit: Decimal,
) -> MatchResult:
    """Consume specific quantities from specific lots, by lot id.

    `selections` is a list of (lot_id, quantity) pairs the user chose
    explicitly (specific-lot identification). Every referenced lot must
    exist and have enough remaining quantity; lots not mentioned in
    `selections` pass through untouched.
    """
    by_id = {lot.id: lot for lot in lots}
    seen_ids = set()
    for lot_id, qty in selections:
        if qty <= ZERO:
            raise LotMatcherError(f"Selected quantity for lot '{lot_id}' must be positive.")
        if lot_id in seen_ids:
            raise LotMatcherError(f"Lot '{lot_id}' selected more than once in a single disposal.")
        seen_ids.add(lot_id)
        lot = by_id.get(lot_id)
        if lot is None:
            raise LotMatcherError(f"Lot '{lot_id}' not found among open lots.")
        if qty > lot.quantity:
            raise LotMatcherError(
                f"Cannot consume {qty} units from lot '{lot_id}'; only {lot.quantity} remain."
            )

    consumptions: List[Consumption] = []
    remaining_lots: List[Lot] = []
    selection_map = dict(selections)

    for lot in lots:
        take = selection_map.get(lot.id, ZERO)
        if take > ZERO:
            consumptions.append(Consumption(
                lot_id=lot.id,
                date_acquired=lot.date_acquired,
                quantity=take,
                cost_basis_per_unit=lot.cost_basis_per_unit,
                proceeds_per_unit=proceeds_per_unit,
            ))
        leftover_qty = lot.quantity - take
        if leftover_qty > ZERO:
            remaining_lots.append(Lot(lot.id, lot.date_acquired, leftover_qty, lot.cost_basis_per_unit))

    return MatchResult(consumptions=consumptions, remaining_lots=remaining_lots)


def average_cost(lots: List[Lot]) -> Decimal:
    """Weighted-average cost per unit across open lots -- for jurisdictions
    (Canada's ACB, France's portfolio-average) whose template layer wants
    an average-cost figure rather than a FIFO/specific-lot disposal.
    Callers needing full ACB semantics (running average that updates on
    every acquisition, per-currency pooling, etc.) should treat this as
    a building block, not the whole rule -- it just computes the average
    over whatever lot set is passed in."""
    total_qty = _total_quantity(lots)
    if total_qty <= ZERO:
        return ZERO
    total_cost = sum((lot.quantity * lot.cost_basis_per_unit for lot in lots), ZERO)
    return total_cost / total_qty
