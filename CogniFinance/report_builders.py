# report_builders.py
"""
Shared report structure and builders for CogniFinance.

A report is a list of `ReportSection`, each containing a list of
`ReportLine`s. Renderers (HTML, XLSX, PDF) walk this structure without
knowing anything about how the numbers were produced.

Currently builds the Income Statement (P&L). The same dataclasses are
intended to serve the balance sheet, equity statement, and trial balance
when those are built.

Sign convention: revenue lines are positive, expense lines are negative.
Subtotal arithmetic is therefore plain addition -- Gross Profit = sum of
revenue lines + sum of COGS lines (COGS lines being negative), and so on.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional, Dict, Any

from ledger import GeneralLedger, ZERO, money


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

@dataclass
class ReportLine:
    label: str
    amount: Decimal = ZERO
    compare_amount: Optional[Decimal] = None
    indent: int = 0
    is_total: bool = False
    is_section_header: bool = False
    accounts: List[str] = field(default_factory=list)
    columns: Optional[Dict[str, Decimal]] = None


@dataclass
class ReportSection:
    title: str
    lines: List[ReportLine] = field(default_factory=list)
    total: Decimal = ZERO
    compare_total: Optional[Decimal] = None
    total_label: str = "TOTAL"
    column_headers: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Comparison-period helper
# ---------------------------------------------------------------------------

def _prior_year_same_dates(start_date: str, end_date: str) -> tuple:
    """
    Same calendar dates, one year earlier. Clamps Feb 29 to Feb 28 when
    the previous year isn't a leap year.
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")

    def _try_shift(dt: datetime, year_delta: int) -> datetime:
        try:
            return dt.replace(year=dt.year + year_delta)
        except ValueError:
            return dt.replace(year=dt.year + year_delta, day=28)

    return (
        _try_shift(start, -1).strftime("%Y-%m-%d"),
        _try_shift(end, -1).strftime("%Y-%m-%d"),
    )


def _prior_period_same_length(start_date: str, end_date: str) -> tuple:
    """
    Immediately preceding period, same number of days.
    """
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    length_days = (end - start).days

    compare_end = start - timedelta(days=1)
    compare_start = compare_end - timedelta(days=length_days)

    return (
        compare_start.strftime("%Y-%m-%d"),
        compare_end.strftime("%Y-%m-%d"),
    )


# ---------------------------------------------------------------------------
# Income Statement builder
# ---------------------------------------------------------------------------

