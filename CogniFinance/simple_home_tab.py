"""
simple_home_tab.py ver 0.1
For CogniFinance ver 1.9

Simple Mode's Home screen: where do I stand, what happened lately, and
a quick way to add something.

Reads and navigates. Budget changes (assigning, moving money) happen on
the Plans screen only, so there is one place that writes the budget.
The one thing Home does write is a quick-add transaction, and it goes
through GeneralLedger.record_transaction, the same call Pro Mode uses.

Every figure comes from home_data, which in turn calls the ledger and
budget engine. Nothing is computed here.
"""
from datetime import datetime
from decimal import Decimal
from typing import Callable, Optional

from PyQt6.QtCore import Qt, pyqtSignal, QPointF, QRectF
from PyQt6.QtGui import (QPainter, QPainterPath, QColor, QPen,
                         QLinearGradient, QBrush)
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QScrollArea, QSizePolicy,
)

import home_data
from budget_summary import STATE_OVERSPENT
from ledger import ZERO
from quick_add import QuickAddBody

from simple_theme import LIGHT, DARK, fmt_money, stylesheet   # noqa: E402


def _fallback_amount(ledger, tx) -> Decimal:
    """Used only if ui_helpers cannot be imported. Net, so a refund shows as
    money coming back rather than zero."""
    kinds = [ledger.accounts[line.account_name].account_type for line in tx.lines]
    expense = sum((l.dr - l.cr for l, k in zip(tx.lines, kinds) if k == "Expense"), ZERO)
    revenue = sum((l.cr - l.dr for l, k in zip(tx.lines, kinds) if k == "Revenue"), ZERO)
    if "Expense" in kinds:
        return -expense
    if "Revenue" in kinds:
        return revenue
    return sum((line.dr for line in tx.lines), ZERO)     # a transfer: the amount moved


def _default_amount_fn() -> Callable:
    try:
        from ui_helpers import get_transaction_display_amount
        return get_transaction_display_amount
    except Exception:
        return _fallback_amount


def _default_display_name() -> Optional[str]:
    try:
        import ui_config
        return ui_config.load_ui_config().get("display_name")
    except Exception:
        return None


class Sparkline(QWidget):
    """A small filled line of a running total. Decoration for a number
    that is printed next to it; it carries no scale of its own."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._points = []
        self._color = QColor("#059669")
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_data(self, points, color: str):
        self._points = [max(0.0, float(p)) for p in points]
        self._color = QColor(color)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h, pad = self.width(), self.height(), 4
        pts = self._points
        top = max(pts) if pts else 0.0
        if len(pts) < 2 or top <= 0:
            pen = QPen(self._color, 2)
            pen.setColor(QColor(self._color.red(), self._color.green(), self._color.blue(), 120))
            painter.setPen(pen)
            painter.drawLine(pad, h - pad, w - pad, h - pad)
            return
        step = (w - 2 * pad) / (len(pts) - 1)
        xy = [QPointF(pad + i * step, h - pad - (v / top) * (h - 2 * pad))
              for i, v in enumerate(pts)]
        line = QPainterPath(xy[0])
        for a, b in zip(xy, xy[1:]):
            mid = (a.x() + b.x()) / 2
            line.cubicTo(QPointF(mid, a.y()), QPointF(mid, b.y()), b)
        fill = QPainterPath(line)
        fill.lineTo(QPointF(xy[-1].x(), h - pad))
        fill.lineTo(QPointF(xy[0].x(), h - pad))
        fill.closeSubpath()
        gradient = QLinearGradient(0, 0, 0, h)
        top_color = QColor(self._color)
        top_color.setAlpha(90)
        bottom_color = QColor(self._color)
        bottom_color.setAlpha(0)
        gradient.setColorAt(0, top_color)
        gradient.setColorAt(1, bottom_color)
        painter.fillPath(fill, QBrush(gradient))
        painter.setPen(QPen(self._color, 2))
        painter.drawPath(line)


class Bar(QWidget):
    """A thin progress bar: how much of the usable money has been spent."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fraction = 0.0
        self._fill = QColor("#059669")
        self._track = QColor("#E5E7EB")
        self.setFixedHeight(8)

    def set_value(self, fraction: float, fill: str, track: str):
        self._fraction = max(0.0, min(1.0, fraction))
        self._fill, self._track = QColor(fill), QColor(track)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        rect = QRectF(0, 0, self.width(), self.height())
        painter.setBrush(self._track)
        painter.drawRoundedRect(rect, 4, 4)
        if self._fraction > 0:
            painter.setBrush(self._fill)
            painter.drawRoundedRect(QRectF(0, 0, self.width() * self._fraction, self.height()), 4, 4)


def _clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


