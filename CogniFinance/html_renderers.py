# html_renderers.py
"""
html_renderers.py ver 1.0
For CogniFinance ver 0.9.67
Pure functions for generating HTML reports.
Depends ONLY on standard Python types (dicts, lists, Decimals) and html.escape.
Has ZERO dependency on PyQt6 or GeneralLedger.
"""
from html import escape
from decimal import Decimal
from typing import List, Dict, Any, Optional

# ==========================================
# Shared CSS Styles & Helpers
# ==========================================
LABEL_STYLE = "width: 100%; white-space: pre; padding-right: 12px;"
AMOUNT_STYLE = "text-align: right; white-space: pre; padding-left: 12px;"
SECTION_STYLE = "font-weight: bold; padding-top: 8px; padding-bottom: 2px; font-size: 12pt;"
MUTED_STYLE = "color: #666;"
OVERVIEW_SECTION_STYLE = "font-weight: bold; font-size: 12pt; padding-top: 8px; padding-bottom: 2px;"

ZERO = Decimal("0.00")

SUBSECTION_STYLE = (
    "font-weight: bold; padding-top: 6px; padding-bottom: 2px; "
    "font-size: 11pt; padding-left: 12px;"
)
REPORT_LINE_STYLE = (
    "font-style: italic; color: #6B7280; padding-left: 24px; padding-top: 2px;"
)


def _subsection(title: str) -> str:
    return (
        f'<tr><td colspan="2" style="{SUBSECTION_STYLE}">'
        f'{escape(str(title))}</td></tr>'
    )


def _report_line_header(title: str) -> str:
    return (
        f'<tr><td colspan="2" style="{REPORT_LINE_STYLE}">'
        f'{escape(str(title))}:</td></tr>'
    )


# Map the ledger's section keys to account-type names, so the renderer
# can look up the right sub-tree in `grouped`.
_SECTION_TO_TYPE = {
    "assets": "Asset",
    "liabilities": "Liability",
    "equity": "Equity",
    "income": "Revenue",
    "expenses": "Expense",
}

def _row(label: str, amount: Decimal, bold: bool = False, indent: int = 0, color: Optional[str] = None) -> str:
    weight = "font-weight: bold;" if bold else ""
    left_pad = f"padding-left: {indent * 18}px;" if indent else ""
    color_style = f"color: {color};" if color else ""
    return (
        "<tr>"
        f'<td style="{LABEL_STYLE}{weight}{left_pad}{color_style}">{escape(str(label))}</td>'
        f'<td align="right" style="{AMOUNT_STYLE}{weight}{color_style}">{amount:,.2f}</td>'
        "</tr>"
    )

def _section(title: str) -> str:
    return f'<tr><td colspan="2" style="{SECTION_STYLE}">{escape(str(title))}</td></tr>'

def _overview_section(title: str) -> str:
    return f'<div style="{OVERVIEW_SECTION_STYLE}">{escape(str(title))}</div>'

def _table(rows: List[str]) -> str:
    return '<table width="100%" cellspacing="0" cellpadding="2">' + "".join(rows) + "</table>"

def _rule() -> str:
    return '<tr><td colspan="2"><hr/></td></tr>'

def _spacer(count: int = 1) -> str:
    return '<tr><td colspan="2">&nbsp;</td></tr>' * count


# ==========================================
# Report Renderers
# ==========================================

