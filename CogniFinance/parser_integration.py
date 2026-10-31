"""
pareser_integration.py ver 0.2
For CogniFinance ver 0.9.67
"""

import os
import re
from typing import List, Dict, Any, Optional, Tuple
from decimal import Decimal
import pdfplumber

def detect_statement_type_from_text(text: str) -> Optional[str]:
    """
    Try to detect whether a PDF is a bank statement or credit card statement.

    Returns:
        "bank"
        "credit_card"
        None if unsure
    """
    if not text:
        return None

    t = " ".join(text.lower().split())

    bank_markers = (
        "transaction date",
        "transaction description",
        "amount($)",
        "balance($)",
        "opening balance",
        "closing balance",
        "balance brought forward",
        "chequing",
        "savings",
        "bank account",
        "account activity",
        "daily details",
    )

    credit_card_markers = (
        "credit card",
        "card number",
        "card statement",
        "previous balance",
        "new balance",
        "payments and credits",
        "purchases and advances",
        "statement ending date",
        "minimum payment due",
        "payment due date",
    )

    bank_score = sum(1 for marker in bank_markers if marker in t)
    card_score = sum(1 for marker in credit_card_markers if marker in t)

    if bank_score > card_score:
        return "bank"

    if card_score > bank_score:
        return "credit_card"

    # Useful Tangerine-specific fallback
    if "balance($)" in t:
        return "bank"

    return None

def detect_bank_from_text(text: str) -> Optional[str]:
    t = text.lower()
    if "cibc" in t or "canadian imperial bank of commerce" in t:
        return "cibc"
    if "tangerine" in t or "tangerine bank" in t:
        return "tangerine"
    return None


# Every parser in this module (CIBC, Tangerine) reads Canadian-dollar statements. Each
# parsed entry carries this so the importer can refuse to post CAD amounts into an
# account of a different currency. Add a per-parser value if a non-CAD parser is added.
STATEMENT_CURRENCY = "CAD"


def clean_amount(val: str) -> Optional[float]:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
        negative = True
    if s.startswith("-"):
        negative = True
        s = s[1:]
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    try:
        amount = float(s)
    except ValueError:
        return None
    return -abs(amount) if negative else abs(amount)


def _dec(x) -> Optional[Decimal]:
    """Safely convert a float/str/None to Decimal via its string form, to
    avoid binary-float precision noise when comparing money values."""
    if x is None:
        return None
    return Decimal(str(x))


# ---------------- CIBC ----------------

CIBC_LINE_RE = re.compile(
    r"^(?P<trans_month>[A-Za-z]{3})\s+(?P<trans_day>\d{1,2})\s+"
    r"(?P<post_month>[A-Za-z]{3})\s+(?P<post_day>\d{1,2})\s+"
    r"(?P<desc>.+?)\s+"
    r"(?P<category>[A-Za-z][A-Za-z ,&]+?)\s+"
    r"(?P<amount>\d+(?:\.\d{1,2})?)\s*$"
)

CIBC_HEADER_FOOTER_PATTERNS = [
    lambda line: "Trans" in line and "date" in line,
    lambda line: "Post" in line and "date" in line,
    lambda line: "Description" in line and "Amount" in line,
    lambda line: "Card number" in line,
    lambda line: "Page" in line and "of" in line,
]