def build_income_statement(
    ledger: GeneralLedger,
    start_date: str,
    end_date: str,
    include_comparison: bool = False,
    comparison_mode: str = "prior_year",
) -> Dict[str, Any]:
    """
    Build the income statement structure.

    comparison_mode:
      "prior_year"   -- same calendar dates, one year earlier (default)
      "prior_period" -- immediately preceding period, same length

    Returns a dict:
      {
        "sections": List[ReportSection],
        "start_date": str, "end_date": str,
        "compare_start": Optional[str], "compare_end": Optional[str],
      }
    """
    current = ledger.income_statement_data(start_date, end_date)

    if include_comparison:
        if comparison_mode == "prior_period":
            compare_start, compare_end = _prior_period_same_length(start_date, end_date)
        else:
            compare_start, compare_end = _prior_year_same_dates(start_date, end_date)
        compare = ledger.income_statement_data(compare_start, compare_end)
    else:
        compare_start = compare_end = None
        compare = None

    def cmp_of(cat_name: str) -> Optional[Decimal]:
        if compare is None:
            return None
        return compare["totals"].get(cat_name, ZERO)

    sections: List[ReportSection] = []

    # ----- REVENUE ---------------------------------------------------------
    revenue_lines: List[ReportLine] = []
    for subgroup_key, subgroup_label in (
        ("operating_revenue", "Operating Revenue"),
        ("non_operating_revenue", "Non-Operating Revenue"),
        ("other_revenue", "Other Income"),
    ):
        accounts = current["groups"].get(subgroup_key, [])
        if not accounts:
            continue
        revenue_lines.append(ReportLine(
            label=subgroup_label, is_section_header=True, indent=0,
        ))
        for name, amt in accounts:
            c_amt = None
            if compare is not None:
                c_amt = next((a for n, a in compare["groups"].get(subgroup_key, []) if n == name), ZERO)
            revenue_lines.append(ReportLine(
                label=name, amount=amt, compare_amount=c_amt, indent=1,
                accounts=[name],
            ))

    total_revenue = current["totals"].get("total_revenue", ZERO)
    sections.append(ReportSection(
        title="REVENUE",
        lines=revenue_lines,
        total=total_revenue,
        compare_total=cmp_of("total_revenue"),
        total_label="TOTAL REVENUE",
    ))

    # ----- COST OF GOODS SOLD ---------------------------------------------
    cogs_lines: List[ReportLine] = []
    for name, amt in current["groups"].get("cogs", []):
        c_amt = None
        if compare is not None:
            c_amt = next((a for n, a in compare["groups"].get("cogs", []) if n == name), ZERO)
        cogs_lines.append(ReportLine(label=name, amount=amt, compare_amount=c_amt, indent=1, accounts=[name]))
    total_cogs = current["totals"].get("total_cogs", ZERO)
    sections.append(ReportSection(
        title="COST OF GOODS SOLD",
        lines=cogs_lines,
        total=total_cogs,
        compare_total=cmp_of("total_cogs"),
        total_label="TOTAL COGS",
    ))

    # ----- GROSS PROFIT ----------------------------------------------------
    gross_profit = current["totals"].get("gross_profit", ZERO)
    gross_profit_cmp = None
    if compare is not None:
        gross_profit_cmp = compare["totals"].get("gross_profit", ZERO)
    sections.append(ReportSection(
        title="",
        lines=[ReportLine(label="GROSS PROFIT", amount=gross_profit, compare_amount=gross_profit_cmp, is_total=True)],
        total=gross_profit,
        compare_total=gross_profit_cmp,
        total_label="GROSS PROFIT",
    ))

    # ----- OPERATING EXPENSES ---------------------------------------------
    opex_lines: List[ReportLine] = []
    for name, amt in current["groups"].get("operating_expenses", []):
        c_amt = None
        if compare is not None:
            c_amt = next((a for n, a in compare["groups"].get("operating_expenses", []) if n == name), ZERO)
        opex_lines.append(ReportLine(label=name, amount=amt, compare_amount=c_amt, indent=1, accounts=[name]))
    total_opex = current["totals"].get("total_operating_expenses", ZERO)
    sections.append(ReportSection(
        title="OPERATING EXPENSES",
        lines=opex_lines,
        total=total_opex,
        compare_total=cmp_of("total_operating_expenses"),
        total_label="TOTAL OPERATING EXPENSES",
    ))

    # ----- OPERATING INCOME -----------------------------------------------
    operating_income = current["totals"].get("operating_income", ZERO)
    operating_income_cmp = None
    if compare is not None:
        operating_income_cmp = compare["totals"].get("operating_income", ZERO)
    sections.append(ReportSection(
        title="",
        lines=[ReportLine(label="OPERATING INCOME", amount=operating_income, compare_amount=operating_income_cmp, is_total=True)],
        total=operating_income,
        compare_total=operating_income_cmp,
        total_label="OPERATING INCOME",
    ))

    # ----- OTHER INCOME / EXPENSE -----------------------------------------
    other_lines: List[ReportLine] = []
    for subgroup_key, subgroup_label in (
        ("investment_income", "Investment & Crypto Income"),
        ("investment_losses", "Investment & Crypto Losses"),
        ("other_expenses", "Other Expenses"),
        ("depreciation", "Depreciation & Amortization"),
        ("interest_and_bank", "Interest & Bank Charges"),
        ("income_tax", "Income Tax Expense"),
        ("other_unclassified", "Unclassified Expenses"),
    ):
        accounts = current["groups"].get(subgroup_key, [])
        if not accounts:
            continue
        other_lines.append(ReportLine(label=subgroup_label, is_section_header=True, indent=0))
        for name, amt in accounts:
            c_amt = None
            if compare is not None:
                c_amt = next((a for n, a in compare["groups"].get(subgroup_key, []) if n == name), ZERO)
            other_lines.append(ReportLine(label=name, amount=amt, compare_amount=c_amt, indent=1, accounts=[name]))

    total_other = current["totals"].get("total_other", ZERO)
    sections.append(ReportSection(
        title="OTHER INCOME / EXPENSE",
        lines=other_lines,
        total=total_other,
        compare_total=cmp_of("total_other"),
        total_label="TOTAL OTHER",
    ))

    # ----- NET INCOME BEFORE TAX ------------------------------------------
    net = current["totals"].get("net_income", ZERO)
    net_cmp = None
    if compare is not None:
        net_cmp = compare["totals"].get("net_income", ZERO)
    sections.append(ReportSection(
        title="",
        lines=[ReportLine(label="NET INCOME BEFORE TAX", amount=net, compare_amount=net_cmp, is_total=True)],
        total=net,
        compare_total=net_cmp,
        total_label="NET INCOME BEFORE TAX",
    ))

    return {
        "sections": sections,
        "start_date": start_date,
        "end_date": end_date,
        "compare_start": compare_start,
        "compare_end": compare_end,
    }