def render_overview_html(
    today_str: str,
    month_str: str,
    report: Dict[str, Any],
    budgets: List[Any],
    spending: Dict[str, Decimal],
    recent_txs: List[Any],
    unreconciled_count: int,
    backup_html_snippet: str
) -> str:
    """Generates the HTML for the Overview tab."""

    # 1. Budget Alerts
    alert_rows = []
    for b in sorted(budgets, key=lambda x: x.category):
        actual = spending.get(b.category, ZERO)
        if actual > b.amount:
            over_by = actual - b.amount
            alert_rows.append(_row(f"{b.category} over budget", over_by, color="#b00000"))
    if not alert_rows:
        msg = "No budget overruns this month." if budgets else "No budgets set for this month."
        alert_rows.append(f'<tr><td style="{MUTED_STYLE}">{msg}</td></tr>')

    # 2. Recent Transactions
    recent_rows = []
    for tx in recent_txs:
        # We pass a pre-calculated display amount to keep this renderer pure
        amount = tx.get("amount", ZERO)
        recent_rows.append(
            "<tr>"
            f'<td style="white-space: pre;">{escape(tx["date"])}</td>'
            f'<td style="width: 100%; padding-left: 10px;">{escape(tx["description"])}</td>'
            f'<td align="right" style="{AMOUNT_STYLE}">{amount:,.2f}</td>'
            "</tr>"
        )
    if not recent_rows:
        recent_rows.append(f'<tr><td style="{MUTED_STYLE}">No transactions yet.</td></tr>')

    # 3. Reconciliation
    reconciliation_rows = [f"<tr><td>{unreconciled_count} unreconciled transaction(s)</td></tr>"]

    # 4. Net Color
    net_color = "#007a00" if report["net"] >= ZERO else "#b00000"

    return f"""
    <html>
    <body style="font-family: 'Segoe UI', sans-serif; font-size: 10pt;">
        <div style="font-size: 20pt; font-weight: bold; color: #3388FF;">
            Welcome to CogniFinance
        </div>
        <div style="{MUTED_STYLE}">
            {escape(today_str)}
        </div>
        <br>
        <div>Use the tabs above to:</div>
        <ul>
            <li>View and filter transactions</li>
            <li>Set and track monthly budgets per category</li>
            <li>See charts of spending</li>
            <li>Reconcile imported statement transactions</li>
            <li>View balance sheet and export/import ledger CSV</li>
        </ul>
        <br/>
        {_overview_section("Budget Alerts")}
        {_table(alert_rows)}
        <br>
        {_overview_section("Reconciliation")}
        {_table(reconciliation_rows)}
        <br>
        {_overview_section("Backup Status")}
        <table width="100%" cellspacing="0" cellpadding="2">
            <tr><td>{backup_html_snippet}</td></tr>
        </table>
        <br>
        {_overview_section("Recent Transactions")}
        <table width="100%" cellspacing="0" cellpadding="2">
            {''.join(recent_rows)}
        </table>
    </body>
    </html>
    """

