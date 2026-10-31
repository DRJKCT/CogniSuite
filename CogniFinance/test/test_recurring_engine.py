"""
test_recurring_engine.py

Verifies the recurring-charge detection engine against plain data -- no
database, no PyQt, no ledger. recurring_engine.py is deliberately a pure
module (plain dicts in, plain dicts out), so every guarantee about how
a recurring pattern is detected can be tested by handing it a list of
charges and inspecting what comes back.

Three classes of guarantee:
  1. CADENCE -- a charge that really does recur on a weekly, monthly,
     quarterly or annual cycle is detected with that interval, and one
     that only looks vaguely periodic is not.
  2. AMOUNT -- a fixed-amount subscription keeps full precision, a
     variable-amount utility bill is still detected but reports its
     spread and scores lower.
  3. ARITHMETIC -- the confidence formula, the amount statistics and
     the calendar-aware next-date math behave as documented.
"""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recurring_engine import (
    detect_recurring_candidates,
    compute_confidence,
    compute_declared_confidence,
    amount_stability,
    add_interval,
    SUGGEST_THRESHOLD,
    VOLUME_CAP,
)


def charge(d, amount, merchant_key="NETFLIX", category="Subscriptions"):
    return {
        "date": d,
        "merchant_key": merchant_key,
        "merchant_display": merchant_key,
        "category": category,
        "amount": Decimal(amount),
    }


class TestCadence(unittest.TestCase):

    def test_fixed_monthly_subscription(self):
        charges = [charge(f"2025-0{m}-15", "15.00") for m in range(1, 7)]
        candidates = detect_recurring_candidates(charges)
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(c["interval"], "monthly")
        self.assertEqual(c["occurrences"], 6)
        self.assertEqual(c["expected_amount"], Decimal("15.00"))
        self.assertEqual(c["amount_variance"], Decimal("0.00"))
        self.assertEqual(c["expected_next_date"], "2025-07-15")

    def test_weekly(self):
        charges = [charge(f"2025-01-{d:02d}", "9.99") for d in (6, 13, 20, 27)]
        candidates = detect_recurring_candidates(charges)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["interval"], "weekly")
        self.assertEqual(candidates[0]["expected_next_date"], "2025-02-03")

    def test_quarterly(self):
        charges = [charge(d, "120.00") for d in
                   ("2025-01-10", "2025-04-10", "2025-07-10", "2025-10-10")]
        candidates = detect_recurring_candidates(charges)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["interval"], "quarterly")
        self.assertEqual(candidates[0]["expected_next_date"], "2026-01-10")

    def test_annual(self):
        charges = [charge(d, "45.00") for d in
                   ("2023-03-02", "2024-03-02", "2025-03-02")]
        candidates = detect_recurring_candidates(charges)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["interval"], "annual")
        self.assertEqual(candidates[0]["expected_next_date"], "2026-03-02")

    def test_irregular_gaps_are_not_detected(self):
        charges = [charge(d, "30.00") for d in
                   ("2025-01-05", "2025-01-30", "2025-04-12", "2025-06-02", "2025-09-17")]
        self.assertEqual(detect_recurring_candidates(charges), [])

    def test_one_gap_outlier_lowers_regularity(self):
        charges = [charge(d, "30.00") for d in
                   ("2025-01-05", "2025-02-05", "2025-03-05", "2025-05-05")]
        candidates = detect_recurring_candidates(charges)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["interval"], "monthly")
        self.assertLess(candidates[0]["interval_regularity"], 1.0)

    def test_below_min_occurrences(self):
        charges = [charge("2025-01-15", "15.00"), charge("2025-02-15", "15.00")]
        self.assertEqual(detect_recurring_candidates(charges), [])

    def test_same_merchant_different_categories_are_separate(self):
        charges = [
            charge("2025-01-15", "15.00", "NETFLIX", "Subscriptions"),
            charge("2025-02-15", "15.00", "NETFLIX", "Subscriptions"),
            charge("2025-03-15", "15.00", "NETFLIX", "Subscriptions"),
            charge("2025-01-20", "40.00", "NETFLIX", "Groceries"),
            charge("2025-02-20", "42.00", "NETFLIX", "Groceries"),
            charge("2025-03-20", "38.00", "NETFLIX", "Groceries"),
        ]
        candidates = detect_recurring_candidates(charges)
        self.assertEqual(len(candidates), 2)
        self.assertEqual({c["category"] for c in candidates},
                         {"Subscriptions", "Groceries"})


class TestAmounts(unittest.TestCase):

    def test_fixed_amount_is_perfectly_stable(self):
        self.assertEqual(amount_stability(Decimal("15.00"), Decimal("0.00")), 1.0)

    def test_variable_amount_reports_spread_and_scores_lower(self):
        charges = [charge(d, a) for d, a in (
            ("2025-01-15", "80.00"), ("2025-02-15", "95.40"),
            ("2025-03-15", "72.10"), ("2025-04-15", "110.25"),
            ("2025-05-15", "88.00"), ("2025-06-15", "91.50"),
        )]
        candidates = detect_recurring_candidates(charges)
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(c["interval"], "monthly")
        self.assertGreater(c["amount_variance"], Decimal("0.00"))
        self.assertLess(c["amount_stability"], 1.0)
        # ...but a regular bill IS recurring, however much it moves.
        self.assertGreaterEqual(c["confidence"], SUGGEST_THRESHOLD)


class TestArithmetic(unittest.TestCase):

    def test_confidence_matches_documented_formula(self):
        volume = min(1.0, 6 / VOLUME_CAP)
        expected = 0.9 * 0.8 * volume * 1.0
        self.assertAlmostEqual(
            compute_confidence(0.9, 0.8, 6, "yellow"), expected, places=6)

    def test_green_rating_is_a_multiplier_not_a_clamp(self):
        yellow = compute_confidence(0.9, 1.0, 6, "yellow")
        green = compute_confidence(0.9, 1.0, 6, "green")
        self.assertAlmostEqual(green, yellow * 1.2, places=6)
        self.assertGreater(green, yellow)

    def test_declared_confidence_is_the_declaration_itself(self):
        self.assertGreater(compute_declared_confidence("green"), 1.0)

    def test_next_date_is_calendar_aware(self):
        self.assertEqual(add_interval("2025-01-31", "monthly"), "2025-02-28")
        self.assertEqual(add_interval("2024-01-31", "monthly"), "2024-02-29")
        self.assertEqual(add_interval("2025-01-15", "quarterly"), "2025-04-15")
        self.assertEqual(add_interval("2025-01-15", "annual"), "2026-01-15")
        self.assertEqual(add_interval("2025-01-06", "weekly"), "2025-01-13")


if __name__ == "__main__":
    unittest.main()