def export_income_statement_xlsx(
    built: Dict[str, Any],
    path: str,
    period_label: str,
    compare_label: Optional[str] = None,
) -> None:
    """
    Write an income statement to XLSX from the structure returned by
    build_income_statement. Reads only the built dict -- no ledger access,
    no recomputation -- so the file always matches what the renderer
    displays.

    Raises ImportError if openpyxl is unavailable, and OSError on write
    failure; callers should surface those to the user.
    """
    import openpyxl
    from openpyxl.styles import Font, Alignment, Border, Side

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Income Statement"

    # --- Column widths ---
    ws.column_dimensions["A"].width = 45
    ws.column_dimensions["B"].width = 16
    if compare_label is not None:
        ws.column_dimensions["C"].width = 16

    # --- Styles ---
    title_font = Font(bold=True, size=14)
    subtitle_font = Font(italic=True, size=11)
    section_font = Font(bold=True)
    total_font = Font(bold=True)
    subtotal_font = Font(bold=True, size=11)
    header_font = Font(bold=True)
    right_align = Alignment(horizontal="right")
    thin_top = Border(top=Side(style="thin"))
    double_bottom = Border(bottom=Side(style="double"))

    def num_cell(amount: Optional[Decimal]) -> Optional[float]:
        if amount is None:
            return None
        return float(amount)

    # --- Title block ---
    ws["A1"] = "INCOME STATEMENT"
    ws["A1"].font = title_font
    ws["A2"] = f"Period: {period_label}"
    ws["A2"].font = subtitle_font
    row = 3
    if compare_label is not None:
        ws.cell(row=row, column=1, value=f"Comparison: {compare_label}").font = subtitle_font
        row += 1

    row += 1  # blank spacer

    # --- Column headers ---
    ws.cell(row=row, column=1, value="").font = header_font
    cell_b = ws.cell(row=row, column=2, value=period_label)
    cell_b.font = header_font
    cell_b.alignment = right_align
    if compare_label is not None:
        cell_c = ws.cell(row=row, column=3, value=compare_label)
        cell_c.font = header_font
        cell_c.alignment = right_align
    row += 1

    # --- Body ---
    for section in built["sections"]:
        # Section title (skip if empty -- pure subtotal sections have "")
        if section.title:
            c = ws.cell(row=row, column=1, value=section.title)
            c.font = section_font
            row += 1

        # Lines
        for line in section.lines:
            label_cell = ws.cell(row=row, column=1, value=line.label)
            # Indent via leading spaces -- openpyxl doesn't do indent for plain cells
            if line.indent:
                label_cell.value = "    " * line.indent + line.label
            if line.is_total:
                label_cell.font = subtotal_font

            amt_cell = ws.cell(row=row, column=2, value=num_cell(line.amount))
            amt_cell.number_format = "#,##0.00;(#,##0.00)"
            amt_cell.alignment = right_align
            if line.is_total:
                amt_cell.font = subtotal_font

            if compare_label is not None:
                cmp_cell = ws.cell(row=row, column=3, value=num_cell(line.compare_amount))
                cmp_cell.number_format = "#,##0.00;(#,##0.00)"
                cmp_cell.alignment = right_align
                if line.is_total:
                    cmp_cell.font = subtotal_font

            row += 1

        # Section total (only if the section has a title -- a subtotal-only
        # section already rendered its label as a line)
        if section.title and section.total_label:
            tot_label = ws.cell(row=row, column=1, value=section.total_label)
            tot_label.font = total_font
            tot_label.border = thin_top

            tot_amt = ws.cell(row=row, column=2, value=num_cell(section.total))
            tot_amt.number_format = "#,##0.00;(#,##0.00)"
            tot_amt.alignment = right_align
            tot_amt.font = total_font
            tot_amt.border = thin_top

            if compare_label is not None:
                tot_cmp = ws.cell(row=row, column=3, value=num_cell(section.compare_total))
                tot_cmp.number_format = "#,##0.00;(#,##0.00)"
                tot_cmp.alignment = right_align
                tot_cmp.font = total_font
                tot_cmp.border = thin_top

            row += 1

        row += 1  # blank spacer between sections

    # --- Final double rule under the last total ---
    # Not strictly required, but visually confirms "end of statement"

    wb.save(path)

# ---------------------------------------------------------------------------
# Balance Sheet builder
# ---------------------------------------------------------------------------

# Canonical ordering of subcategories within each top-level section.
# Subcategories not listed here sort to the end, alphabetically, so an
# account with an unexpected classification still appears -- just last.
_SUBCATEGORY_ORDER = {
    "Asset": [
        "Current Assets",
        "Non-Current Assets",
        "Investments",
        "Digital Assets",
    ],
    "Liability": [
        "Current Liabilities",
        "Non-Current Liabilities",
    ],
    "Equity": [
        "Contributed Capital",
        "Earnings",
        "Distributions",
    ],
}

# Display labels for the top-level sections
_SECTION_LABELS = {
    "Asset": "ASSETS",
    "Liability": "LIABILITIES",
    "Equity": "EQUITY",
}


def _shift_year_back(date_str: str) -> str:
    """One year earlier, clamping Feb 29 to Feb 28 on non-leap years."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    try:
        return dt.replace(year=dt.year - 1).strftime("%Y-%m-%d")
    except ValueError:
        return dt.replace(year=dt.year - 1, day=28).strftime("%Y-%m-%d")


def _prior_year_end(date_str: str) -> str:
    """December 31 of the year preceding the given date."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{dt.year - 1}-12-31"