def render_balance_sheet_html(
    bs: Dict[str, Any],
    grouped: Optional[Dict[str, Dict[Optional[str], Dict[Optional[str], List[str]]]]] = None,
    simple_mode: bool = False,
) -> str:
    """
    Render the balance sheet.

    - bs: the ledger's balance_sheet() result (flat totals + per-account
      balances). Always required.
    - grouped: the ledger's get_accounts_grouped_for_report() result
      (type -> subcategory -> report_line -> [names]). Optional.
    - simple_mode: when True, ignore `grouped` and render the flat
      per-account listing (pre-report-line behavior).

    When `grouped` is present and not in simple_mode, the sheet renders
    three levels deep:
        Subcategory (e.g. "Current Assets")
          Report line (e.g. "Cash and Cash Equivalents")
            Account (e.g. "Checking Account")
    with subtotal rows at the report-line level when a report line has
    more than one account under it. A report line with exactly one
    account collapses to a single line -- a subtotal of one is noise.
    """

    def section_rows(section_title: str, section_key: str, section_data: Dict[str, Any]) -> str:
        rows = [_section(section_title)]
        account_balances: Dict[str, Decimal] = section_data.get("accounts", {})

        type_name = _SECTION_TO_TYPE.get(section_key)
        subcat_map = (grouped or {}).get(type_name) if type_name else None
        use_grouped = (
            not simple_mode
            and subcat_map is not None
            and any(k is not None for k in subcat_map.keys())
        )

        if not use_grouped:
            # Flat: same output as the pre-report-line renderer.
            for name in sorted(account_balances.keys()):
                dname = (name[:28] + "..") if len(name) > 30 else name
                rows.append(_row(dname, account_balances[name], indent=1))
        else:
            # Grouped: three levels. Subcategory headers only shown when
            # there's more than one subcategory in this section --
            # otherwise the header would be a needless layer above an
            # already-single group.
            show_subcat_headers = len(subcat_map) > 1

            # Unclassified sorts last; every other subcategory sorts
            # alphabetically. Same rule at the report-line level below.
            ordered_subcats = sorted(
                subcat_map.items(),
                key=lambda kv: (kv[0] is None, kv[0] or ""),
            )

            for subcat_name, report_line_map in ordered_subcats:
                display_subcat = subcat_name if subcat_name else "Unclassified"

                if show_subcat_headers:
                    rows.append(_subsection(display_subcat))

                # base_indent is where report lines sit; accounts go one
                # deeper. When subcategory headers are hidden, everything
                # shifts up one level to avoid an over-indented block.
                base_indent = 2 if show_subcat_headers else 1

                ordered_rls = sorted(
                    report_line_map.items(),
                    key=lambda kv: (kv[0] is None, kv[0] or ""),
                )

                for rl_name, account_names in ordered_rls:
                    # Filter to accounts that actually have a balance
                    # entry, and compute the report-line subtotal.
                    present = [
                        (n, account_balances[n])
                        for n in account_names
                        if n in account_balances
                    ]
                    if not present:
                        continue

                    if rl_name is None:
                        # Unclassified accounts: no report-line header,
                        # just list them. This keeps the "Unclassified"
                        # bucket visually lighter than a real report
                        # line, which is honest about its status.
                        for name, bal in present:
                            dname = (name[:28] + "..") if len(name) > 30 else name
                            rows.append(_row(dname, bal, indent=base_indent))
                    elif len(present) == 1:
                        # Single-account report line: no subtotal. Show
                        # the account name at report-line indent so the
                        # user sees the classification without an extra
                        # "Subtotal — $X of $X" line.
                        name, bal = present[0]
                        dname = (name[:28] + "..") if len(name) > 30 else name
                        rows.append(_row(dname, bal, indent=base_indent))
                    else:
                        # Multi-account report line: header + accounts +
                        # subtotal.
                        rows.append(_report_line_header(rl_name))
                        subtotal = ZERO
                        for name, bal in present:
                            dname = (name[:28] + "..") if len(name) > 30 else name
                            rows.append(_row(dname, bal, indent=base_indent + 1))
                            subtotal += bal
                        rows.append(
                            _row(
                                f"Subtotal \u2014 {rl_name}",
                                subtotal,
                                indent=base_indent + 1,
                                bold=True,
                            )
                        )

        rows.append(_rule())
        rows.append(_row(f"TOTAL {section_title}", section_data["total"], bold=True))
        rows.append(_spacer(2))
        return "".join(rows)

    net = (
        bs["assets"]["total"]
        - bs["liabilities"]["total"]
        - bs["equity"]["total"]
    )

    return f"""
    <html>
    <body style="font-family: 'Inter', sans-serif; font-size: 10pt;">
        <div style="font-weight: bold; font-size: 12pt;">BALANCE SHEET</div>
        <br/>
        <table width="100%" cellspacing="0" cellpadding="1">
            {section_rows('Assets', 'assets', bs['assets'])}
            {_rule()}
            {section_rows('Liabilities', 'liabilities', bs['liabilities'])}
            {_rule()}
            {section_rows('Equity', 'equity', bs['equity'])}
            {_rule()}
            {_section('INCOME AND EXPENSES')}
            {_rule()}
            {section_rows('Income', 'income', bs['income'])}
            {section_rows('Expenses', 'expenses', bs['expenses'])}
            {_rule()}
            {_row('Net (Assets − Liabilities − Equity)', net, bold=True)}
            {_rule()}
        </table>
    </body>
    </html>
    """

