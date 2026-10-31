"""
test_simple_home_tab.py

Home's own behaviour: its links, its scope text, and that a quick-add
refreshes it. (The quick-add form itself is tested in test_quick_add.py.)
"""
import os
import sys
from datetime import datetime
from decimal import Decimal

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtWidgets import QApplication, QLabel

import simple_home_tab
from test_home_data import make

D = Decimal


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def tab(app):
    gl = make()
    gl.post_simple_transaction = lambda *a, **k: (True, "ok")
    return simple_home_tab.SimpleHomeTab(
        gl, display_name_fn=lambda: None,
        now_fn=lambda: datetime(2026, 10, 24, 9, 30),
        amount_fn=simple_home_tab._fallback_amount)


def test_see_all_links_follow_the_sidebar(tab):
    tab.set_navigable_targets({"Simple Money"})
    visible = {t for b, t in tab._links if not b.isHidden()}
    assert visible == {"Simple Money"}
    tab.set_navigable_targets(None)
    assert all(not b.isHidden() for b, _t in tab._links)


def test_link_click_requests_navigation(tab):
    got = []
    tab.navigate_requested.connect(got.append)
    button = next(b for b, t in tab._links if t == "Plans")
    button.click()
    assert got == ["Plans"]


def test_quick_add_refreshes_home_and_reports_the_change(tab):
    seen = []
    tab.data_modified.connect(lambda: seen.append(True))
    tab.quick_add.payee_edit.setText("Cafe")
    tab.quick_add.category_combo.setCurrentIndex(tab.quick_add.category_combo.findData("Groceries Expense"))
    tab.quick_add.amount_edit.setText("5")
    tab.quick_add.add_button.click()
    assert seen == [True]


def test_scope_text_is_shown(tab):
    texts = [label.text() for label in tab.findChildren(QLabel)]
    assert any(t.startswith("Latest ") and t.endswith("transactions") for t in texts)
    assert any("of 2 envelopes" in t for t in texts)


def test_fmt_money():
    assert simple_home_tab.fmt_money(D("1234.5")) == "$1,234.50"
    assert simple_home_tab.fmt_money(D("-3")) == "-$3.00"
    assert simple_home_tab.fmt_money(D("3"), signed=True) == "+$3.00"