def _sum_leaves(by_line: Dict[str, List[Tuple[str, Decimal]]]) -> Decimal:
    """Total of every account balance in a report_line group."""
    total = ZERO
    for entries in by_line.values():
        for _, bal in entries:
            total += bal
    return money(total)


def build_balance_sheet(
    ledger: GeneralLedger,
    as_of_date: str,
    include_comparison: bool = False,
    comparison_mode: str = "prior_year",
) -> Dict[str, Any]:
    """
    Build the balance sheet structure.

    comparison_mode:
      "prior_year"     -- same date, one year earlier (default)
      "prior_year_end" -- December 31 of the preceding year

    Returns a dict matching build_income_statement's shape:
      {
        "sections": List[ReportSection],
        "start_date": None, "end_date": str,      # as_of date in end
        "compare_start": None,
        "compare_end": Optional[str],
        "balance_check": {"balanced": bool, "difference": Decimal},
      }
    """
    current = ledger.balance_sheet_data(as_of_date)

    if include_comparison:
        if comparison_mode == "prior_year_end":
            compare_date = _prior_year_end(as_of_date)
        else:
            compare_date = _shift_year_back(as_of_date)
        compare = ledger.balance_sheet_data(compare_date)
    else:
        compare_date = None
        compare = None

    sections: List[ReportSection] = []

    def _emit_group(
        section_leaf_map: Dict[str, Dict[str, List[Tuple[str, Decimal]]]],
        compare_leaf_map: Optional[Dict[str, Dict[str, List[Tuple[str, Decimal]]]]],
        subcat_name: str,
    ) -> List[ReportLine]:
        """
        Emit all lines for one subcategory:
          - subcategory header
          - per report_line: either a single flat account (if one account
            in the line) or a header + accounts + subtotal
        """
        lines: List[ReportLine] = []

        if subcat_name is None:
            display_subcat = "Unclassified"
        else:
            display_subcat = subcat_name

        # Subcategory header
        lines.append(ReportLine(label=display_subcat, is_section_header=True, indent=0))

        report_lines = section_leaf_map.get(subcat_name, {})
        # Sort report_line groups alphabetically within the subcategory.
        # The Unclassified group (None key) sorts last via the key function.
        for rl_name in sorted(report_lines.keys(),
                              key=lambda x: (x is None, x or "")):
            entries = report_lines[rl_name]

            # Empty report line -- skip entirely
            if not entries:
                continue

            if rl_name is None:
                # Unclassified accounts: no report-line header, just list them
                for acct_name, bal in entries:
                    c_amt = None
                    if compare_leaf_map is not None:
                        c_amt = _lookup_account(compare_leaf_map, subcat_name, rl_name, acct_name)
                    lines.append(ReportLine(
                        label=acct_name, amount=bal, compare_amount=c_amt,
                        indent=1, accounts=[acct_name],
                    ))
            elif len(entries) == 1:
                # Single-account report line: collapse to just the account
                acct_name, bal = entries[0]
                c_amt = None
                if compare_leaf_map is not None:
                    c_amt = _lookup_account(compare_leaf_map, subcat_name, rl_name, acct_name)
                lines.append(ReportLine(
                    label=acct_name, amount=bal, compare_amount=c_amt,
                    indent=1, accounts=[acct_name],
                ))
            else:
                # Multi-account report line: header + accounts + subtotal
                lines.append(ReportLine(label=rl_name, is_section_header=True, indent=1))

                subtotal = ZERO
                c_subtotal = ZERO if compare_leaf_map is not None else None

                for acct_name, bal in entries:
                    c_amt = None
                    if compare_leaf_map is not None:
                        c_amt = _lookup_account(compare_leaf_map, subcat_name, rl_name, acct_name)
                        c_subtotal += c_amt
                    lines.append(ReportLine(
                        label=acct_name, amount=bal, compare_amount=c_amt,
                        indent=2, accounts=[acct_name],
                    ))
                    subtotal += bal

                lines.append(ReportLine(
                    label=f"Subtotal — {rl_name}",
                    amount=money(subtotal),
                    compare_amount=money(c_subtotal) if c_subtotal is not None else None,
                    indent=2, is_total=False,
                ))

        return lines

    # ----- ASSETS / LIABILITIES / EQUITY ---------------------------------
    for account_type in ("Asset", "Liability", "Equity"):
        section_leaf_map = current.get(account_type, {})
        compare_leaf_map = compare.get(account_type, {}) if compare is not None else None

        lines: List[ReportLine] = []

        # Order subcategories per the canonical order, then any extras alphabetically
        known_order = _SUBCATEGORY_ORDER.get(account_type, [])
        present_subcats = set(section_leaf_map.keys())
        ordered_subcats: List[Optional[str]] = []
        for s in known_order:
            if s in present_subcats:
                ordered_subcats.append(s)
        # Unclassified (None) always last
        extras = sorted(
            (s for s in present_subcats if s is not None and s not in known_order),
            key=lambda x: x or "",
        )
        ordered_subcats.extend(extras)
        if None in present_subcats:
            ordered_subcats.append(None)

        # Emit each subcategory
        for subcat_name in ordered_subcats:
            lines.extend(_emit_group(section_leaf_map, compare_leaf_map, subcat_name))

        # Section total
        section_total = ZERO
        section_total_cmp = ZERO if compare is not None else None
        for by_line in section_leaf_map.values():
            for entries in by_line.values():
                for _, bal in entries:
                    section_total += bal
        if compare is not None:
            for by_line in compare_leaf_map.values():
                for entries in by_line.values():
                    for _, bal in entries:
                        section_total_cmp += bal

        # Attach the total as a synthetic line so it renders inside the section
        lines.append(ReportLine(
            label=f"TOTAL {_SECTION_LABELS[account_type]}",
            amount=money(section_total),
            compare_amount=money(section_total_cmp) if section_total_cmp is not None else None,
            is_total=True,
        ))

        sections.append(ReportSection(
            title=_SECTION_LABELS[account_type],
            lines=lines,
            total=money(section_total),
            compare_total=money(section_total_cmp) if section_total_cmp is not None else None,
            total_label="",  # total rendered as a line, not a section footer
        ))

    # ----- Balance check --------------------------------------------------
    assets_total = _sum_section(current, "Asset")
    liabilities_total = _sum_section(current, "Liability")
    equity_total = _sum_section(current, "Equity")
    difference = money(assets_total - (liabilities_total + equity_total))

    return {
        "sections": sections,
        "start_date": None,
        "end_date": as_of_date,
        "compare_start": None,
        "compare_end": compare_date,
        "balance_check": {
            "balanced": abs(difference) < Decimal("0.005"),
            "difference": difference,
        },
    }


