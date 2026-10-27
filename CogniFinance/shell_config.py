"""
shell_config.py ver 0.1
For CogniFinance ver 1.9

Which sidebar entries each mode shows, and where they go.

THE IDEA
========
BudgetApp builds every screen once and keeps them all in one stacked
widget, as it does today. A mode never creates or destroys screens; it
only decides which sidebar entries point at them. Switching modes is
therefore "rebuild the sidebar list, select the home screen" -- no
widget teardown, no signal rewiring, no restart.

``target`` is the name of a screen in that stack. Today every target is
an existing tab name. When a real Simple screen is built (SimpleHome,
SimplePlans, ...), it is added to the END of the stack and the entry's
target is changed to its name; the stack order of existing screens, and
so every existing index, never moves.

Pure Python, no Qt.
"""
from dataclasses import dataclass
from typing import List

# The Pro sidebar: exactly today's tabs, in today's order.
PRO_TABS: List[str] = [
    "Overview", "Reconciliation", "Transactions", "Add/Edit",
    "Budgets", "Recurring", "Investments", "Charts", "Reports",
    "Accounts", "Balance Sheet", "Category Rules", "Taxes",
    "Mobile Sync", "Logs", "Audit Log", "Settings",
]

# Screens that exist only for Simple Mode. They are appended to the END of
# the stacked widget (after every Pro screen) so no existing index moves.
SIMPLE_ONLY_SCREENS: List[str] = ["Simple Home", "Simple Money", "Plans"]

#: Every screen in the stacked widget, in stack order.
ALL_SCREENS: List[str] = PRO_TABS + SIMPLE_ONLY_SCREENS

GROUP_MAIN = "main"
GROUP_FULL_LAYOUT = "full_layout"   # Pro screens shown as-is in Simple
GROUP_MORE = "more"                 # only when "Show advanced screens" is on
GROUP_FOOTER = "footer"

# Heading shown above each group in Simple Mode ("" = no heading).
GROUP_HEADINGS = {
    GROUP_MAIN: "",
    GROUP_FULL_LAYOUT: "Full layout",
    GROUP_MORE: "More",
    GROUP_FOOTER: "",
}


@dataclass(frozen=True)
class SidebarEntry:
    label: str              # text in the sidebar
    target: str             # screen name in the stacked widget
    group: str = GROUP_MAIN
    placeholder: bool = False   # True while the screen is a Pro screen standing in
    note: str = ""              # one line shown on a placeholder screen


_FULL_NOTE = "This screen shows the full layout. A simpler version is planned."

# Simple Mode, in display order within each group.
_SIMPLE_MAIN = [
    SidebarEntry("Home", "Simple Home"),
    SidebarEntry("Money", "Simple Money"),
    SidebarEntry("Plans", "Plans"),
    SidebarEntry("Reports", "Simple Reports"),
    # Pairing a phone is how the mobile apps connect at all, so it cannot
    # sit behind "Show advanced screens".
    SidebarEntry("Mobile Sync", "Mobile Sync", placeholder=True,
                 note="This is the full sync screen. A simpler version is planned."),
    SidebarEntry("Settings", "Settings"),
]

_SIMPLE_FULL_LAYOUT = [
    SidebarEntry("Investments", "Investments", GROUP_FULL_LAYOUT,
                 placeholder=True, note=_FULL_NOTE),
    SidebarEntry("Taxes", "Taxes", GROUP_FULL_LAYOUT,
                 placeholder=True, note=_FULL_NOTE),
]

_SIMPLE_MORE = [
    # Pro screens that Home and Money replace stay one click away.
    SidebarEntry("Full overview", "Overview", GROUP_MORE),
    SidebarEntry("Full transactions", "Transactions", GROUP_MORE),
    SidebarEntry("Full add/edit", "Add/Edit", GROUP_MORE),
    SidebarEntry("Full budgets", "Budgets", GROUP_MORE),
] + [
    SidebarEntry(name, name, GROUP_MORE) for name in (
        "Reconciliation", "Recurring", "Charts", "Accounts",
        "Balance Sheet", "Category Rules", "Logs", "Audit Log",
    )
]

_SIMPLE_FOOTER = []

#: Screens Simple Mode reaches only through "Show advanced screens".
ADVANCED_TARGETS: List[str] = [e.target for e in _SIMPLE_MORE]

#: The screen selected when a mode is entered.
HOME_TARGET = {"pro": "Overview", "simple": "Simple Home"}


def sidebar_entries(simple: bool, show_advanced: bool = False) -> List[SidebarEntry]:
    """The ordered sidebar for a mode.

    Pro always shows every tab, in its original order, and ignores
    ``show_advanced``. Simple shows its own entries, plus the advanced
    group only when asked.
    """
    if not simple:
        return [SidebarEntry(name, name) for name in PRO_TABS]
    entries = list(_SIMPLE_MAIN) + list(_SIMPLE_FULL_LAYOUT)
    if show_advanced:
        entries += _SIMPLE_MORE
    entries += _SIMPLE_FOOTER
    return entries


def targets(simple: bool, show_advanced: bool = False) -> List[str]:
    return [e.target for e in sidebar_entries(simple, show_advanced)]


def entry_for_target(simple: bool, show_advanced: bool, target: str):
    """The entry that opens ``target`` in this view, or None if hidden."""
    for entry in sidebar_entries(simple, show_advanced):
        if entry.target == target:
            return entry
    return None


def is_reachable(simple: bool, show_advanced: bool, target: str) -> bool:
    return entry_for_target(simple, show_advanced, target) is not None