def render_income_statement_html(
    sections: List,
    period_label: str,
    compare_label: Optional[str] = None,
    simple_mode: bool = False,
) -> str:
    """
    Render an income statement from a list of ReportSection objects.

    If `compare_label` is None, the comparison column is omitted.
    """
    rows: List[str] = []

    def cell(text: str, *, align: str = "left", bold: bool = False,
             indent: int = 0, color: Optional[str] = None) -> str:
        style_parts = []
        if align == "right":
            style_parts.append("text-align: right; white-space: pre; padding-left: 12px;")
        else:
            style_parts.append("white-space: pre; padding-right: 12px;")
            if indent:
                style_parts.append(f"padding-left: {indent * 18}px;")
        if bold:
            style_parts.append("font-weight: bold;")
        if color:
            style_parts.append(f"color: {color};")
        return f'<td style="{" ".join(style_parts)}">{escape(text)}</td>'

    def row(cells: List[str]) -> str:
        return f'<tr>{"".join(cells)}</tr>'

    def line_row(line) -> str:
        cells = [cell(line.label, indent=line.indent, bold=line.is_total)]
        cells.append(cell(f"{line.amount:,.2f}", align="right", bold=line.is_total))
        if compare_label is not None:
            cmp = line.compare_amount if line.compare_amount is not None else ZERO
            cells.append(cell(f"{cmp:,.2f}", align="right", bold=line.is_total))
        return row(cells)

    for section in sections:
        # Section title
        if section.title:
            rows.append(row([cell(section.title, bold=True)] +
                            [cell("")] * (2 if compare_label is not None else 1)))

        # Individual lines
        for line in section.lines:
            if line.is_section_header:
                rows.append(row([cell(line.label, indent=line.indent)] +
                                [cell("")] * (2 if compare_label is not None else 1)))
            else:
                rows.append(line_row(line))

        # Section total (skip if the section has no lines and no explicit
        # total_label matching a subtotal name we already render as a line)
        if section.total_label and section.title:
            cells = [
                cell(section.total_label, bold=True),
                cell(f"{section.total:,.2f}", align="right", bold=True),
            ]
            if compare_label is not None:
                cmp = section.compare_total if section.compare_total is not None else ZERO
                cells.append(cell(f"{cmp:,.2f}", align="right", bold=True))
            rows.append(row(cells))

    # Header row
    header_cells = [cell("")]
    header_cells.append(cell(period_label, align="right", bold=True))
    if compare_label is not None:
        header_cells.append(cell(compare_label, align="right", bold=True))
    header_row = row(header_cells)

    return f"""
    <html>
    <body style="font-family: 'Inter', sans-serif; font-size: 10pt;">
        <div style="font-weight: bold; font-size: 12pt;">INCOME STATEMENT</div>
        <br/>
        <table width="100%" cellspacing="0" cellpadding="2">
            {header_row}
            {''.join(rows)}
        </table>
    </body>
    </html>
    """

def render_monthly_report_html(year: int, month: int, data: Dict[str, Any]) -> str:
    """Generates the HTML for the Monthly Income & Expense Report."""

    income_rows = "".join(
        _row(acc, amt, indent=1) for acc, amt in data["income_by_account"].items()
    ) or _row("(none)", ZERO, indent=1)

    expense_rows = "".join(
        _row(acc, amt, indent=1) for acc, amt in data["expense_by_account"].items()
    ) or _row("(none)", ZERO, indent=1)

    return f"""
    <html>
    <body style="font-family: 'Inter', sans-serif; font-size: 10pt;">
        <div style="font-weight: bold; font-size: 12pt;">
        Monthly Income &amp; Expense Report – {year}-{month:02d}
        </div>
        <br/>
        <table width="100%" cellspacing="0" cellpadding="1">
            {_row('Total Income', data['income_total'], bold=True)}
            {_row('Total Expenses', data['expense_total'], bold=True)}
            {_row('Net (Income − Expenses)', data['net'], bold=True)}
            <tr><td colspan="2">&nbsp;</td></tr>
            {_section('Income by Account:')}
            {income_rows}
            <tr><td colspan="2">&nbsp;</td></tr>
            {_section('Expenses by Account:')}
            {expense_rows}
        </table>
    </body>
    </html>
    """