def _lookup_account(
    leaf_map: Dict[str, Dict[str, List[Tuple[str, Decimal]]]],
    subcat: Optional[str],
    report_line: Optional[str],
    account_name: str,
) -> Decimal:
    """Find an account's balance in a leaf map; ZERO if not present."""
    by_line = leaf_map.get(subcat, {})
    entries = by_line.get(report_line, [])
    for name, bal in entries:
        if name == account_name:
            return bal
    return ZERO


def _sum_section(current: Dict[str, Any], account_type: str) -> Decimal:
    """Sum every account balance in a top-level account_type section."""
    total = ZERO
    for by_line in current.get(account_type, {}).values():
        for entries in by_line.values():
            for _, bal in entries:
                total += bal
    return money(total)

def export_balance_sheet_xlsx(
    built: Dict[str, Any],
    path: str,
    period_label: str,
    compare_label: Optional[str] = None,
) -> None:
    """
    Write a balance sheet to XLSX from the structure returned by
    build_balance_sheet. Mirrors export_income_statement_xlsx.
    """
    import openpyxl
    from openpyxl.styles import Font, Alignment, Border, Side

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Balance Sheet"

    ws.column_dimensions["A"].width = 45
    ws.column_dimensions["B"].width = 16
    if compare_label is not None:
        ws.column_dimensions["C"].width = 16

    title_font = Font(bold=True, size=14)
    subtitle_font = Font(italic=True, size=11)
    section_font = Font(bold=True)
    total_font = Font(bold=True)
    header_font = Font(bold=True)
    right_align = Alignment(horizontal="right")
    thin_top = Border(top=Side(style="thin"))

    ws["A1"] = "BALANCE SHEET"
    ws["A1"].font = title_font
    ws["A2"] = f"As of: {period_label}"
    ws["A2"].font = subtitle_font
    row = 3
    if compare_label is not None:
        ws.cell(row=row, column=1, value=f"Comparison: {compare_label}").font = subtitle_font
        row += 1

    row += 1

    # Column headers
    cell_b = ws.cell(row=row, column=2, value=period_label)
    cell_b.font = header_font
    cell_b.alignment = right_align
    if compare_label is not None:
        cell_c = ws.cell(row=row, column=3, value=compare_label)
        cell_c.font = header_font
        cell_c.alignment = right_align
    row += 1

    for section in built["sections"]:
        if section.title:
            ws.cell(row=row, column=1, value=section.title).font = section_font
            row += 1

        for line in section.lines:
            label_cell = ws.cell(row=row, column=1, value=("    " * line.indent) + line.label if line.indent else line.label)
            if line.is_total or line.is_section_header:
                label_cell.font = total_font if line.is_total else section_font

            amt_cell = ws.cell(row=row, column=2, value=float(line.amount))
            amt_cell.number_format = "#,##0.00;(#,##0.00)"
            amt_cell.alignment = right_align
            if line.is_total:
                amt_cell.font = total_font
                amt_cell.border = thin_top

            if compare_label is not None:
                cmp_cell = ws.cell(row=row, column=3, value=float(line.compare_amount) if line.compare_amount is not None else None)
                cmp_cell.number_format = "#,##0.00;(#,##0.00)"
                cmp_cell.alignment = right_align
                if line.is_total:
                    cmp_cell.font = total_font
                    cmp_cell.border = thin_top

            row += 1

        row += 1

    # Balance check footnote
    bc = built.get("balance_check", {})
    if bc.get("balanced"):
        ws.cell(row=row, column=1, value="Balance check: ✓").font = subtitle_font
    else:
        ws.cell(row=row, column=1, value=f"Balance check: ✗ difference of {bc.get('difference')}").font = subtitle_font

    wb.save(path)