class SimpleHomeTab(QWidget):
    data_modified = pyqtSignal()
    navigate_requested = pyqtSignal(str)       # a screen name

    def __init__(self, ledger, parent=None, amount_fn: Optional[Callable] = None,
                 display_name_fn: Optional[Callable] = None,
                 now_fn: Optional[Callable] = None):
        super().__init__(parent)
        self.ledger = ledger
        self._amount_fn = amount_fn or _default_amount_fn()
        self._display_name_fn = display_name_fn or _default_display_name
        self._now_fn = now_fn or datetime.now
        self.is_dark_mode = False
        self._theme = LIGHT
        self._links = []                       # (button, target screen name)
        self._navigable = None                 # None = every link shown
        self._build()
        self.refresh()

    # ------------------------------------------------------------------ build

    def _card(self, title: str, link_target: Optional[str] = None,
              link_text: str = "See all \u203a", tooltip: str = ""):
        frame = QFrame()
        frame.setObjectName("HomeCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)
        head = QHBoxLayout()
        label = QLabel(title.upper())
        label.setObjectName("CardTitle")
        head.addWidget(label)
        if tooltip:
            info = QLabel("\u24d8")
            info.setObjectName("CardInfo")
            info.setToolTip(tooltip)
            head.addWidget(info)
        head.addStretch()
        if link_target:
            button = QPushButton(link_text)
            button.setObjectName("CardLink")
            button.setFlat(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, t=link_target: self.navigate_requested.emit(t))
            head.addWidget(button)
            self._links.append((button, link_target))
        layout.addLayout(head)
        return frame, layout

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)
        page = QWidget()
        page.setObjectName("HomePage")
        scroll.setWidget(page)
        grid = QGridLayout(page)
        grid.setContentsMargins(24, 20, 24, 24)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(16)
        for column in range(12):
            grid.setColumnStretch(column, 1)

        self.greeting_label = QLabel()
        self.greeting_label.setObjectName("Greeting")
        self.date_label = QLabel()
        self.date_label.setObjectName("DateLine")
        header = QVBoxLayout()
        header.setSpacing(2)
        header.addWidget(self.greeting_label)
        header.addWidget(self.date_label)
        grid.addLayout(header, 0, 0, 1, 12)

        # Spending
        self.spending_card, lay = self._card(
            "This month's spending", link_target="Reports", link_text="Reports \u203a",
            tooltip="Money spent this month: debits to expense accounts. "
                    "Transfers between your accounts and credit card payments "
                    "are not counted.")
        self.spending_period = QLabel()
        self.spending_period.setObjectName("Muted")
        self.spending_total = QLabel()
        self.spending_total.setObjectName("BigNumber")
        self.sparkline = Sparkline()
        self.spending_top = QLabel()
        self.spending_top.setObjectName("Muted")
        self.spending_top.setWordWrap(True)
        for widget in (self.spending_period, self.spending_total, self.sparkline, self.spending_top):
            lay.addWidget(widget)
        grid.addWidget(self.spending_card, 1, 0, 1, 6)

        # Accounts
        self.accounts_card, self.accounts_layout = self._card(
            "Your accounts", link_target="Accounts")
        grid.addWidget(self.accounts_card, 1, 6, 1, 6)

        # Recent activity
        self.recent_card, self.recent_layout = self._card(
            "Recent activity", link_target="Simple Money")
        grid.addWidget(self.recent_card, 2, 0, 1, 6)

        # Quick add (shared with Money)
        add_card, add_layout = self._card("Add transaction")
        add_card.setMinimumWidth(230)
        self.quick_add = QuickAddBody(self.ledger, now_fn=self._now_fn)
        self.quick_add.data_modified.connect(self._after_quick_add)
        add_layout.addWidget(self.quick_add)
        add_layout.addStretch()
        grid.addWidget(add_card, 2, 6, 1, 3)

        # Budget
        self.budget_card, self.budget_layout = self._card("Budget", link_target="Plans")
        self.budget_card.setMinimumWidth(230)
        grid.addWidget(self.budget_card, 2, 9, 1, 3)
        grid.setRowStretch(3, 1)

    # ------------------------------------------------------------------ theme

    def set_dark_mode(self, is_dark: bool):
        self.is_dark_mode = bool(is_dark)
        self._theme = DARK if is_dark else LIGHT
        self._apply_theme()
        self.refresh()

    def set_navigable_targets(self, targets):
        """Only show a "See all" link whose screen is in the sidebar."""
        self._navigable = None if targets is None else set(targets)
        self._sync_links()

    def _sync_links(self):
        for button, target in self._links:
            button.setVisible(self._navigable is None or target in self._navigable)

    def _apply_theme(self):
        self.setStyleSheet(stylesheet(self._theme, "HomePage"))
        self.quick_add.set_theme(self._theme)

    # ------------------------------------------------------------------ data

    def refresh(self):
        self._apply_theme()
        data = home_data.build_home_data(
            self.ledger, self._now_fn(), self._amount_fn,
            display_name=self._display_name_fn())
        t = self._theme

        self.greeting_label.setText(data.greeting)
        self.date_label.setText(data.date_line)

        s = data.spending
        year, month = s.month.split("-")
        self.spending_period.setText(datetime(int(year), int(month), 1).strftime("%B %Y"))
        self.spending_total.setText(fmt_money(s.total))
        self.sparkline.set_data(s.cumulative, t["accent"])
        if s.top_categories:
            self.spending_top.setText("Top categories: " + ", ".join(n for n, _ in s.top_categories))
        else:
            self.spending_top.setText("No spending recorded this month.")

        self._fill_accounts(data)
        self._fill_recent(data)
        self._fill_budget(data)
        self.quick_add.refresh_choices()
        self._sync_links()

    def _fill_accounts(self, data):
        t = self._theme
        # keep the header row (index 0); rebuild the rest
        while self.accounts_layout.count() > 1:
            item = self.accounts_layout.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                _clear(item.layout())
        if not data.accounts:
            empty = QLabel("No accounts yet.")
            empty.setObjectName("Muted")
            self.accounts_layout.addWidget(empty)
            return
        for account in data.accounts:
            row = QHBoxLayout()
            name = QLabel(account.name)
            if account.owed:
                value = QLabel(f"{fmt_money(account.balance)} owed")
                value.setStyleSheet(f"color: {t['negative']}; font-weight: 600; font-size: 15px;")
            else:
                value = QLabel(fmt_money(account.balance))
                value.setStyleSheet("font-weight: 600; font-size: 15px;")
            row.addWidget(name)
            row.addStretch()
            row.addWidget(value)
            self.accounts_layout.addLayout(row)
        if data.accounts_total > len(data.accounts):
            note = QLabel(f"Showing {len(data.accounts)} of {data.accounts_total} accounts")
            note.setObjectName("Muted")
            self.accounts_layout.addWidget(note)

    def _fill_recent(self, data):
        t = self._theme
        while self.recent_layout.count() > 1:
            item = self.recent_layout.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                _clear(item.layout())
        if not data.recent:
            empty = QLabel("Nothing yet. Add your first transaction.")
            empty.setObjectName("Muted")
            self.recent_layout.addWidget(empty)
            return
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(0, 1)
        for r, row in enumerate(data.recent):
            left = QVBoxLayout()
            left.setSpacing(0)
            title = QLabel(row.description)
            title.setStyleSheet("font-weight: 600;")
            sub = QLabel(row.category)
            sub.setObjectName("Muted")
            left.addWidget(title)
            left.addWidget(sub)
            when = QLabel(row.date)
            when.setObjectName("Muted")
            amount = QLabel(fmt_money(row.amount, signed=True))
            color = t["accent"] if row.amount > 0 else (t["negative"] if row.amount < 0 else t["muted"])
            amount.setStyleSheet(f"color: {color}; font-weight: 600;")
            amount.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addLayout(left, r, 0)
            grid.addWidget(when, r, 1)
            grid.addWidget(amount, r, 2)
        self.recent_layout.addLayout(grid)
        note = QLabel(f"Latest {len(data.recent)} transactions")
        note.setObjectName("Muted")
        self.recent_layout.addWidget(note)

    def _fill_budget(self, data):
        t = self._theme
        while self.budget_layout.count() > 1:
            item = self.budget_layout.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                _clear(item.layout())
        b = data.budget
        month_name = datetime(int(b.month[:4]), int(b.month[5:7]), 1).strftime("%B")
        if b.left_to_assign < 0:
            lta = QLabel(f"Over-assigned by {fmt_money(abs(b.left_to_assign))} for {month_name}")
            lta.setStyleSheet(f"color: {t['negative']}; font-size: 12px; font-weight: 600;")
        else:
            lta = QLabel(f"Left to assign for {month_name}: {fmt_money(b.left_to_assign)}")
            lta.setObjectName("Muted")
        lta.setWordWrap(True)
        self.budget_layout.addWidget(lta)
        if not b.shown:
            empty = QLabel("No envelopes yet. Create one on the Plans screen.")
            empty.setObjectName("Muted")
            empty.setWordWrap(True)
            self.budget_layout.addWidget(empty)
            return
        for env in b.shown:
            over = env.state == STATE_OVERSPENT
            top = QHBoxLayout()
            top.addWidget(QLabel(env.category))
            top.addStretch()
            text = (f"{fmt_money(abs(env.available))} over" if over
                    else f"{fmt_money(env.available)} left")
            value = QLabel(text)
            value.setStyleSheet(f"color: {t['negative'] if over else t['text']}; font-weight: 600;")
            top.addWidget(value)
            self.budget_layout.addLayout(top)
            bar = Bar()
            bar.set_value(env.fraction, t["negative"] if over else t["accent"], t["track"])
            self.budget_layout.addWidget(bar)
        scope = f"{len(b.shown)} of {b.total_envelopes} envelopes"
        if b.total_envelopes > len(b.shown) or b.overspent_count:
            scope += " \u00b7 overspent first"
        note = QLabel(scope)
        note.setObjectName("Muted")
        self.budget_layout.addWidget(note)

    def _after_quick_add(self):
        self.refresh()
        self.data_modified.emit()