def _parse_cibc_full(text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """
    Pure text-parsing logic for a CIBC credit card statement. Returns
    (transactions, skipped_lines). skipped_lines lists non-blank lines
    that looked like they might be transactions but didn't match the
    expected format -- worth a human glancing at, since a silently
    dropped line is a silently incomplete import. Recognized header/
    footer boilerplate is *not* included; that's an intentional, expected
    skip, not a parsing failure.
    """
    transactions: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    if not text:
        return transactions, skipped

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if any(pattern(line) for pattern in CIBC_HEADER_FOOTER_PATTERNS):
            continue

        m = CIBC_LINE_RE.match(line)
        if not m:
            skipped.append({"line": line, "reason": "did not match the expected CIBC transaction line format"})
            continue

        trans_month = m.group("trans_month")
        trans_day = m.group("trans_day")
        post_month = m.group("post_month")
        post_day = m.group("post_day")
        desc = m.group("desc").strip()
        category = m.group("category").strip()
        amount_str = m.group("amount")

        trans_date = f"{trans_month} {trans_day}"
        post_date = f"{post_month} {post_day}"
        amount = clean_amount(amount_str)
        if amount is None:
            skipped.append({"line": line, "reason": "matched the transaction format but the amount could not be parsed"})
            continue

        transactions.append(
            {
                "bank": "CIBC",
                "trans_date": trans_date,
                "post_date": post_date,
                "description": desc,
                "category": category,
                "amount": amount,
            }
        )
    return transactions, skipped


def parse_cibc_text(text: str) -> List[Dict[str, Any]]:
    """Backward-compatible entry point: transactions only, no diagnostics."""
    transactions, _skipped = _parse_cibc_full(text)
    return transactions


def parse_cibc_pdf(pdf_path: str) -> List[Dict[str, Any]]:
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_cibc_text(text)


# ---------------- Tangerine ----------------

TANGERINE_DATE_PAIR_RE = re.compile(
    r"(?P<trans_date>\d{1,2}-[A-Za-z]{3}-\d{4})\s+"
    r"(?P<post_date>\d{1,2}-[A-Za-z]{3}-\d{4})"
)
TANGERINE_AMOUNT_RE = re.compile(r"(?P<amount>\(?\$?-?\d{1,3}(?:,\d{3})*\.\d{2}\)?)")
TANGERINE_HEADER_KEYWORDS = ["Posted", "Description", "Category", "Amount", "Transaction", "Date", "Page", "of", "THANK YOU"]


def _parse_tangerine_full(text: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """
    Pure text-parsing logic for a Tangerine credit card statement.
    Returns (transactions, skipped_lines).
    """
    transactions: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    if not text:
        return transactions, skipped

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if any(kw in line for kw in TANGERINE_HEADER_KEYWORDS):
            i += 1
            continue

        m = TANGERINE_DATE_PAIR_RE.search(line)
        if not m:
            # Not a transaction start. Only worth flagging if it looks
            # like it might contain a stray amount (e.g. part of a
            # multi-line description whose date line wasn't recognized) --
            # otherwise this is almost certainly boilerplate/prose, and
            # flagging every non-matching line would bury real issues in
            # noise.
            if TANGERINE_AMOUNT_RE.search(line):
                skipped.append({"line": line, "reason": "contains what looks like an amount but no transaction date pair was found nearby"})
            i += 1
            continue

        trans_date = m.group("trans_date")
        post_date = m.group("post_date")

        j = i
        candidate_text = line
        amounts = TANGERINE_AMOUNT_RE.findall(candidate_text)
        while not amounts and j + 1 < len(lines):
            next_line = lines[j + 1].strip()
            if not next_line:
                j += 1
                continue
            if TANGERINE_DATE_PAIR_RE.search(next_line):
                break
            candidate_text += " " + next_line
            amounts = TANGERINE_AMOUNT_RE.findall(candidate_text)
            j += 1

        if not amounts:
            skipped.append({"line": line, "reason": "found a transaction date but could not locate an amount"})
            i += 1
            continue

        last_amount_str = amounts[-1]
        # NOTE: last_amount_str already includes a leading "$" if one was
        # present in the source text, since TANGERINE_AMOUNT_RE's "\$?" is
        # part of the captured group. Searching for "$" + last_amount_str
        # (as this used to) would look for a doubled "$$" and silently
        # fail to find plain, un-prefixed amounts like "45.67", which is
        # how most bank-statement PDF text extracts actually look --
        # dropping the transaction entirely.
        idx = candidate_text.rfind(last_amount_str)
        if idx == -1:
            skipped.append({"line": line, "reason": "amount text could not be located within the transaction line"})
            i += 1
            continue

        desc_full = candidate_text[:idx].strip()
        desc_full = TANGERINE_DATE_PAIR_RE.sub("", desc_full).strip()
        desc_full = re.sub(r"\s+", " ", desc_full)

        amount = clean_amount(last_amount_str)
        if amount is None:
            skipped.append({"line": line, "reason": "amount text found but could not be parsed as a number"})
            i += 1
            continue

        transactions.append(
            {
                "bank": "Tangerine",
                "trans_date": trans_date,
                "post_date": post_date,
                "description": desc_full,
                "amount": amount,
            }
        )
        i = j + 1

    return transactions, skipped


def parse_tangerine_text(text: str) -> List[Dict[str, Any]]:
    """Backward-compatible entry point: transactions only, no diagnostics."""
    transactions, _skipped = _parse_tangerine_full(text)
    return transactions


def parse_tangerine_pdf(pdf_path: str) -> List[Dict[str, Any]]:
    with pdfplumber.open(pdf_path) as pdf:
        text = ""
        for page in pdf.pages:
            pt = page.extract_text()
            if pt:
                text += pt + "\n"
    return parse_tangerine_text(text)

# ---------------- Tangerine Bank Statement (chequing/savings) ----------------

TANGERINE_BANK_DATE_RE = re.compile(
    r"(?P<day>\d{1,2})\s+(?P<month>[A-Za-z]{3})\s+(?P<year>\d{4})"
)
TANGERINE_BANK_AMOUNT_RE = re.compile(
    r"(?P<amt>\d{1,3}(?:,\d{3})*\.\d{2})"
)
TANGERINE_BANK_SKIP_KEYWORDS = [
    "Transaction Date",
    "Transaction Description",
    "Amount($)",
    "Balance($)",
    "Tangerine",
    "Statement",
    "Page",
    "Account Number",
    "Closing Balance",
    "Total",
    "Balance brought forward",
    "Interest Rate Change",
]
BALANCE_TOLERANCE = Decimal("0.01")


def _parse_tangerine_bank_statement_full(text: str) -> Dict[str, Any]:
    """
    Pure text-parsing logic for a Tangerine bank account statement
    (chequing/savings). Based on the format:

      Transaction Date  Transaction Description              Amount($)  Balance($)
      Opening Balance                                                    1,000.00
      DD MMM YYYY       INTERAC e-Transfer To: XXX         00.00      00,000.00
      DD MMM YYYY       Bill Payment - XX VISA             0,000.00   00,000.00

    Every row on a real bank statement includes the *running balance*
    after that transaction, which tells us two things the row's own
    "Amount" text never does on its own:

      1. Whether it was a deposit (balance went up) or a withdrawal
         (balance went down) -- the source text has no minus sign or
         parentheses to indicate this, so "amount" alone is always a
         bare magnitude.
      2. Whether the parser actually caught every transaction: if
         consecutive balances don't add up to the amounts parsed between
         them, something was mis-parsed or skipped.

    This function uses the balance column as the source of truth for
    sign, and flags any row where the computed delta doesn't match the
    row's own parsed amount within a one-cent tolerance.

    Returns a dict:
      {
        "transactions": [...],       # each with a *signed* "amount"
        "skipped_lines": [...],      # lines that looked like transactions but didn't fully parse
        "opening_balance": Decimal | None,
        "closing_balance": Decimal | None,
      }
    """
    transactions: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    opening_balance: Optional[Decimal] = None
    closing_balance: Optional[Decimal] = None

    if not text:
        return {
            "transactions": transactions,
            "skipped_lines": skipped,
            "opening_balance": opening_balance,
            "closing_balance": closing_balance,
        }

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        low = line.lower()

        if "opening balance" in low:
            amounts = TANGERINE_BANK_AMOUNT_RE.findall(line)
            if amounts:
                opening_balance = _dec(clean_amount(amounts[-1]))
            continue

        if "closing balance" in low:
            amounts = TANGERINE_BANK_AMOUNT_RE.findall(line)
            if amounts:
                closing_balance = _dec(clean_amount(amounts[-1]))
            continue

        # Skip obvious header/footer and non-transaction lines
        if any(kw.lower() in low for kw in TANGERINE_BANK_SKIP_KEYWORDS):
            continue

        # Must start with a date: DD Mon YYYY
        dm = TANGERINE_BANK_DATE_RE.match(line)
        if not dm:
            # Doesn't start with a date and isn't a recognized skip line.
            # This is exactly the shape of a wrapped-description
            # continuation line (e.g. "Subscription Fee" on its own line
            # after "Bill Payment - Long Distance Internet Provider
            # Monthly" on the line above) -- something the current
            # line-by-line parser has no way to reattach to its
            # transaction. Flag it rather than silently drop it.
            skipped.append({"line": line, "reason": "no date at the start of the line -- possibly a wrapped description continuing from the line above"})
            continue

        rest = line[dm.end():].strip()
        amounts = list(TANGERINE_BANK_AMOUNT_RE.finditer(rest))
        if len(amounts) < 2:
            skipped.append({"line": line, "reason": "found a transaction date but not both an amount and a balance"})
            continue

        tx_amount_match = amounts[-2]
        balance_match = amounts[-1]
        amount_str = tx_amount_match.group("amt")
        amount = clean_amount(amount_str)
        if amount is None:
            skipped.append({"line": line, "reason": "amount text found but could not be parsed as a number"})
            continue

        balance = clean_amount(balance_match.group("amt"))

        desc = rest[: tx_amount_match.start()].strip()
        desc = re.sub(r"\s+", " ", desc)

        if not desc or desc.lower() in ["balance", "brought forward", "carried forward"]:
            skipped.append({"line": line, "reason": "matched date/amount/balance but description was empty or looked like a header"})
            continue

        date_str = f"{dm.group('day')} {dm.group('month')} {dm.group('year')}"

        transactions.append(
            {
                "bank": "Tangerine",
                "account_type": "BANK",
                "date": date_str,
                "description": desc,
                "amount": amount,  # magnitude for now; signed below using the balance trail
                "balance": balance,
            }
        )

    # --- Sign inference + balance-consistency check ---
    # Walk the transactions in the order they were parsed (statements
    # list transactions chronologically) and use each row's own reported
    # balance, chained from the previous known balance, to determine the
    # true signed amount and to flag any row where the numbers don't add
    # up.
    prev_balance = opening_balance
    for tx in transactions:
        row_balance = _dec(tx["balance"])
        row_amount_magnitude = _dec(tx["amount"])

        if prev_balance is not None and row_balance is not None:
            delta = row_balance - prev_balance
            if row_amount_magnitude is not None and abs(abs(delta) - row_amount_magnitude) <= BALANCE_TOLERANCE:
                # Balance trail confirms both the magnitude and the
                # direction -- trust the delta's sign.
                tx["amount"] = float(delta)
                tx["balance_check"] = "ok"
            else:
                # Numbers don't reconcile: something was likely missed
                # or mis-parsed between the previous checkpoint and this
                # row. Keep the originally parsed magnitude (as a
                # conservative default, still treated as an outflow
                # downstream) but flag it clearly rather than silently
                # trusting it.
                tx["balance_check"] = "mismatch"
                tx["balance_check_detail"] = (
                    f"statement balance moved by {delta} but the parsed amount was {row_amount_magnitude}"
                )
        else:
            # No prior balance to compare against (e.g. opening balance
            # wasn't found/captured) -- can't determine sign or verify
            # this row.
            tx["balance_check"] = "unverified"

        if row_balance is not None:
            prev_balance = row_balance

    return {
        "transactions": transactions,
        "skipped_lines": skipped,
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
    }


def parse_tangerine_bank_statement_text(text: str) -> List[Dict[str, Any]]:
    """Backward-compatible entry point: signed transactions only, no diagnostics."""
    return _parse_tangerine_bank_statement_full(text)["transactions"]


def parse_tangerine_bank_statement_pdf(pdf_path: str) -> List[Dict[str, Any]]:
    with pdfplumber.open(pdf_path) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_tangerine_bank_statement_text(text)


# ---------------- Integration ----------------

def raw_rows_to_import_lines(raw: List[Dict[str, Any]], ledger: "GeneralLedger") -> List[Dict[str, Any]]:
    """
    Turn raw parsed statement rows (as produced by the parse_*_text /
    parse_*_pdf functions) into fully-resolved ledger postings.

    Each returned dict includes a "lines" key: [(account, dr, cr), (account, dr, cr)],
    ready to hand straight to GeneralLedger.record_transaction. Building
    the debit/credit assignment here (rather than leaving it to the
    caller to infer from an implicit convention) matters once bank-
    statement rows can be signed: a withdrawal debits an expense account
    and credits Checking Account, but a deposit needs the *opposite*
    polarity, and getting that backwards would silently understate your
    checking balance for every deposit.

    Category rules (ledger.apply_category_rules) are only applied to
    outflows. They're intended for expense categorization; blindly
    applying the same keyword rules to a deposit description risks
    matching an unrelated expense rule and crediting an Expense-type
    account, which breaks the normal accounting convention for that
    account type. Deposits are posted to "Uncategorized Income" instead
    -- categorizing income by rule is a reasonable future enhancement,
    not required for a safe import.
    """
    lines = []
    for r in raw:
        amount = r["amount"]
        if amount == 0:
            continue

        desc = r["description"]
        is_bank = r.get("account_type") == "BANK"
        contra_account = "Checking Account" if is_bank else "Credit Card"

        if is_bank and amount > 0:
            # Deposit / inflow: debit the contra (checking) account, credit income.
            category = "Uncategorized Income"
            posting_lines = [(contra_account, abs(amount), 0.0), (category, 0.0, abs(amount))]
            direction = "inflow"
        else:
            # Withdrawal / purchase / outflow: debit the expense category, credit the contra account.
            matched_category = ledger.apply_category_rules(desc)

            # ✅ DIAGNOSTIC LOGGING: See exactly what the rule engine is doing
            import logging
            logging.getLogger(__name__).info(f"[RULE ENGINE DEBUG] Desc: '{desc}' | Matched: '{matched_category}'")

            category = matched_category or "Uncategorized Expense"
            posting_lines = [(category, abs(amount), 0.0), (contra_account, 0.0, abs(amount))]
            direction = "outflow"

        lines.append(
            {
                "date": r.get("date") or r.get("trans_date") or r.get("post_date"),
                "description": desc[:80],
                "category_account": category,
                "contra_account": contra_account,
                "amount": amount,
                "direction": direction,
                "lines": posting_lines,
                "statement_currency": STATEMENT_CURRENCY,
                "statement_balance": r.get("balance"),
                "balance_check": r.get("balance_check"),
                "balance_check_detail": r.get("balance_check_detail"),
            }
        )
    return lines


def parse_statement_to_transactions(
    pdf_path: str,
    ledger: "GeneralLedger",
    bank: Optional[str] = None,
    statement_type: Optional[str] = "auto",  # "credit_card" or "bank"
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Returns (lines, diagnostics).

    lines: ledger-ready import lines (see raw_rows_to_import_lines).
    diagnostics: {
        "bank": str,
        "statement_type": str,
        "skipped_lines": [{"line": str, "reason": str}, ...],
        "opening_balance": Decimal | None,
        "closing_balance": Decimal | None,
        "balance_mismatches": [ {date, description, detail}, ... ],
    }
    """
    with pdfplumber.open(pdf_path) as pdf:
        sample = ""
        for page in pdf.pages[:3]:
            sample += page.extract_text() or ""

    if not bank:
        bank = detect_bank_from_text(sample)

    if not bank:
        raise ValueError(
            "Unable to detect bank. Please choose the bank manually."
        )

    bank = bank.lower()

    if statement_type in (None, "", "auto"):
        detected_type = detect_statement_type_from_text(sample)

        if not detected_type:
            raise ValueError(
                "Unable to detect statement type. "
                "Please choose Bank Account or Credit Card manually."
            )

        statement_type = detected_type

    statement_type = statement_type.lower()

    skipped_lines: List[Dict[str, str]] = []
    opening_balance = None
    closing_balance = None

    # Choose parser based on bank + statement type
    if bank == "cibc":
        if statement_type == "bank":
            # You can add parse_cibc_bank_statement_pdf later if needed
            raise ValueError("CIBC bank statement parser not implemented yet.")
        else:
            with pdfplumber.open(pdf_path) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            raw, skipped_lines = _parse_cibc_full(text)
    elif bank == "tangerine":
        if statement_type == "bank":
            with pdfplumber.open(pdf_path) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            result = _parse_tangerine_bank_statement_full(text)
            raw = result["transactions"]
            skipped_lines = result["skipped_lines"]
            opening_balance = result["opening_balance"]
            closing_balance = result["closing_balance"]
        else:
            with pdfplumber.open(pdf_path) as pdf:
                text = ""
                for page in pdf.pages:
                    pt = page.extract_text()
                    if pt:
                        text += pt + "\n"
            raw, skipped_lines = _parse_tangerine_full(text)
    else:
        raise ValueError(f"Unsupported bank: {bank}")

    lines = raw_rows_to_import_lines(raw, ledger)

    balance_mismatches = [
        {"date": ln["date"], "description": ln["description"], "detail": ln["balance_check_detail"]}
        for ln in lines
        if ln.get("balance_check") == "mismatch"
    ]

    diagnostics = {
        "bank": bank,
        "statement_type": statement_type,
        "skipped_lines": skipped_lines,
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
        "balance_mismatches": balance_mismatches,
    }

    return lines, diagnostics