# ---------------------------------------------------------------------------
# Trial Balance builder
# ---------------------------------------------------------------------------

_TRIAL_BALANCE_SECTION_ORDER = [
    ("Asset", "ASSETS"),
    ("Liability", "LIABILITIES"),
    ("Equity", "EQUITY"),
    ("Revenue", "REVENUE"),
    ("Expense", "EXPENSES"),
]


def build_trial_balance(
    ledger: GeneralLedger,
    as_of_date: str,
) -> Dict[str, Any]:
    """
    Build a trial balance. No comparison columns -- a trial balance is a
    snapshot; comparing two periods isn't standard.

    Returns:
      {
        "sections": List[ReportSection],   # one per account_type, DR/CR as two "amount" slots
        "as_of_date": str,
        "total_dr": Decimal,
        "total_cr": Decimal,
        "balanced": bool,
        "difference": Decimal,
      }

    Each ReportLine in each section carries:
      - label: the account name
      - amount: the DR value (or ZERO)
      - compare_amount: reused as the CR value
    The reused field is a pragmatic shape choice -- it keeps ReportLine
    unchanged. The renderer treats these two fields as the two columns
    specifically for the trial balance.
    """
    data = ledger.trial_balance_data(as_of_date)

    sections: List[ReportSection] = []

    for account_type, section_title in _TRIAL_BALANCE_SECTION_ORDER:
        entries = data["groups"].get(account_type, [])

        lines: List[ReportLine] = []
        for name, dr, cr in entries:
            lines.append(ReportLine(
                label=name,
                amount=dr,
                compare_amount=cr,
                indent=1,
                accounts=[name],
            ))

        section_total_dr = money(sum((dr for _, dr, _ in entries), ZERO))
        section_total_cr = money(sum((cr for _, _, cr in entries), ZERO))

        sections.append(ReportSection(
            title=section_title,
            lines=lines,
            total=section_total_dr,
            compare_total=section_total_cr,
            total_label=f"TOTAL {section_title}",
        ))

    return {
        "sections": sections,
        "as_of_date": as_of_date,
        "total_dr": data["total_dr"],
        "total_cr": data["total_cr"],
        "balanced": data["balanced"],
        "difference": data["difference"],
    }


def export_trial_balance_xlsx(
    built: Dict[str, Any],
    path: str,
) -> None:
    """
    Write a trial balance to XLSX. Two numeric columns: Debit and Credit.
    """
    import openpyxl
    from openpyxl.styles import Font, Alignment, Border, Side

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Trial Balance"

    ws.column_dimensions["A"].width = 45
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 16

    title_font = Font(bold=True, size=14)
    subtitle_font = Font(italic=True, size=11)
    section_font = Font(bold=True)
    total_font = Font(bold=True)
    header_font = Font(bold=True)
    right_align = Alignment(horizontal="right")
    thin_top = Border(top=Side(style="thin"))
    double_top = Border(top=Side(style="double"))

    ws["A1"] = "TRIAL BALANCE"
    ws["A1"].font = title_font
    ws["A2"] = f"As of: {built['as_of_date']}"
    ws["A2"].font = subtitle_font
    row = 4

    # Column headers
    for col, label in ((2, "Debit"), (3, "Credit")):
        c = ws.cell(row=row, column=col, value=label)
        c.font = header_font
        c.alignment = right_align
    row += 1

    for section in built["sections"]:
        if not section.lines:
            continue

        ws.cell(row=row, column=1, value=section.title).font = section_font
        row += 1

        for line in section.lines:
            label_cell = ws.cell(row=row, column=1, value=("    " * line.indent) + line.label if line.indent else line.label)

            dr_cell = ws.cell(row=row, column=2,
                              value=float(line.amount) if line.amount else None)
            dr_cell.number_format = "#,##0.00;(#,##0.00)"
            dr_cell.alignment = right_align

            cr_val = line.compare_amount if line.compare_amount else None
            cr_cell = ws.cell(row=row, column=3, value=float(cr_val) if cr_val else None)
            cr_cell.number_format = "#,##0.00;(#,##0.00)"
            cr_cell.alignment = right_align

            row += 1

        # Section subtotal
        sec_dr = ws.cell(row=row, column=2, value=float(section.total))
        sec_dr.number_format = "#,##0.00;(#,##0.00)"
        sec_dr.alignment = right_align
        sec_dr.font = total_font
        sec_dr.border = thin_top

        sec_cr_val = section.compare_total if section.compare_total else ZERO
        sec_cr = ws.cell(row=row, column=3, value=float(sec_cr_val))
        sec_cr.number_format = "#,##0.00;(#,##0.00)"
        sec_cr.alignment = right_align
        sec_cr.font = total_font
        sec_cr.border = thin_top

        row += 2  # blank line between sections

    # Final totals
    total_dr = ws.cell(row=row, column=2, value=float(built["total_dr"]))
    total_dr.number_format = "#,##0.00;(#,##0.00)"
    total_dr.alignment = right_align
    total_dr.font = total_font
    total_dr.border = double_top

    total_cr = ws.cell(row=row, column=3, value=float(built["total_cr"]))
    total_cr.number_format = "#,##0.00;(#,##0.00)"
    total_cr.alignment = right_align
    total_cr.font = total_font
    total_cr.border = double_top

    ws.cell(row=row, column=1, value="TOTAL").font = total_font

    row += 2
    if built["balanced"]:
        ws.cell(row=row, column=1, value="Balance check: ✓").font = subtitle_font
    else:
        ws.cell(row=row, column=1,
                value=f"Balance check: ✗ difference of {built['difference']}").font = subtitle_font

    wb.save(path)
