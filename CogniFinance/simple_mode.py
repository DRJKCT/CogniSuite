"""
simple_mode.py ver 0.1
For CogniFinance ver 0.9.67

The app-wide presentation policy behind CogniFinance's Simple Mode.

WHAT THIS IS
============
Simple Mode is a *presentation* policy. It decides how much of the same
truth is on screen at once. It never decides what is stored.

THE CONTRACT (read this before adding a tab or a feature)
=========================================================
One rule, and every tab is checked against it:

    "Simple Mode collapses detail into counts, hides specialist
     controls, and leaves every number, every write, and every audit
     entry exactly as Pro Mode produced it -- and whatever it hides,
     it hides behind a one-click reveal."

Four legal answers for any tab or feature:

    SIMPLIFIED       The surface changes shape in Simple Mode: rows
                     collapse into counts, specialist controls move
                     behind a "Show advanced" reveal.
    ALREADY_SIMPLE   The surface is already a plain summary and Simple
                     Mode needs no change. This is a complete answer,
                     not a deferral.
    PRO_ONLY         The surface cannot be simplified without hiding
                     truth. A non-empty reason is REQUIRED, and the tab
                     degrades to Pro behaviour rather than silently
                     ignoring the mode.
    PENDING          Not simplified yet. The Pro layout is shown, and
                     that fact is disclosed in Settings -- never
                     silently.

Non-negotiable invariants:

    * Storage is identical in both modes. Merchant patterns stay
      granular, budgets keep their assignments, locked periods stay
      locked, the audit trail stays complete, tax math stays exact.
    * An action that changes more than one row must say so, before it
      happens ("this will rate 4 locations") and after ("Rated 4
      locations"). A silent fan-out is a bug, not a simplification.
    * A number shown in Simple Mode is the engine's number restated in
      fewer words -- never a different number.
    * Nothing becomes unreachable. Anything hidden is one click away.

WHERE THE MODE LIVES
====================
ui_config.json, top-level "simple_mode" key. It is a per-app
preference, NOT a property of the ledger: the same .db file opens
correctly in either mode, and two people sharing one ledger can each
pick their own mode. (That is deliberate -- it keeps the stage-3
SMB question open rather than deciding it here.)
"""
import logging
from typing import Dict, List, Tuple

import ui_config

logger = logging.getLogger(__name__)

# --- The mode flag --------------------------------------------------------

SIMPLE_MODE_KEY = "simple_mode"

# The mode a user gets until they choose otherwise. Flipping this to
# True is the launch decision for Simple Mode (Phase 1's primary
# audience); it is deliberately NOT flipped in the same change that
# introduces the mechanism, because that would silently change what
# every existing user sees on upgrade.
DEFAULT_SIMPLE_MODE = False


# --- Tab participation statuses -------------------------------------------

STATUS_SIMPLIFIED = "simplified"
STATUS_ALREADY_SIMPLE = "already_simple"
STATUS_PRO_ONLY = "pro_only"
STATUS_PENDING = "pending"

_PENDING_REASON = (
    "Not simplified yet -- this screen still shows the full Pro layout."
)

