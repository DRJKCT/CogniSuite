# html_renderers.py
"""
html_renderers.py ver 1.0
For CogniFinance ver 1.9
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

def render_balance_sheet_html(bs: Dict[str, Any]) -> str:
    """Generates the HTML for the Balance Sheet."""

    def section_rows(title: str, section_data: Dict[str, Any]) -> str:
        rows = [_section(title)]
        for name, bal in section_data["accounts"].items():
            dname = (name[:28] + "..") if len(name) > 30 else name
            rows.append(_row(dname, bal, indent=1))
        rows.append(_rule())
        rows.append(_row(f"TOTAL {title}", section_data["total"], bold=True))
        rows.append(_spacer(2))
        return "".join(rows)

    net = bs["assets"]["total"] - bs["liabilities"]["total"] - bs["equity"]["total"]

    return f"""
    <html>
    <body style="font-family: 'Inter', sans-serif; font-size: 10pt;">
        <div style="font-weight: bold; font-size: 12pt;">BALANCE SHEET</div>
        <br/>
        <table width="100%" cellspacing="0" cellpadding="1">
            {section_rows('Assets', bs['assets'])}
            {_rule()}
            {section_rows('Liabilities', bs['liabilities'])}
            {_rule()}
            {section_rows('Equity', bs['equity'])}
            {_rule()}
            {_section('INCOME AND EXPENSES')}
            {_rule()}
            {section_rows('Income', bs['income'])}
            {section_rows('Expenses', bs['expenses'])}
            {_rule()}
            {_row('Net (Assets − Liabilities − Equity)', net, bold=True)}
            {_rule()}
        </table>
    </body>
    </html>
    """