# ---------------------------------------------------------------------------
# Equity Statement builder
# ---------------------------------------------------------------------------

_MAX_EQUITY_COLUMNS = 6


def build_equity_statement(
    ledger: GeneralLedger,
    start_date: str,
    end_date: str,
) -> Dict[str, Any]:
    """
    Build the statement of changes in equity.

    Position 1 (per design): shows only direct equity-account activity.
    Net income is not synthesized into Retained Earnings; year-end close
    is deferred. A footnote is rendered by the caller.

    Multi-column layout: one column per equity subcategory, plus a
    "Total" column. If more than _MAX_EQUITY_COLUMNS subcategories exist,
    the extras collapse into "Other Equity" to keep the statement
    readable.
    """
    data = ledger.equity_statement_data(start_date, end_date)
    subcats = data["subcategories"]

    # Column names
    ordered_subcats = list(subcats.keys())
    if len(ordered_subcats) > _MAX_EQUITY_COLUMNS:
        kept = ordered_subcats[:_MAX_EQUITY_COLUMNS - 1]
        dropped = ordered_subcats[_MAX_EQUITY_COLUMNS - 1:]
        merged = {
            "accounts": [],
            "opening": ZERO,
            "movement": ZERO,
            "closing": ZERO,
        }
        for key in dropped:
            merged["accounts"].extend(subcats[key]["accounts"])
            merged["opening"] += subcats[key]["opening"]
            merged["movement"] += subcats[key]["movement"]
            merged["closing"] += subcats[key]["closing"]
        merged["opening"] = money(merged["opening"])
        merged["movement"] = money(merged["movement"])
        merged["closing"] = money(merged["closing"])
        subcats = {k: subcats[k] for k in kept}
        subcats["Other Equity"] = merged
        ordered_subcats = kept + ["Other Equity"]

    # Section header labels: "Contributed Capital", etc. Set column_headers
    # to the ordered subcategory names, with "Total" appended.
    column_headers = ordered_subcats + ["Total"]

    # Build the single section
    lines: List[ReportLine] = []

    def _row_total(columns: Dict[str, Decimal]) -> Decimal:
        return money(sum(columns.values(), ZERO))

    # Opening balance row
    opening_columns = {k: subcats[k]["opening"] for k in ordered_subcats}
    lines.append(ReportLine(
        label="Opening balance",
        amount=_row_total(opening_columns),
        indent=0,
        columns=opening_columns,
    ))

    # Movement rows: derive contributions and draws from movements.
    # Positive movement on a Contributed Capital account = contribution.
    # Negative movement on a Distributions account = draws (shown as
    # positive magnitude with a subtraction sign in the label).
    contributions_columns: Dict[str, Decimal] = {}
    draws_columns: Dict[str, Decimal] = {}
    other_columns: Dict[str, Decimal] = {}

    for subcat_name in ordered_subcats:
        sub = subcats[subcat_name]
        contrib = ZERO
        draws = ZERO

        for acc_entry in sub["accounts"]:
            mv = acc_entry["movement"]
            if subcat_name == "Contributed Capital":
                if mv > ZERO:
                    contrib += mv
                elif mv < ZERO:
                    draws += -mv
            elif subcat_name == "Distributions":
                # In this ledger, distributions are debit-normal in effect
                # (they reduce equity). A positive movement shown on the
                # statement means the owner took money out.
                if mv < ZERO:
                    # Balance went down: draws
                    draws += -mv
                elif mv > ZERO:
                    # Balance went up: unusual; treat as a contribution back
                    contrib += mv
            else:
                # Earnings and other subcategories: all movements go to
                # "Other movements" for this column
                other_columns[subcat_name] = other_columns.get(subcat_name, ZERO) + mv

        if contrib:
            contributions_columns[subcat_name] = money(contrib)
        if draws:
            draws_columns[subcat_name] = money(draws)

    # Pad missing columns with ZERO so the dict always has every column
    def _pad(d: Dict[str, Decimal]) -> Dict[str, Decimal]:
        return {k: d.get(k, ZERO) for k in ordered_subcats}

    contributions_columns = _pad(contributions_columns)
    draws_columns = _pad(draws_columns)
    other_columns = _pad(other_columns)

    if any(v != ZERO for v in contributions_columns.values()):
        lines.append(ReportLine(
            label="Contributions",
            amount=_row_total(contributions_columns),
            indent=1,
            columns=contributions_columns,
        ))

    if any(v != ZERO for v in draws_columns.values()):
        lines.append(ReportLine(
            label="Owner draws",
            amount=-_row_total(draws_columns),
            indent=1,
            columns={k: -v for k, v in draws_columns.items()},
        ))

    # Any residual movement not classified as contributions or draws
    # (e.g. direct journal entries to equity accounts) falls into Other
    # movements. Compute it as closing - opening - contributions + draws
    # for each column so nothing goes unaccounted.
    residual_columns: Dict[str, Decimal] = {}
    for subcat_name in ordered_subcats:
        closing = subcats[subcat_name]["closing"]
        opening = subcats[subcat_name]["opening"]
        contrib = contributions_columns[subcat_name]
        draws = draws_columns[subcat_name]
        residual = money(closing - opening - contrib + draws)
        residual_columns[subcat_name] = residual

    if any(v != ZERO for v in residual_columns.values()):
        lines.append(ReportLine(
            label="Other movements",
            amount=_row_total(residual_columns),
            indent=1,
            columns=residual_columns,
        ))

    # Closing balance row
    closing_columns = {k: subcats[k]["closing"] for k in ordered_subcats}
    lines.append(ReportLine(
        label="Closing balance",
        amount=_row_total(closing_columns),
        indent=0,
        is_total=True,
        columns=closing_columns,
    ))

    section = ReportSection(
        title="STATEMENT OF CHANGES IN EQUITY",
        lines=lines,
        total=data["total_closing"],
        total_label="",  # rendered as a line
        column_headers=column_headers,
    )

    return {
        "sections": [section],
        "start_date": start_date,
        "end_date": end_date,
        "total_opening": data["total_opening"],
        "total_movement": data["total_movement"],
        "total_closing": data["total_closing"],
    }


