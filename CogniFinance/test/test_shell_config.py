"""
test_shell_config.py

Contract for the sidebar layouts. The point of these tests is that a
mode can reshape the sidebar but can never make a screen vanish, and
that Pro Mode stays exactly what it is today.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shell_config
from shell_config import PRO_TABS, ADVANCED_TARGETS, sidebar_entries, targets


def test_pro_sidebar_is_unchanged():
    entries = sidebar_entries(simple=False)
    assert [e.label for e in entries] == PRO_TABS
    assert [e.target for e in entries] == PRO_TABS
    assert not any(e.placeholder for e in entries)


def test_pro_ignores_show_advanced():
    assert targets(False, True) == targets(False, False) == PRO_TABS


def test_every_pro_screen_is_reachable_in_simple():
    """With advanced screens shown, nothing is missing."""
    reachable = set(targets(simple=True, show_advanced=True))
    missing = [t for t in PRO_TABS if t not in reachable]
    assert not missing, f"Unreachable in Simple Mode: {missing}"


def test_hidden_screens_are_exactly_the_advanced_list():
    """Without advanced, the screens that are missing must be exactly the
    ones the Settings toggle reveals -- no screen is hidden with no way back."""
    shown = set(targets(simple=True, show_advanced=False))
    hidden = {t for t in PRO_TABS if t not in shown}
    assert hidden == set(ADVANCED_TARGETS)


def test_advanced_group_appears_only_when_asked():
    plain = [e.group for e in sidebar_entries(True, False)]
    more = [e.group for e in sidebar_entries(True, True)]
    assert shell_config.GROUP_MORE not in plain
    assert shell_config.GROUP_MORE in more


def test_no_duplicate_labels_or_targets_in_a_view():
    for advanced in (False, True):
        entries = sidebar_entries(True, advanced)
        labels = [e.label for e in entries]
        tgts = [e.target for e in entries]
        assert len(labels) == len(set(labels))
        assert len(tgts) == len(set(tgts))


def test_every_target_is_a_known_screen():
    known = set(shell_config.ALL_SCREENS)
    for advanced in (False, True):
        for entry in sidebar_entries(True, advanced):
            assert entry.target in known, entry


def test_placeholders_say_so():
    """A Pro screen standing in for a Simple one must tell the user."""
    for advanced in (False, True):
        for entry in sidebar_entries(True, advanced):
            if entry.placeholder:
                assert len(entry.note.strip()) > 20, entry


def test_simple_only_screens_are_appended_after_every_pro_screen():
    """Appending keeps every existing stack index where it was."""
    assert shell_config.ALL_SCREENS[:len(PRO_TABS)] == PRO_TABS
    assert shell_config.ALL_SCREENS[len(PRO_TABS):] == shell_config.SIMPLE_ONLY_SCREENS


def test_pro_never_lists_a_simple_only_screen():
    for advanced in (False, True):
        for target in targets(False, advanced):
            assert target not in shell_config.SIMPLE_ONLY_SCREENS


def test_home_opens_the_simple_home_screen():
    assert sidebar_entries(True, False)[0].target == "Simple Home"
    assert shell_config.HOME_TARGET["simple"] == "Simple Home"
    assert not sidebar_entries(True, False)[0].placeholder


def test_mobile_sync_is_a_main_destination_not_an_advanced_one():
    """The phone apps can only connect through this screen."""
    assert "Mobile Sync" not in ADVANCED_TARGETS
    entry = shell_config.entry_for_target(True, False, "Mobile Sync")
    assert entry is not None and entry.group == shell_config.GROUP_MAIN


def test_money_opens_the_simple_money_screen():
    entry = shell_config.entry_for_target(True, False, "Simple Money")
    assert entry is not None and entry.label == "Money" and not entry.placeholder


def test_the_pro_screens_money_replaces_are_still_reachable_in_simple():
    for target in ("Transactions", "Add/Edit"):
        assert not shell_config.is_reachable(True, False, target)
        assert shell_config.is_reachable(True, True, target)


def test_plans_opens_the_plans_screen_and_pros_budgets_stay_reachable():
    entry = shell_config.entry_for_target(True, False, "Plans")
    assert entry is not None and entry.label == "Plans" and not entry.placeholder
    assert not shell_config.is_reachable(True, False, "Budgets")
    assert shell_config.is_reachable(True, True, "Budgets")


def test_pro_overview_is_still_reachable_in_simple():
    assert shell_config.is_reachable(True, True, "Overview")
    assert not shell_config.is_reachable(True, False, "Overview")


def test_settings_is_last_in_simple():
    entries = sidebar_entries(True, True)
    assert entries[-1].target == "Settings"


def test_simple_main_destinations_come_first():
    labels = [e.label for e in sidebar_entries(True, False)]
    assert labels[:5] == ["Home", "Money", "Plans", "Reports", "Mobile Sync"]


def test_home_target_exists_in_both_modes():
    for simple in (False, True):
        assert shell_config.is_reachable(
            simple, False, shell_config.HOME_TARGET["simple" if simple else "pro"])