def render_balance_sheet_statement_html(
    built: Dict,
    period_label: str,
    compare_label: Optional[str] = None,
) -> str:
    """
    Render a formal balance sheet from the structure produced by
    build_balance_sheet. `built` includes `sections` and `balance_check`.
    """
    sections = built["sections"]
    balance_check = built.get("balance_check", {})

    rows: List[str] = []

    def cell(text: str, *, align: str = "left", bold: bool = False,
             indent: int = 0) -> str:
        style_parts = []
        if align == "right":
            style_parts.append("text-align: right; white-space: pre; padding-left: 12px;")
        else:
            style_parts.append("white-space: pre; padding-right: 12px;")
            if indent:
                style_parts.append(f"padding-left: {indent * 18}px;")
        if bold:
            style_parts.append("font-weight: bold;")
        return f'<td style="{" ".join(style_parts)}">{escape(text)}</td>'

    def row(cells: List[str]) -> str:
        return f'<tr>{"".join(cells)}</tr>'

    def line_row(line) -> str:
        cells = [cell(line.label, indent=line.indent, bold=line.is_total)]
        cells.append(cell(f"{line.amount:,.2f}", align="right", bold=line.is_total))
        if compare_label is not None:
            cmp = line.compare_amount if line.compare_amount is not None else ZERO
            cells.append(cell(f"{cmp:,.2f}", align="right", bold=line.is_total))
        return row(cells)

    for section in sections:
        rows.append(row([cell(section.title, bold=True)] +
                        [cell("")] * (2 if compare_label is not None else 1)))
        for line in section.lines:
            rows.append(line_row(line))
        rows.append(row([cell("")] * (3 if compare_label is not None else 2)))

    # Balance check line
    if balance_check.get("balanced"):
        check_html = (
            '<span style="color: #059669; font-weight: bold;">'
            '✓ Balance check: Assets = Liabilities + Equity'
            '</span>'
        )
    else:
        diff = balance_check.get("difference", ZERO)
        check_html = (
            f'<span style="color: #dc2626; font-weight: bold;">'
            f'✗ Balance check failed: difference of {diff:,.2f}'
            f'</span>'
        )

    header_cells = [cell("")]
    header_cells.append(cell(period_label, align="right", bold=True))
    if compare_label is not None:
        header_cells.append(cell(compare_label, align="right", bold=True))
    header_row = row(header_cells)

    return f"""
    <html>
    <body style="font-family: 'Inter', sans-serif; font-size: 10pt;">
        <div style="font-weight: bold; font-size: 12pt;">BALANCE SHEET</div>
        <br/>
        <table width="100%" cellspacing="0" cellpadding="2">
            {header_row}
            {''.join(rows)}
        </table>
        <br/>
        {check_html}
    </body>
    </html>
    """
