"""
Fuzzed balance-invariant test.

test_accounting_equation_holds (in test_core_finance.py) checks one fixed
scenario. This file instead throws a long, seeded-random sequence of
postings, edits, deletes, and deliberately-invalid transactions at the
ledger and re-checks, after *every single operation*, that the books
never go out of balance -- via three independent checks that don't share
implementation with each other, so a bug in any one of them (trial_balance,
balance_sheet, or the raw stored lines themselves) would still be caught
by the other two.

Seeded (not truly random) so failures are 100% reproducible: if this ever
fails, the printed seed reproduces the exact same sequence of operations.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import random
import tempfile
import unittest
from decimal import Decimal

from ledger import GeneralLedger, ZERO, money

# Number of random operations per run. Kept moderate so the suite stays
# usable day-to-day: every ledger operation here re-derives the SQLCipher
# key via a 256,000-iteration KDF (by design, for brute-force resistance
# -- see the passphrase-hardening work), so this test is inherently much
# slower per-operation than the rest of the suite. Raise ITERATIONS
# and/or add seeds locally (e.g. ITERATIONS=2000, five+ seeds) when
# specifically hunting for a rare invariant-violation bug; this default
# is tuned to still run in well under a minute.
ITERATIONS = 120

# Multiple seeds = broader coverage without any single run becoming slow
# or the test becoming flaky (each seed is still fully deterministic).
SEEDS = [1234, 987654321]


def _all_account_names(gl: GeneralLedger):
    return list(gl.accounts.keys())


def _raw_total_dr_cr(gl: GeneralLedger):
    """
    The most primitive possible check: sum debit/credit directly off the
    in-memory Transaction/TransactionLine objects, bypassing
    trial_balance() and balance_sheet() entirely. If this ever diverges
    from trial_balance()'s own total, the bug is in trial_balance(), not
    in the underlying data.
    """
    total_dr = ZERO
    total_cr = ZERO
    for tx in gl.transactions:
        for line in tx.lines:
            total_dr += line.dr
            total_cr += line.cr
    return total_dr, total_cr


def _assert_books_balance(test_case: unittest.TestCase, gl: GeneralLedger, context: str):
    # Check 1: raw stored lines, bypassing all reporting methods.
    raw_dr, raw_cr = _raw_total_dr_cr(gl)
    test_case.assertEqual(raw_dr, raw_cr, f"{context}: raw stored debits/credits diverged ({raw_dr} vs {raw_cr})")

    # Check 2: trial_balance(), which re-derives dr/cr from each account's
    # own signed balance and its normal-balance-type classification.
    tb = gl.trial_balance()
    tb_dr = sum((row["dr"] for row in tb), ZERO)
    tb_cr = sum((row["cr"] for row in tb), ZERO)
    test_case.assertEqual(tb_dr, tb_cr, f"{context}: trial_balance() debits/credits diverged ({tb_dr} vs {tb_cr})")

    # Check 3: the fundamental accounting equation via balance_sheet(),
    # a third, independent code path.
    bs = gl.balance_sheet()
    assets = bs["assets"]["total"]
    liab = bs["liabilities"]["total"]
    eq = bs["equity"]["total"]
    rev = bs["income"]["total"]
    exp = bs["expenses"]["total"]
    test_case.assertEqual(
        assets, liab + eq + rev - exp,
        f"{context}: accounting equation violated (assets={assets}, liab+eq+rev-exp={liab + eq + rev - exp})"
    )


class TestBalanceInvariantFuzz(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cognibudget_fuzz_")
        self.db_path = os.path.join(self.tmpdir, "fuzz.db")
        self.gl = GeneralLedger(self.db_path, "FuzzTestPassphrase123!")

        # Broaden beyond the default chart of accounts so all five
        # account types (and several accounts per type) get exercised,
        # not just the two or three that happen to be seeded by default.
        extra_accounts = [
            ("Savings", "Asset"),
            ("Owner Equity", "Equity"),
            ("Loan Payable", "Liability"),
            ("Consulting Income", "Revenue"),
            ("Office Supplies", "Expense"),
            ("Travel Expense", "Expense"),
        ]
        for name, acc_type in extra_accounts:
            if name not in self.gl.accounts:
                self.gl.create_account(name, acc_type)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_fuzz(self, seed: int):
        rng = random.Random(seed)
        accounts = _all_account_names(self.gl)
        posted_tx_ids = []

        _assert_books_balance(self, self.gl, f"seed={seed} initial state")

        for i in range(ITERATIONS):
            action = rng.choice(["post_2leg", "post_multileg", "edit", "delete", "reject_unbalanced"])
            ctx = f"seed={seed} iteration={i} action={action}"

            if action == "post_2leg":
                dr_acc, cr_acc = rng.sample(accounts, 2)
                amount = Decimal(rng.randint(1, 500000)) / 100  # $0.01 - $5000.00
                date = f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
                ok, msg = self.gl.record_transaction(
                    date, f"Fuzz {i}", [(dr_acc, amount, 0), (cr_acc, 0, amount)]
                )
                self.assertTrue(ok, f"{ctx}: expected valid balanced post to succeed, got: {msg}")
                posted_tx_ids.append(self.gl.transactions[-1].id)

            elif action == "post_multileg":
                # Split one side across two accounts -- still balanced overall.
                legs = rng.sample(accounts, 3)
                total = Decimal(rng.randint(100, 500000)) / 100
                part = money(total / 2)
                remainder = money(total - part)
                date = f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
                ok, msg = self.gl.record_transaction(
                    date, f"Fuzz split {i}",
                    [(legs[0], part, 0), (legs[1], remainder, 0), (legs[2], 0, total)]
                )
                self.assertTrue(ok, f"{ctx}: expected valid balanced multi-leg post to succeed, got: {msg}")
                posted_tx_ids.append(self.gl.transactions[-1].id)

            elif action == "edit" and posted_tx_ids:
                tx_id = rng.choice(posted_tx_ids)
                dr_acc, cr_acc = rng.sample(accounts, 2)
                amount = Decimal(rng.randint(1, 500000)) / 100
                date = f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
                # update_transaction may legitimately fail if tx_id was
                # already deleted by a prior fuzz step -- that's fine,
                # just don't assert success unconditionally.
                self.gl.update_transaction(tx_id, date, f"Fuzz edit {i}", [(dr_acc, amount, 0), (cr_acc, 0, amount)])

            elif action == "delete" and posted_tx_ids:
                tx_id = rng.choice(posted_tx_ids)
                self.gl.delete_transaction(tx_id)
                # Leave it in posted_tx_ids -- deleting an already-deleted
                # id should just fail gracefully, which is itself worth
                # exercising here.

            elif action == "reject_unbalanced":
                dr_acc, cr_acc = rng.sample(accounts, 2)
                amount = Decimal(rng.randint(1, 500000)) / 100
                off_by = Decimal("0.01")
                date = f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}"
                ok, msg = self.gl.record_transaction(
                    date, f"Fuzz bad {i}", [(dr_acc, amount, 0), (cr_acc, 0, amount - off_by)]
                )
                self.assertFalse(ok, f"{ctx}: an unbalanced transaction was incorrectly accepted")

            # The core assertion: no matter which operation just ran
            # (including a *rejected* one), the books must still balance.
            _assert_books_balance(self, self.gl, ctx)

    def test_fuzz_seed_1234(self):
        self._run_fuzz(SEEDS[0])

    def test_fuzz_seed_987654321(self):
        self._run_fuzz(SEEDS[1])


if __name__ == "__main__":
    unittest.main()
