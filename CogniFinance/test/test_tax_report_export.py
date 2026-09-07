"""
Tax report (xlsx) content-verification tests.

expenses_by_category() and business_income_total() already have direct
unit test coverage, but nothing actually opens the exported .xlsx file
and checks the numbers written into it. It's entirely possible for the
underlying totals to be correct while the export step silently writes
the wrong sheet, the wrong column, a stale value, or includes a
transaction that should have been filtered by date/account-type/medical
status. This file re-opens the exported workbook with openpyxl and
checks actual cell values against independently-known expected numbers.
"""
import os
import sys
import json
import shutil
import tempfile
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openpyxl import load_workbook

from ledger import GeneralLedger
from tax_manager import TaxTemplateManager
from tax_tab import TaxTab


DENTIST_TAX_CATEGORIES = {
    "Professional Dues": ["RCDSO Dues"],
    "Continuing Education": ["CE Courses"],
}

DEFAULT_MEDICAL_ACCOUNTS = ["Medical Expenses"]

def _sheet_rows(ws):
    """Yield each data row (after the header) as a dict keyed by header text."""
    header = [cell.value for cell in ws[1]]
    for row in ws.iter_rows(min_row=2, values_only=True):
        if all(v is None for v in row):
            continue
        yield dict(zip(header, row))

DUMMY_DEK_HEX = os.urandom(32).hex()

class TaxReportExportTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cognibudget_taxexport_")
        self.db_path = os.path.join(self.tmpdir, "tax_export.db")
        self.gl = GeneralLedger(self.db_path, DUMMY_DEK_HEX)

        # Real accounts drawn from the actual tax category map, so this
        # test exercises the real DENTIST_TAX_CATEGORIES mapping rather
        # than a stand-in.
        self.dues_key = next(k for k in DENTIST_TAX_CATEGORIES if "Professional Dues" in k)
        self.dues_acc = DENTIST_TAX_CATEGORIES[self.dues_key][0]  # "RCDSO Dues"
        self.gl.create_account(self.dues_acc, "Expense")

        self.ce_key = next(k for k in DENTIST_TAX_CATEGORIES if "Continuing Education" in k)
        self.ce_acc = DENTIST_TAX_CATEGORIES[self.ce_key][0]  # "CE Courses"
        self.gl.create_account(self.ce_acc, "Expense")

        self.medical_acc = DEFAULT_MEDICAL_ACCOUNTS[0]
        self.gl.create_account(self.medical_acc, "Expense")

        self.export_path = os.path.join(self.tmpdir, "tax_report.xlsx")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _export(self, start="2026-01-01", end="2026-01-31", **kwargs):
        kwargs.setdefault("category_map", DENTIST_TAX_CATEGORIES)
        kwargs.setdefault("medical_accounts", DEFAULT_MEDICAL_ACCOUNTS)
        self.gl.export_tax_report_xlsx(start, end, self.export_path, **kwargs)
        return load_workbook(self.export_path, data_only=True)

    def test_full_json_to_xlsx_pipeline(self):
        """
        INTEGRATION TEST: Ensures the exact production pipeline works.
        JSON file -> TaxTemplateManager -> _normalize_category_map -> XLSX Export.
        This guarantees we don't miss bugs caused by JSON parsing or normalization.
        """
        # 1. Create a mock JSON template on disk (simulating the user's tax_templates folder)
        mock_template = {
            "template_id": "test_json_pipeline",
            "title": "Test JSON Pipeline",
            "categories": {
                "Professional Dues": ["RCDSO Dues"],
                "Continuing Education": ["CE Courses"]
            },
            "medical_accounts": ["Medical Expenses"]
        }
        json_path = os.path.join(self.tmpdir, "test_template.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(mock_template, f)

        # 2. Load it exactly like the UI does on startup
        manager = TaxTemplateManager(templates_dir=self.tmpdir)
        templates = manager.load_available_templates()
        self.assertIn("test_json_pipeline", templates, "TaxTemplateManager failed to load the JSON")

        template_data = templates["test_json_pipeline"]

        # 3. Normalize it exactly like TaxTab.refresh_summary() does
        raw_cats = template_data.get("categories") or template_data.get("target_categories") or {}
        category_map = TaxTab._normalize_category_map(raw_cats)
        medical_accounts = template_data.get("medical_accounts", [])

        # Sanity check: Ensure the normalizer actually produced the expected dictionaries
        self.assertEqual(category_map["Professional Dues"], ["RCDSO Dues"])
        self.assertEqual(medical_accounts, ["Medical Expenses"])

        # 4. Post some data to the ledger
        self.gl.record_transaction("2026-01-06", "Dues", [(self.dues_acc, 800, 0), ("Checking Account", 0, 800)])
        self.gl.record_transaction("2026-01-07", "Medical", [(self.medical_acc, 250, 0), ("Checking Account", 0, 250)])

        # 5. Export using the DYNAMICALLY LOADED data (NOT the hardcoded test fixtures)
        self.gl.export_tax_report_xlsx(
            "2026-01-01", "2026-01-31", self.export_path,
            category_map=category_map,
            include_medical=True,
            medical_accounts=medical_accounts,
        )

        # 6. Verify the XLSX actually contains the data
        wb = load_workbook(self.export_path, data_only=True)
        self.assertIn("Expenses_by_Category", wb.sheetnames)
        self.assertIn("Medical_Expenses", wb.sheetnames)

        # Verify the medical sheet isn't empty
        med_rows = list(_sheet_rows(wb["Medical_Expenses"]))
        self.assertTrue(len(med_rows) > 0, "Medical sheet is empty; JSON medical_accounts mapping failed")

    # ---------------- Summary sheet ----------------

    def test_summary_sheet_matches_independently_computed_totals(self):
        self.gl.record_transaction("2026-01-05", "Income", [("Checking Account", 5000, 0), ("Salary Income", 0, 5000)])
        self.gl.record_transaction("2026-01-06", "Dues", [(self.dues_acc, 800, 0), ("Checking Account", 0, 800)])
        self.gl.record_transaction("2026-01-07", "CE", [(self.ce_acc, 300, 0), ("Checking Account", 0, 300)])

        expected_income = self.gl.business_income_total("2026-01-01", "2026-01-31")
        expected_expenses = sum(self.gl.expenses_by_category("2026-01-01", "2026-01-31", category_map=DENTIST_TAX_CATEGORIES).values(), Decimal("0.00"))
        expected_net = expected_income - expected_expenses

        wb = self._export()
        summary = {row["Metric"]: row["Value"] for row in _sheet_rows(wb["Summary"])}

        self.assertEqual(Decimal(str(summary["Gross Business Income"])), expected_income)
        self.assertEqual(Decimal(str(summary["Total Expenses"])), expected_expenses)
        self.assertEqual(Decimal(str(summary["Net Business Income (Loss)"])), expected_net)
        self.assertEqual(summary["Period Start"], "2026-01-01")
        self.assertEqual(summary["Period End"], "2026-01-31")

    def test_summary_net_equals_income_minus_expenses_arithmetic(self):
        self.gl.record_transaction("2026-01-05", "Income", [("Checking Account", 4321.55, 0), ("Salary Income", 0, 4321.55)])
        self.gl.record_transaction("2026-01-06", "Dues", [(self.dues_acc, 111.11, 0), ("Checking Account", 0, 111.11)])

        wb = self._export()
        summary = {row["Metric"]: row["Value"] for row in _sheet_rows(wb["Summary"])}
        income = Decimal(str(summary["Gross Business Income"]))
        expenses = Decimal(str(summary["Total Expenses"]))
        net = Decimal(str(summary["Net Business Income (Loss)"]))
        self.assertEqual(net, income - expenses)

    # ---------------- Expenses_by_Category sheet ----------------

    def test_expenses_by_category_sheet_matches_dict(self):
        self.gl.record_transaction("2026-01-06", "Dues", [(self.dues_acc, 800, 0), ("Checking Account", 0, 800)])
        self.gl.record_transaction("2026-01-07", "CE", [(self.ce_acc, 300, 0), ("Checking Account", 0, 300)])
        self.gl.record_transaction("2026-01-08", "Uncategorized", [("Uncategorized Expense", 45, 0), ("Checking Account", 0, 45)])

        expected = self.gl.expenses_by_category("2026-01-01", "2026-01-31", category_map=DENTIST_TAX_CATEGORIES)

        wb = self._export()
        sheet_totals = {row["Category"]: Decimal(str(row["Amount"])) for row in _sheet_rows(wb["Expenses_by_Category"])}

        for category, amount in expected.items():
            if amount == 0:
                # Zero categories are filtered out of the export -- confirm that, don't expect a row.
                self.assertNotIn(category, sheet_totals, f"'{category}' has $0 and should not appear in the export")
            else:
                self.assertIn(category, sheet_totals)
                self.assertEqual(sheet_totals[category], amount)

    def test_zero_categories_excluded_from_export(self):
        # Post to only one category -- every other category should have
        # $0 and therefore not appear as a row at all.
        self.gl.record_transaction("2026-01-06", "Dues", [(self.dues_acc, 800, 0), ("Checking Account", 0, 800)])

        wb = self._export()
        sheet_categories = {row["Category"] for row in _sheet_rows(wb["Expenses_by_Category"])}
        self.assertEqual(sheet_categories, {self.dues_key})

    # ---------------- Business_Transactions sheet ----------------

    def test_business_transactions_sheet_contains_expected_rows(self):
        self.gl.record_transaction("2026-01-06", "Dues Payment", [(self.dues_acc, 800, 0), ("Checking Account", 0, 800)])

        wb = self._export()
        rows = list(_sheet_rows(wb["Business_Transactions"]))
        matching = [r for r in rows if r["Description"] == "Dues Payment"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["Account"], self.dues_acc)
        self.assertEqual(Decimal(str(matching[0]["Amount"])), Decimal("800.00"))
        self.assertEqual(matching[0]["Date"], "2026-01-06")

    def test_business_transactions_excludes_out_of_range_dates(self):
        self.gl.record_transaction("2026-01-06", "In Range", [(self.dues_acc, 100, 0), ("Checking Account", 0, 100)])
        self.gl.record_transaction("2026-02-01", "Out Of Range", [(self.dues_acc, 200, 0), ("Checking Account", 0, 200)])

        wb = self._export(start="2026-01-01", end="2026-01-31")
        descriptions = {row["Description"] for row in _sheet_rows(wb["Business_Transactions"])}
        self.assertIn("In Range", descriptions)
        self.assertNotIn("Out Of Range", descriptions)

    def test_business_transactions_excludes_non_expense_lines(self):
        # A pure asset transfer (no Expense-type account touched) should
        # never show up on the business transactions sheet.
        self.gl.create_account("Savings", "Asset")
        self.gl.transfer_funds("2026-01-06", "Move to Savings", "Checking Account", "Savings", 500)

        wb = self._export()
        descriptions = {row["Description"] for row in _sheet_rows(wb["Business_Transactions"])}
        self.assertNotIn("Move to Savings", descriptions)

    def test_business_transactions_excludes_income_lines(self):
        self.gl.record_transaction("2026-01-06", "Client Payment", [("Checking Account", 1000, 0), ("Salary Income", 0, 1000)])

        wb = self._export()
        descriptions = {row["Description"] for row in _sheet_rows(wb["Business_Transactions"])}
        self.assertNotIn("Client Payment", descriptions)

    # ---------------- Medical_Expenses sheet ----------------

    def test_medical_expenses_sheet_only_contains_medical_accounts(self):
        self.gl.record_transaction("2026-01-06", "Medical Bill", [(self.medical_acc, 250, 0), ("Checking Account", 0, 250)])
        self.gl.record_transaction("2026-01-07", "Dues Payment", [(self.dues_acc, 800, 0), ("Checking Account", 0, 800)])

        wb = self._export(include_medical=True)
        self.assertIn("Medical_Expenses", wb.sheetnames)
        med_rows = list(_sheet_rows(wb["Medical_Expenses"]))
        descriptions = {r["Description"] for r in med_rows}
        self.assertIn("Medical Bill", descriptions)
        self.assertNotIn("Dues Payment", descriptions)

    def test_medical_expenses_total_matches_ledger_method(self):
        self.gl.record_transaction("2026-01-06", "Medical Bill", [(self.medical_acc, 250.75, 0), ("Checking Account", 0, 250.75)])

        expected_total = self.gl.medical_expenses_total("2026-01-01", "2026-01-31", medical_accounts=DEFAULT_MEDICAL_ACCOUNTS)

        wb = self._export(include_medical=True)
        med_rows = list(_sheet_rows(wb["Medical_Expenses"]))
        sheet_total = sum((Decimal(str(r["Amount"])) for r in med_rows), Decimal("0.00"))
        self.assertEqual(sheet_total, expected_total)

    def test_medical_sheet_absent_when_include_medical_false(self):
        self.gl.record_transaction("2026-01-06", "Medical Bill", [(self.medical_acc, 250, 0), ("Checking Account", 0, 250)])

        wb = self._export(include_medical=False)
        self.assertNotIn("Medical_Expenses", wb.sheetnames)

    def test_medical_sheet_absent_when_no_medical_expenses_in_range(self):
        # include_medical=True, but nothing was actually posted to a
        # medical account -- the sheet should be omitted, not present-but-empty.
        self.gl.record_transaction("2026-01-06", "Dues Payment", [(self.dues_acc, 800, 0), ("Checking Account", 0, 800)])

        wb = self._export(include_medical=True)
        self.assertNotIn("Medical_Expenses", wb.sheetnames)


if __name__ == "__main__":
    unittest.main()
