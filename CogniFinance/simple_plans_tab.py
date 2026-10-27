"""
simple_plans_tab.py ver 0.3
For CogniFinance ver 1.9

Simple Mode's Plans screen: this month's envelopes, and the one place in
Simple Mode where the budget is changed (Home only reads it).

Every number comes from budget_summary, which calls the same budget
engine Pro uses, so Home and Plans cannot disagree. Writes go through
the dialogs in plans_dialogs, which call Pro's own ledger methods.

Layout: header (month nav) → summary row (left-to-assign | income plan)
→ main row (envelope cards | Change the plan card) → footer.

The rollover rule is not shown as a column, but it is never hidden from
the numbers: each envelope says in one sentence whether unspent money
carries over, and how much arrived from earlier months, so Assigned,
Spent and Available always add up in front of the user.
"""
import logging
from datetime import datetime
from typing import Callable, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy,
)

import budget_summary
import home_data
import plans_data
from budget_summary import STATE_OVERSPENT
from plans_dialogs import AssignDialog, MoveDialog, AddEnvelopeDialog, ChangeLinkDialog
from simple_home_tab import Bar
from simple_theme import LIGHT, DARK, fmt_money, stylesheet

logger = logging.getLogger(__name__)


class SimplePlansTab(QWidget):
    data_modified = pyqtSignal()

    def __init__(self, ledger, parent=None, now_fn: Optional[Callable] = None):
        super().__init__(parent)
        self.gl = ledger
        self._now_fn = now_fn or datetime.now
        self.month = plans_data.month_of(self._now_fn())
        self.is_dark_mode = False
        self._theme = LIGHT
        self.summary = None
        self._build()
        self.refresh()

    # ----------------------------------------------------------------- build

    def _card(self, title: str = ""):
        frame = QFrame()
        frame.setObjectName("HomeCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)
        if title:
            label = QLabel(title.upper())
            label.setObjectName("CardTitle")
            layout.addWidget(label)
        return frame, layout

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)

        page = QWidget()
        page.setObjectName("PlansPage")
        scroll.setWidget(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(16)

        # ---- Header: title + month navigation ------------------------------
        header = QHBoxLayout()
        title = QLabel("Plans")
        title.setObjectName("Greeting")
        header.addWidget(title)
        header.addStretch()

        self.prev_button = QPushButton("\u2039")
        self.next_button = QPushButton("\u203a")
        for button in (self.prev_button, self.next_button):
            button.setObjectName("SecondaryButton")
            button.setFixedWidth(36)
        self.month_label = QLabel()
        self.month_label.setStyleSheet("font-weight: 600; padding: 0 8px;")
        self.prev_button.clicked.connect(lambda: self._shift(-1))
        self.next_button.clicked.connect(lambda: self._shift(+1))
        header.addWidget(self.prev_button)
        header.addWidget(self.month_label)
        header.addWidget(self.next_button)
        layout.addLayout(header)

        # ---- Summary row: left to assign | income plan ---------------------
        top = QHBoxLayout()
        top.setSpacing(16)

        self.left_card, left_layout = self._card("Left to assign")
        self.left_amount = QLabel()
        self.left_amount.setObjectName("BigNumber")
        self.left_note = QLabel()
        self.left_note.setObjectName("Muted")
        self.left_note.setWordWrap(True)
        left_layout.addWidget(self.left_amount)
        left_layout.addWidget(self.left_note)
        top.addWidget(self.left_card, 1)

        self.income_card, income_layout = self._card("Income plan")
        self.income_label = QLabel()
        self.income_label.setWordWrap(True)
        income_layout.addWidget(self.income_label)
        income_layout.addStretch()
        top.addWidget(self.income_card, 1)

        layout.addLayout(top)

        # ---- Main row: envelopes (left) | Change the plan (right) ----------
        mid_row = QHBoxLayout()
        mid_row.setSpacing(16)

        # Left column: stacked envelope cards
        self.cards_host = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_host)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(10)
        mid_row.addWidget(self.cards_host, 1)

        # Right column: actions card, narrow and self-sized
        self.actions_card, actions_layout = self._card("Change the plan")
        actions_layout.setSpacing(8)
        actions_layout.setContentsMargins(16, 14, 16, 14)

        self.action_buttons = {}
        for text, handler in (
            ("Assign money", self._assign),
            ("Take money back", self._take_back),
            ("Move money", self._move),
            ("Add envelope", self._add_envelope),
            ("Change what an envelope tracks", self._change_link),
        ):
            button = QPushButton(text)
            button.setObjectName("SecondaryButton")
            button.clicked.connect(lambda _=False, fn=handler: fn())
            actions_layout.addWidget(button)
            self.action_buttons[text] = button
        actions_layout.addStretch()

        self.actions_card.setMinimumWidth(220)
        self.actions_card.setMaximumWidth(280)
        self.actions_card.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Preferred,
        )
        mid_row.addWidget(self.actions_card, 0, Qt.AlignmentFlag.AlignTop)

        layout.addLayout(mid_row)

        # ---- Footer --------------------------------------------------------
        self.footer_label = QLabel()
        self.footer_label.setObjectName("Muted")
        self.footer_label.setWordWrap(True)
        layout.addWidget(self.footer_label)
        layout.addStretch()

    # ----------------------------------------------------------------- theme

    def set_dark_mode(self, is_dark: bool):
        self.is_dark_mode = bool(is_dark)
        self._theme = DARK if is_dark else LIGHT
        self.refresh()

    def _apply_theme(self):
        self.setStyleSheet(stylesheet(self._theme, "PlansPage"))

    # ------------------------------------------------------------------ data

    def _shift(self, delta: int):
        self.month = plans_data.shift_month(self.month, delta)
        self.refresh()

    def refresh(self):
        self._apply_theme()
        t = self._theme
        self.month_label.setText(plans_data.month_title(self.month))

        try:
            self.summary = budget_summary.summarize(self.gl, self.month)
            plan = self.gl.get_income_plan(self.month)
        except Exception as error:                      # show the failure, never an empty plan
            logger.exception("Plans could not load the budget")
            self.summary = None
            self._clear_cards()
            self.left_amount.setText("\u2014")
            self.left_note.setText("")
            self.income_label.setText("")
            self.footer_label.setText(f"Your envelopes could not be loaded: {error}")
            self._set_actions_enabled(False)
            return

        self._set_actions_enabled(True)
        left = self.summary.left_to_assign
        if left < 0:
            self.left_amount.setText(fmt_money(-left))
            self.left_amount.setStyleSheet(f"color: {t['negative']};")
            self.left_note.setText(
                "Over-assigned: you have given envelopes more than you "
                "received this month."
            )
        else:
            self.left_amount.setStyleSheet("")
            self.left_amount.setText(fmt_money(left))
            self.left_note.setText(
                f"Income received in {plans_data.month_title(self.month)}, "
                "minus what you have assigned to envelopes."
            )

        if plan:
            self.income_label.setText(
                f"Expected this month: {fmt_money(plan['expected_amount'])}"
            )
        else:
            self.income_label.setText("No income plan set for this month.")

        self._fill_cards()

    def _set_actions_enabled(self, enabled: bool):
        for button in self.action_buttons.values():
            button.setEnabled(enabled)

    def _clear_cards(self):
        while self.cards_layout.count():
            item = self.cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _fill_cards(self):
        self._clear_cards()
        envelopes = self.summary.envelopes
        if not envelopes:
            empty = QLabel(
                "No envelopes yet. Use Add envelope to create your first one."
            )
            empty.setObjectName("Muted")
            empty.setWordWrap(True)
            self.cards_layout.addWidget(empty)
            self.footer_label.setText("")
            return
        for env in envelopes:
            self.cards_layout.addWidget(self._envelope_card(env))
        note = f"{len(envelopes)} envelopes"
        if self.summary.overspent:
            note += f" \u00b7 {len(self.summary.overspent)} overspent, shown first"
        self.footer_label.setText(note)

    def _envelope_card(self, env) -> QFrame:
        t = self._theme
        over = env.state == STATE_OVERSPENT
        card, layout = self._card()

        head = QHBoxLayout()
        name = QLabel(env.category)
        name.setStyleSheet("font-size: 15px; font-weight: 700;")
        head.addWidget(name)
        head.addStretch()
        value = QLabel(
            f"{fmt_money(abs(env.available))} over" if over
            else f"{fmt_money(env.available)} left"
        )
        value.setStyleSheet(
            f"font-size: 15px; font-weight: 700; "
            f"color: {t['negative'] if over else t['text']};"
        )
        head.addWidget(value)
        layout.addLayout(head)

        bar = Bar()
        bar.set_value(
            home_data.bar_fraction(env),
            t["negative"] if over else t["accent"],
            t["track"],
        )
        layout.addWidget(bar)

        detail = QLabel(
            f"Assigned {fmt_money(env.assigned)}  \u00b7  "
            f"Spent {fmt_money(env.spent)}"
        )
        detail.setObjectName("Muted")
        layout.addWidget(detail)

        explain = [budget_summary.policy_sentence(env.policy)]
        carried = plans_data.carried_sentence(env, fmt_money)
        if carried:
            explain.append(carried)
        if not env.tracks_spending:
            explain.append(
                "Not tracking any spending: no spending account is linked."
            )
        text = QLabel(" ".join(explain))
        text.setObjectName("Muted")
        text.setWordWrap(True)
        layout.addWidget(text)

        if over:
            row = QHBoxLayout()
            cover = QPushButton("Cover it")
            cover.setObjectName("PrimaryButton")
            cover.clicked.connect(
                lambda _=False, c=env.category: self._cover(c)
            )
            row.addWidget(cover)
            row.addStretch()
            layout.addLayout(row)

        return card

    # ---------------------------------------------------------------- actions

    def _run(self, dialog):
        dialog.setStyleSheet(stylesheet(self._theme, "PlansDialog"))
        dialog.exec()
        if dialog.changed:
            self.refresh()
            self.data_modified.emit()

    def _assign(self):
        self._run(AssignDialog(self.gl, self.summary, self.month, self))

    def _take_back(self):
        self._run(
            AssignDialog(self.gl, self.summary, self.month, self, take_back=True)
        )

    def _move(self):
        self._run(MoveDialog(self.gl, self.summary, self.month, self))

    def _cover(self, category: str):
        self._run(
            MoveDialog(self.gl, self.summary, self.month, self, target=category)
        )

    def _add_envelope(self):
        self._run(AddEnvelopeDialog(self.gl, self.summary, self.month, self))

    def _change_link(self):
        self._run(ChangeLinkDialog(self.gl, self.summary, self.month, self))
