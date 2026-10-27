"""
merchant_normalization.py
Pure, deterministic merchant-string normalization for CogniFinance.

Decoupled from categorization by design (Merchant Memory decision #1):
this module only turns a raw bank/card description into two identity
strings. It never touches the ledger, never affects transaction math, and
has no side effects -- call it as many times as you like on the same
string and always get the same answer back.

Two outputs, always both computed:
  merchant_key      -- granular. Preserves a detected store/location
                        number (e.g. "STARBUCKS #0234"). This is the
                        actual learning key -- always granular, for
                        everyone, regardless of Simple/Pro mode.
  merchant_display  -- collapsed. The same string with any detected store
                        number stripped (e.g. "STARBUCKS"). Used only for
                        Simple Mode pooling/presentation.

If no store number is detected, merchant_key == merchant_display -- there
is nothing to pool, so granular and collapsed are the same value.

This is deliberately rule-based, not a merchant-ID lookup service or a
learned model: it has to run fully offline in an encrypted, local-first
app with no network calls and no external merchant database. It is also
deliberately a *best-effort* normalizer, not an authoritative one -- see
"Known limitations" at the bottom. The Merchant Memory design already
assumes normalization noise is possible and absorbs it (draft staging,
minimum occurrence floor, user correction feedback) rather than requiring
this function to be perfect.
"""
import re
from typing import Tuple

# --- Payment-processor prefixes banks/cards commonly prepend ---
# Stripped before anything else -- they carry no merchant identity of
# their own (e.g. "SQ *STARBUCKS" and "TST* STARBUCKS" are the same
# merchant as far as the user is concerned).
_PROCESSOR_PREFIXES = (
    r"SQ \*", r"SQU\*", r"TST\*", r"TST \*", r"SP \*", r"SP\*",
    r"PAYPAL \*", r"PP\*", r"CKO\*", r"IZ \*", r"IZI\*",
    r"CLOVER\*", r"TOAST\*", r"SHOPIFY\*", r"STRIPE\*",
)
_PROCESSOR_PREFIX_RE = re.compile(
    r"^(?:" + "|".join(_PROCESSOR_PREFIXES) + r")\s*", re.IGNORECASE
)

# --- Trailing noise: phone numbers, zips, state codes, URLs, ref IDs ---
# Each is matched and stripped from the END of the string only, one kind
# at a time, in a loop -- so "...SEATTLE WA 98101" sheds the zip, then the
# state, without one monster regex trying to do everything in one pass.
_TRAILING_NOISE_PATTERNS = (
    re.compile(r"\s+\d{3}[-.\s]?\d{3}[-.\s]?\d{4}$"),                 # phone number
    re.compile(r"\s+\d{5}(-\d{4})?$"),                                # zip / zip+4
    re.compile(r"\s+[A-Z0-9]+\.(COM|NET|ORG|CA)(/[A-Z0-9]*)?$", re.IGNORECASE),  # trailing domain
    re.compile(r"\s+(?:REF|CONF|TXN|AUTH)\s*#?\s*[A-Z0-9]{4,}$", re.IGNORECASE), # reference id
    re.compile(r"\s+[A-Z]{2}$"),                                      # trailing 2-letter state
)

# --- Store/location number: the thing that makes merchant_key granular ---
# Keyworded forms ("STORE #123", "UNIT 123") are tried before the bare
# "#123" fallback so the keyword itself gets consumed as part of the
# match rather than left dangling in front of it.
_STORE_NUMBER_PATTERNS = (
    re.compile(r"\bSTORE\s?#?\s?(\d{1,6})\b", re.IGNORECASE),
    re.compile(r"\bUNIT\s?#?\s?(\d{1,6})\b", re.IGNORECASE),
    re.compile(r"\bNO\.?\s?(\d{1,6})\b", re.IGNORECASE),
    re.compile(r"#\s?(\d{1,6})\b"),
)

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCTUATION_RE = re.compile(r"[^\w\s&']")


def _strip_trailing_noise(s: str) -> str:
    changed = True
    while changed:
        changed = False
        for pattern in _TRAILING_NOISE_PATTERNS:
            new_s = pattern.sub("", s)
            if new_s != s:
                s = new_s
                changed = True
    return s


def _clean_base(s: str) -> str:
    """Uppercase, drop punctuation (keep & and '), collapse whitespace."""
    s = s.upper()
    s = _PUNCTUATION_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


def normalize_merchant(raw_string: str) -> Tuple[str, str]:
    """
    Turn a raw bank/card description into (merchant_key, merchant_display).

    Safe to call speculatively -- e.g. for a live preview before the user
    confirms anything -- since it's a pure function with no side effects.
    """
    if not raw_string or not raw_string.strip():
        return "", ""

    s = raw_string.strip()
    s = _PROCESSOR_PREFIX_RE.sub("", s)
    s = _strip_trailing_noise(s)

    store_number = None
    for pattern in _STORE_NUMBER_PATTERNS:
        match = pattern.search(s)
        if match:
            store_number = match.group(1)
            # Truncate at the store number, discarding everything after
            # it -- real descriptors follow "<brand> STORE #123 <city>
            # <state>", so text after the number is locational noise, not
            # merchant identity. This is also what makes pooling actually
            # work: "STARBUCKS STORE #01234 SEATTLE" and "STARBUCKS STORE
            # #04567 PORTLAND" need to collapse to the same
            # merchant_display, not two different ones with the city
            # baked in.
            s = s[:match.start()]
            break

    merchant_display = _clean_base(s)
    if not merchant_display:
        # Degenerate case: the string was entirely prefix/noise/store
        # number. Fall back to a cleaned copy of the raw input so we
        # never hand back an empty identity.
        merchant_display = _clean_base(raw_string)

    if store_number:
        merchant_key = f"{merchant_display} #{store_number.zfill(4)}"
    else:
        merchant_key = merchant_display

    return merchant_key, merchant_display


# --- Known limitations (acceptable given how this feeds Merchant Memory) ---
#
# 1. City/neighborhood names with no recognizable state/zip/phone after
#    them (e.g. "STARBUCKS STORE #123 SEATTLE") won't be stripped -- there's
#    no offline city gazetteer here, by design (no network calls, no
#    external database). The leftover city text becomes part of
#    merchant_display, which just means that one location's pattern
#    accumulates slightly slower than a perfectly-collapsed one would --
#    it doesn't corrupt anything, and a real merchant with real volume
#    still crosses the occurrence floor in normal use.
# 2. Purely numeric order/transaction IDs with no separator (e.g.
#    "AMAZON.COM*A1B2C3D4") are left attached to the base name if they
#    don't match one of the explicit store-number patterns -- intentional,
#    since treating every such ID as a "store number" would make
#    merchant_key nearly unique per transaction and defeat pattern
#    learning entirely. False negatives here are far cheaper than false
#    positives.
# 3. This function does not deduplicate near-synonyms (e.g. "WAL-MART"
#    vs "WALMART") -- each distinct raw spelling becomes its own pattern.
#    If this turns out to matter in practice, it's a candidate for a small
#    static alias table, not a change to the parsing logic itself.