def export_equity_statement_xlsx(
    built: Dict[str, Any],
    path: str,
) -> None:
    """Write the equity statement to XLSX. Multi-column layout."""
    import openpyxl
    from openpyxl.styles import Font, Alignment, Border, Side

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Equity Statement"

    section = built["sections"][0]
    headers = section.column_headers  # includes "Total" at the end

    # Column widths: A is the label column, then one per header
    ws.column_dimensions["A"].width = 32
    for i, _ in enumerate(headers):
        col_letter = chr(ord("B") + i)
        ws.column_dimensions[col_letter].width = 16

    title_font = Font(bold=True, size=14)
    subtitle_font = Font(italic=True, size=11)
    header_font = Font(bold=True)
    total_font = Font(bold=True)
    right_align = Alignment(horizontal="right")
    thin_top = Border(top=Side(style="thin"))

    ws["A1"] = "STATEMENT OF CHANGES IN EQUITY"
    ws["A1"].font = title_font
    ws["A2"] = f"Period: {built['start_date']} to {built['end_date']}"
    ws["A2"].font = subtitle_font
    row = 4

    # Header row
    for i, h in enumerate(headers):
        c = ws.cell(row=row, column=2 + i, value=h)
        c.font = header_font
        c.alignment = right_align
    row += 1

    # Lines
    for line in section.lines:
        label_cell = ws.cell(row=row, column=1, value=("    " * line.indent) + line.label if line.indent else line.label)
        if line.is_total:
            label_cell.font = total_font

        for i, h in enumerate(headers[:-1]):  # subcategory columns
            val = line.columns.get(h, ZERO) if line.columns else ZERO
            c = ws.cell(row=row, column=2 + i, value=float(val) if val else None)
            c.number_format = "#,##0.00;(#,##0.00)"
            c.alignment = right_align
            if line.is_total:
                c.font = total_font
                c.border = thin_top

        # Total column
        total_val = line.amount
        c = ws.cell(row=row, column=2 + len(headers) - 1,
                    value=float(total_val) if total_val else None)
        c.number_format = "#,##0.00;(#,##0.00)"
        c.alignment = right_align
        c.font = total_font
        if line.is_total:
            c.border = thin_top

        row += 1

    row += 1
    ws.cell(row=row, column=1,
            value="Note: net income is not transferred to Retained Earnings "
                  "automatically; year-end close is deferred.").font = subtitle_font

    wb.save(path)