#: Sidebar tab name -> (status, reason).
#:
#: The key must match the tab name in ui_main.BudgetApp.tab_names exactly.
#: A reason is required for PRO_ONLY and PENDING; a blank reason there is
#: a bug, and the contract test in test/test_simple_mode_contract.py
#: fails on one. PRO_ONLY reasons are user-facing (Settings shows them),
#: so they are written for a person, not for a developer.
TAB_MODE_REGISTRY: Dict[str, Tuple[str, str]] = {
    "Overview": (
        STATUS_ALREADY_SIMPLE,
        "The overview is already a monthly summary.",
    ),
    "Reconciliation": (
        STATUS_SIMPLIFIED,
        "",
    ),
    "Transactions": (
        STATUS_PENDING,
        _PENDING_REASON,
    ),
    "Add/Edit": (
        STATUS_PENDING,
        _PENDING_REASON,
    ),
    "Budgets": (
        STATUS_PENDING,
        _PENDING_REASON,
    ),
    "Recurring": (
        STATUS_PENDING,
        _PENDING_REASON,
    ),
    # User request: Investments deferred to PENDING (not permanently
    # excluded). The cost-basis display can be collapsed behind a reveal
    # in a future slice.
    "Investments": (
        STATUS_PENDING,
        "Not simplified yet -- cost basis and unrealized gains need a "
        "collapsed view rather than exclusion; deferred.",
    ),
    "Charts": (
        STATUS_SIMPLIFIED,
        "",
    ),
    "Reports": (
        STATUS_PENDING,
        _PENDING_REASON,
    ),
    "Accounts": (
        STATUS_ALREADY_SIMPLE,
        "The chart of accounts is already a name-and-type list.",
    ),
    "Balance Sheet": (
        STATUS_SIMPLIFIED,
        "",
    ),
    "Category Rules": (
        STATUS_PENDING,
        _PENDING_REASON,
    ),
    # Placeholder for now: the full calculator is shown in Simple Mode
    # until a simpler version exists. Pending, not exempt.
    "Taxes": (
        STATUS_PENDING,
        _PENDING_REASON,
    ),
    "Mobile Sync": (
        STATUS_PENDING,
        _PENDING_REASON,
    ),
    "Logs": (
        STATUS_PENDING,
        _PENDING_REASON,
    ),
    "Audit Log": (
        STATUS_PRO_ONLY,
        "The audit trail is the evidence layer: every entry is a "
        "statement of fact about what happened to the ledger. There is "
        "no way to collapse it that does not withhold truth, so it "
        "always shows everything.",
    ),
    "Settings": (
        STATUS_PENDING,
        _PENDING_REASON,
    ),
}


# --- Persistence ----------------------------------------------------------

def load_simple_mode() -> bool:
    """The persisted mode. Anything unrecognized means Pro.

    A missing key means DEFAULT_SIMPLE_MODE; a hand-edited file with a
    non-boolean value is ignored rather than coerced, so a corrupted
    config can never put the app into a mode it does not understand.
    """
    raw = ui_config.load_ui_config().get(SIMPLE_MODE_KEY, DEFAULT_SIMPLE_MODE)
    if isinstance(raw, bool):
        return raw
    return DEFAULT_SIMPLE_MODE


def save_simple_mode(simple: bool) -> None:
    """Persist the mode. Raises on write failure so the caller can tell
    the user instead of claiming success."""
    ui_config.update_ui_config({SIMPLE_MODE_KEY: bool(simple)})


def is_simple_mode() -> bool:
    """Current persisted mode (for callers with no access to the app)."""
    return load_simple_mode()


def ledger_mode(simple: bool) -> str:
    """The mode string the ledger's ``mode=`` parameter expects.

    The ledger already speaks "simple"/"pro"; this is the one place the
    UI policy maps onto that vocabulary, so the two cannot drift.
    """
    return "simple" if simple else "pro"


# --- Registry queries -----------------------------------------------------

def tab_status(tab_name: str) -> str:
    entry = TAB_MODE_REGISTRY.get(tab_name)
    return entry[0] if entry else STATUS_PENDING


def tab_reason(tab_name: str) -> str:
    entry = TAB_MODE_REGISTRY.get(tab_name)
    return entry[1] if entry else _PENDING_REASON


def pro_only_tabs() -> List[str]:
    """Tabs that are always full, by design, with a documented reason."""
    return [name for name, (status, _r) in TAB_MODE_REGISTRY.items()
            if status == STATUS_PRO_ONLY]


def pending_tabs() -> List[str]:
    """Tabs not simplified yet -- shown in the full Pro layout."""
    return [name for name, (status, _r) in TAB_MODE_REGISTRY.items()
            if status == STATUS_PENDING]


def unsimplified_tabs() -> List[str]:
    """Every tab that will not change shape in Simple Mode, for disclosure."""
    return pro_only_tabs() + pending_tabs()


def describe_unsimplified() -> str:
    """One user-facing paragraph naming the screens that stay full.

    Shown in Settings so a Simple user is never surprised by a Pro-shaped
    screen, and never told that the mode is in effect when it is not.
    """
    always = pro_only_tabs()
    later = pending_tabs()
    parts = []
    if always:
        parts.append(
            "Always shown in full (they would hide the truth): "
            + ", ".join(always) + "."
        )
    if later:
        parts.append(
            "Not simplified yet, so they still show the full layout: "
            + ", ".join(later) + "."
        )
    return " ".join(parts) if parts else "Every screen supports Simple Mode."