def render_trial_balance_html(built: Dict) -> str:
    """
    Render a trial balance. Two numeric columns (Debit, Credit).
    No comparison.
    """
    sections = built["sections"]

    def cell(text: str, *, align: str = "left", bold: bool = False,
             indent: int = 0) -> str:
        style_parts = []
        if align == "right":
            style_parts.append("text-align: right; white-space: pre; padding-left: 12px;")
        else:
            style_parts.append("white-space: pre; padding-right: 12px;")
            if indent:
                style_parts.append(f"padding-left: {indent * 18}px;")
        if bold:
            style_parts.append("font-weight: bold;")
        return f'<td style="{" ".join(style_parts)}">{escape(text)}</td>'

    def row(cells):
        return f'<tr>{"".join(cells)}</tr>'

    def fmt(val) -> str:
        return f"{val:,.2f}" if val and val != ZERO else ""

    rows: List[str] = []

    for section in sections:
        if not section.lines:
            continue
        rows.append(row([cell(section.title, bold=True), cell(""), cell("")]))
        for line in section.lines:
            rows.append(row([
                cell(line.label, indent=line.indent),
                cell(fmt(line.amount), align="right"),
                cell(fmt(line.compare_amount), align="right"),
            ]))
        rows.append(row([
            cell(section.total_label, bold=True),
            cell(f"{section.total:,.2f}", align="right", bold=True),
            cell(f"{section.compare_total:,.2f}" if section.compare_total else "", align="right", bold=True),
        ]))
        rows.append(row([cell(""), cell(""), cell("")]))

    # Final totals
    rows.append(row([
        cell("TOTAL", bold=True),
        cell(f"{built['total_dr']:,.2f}", align="right", bold=True),
        cell(f"{built['total_cr']:,.2f}", align="right", bold=True),
    ]))

    if built["balanced"]:
        check_html = (
            '<span style="color: #059669; font-weight: bold;">'
            '✓ Balance check: Debits = Credits'
            '</span>'
        )
    else:
        check_html = (
            f'<span style="color: #dc2626; font-weight: bold;">'
            f'✗ Balance check failed: difference of {built["difference"]:,.2f}'
            f'</span>'
        )

    return f"""
    <html>
    <body style="font-family: 'Inter', sans-serif; font-size: 10pt;">
        <div style="font-weight: bold; font-size: 12pt;">TRIAL BALANCE</div>
        <div style="color: #6B7280;">As of {escape(built['as_of_date'])}</div>
        <br/>
        <table width="100%" cellspacing="0" cellpadding="2">
            <tr>
                <td></td>
                <td style="text-align: right; font-weight: bold; padding-left: 12px;">Debit</td>
                <td style="text-align: right; font-weight: bold; padding-left: 12px;">Credit</td>
            </tr>
            {''.join(rows)}
        </table>
        <br/>
        {check_html}
    </body>
    </html>
    """

def render_equity_statement_html(built: Dict) -> str:
    """
    Render a statement of changes in equity. Multi-column layout:
    one column per equity subcategory, plus a total.
    """
    section = built["sections"][0]
    headers = section.column_headers

    def cell(text: str, *, align: str = "left", bold: bool = False,
             indent: int = 0) -> str:
        style_parts = []
        if align == "right":
            style_parts.append("text-align: right; white-space: pre; padding-left: 12px;")
        else:
            style_parts.append("white-space: pre; padding-right: 12px;")
            if indent:
                style_parts.append(f"padding-left: {indent * 18}px;")
        if bold:
            style_parts.append("font-weight: bold;")
        return f'<td style="{" ".join(style_parts)}">{escape(text)}</td>'

    def row(cells):
        return f'<tr>{"".join(cells)}</tr>'

    def fmt(val: Decimal) -> str:
        if val is None or val == ZERO:
            return ""
        return f"{val:,.2f}"

    rows: List[str] = []

    # Header row: label column then one per subcategory plus Total
    header_cells = [cell("")]
    for h in headers:
        header_cells.append(cell(h, align="right", bold=True))
    rows.append(row(header_cells))

    # Body
    for line in section.lines:
        cells = [cell(line.label, indent=line.indent, bold=line.is_total)]
        for h in headers[:-1]:
            val = line.columns.get(h, ZERO) if line.columns else ZERO
            cells.append(cell(fmt(val), align="right", bold=line.is_total))
        cells.append(cell(fmt(line.amount), align="right", bold=line.is_total))
        rows.append(row(cells))

    footnote = (
        '<div style="color: #6B7280; font-size: 9pt; margin-top: 12px; '
        'font-style: italic;">'
        'Net income is not transferred to Retained Earnings automatically; '
        'year-end close is deferred.'
        '</div>'
    )

    return f"""
    <html>
    <body style="font-family: 'Inter', sans-serif; font-size: 10pt;">
        <div style="font-weight: bold; font-size: 12pt;">STATEMENT OF CHANGES IN EQUITY</div>
        <div style="color: #6B7280;">Period: {escape(built['start_date'])} to {escape(built['end_date'])}</div>
        <br/>
        <table width="100%" cellspacing="0" cellpadding="2">
            {''.join(rows)}
        </table>
        {footnote}
    </body>
    </html>
    """
