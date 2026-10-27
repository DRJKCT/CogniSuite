# simple_reports_tab.py ver 0.2
# For CogniFinance ver 1.9
# Simple Mode Reports screen: statements + charts in one place.
# Same builders, same ChartFactory, same export functions as Pro.
# Everything is read-only here — no ledger writes.

from datetime import datetime, timedelta
from typing import Callable, Optional

from PyQt6.QtCore import Qt, QDate, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel,
    QComboBox, QDateEdit, QCheckBox, QPushButton, QFileDialog,
    QMessageBox, QScrollArea, QSizePolicy, QTextEdit, QGridLayout,
)

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

from ledger import GeneralLedger
from report_builders import (
    build_income_statement,
    export_income_statement_xlsx,
    build_balance_sheet,
    export_balance_sheet_xlsx,
    build_trial_balance,
    export_trial_balance_xlsx,
    build_equity_statement,
    export_equity_statement_xlsx,
)
from graphs import ChartFactory
import html_renderers
from simple_theme import LIGHT, DARK, stylesheet


REPORTS = [
    ("Income Statement", "income_statement"),
    ("Trial Balance", "trial_balance"),
    ("Balance Sheet", "balance_sheet"),
    ("Statement of Changes in Equity", "equity_statement"),
]

CHARTS = [
    ("Income vs Expenses Over Time", "income_expenses"),
    ("Expense Breakdown (Donut)", "expense_pie"),
    ("Net Worth Trend", "net_worth"),
    ("Top Spending Categories", "top_categories"),
    ("Year-over-Year Comparison", "yoy_comparison"),
]

# Below this width the report and the chart stack in one column instead of
# sitting side by side, so neither is squeezed too narrow to read.
TWO_COLUMN_MIN_WIDTH = 1000

TIMEFRAMES = [
    ("Current Month", "month"),
    ("Current Year", "year"),
    ("Custom Range", "custom"),
]


class SimpleReportsTab(QWidget):
    data_modified = pyqtSignal()

    def __init__(self, ledger: GeneralLedger, parent=None,
                 now_fn: Optional[Callable] = None):
        super().__init__(parent)
        self.gl = ledger
        self._now_fn = now_fn or datetime.now
        self.is_dark_mode = False
        self._theme = LIGHT
        self._build()
        self.refresh()

    # ---------------------------------------------------------------- helpers

    def set_dark_mode(self, is_dark: bool):
        self.is_dark_mode = bool(is_dark)
        self._theme = DARK if is_dark else LIGHT
        self.refresh()

    def _card(self, title: str, tooltip: str = ""):
        frame = QFrame()
        frame.setObjectName("HomeCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(6)
        label = QLabel(title.upper())
        label.setObjectName("CardTitle")
        head.addWidget(label)
        if tooltip:
            info = QLabel("\u24d8")
            info.setObjectName("CardInfo")
            info.setToolTip(tooltip)
            head.addWidget(info)
        head.addStretch()
        layout.addLayout(head)
        return frame, layout

    def _apply_theme(self):
        t = self._theme
        # The shared stylesheet does not know about the statement editor or
        # the checkbox, so without these the editor stays white and the
        # "Compare" label stays black in dark mode.
        self.setStyleSheet(stylesheet(t, "ReportsPage") + f"""
            QTextEdit {{ background: {t['card']}; color: {t['text']};
                         border: 1px solid {t['border']}; border-radius: 8px; padding: 6px; }}
            QCheckBox {{ color: {t['text']}; }}
        """)

    @staticmethod
    def _shrinkable(widget):
        """Let a widget be narrower than its sizeHint says. Combos and
        date editors otherwise refuse to shrink and force the HBox to
        wrap or scroll."""
        widget.setMinimumWidth(0)
        widget.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    # ---------------------------------------------------------------- build

    def _build(self):
        # Outer scroll so the page can grow beyond the window if needed.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        page = QWidget()
        page.setObjectName("ReportsPage")
        scroll.setWidget(page)

        # Everything below goes into `v`, a plain vertical stack.
        v = QVBoxLayout(page)
        v.setContentsMargins(24, 20, 24, 24)
        v.setSpacing(16)

        # ---- Title ----------------------------------------------------------
        title = QLabel("Reports")
        title.setObjectName("Greeting")
        v.addWidget(title)

        # ---- The four cards -------------------------------------------------
        # Report controls, Statement, Chart controls and Chart are built
        # here and placed by _arrange(): side by side (each output under
        # its own controls) when there is room, one column when not.

        # --- Report card ---
        report_card, r_lay = self._card(
            "Report",
            tooltip="Financial statements for a selected period. Read-only.")

        # Type row
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("Type"))
        self.report_type_combo = QComboBox()
        for label, key in REPORTS:
            self.report_type_combo.addItem(label, key)
        self.report_type_combo.currentIndexChanged.connect(self._on_report_type_changed)
        self._shrinkable(self.report_type_combo)
        row.addWidget(self.report_type_combo, 1)
        r_lay.addLayout(row)

        # Dates row
        row = QHBoxLayout()
        row.setSpacing(8)
        self.report_start_label = QLabel("From")
        row.addWidget(self.report_start_label)
        self.report_start_edit = QDateEdit()
        self.report_start_edit.setCalendarPopup(True)
        self.report_start_edit.setDate(QDate(QDate.currentDate().year(), 1, 1))
        self.report_start_edit.dateChanged.connect(self._refresh_statement)
        self._shrinkable(self.report_start_edit)
        row.addWidget(self.report_start_edit, 1)

        self.report_end_label = QLabel("To")
        row.addWidget(self.report_end_label)
        self.report_end_edit = QDateEdit()
        self.report_end_edit.setCalendarPopup(True)
        self.report_end_edit.setDate(QDate.currentDate())
        self.report_end_edit.dateChanged.connect(self._refresh_statement)
        self._shrinkable(self.report_end_edit)
        row.addWidget(self.report_end_edit, 1)
        r_lay.addLayout(row)

        # Comparison row
        row = QHBoxLayout()
        row.setSpacing(8)
        self.report_compare_checkbox = QCheckBox("Compare")
        self.report_compare_checkbox.toggled.connect(self._refresh_statement)
        row.addWidget(self.report_compare_checkbox)
        self.report_compare_mode_combo = QComboBox()
        self.report_compare_mode_combo.currentIndexChanged.connect(self._refresh_statement)
        self._shrinkable(self.report_compare_mode_combo)
        row.addWidget(self.report_compare_mode_combo, 1)
        r_lay.addLayout(row)

        # Export row
        self.export_button = QPushButton("Export statement (XLSX)")
        self.export_button.setObjectName("SecondaryButton")
        self.export_button.clicked.connect(self.export_report)
        r_lay.addWidget(self.export_button)
        r_lay.addStretch()

        self._report_card = report_card

        # --- Chart card ---
        chart_controls, c_lay = self._card(
            "Chart",
            tooltip="Charts are drawn from the same transactions the rest "
                    "of the app uses. Nothing is written.")

        # Chart type row
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("Chart"))
        self.chart_type_combo = QComboBox()
        for label, key in CHARTS:
            self.chart_type_combo.addItem(label, key)
        self.chart_type_combo.currentIndexChanged.connect(self._on_chart_type_changed)
        self.chart_type_combo.currentIndexChanged.connect(self.refresh_chart)
        self._shrinkable(self.chart_type_combo)
        row.addWidget(self.chart_type_combo, 1)
        c_lay.addLayout(row)

        # Timeframe row
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("Timeframe"))
        self.timeframe_combo = QComboBox()
        for label, key in TIMEFRAMES:
            self.timeframe_combo.addItem(label, key)
        self.timeframe_combo.currentIndexChanged.connect(self._on_timeframe_changed)
        self.timeframe_combo.currentIndexChanged.connect(self.refresh_chart)
        self._shrinkable(self.timeframe_combo)
        row.addWidget(self.timeframe_combo, 1)
        c_lay.addLayout(row)

        # Date pickers row (visibility driven by _on_timeframe_changed)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.month_edit = QDateEdit()
        self.month_edit.setCalendarPopup(True)
        self.month_edit.setDisplayFormat("yyyy-MM")
        self.month_edit.setDate(QDate.currentDate())
        self.month_edit.dateChanged.connect(self.refresh_chart)
        self._shrinkable(self.month_edit)
        row.addWidget(self.month_edit, 1)

        self.year_edit = QDateEdit()
        self.year_edit.setCalendarPopup(True)
        self.year_edit.setDisplayFormat("yyyy")
        self.year_edit.setDate(QDate.currentDate())
        self.year_edit.dateChanged.connect(self.refresh_chart)
        self._shrinkable(self.year_edit)
        row.addWidget(self.year_edit, 1)

        self.custom_start_edit = QDateEdit()
        self.custom_start_edit.setCalendarPopup(True)
        self.custom_start_edit.setDate(QDate.currentDate().addMonths(-1))
        self.custom_start_edit.dateChanged.connect(self.refresh_chart)
        self._shrinkable(self.custom_start_edit)
        row.addWidget(self.custom_start_edit, 1)

        self.custom_to_label = QLabel("to")
        row.addWidget(self.custom_to_label)

        self.custom_end_edit = QDateEdit()
        self.custom_end_edit.setCalendarPopup(True)
        self.custom_end_edit.setDate(QDate.currentDate())
        self.custom_end_edit.dateChanged.connect(self.refresh_chart)
        self._shrinkable(self.custom_end_edit)
        row.addWidget(self.custom_end_edit, 1)
        c_lay.addLayout(row)

        # Category row (YoY only)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.category_label = QLabel("Category")
        row.addWidget(self.category_label)
        self.category_combo = QComboBox()
        self.category_combo.addItem("All Expenses", None)
        for name, acc in sorted(self.gl.accounts.items()):
            if acc.account_type == "Expense":
                self.category_combo.addItem(name, name)
        self.category_combo.currentIndexChanged.connect(self.refresh_chart)
        self._shrinkable(self.category_combo)
        row.addWidget(self.category_combo, 1)
        c_lay.addLayout(row)
        c_lay.addStretch()

        self._chart_controls_card = chart_controls

        # ---- Statement (output of the Report card) -------------------------
        statement_card, s_lay = self._card(
            "Statement",
            tooltip="Rendered on the same builders the Pro Reports tab uses.")
        self.report_display = QTextEdit()
        self.report_display.setReadOnly(True)
        self.report_display.setFont(QFont("Monospace", 10))
        self.report_display.setMinimumHeight(320)
        # Ignore the editor's own preferred width so both columns share space evenly.
        self.report_display.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        s_lay.addWidget(self.report_display, 1)
        self._statement_card = statement_card

        # ---- Chart canvas (output of the Chart card) ------------------------
        chart_card, c_lay = self._card("Chart")
        # layout="tight" re-fits the labels on every draw, so they are not
        # clipped when the card is resized (it is half as wide now).
        self.figure = Figure(figsize=(10, 5), dpi=100, layout="tight")
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.canvas.setMinimumHeight(360)
        c_lay.addWidget(self.canvas, 1)
        self._chart_card = chart_card

        # ---- Place the cards ------------------------------------------------
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(16)
        self._grid.setVerticalSpacing(16)
        v.addLayout(self._grid, 1)
        self._columns = 0
        self._arrange(2)

        # Initial state
        self._on_report_type_changed()
        self._on_timeframe_changed()
        self._on_chart_type_changed()

    # --------------------------------------------------------------- layout

    def _arrange(self, columns: int):
        """Two columns: controls on top, each output right under its own
        controls (Report | Chart, then Statement | Chart canvas). One
        column: Report, Statement, Chart controls, Chart, top to bottom."""
        if columns == self._columns:
            return
        self._columns = columns
        cards = (self._report_card, self._chart_controls_card,
                 self._statement_card, self._chart_card)
        for card in cards:
            self._grid.removeWidget(card)
        if columns == 2:
            self._grid.addWidget(self._report_card, 0, 0)
            self._grid.addWidget(self._chart_controls_card, 0, 1)
            self._grid.addWidget(self._statement_card, 1, 0)
            self._grid.addWidget(self._chart_card, 1, 1)
            self._set_stretch(columns=(1, 1), rows=(0, 1))
        else:
            self._grid.addWidget(self._report_card, 0, 0)
            self._grid.addWidget(self._statement_card, 1, 0)
            self._grid.addWidget(self._chart_controls_card, 2, 0)
            self._grid.addWidget(self._chart_card, 3, 0)
            self._set_stretch(columns=(1, 0), rows=(0, 1, 0, 1))

    def _set_stretch(self, columns, rows):
        for index in range(2):
            self._grid.setColumnStretch(index, columns[index] if index < len(columns) else 0)
        for index in range(4):
            self._grid.setRowStretch(index, rows[index] if index < len(rows) else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_grid"):
            self._arrange(2 if self.width() >= TWO_COLUMN_MIN_WIDTH else 1)

    # ---------------------------------------------------------------- report

    def _on_report_type_changed(self):
        rtype = self.report_type_combo.currentData()

        self.report_compare_mode_combo.blockSignals(True)
        self.report_compare_mode_combo.clear()
        if rtype == "balance_sheet":
            self.report_compare_mode_combo.addItem(
                "Prior year, same date", "prior_year")
            self.report_compare_mode_combo.addItem(
                "Prior year-end (Dec 31)", "prior_year_end")
        elif rtype in ("trial_balance", "equity_statement"):
            pass
        else:
            self.report_compare_mode_combo.addItem(
                "Prior year, same dates", "prior_year")
            self.report_compare_mode_combo.addItem(
                "Prior period, same length", "prior_period")
        self.report_compare_mode_combo.setCurrentIndex(0)
        self.report_compare_mode_combo.blockSignals(False)

        point_in_time = rtype in ("balance_sheet", "trial_balance")
        self.report_start_label.setVisible(not point_in_time)
        self.report_start_edit.setVisible(not point_in_time)
        self.report_end_label.setText("As of" if point_in_time else "To")

        show_cmp = rtype in ("income_statement", "balance_sheet")
        self.report_compare_checkbox.setVisible(show_cmp)
        self.report_compare_mode_combo.setVisible(
            show_cmp and self.report_compare_checkbox.isChecked())
        if not show_cmp and self.report_compare_checkbox.isChecked():
            self.report_compare_checkbox.blockSignals(True)
            self.report_compare_checkbox.setChecked(False)
            self.report_compare_checkbox.blockSignals(False)

        self._refresh_statement()

    def _refresh_statement(self):
        rtype = self.report_type_combo.currentData()
        include_cmp = self.report_compare_checkbox.isChecked()
        comparison_mode = self.report_compare_mode_combo.currentData() or "prior_year"

        self.report_compare_mode_combo.setVisible(
            rtype in ("income_statement", "balance_sheet") and include_cmp)

        try:
            if rtype == "balance_sheet":
                as_of = self.report_end_edit.date().toString("yyyy-MM-dd")
                built = build_balance_sheet(
                    self.gl, as_of,
                    include_comparison=include_cmp,
                    comparison_mode=comparison_mode,
                )
                compare_label = built.get("compare_end") if include_cmp else None
                html = html_renderers.render_balance_sheet_statement_html(
                    built, as_of, compare_label)
            elif rtype == "trial_balance":
                as_of = self.report_end_edit.date().toString("yyyy-MM-dd")
                built = build_trial_balance(self.gl, as_of)
                html = html_renderers.render_trial_balance_html(built)
            elif rtype == "equity_statement":
                start = self.report_start_edit.date().toString("yyyy-MM-dd")
                end = self.report_end_edit.date().toString("yyyy-MM-dd")
                built = build_equity_statement(self.gl, start, end)
                html = html_renderers.render_equity_statement_html(built)
            else:
                start = self.report_start_edit.date().toString("yyyy-MM-dd")
                end = self.report_end_edit.date().toString("yyyy-MM-dd")
                built = build_income_statement(
                    self.gl, start, end,
                    include_comparison=include_cmp,
                    comparison_mode=comparison_mode,
                )
                period_label = f"{start} \u2013 {end}"
                compare_label = None
                if include_cmp:
                    compare_label = (
                        f"{built['compare_start']} \u2013 {built['compare_end']}"
                    )
                html = html_renderers.render_income_statement_html(
                    built["sections"], period_label, compare_label)
        except Exception as exc:
            self.report_display.setHtml(
                f"<p style='color:red'>Could not build statement: {exc}</p>"
            )
            return

        if self.is_dark_mode:
            html = self._apply_dark_css(html)
        self.report_display.setHtml(html)

    def export_report(self):
        rtype = self.report_type_combo.currentData()
        include_cmp = self.report_compare_checkbox.isChecked()
        comparison_mode = self.report_compare_mode_combo.currentData() or "prior_year"

        try:
            if rtype == "trial_balance":
                as_of = self.report_end_edit.date().toString("yyyy-MM-dd")
                built = build_trial_balance(self.gl, as_of)
                default_name = f"trial_balance_{as_of}.xlsx"
                exporter = lambda p: export_trial_balance_xlsx(built, p)
            elif rtype == "equity_statement":
                start = self.report_start_edit.date().toString("yyyy-MM-dd")
                end = self.report_end_edit.date().toString("yyyy-MM-dd")
                built = build_equity_statement(self.gl, start, end)
                default_name = f"equity_statement_{start}_to_{end}.xlsx"
                exporter = lambda p: export_equity_statement_xlsx(built, p)
            elif rtype == "balance_sheet":
                as_of = self.report_end_edit.date().toString("yyyy-MM-dd")
                built = build_balance_sheet(
                    self.gl, as_of,
                    include_comparison=include_cmp,
                    comparison_mode=comparison_mode,
                )
                compare_label = built.get("compare_end") if include_cmp else None
                default_name = f"balance_sheet_{as_of}.xlsx"
                exporter = lambda p: export_balance_sheet_xlsx(
                    built, p,
                    period_label=as_of, compare_label=compare_label,
                )
            else:
                start = self.report_start_edit.date().toString("yyyy-MM-dd")
                end = self.report_end_edit.date().toString("yyyy-MM-dd")
                built = build_income_statement(
                    self.gl, start, end,
                    include_comparison=include_cmp,
                    comparison_mode=comparison_mode,
                )
                period_label = f"{start} \u2013 {end}"
                compare_label = None
                if include_cmp:
                    compare_label = (
                        f"{built['compare_start']} \u2013 {built['compare_end']}"
                    )
                default_name = f"income_statement_{start}_to_{end}.xlsx"
                exporter = lambda p: export_income_statement_xlsx(
                    built, p,
                    period_label=period_label, compare_label=compare_label,
                )
        except Exception as exc:
            QMessageBox.critical(self, "Report build failed", str(exc))
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Report", default_name,
            "Excel Files (*.xlsx);;All Files (*)"
        )
        if not path:
            return
        if not path.endswith(".xlsx"):
            path += ".xlsx"
        try:
            exporter(path)
            QMessageBox.information(self, "Export Complete",
                                    f"Exported to:\n{path}")
        except ImportError:
            QMessageBox.critical(
                self, "Missing Dependency",
                "The 'openpyxl' library is required to export XLSX files.")
        except Exception as exc:
            QMessageBox.critical(self, "Export Failed", str(exc))

    @staticmethod
    def _apply_dark_css(html: str) -> str:
        # Same colors as the card the statement sits in.
        bg, fg, accent = DARK["card"], DARK["text"], DARK["accent"]
        dark_css = f"""
        <style>
            body, html {{ background-color: {bg} !important; color: {fg} !important; }}
            table, td, th, p, h1, h2, h3, h4, span, div, li {{ color: {fg} !important; background-color: {bg} !important; }}
            .header, .title, h1, h2 {{ color: {accent} !important; }}
        </style>
        """
        return dark_css + html

    # ---------------------------------------------------------------- chart

    def _on_chart_type_changed(self):
        is_yoy = self.chart_type_combo.currentData() == "yoy_comparison"
        self.category_label.setVisible(is_yoy)
        self.category_combo.setVisible(is_yoy)

    def _on_timeframe_changed(self):
        tf = self.timeframe_combo.currentData()
        self.month_edit.setVisible(tf == "month")
        self.year_edit.setVisible(tf == "year")
        self.custom_start_edit.setVisible(tf == "custom")
        self.custom_end_edit.setVisible(tf == "custom")
        self.custom_to_label.setVisible(tf == "custom")

    def _chart_theme(self) -> dict:
        """The chart is drawn on the card's own colors so it reads as part
        of the card, not a box inside it."""
        t = self._theme
        return {
            'fig_bg': t['card'], 'ax_bg': t['card'], 'text': t['text'],
            'grid': t['border'], 'tick': t['muted'],
            'legend_bg': t['card'], 'legend_text': t['text'],
        }

    def _chart_dates(self):
        tf = self.timeframe_combo.currentData()
        if tf == "month":
            qd = self.month_edit.date()
            start = datetime(qd.year(), qd.month(), 1)
            if qd.month() == 12:
                end = datetime(qd.year() + 1, 1, 1) - timedelta(days=1)
            else:
                end = datetime(qd.year(), qd.month() + 1, 1) - timedelta(days=1)
            return start, end.replace(hour=23, minute=59, second=59)
        if tf == "year":
            qd = self.year_edit.date()
            return (datetime(qd.year(), 1, 1),
                    datetime(qd.year(), 12, 31, 23, 59, 59))
        qs = self.custom_start_edit.date()
        qe = self.custom_end_edit.date()
        return (datetime(qs.year(), qs.month(), qs.day()),
                datetime(qe.year(), qe.month(), qe.day(), 23, 59, 59))

    def refresh_chart(self):
        theme = self._chart_theme()
        self.figure.clear()
        self.figure.set_facecolor(theme['fig_bg'])
        ax = self.figure.add_subplot(111)
        ax.set_facecolor(theme['ax_bg'])
        ax.title.set_color(theme['text'])
        ax.xaxis.label.set_color(theme['text'])
        ax.yaxis.label.set_color(theme['text'])
        ax.tick_params(colors=theme['tick'], labelcolor=theme['tick'])
        for spine in ax.spines.values():
            spine.set_edgecolor(theme['grid'])

        chart_type = self.chart_type_combo.currentData()
        start_date, end_date = self._chart_dates()
        category = self.category_combo.currentData()

        try:
            if chart_type == "income_expenses":
                ChartFactory.income_vs_expenses_over_time(
                    self.gl, ax, start_date=start_date, end_date=end_date, theme=theme)
            elif chart_type == "expense_pie":
                ChartFactory.expense_breakdown_by_category(
                    self.gl, ax, start_date=start_date, end_date=end_date, theme=theme)
            elif chart_type == "net_worth":
                ChartFactory.net_worth_trend(
                    self.gl, ax, start_date=start_date, end_date=end_date, theme=theme)
            elif chart_type == "top_categories":
                ChartFactory.top_spending_categories(
                    self.gl, ax, start_date=start_date, end_date=end_date,
                    top_n=10, theme=theme)
            elif chart_type == "yoy_comparison":
                ChartFactory.year_over_year_comparison(
                    self.gl, ax, category=category, theme=theme)
            self.canvas.draw()
        except Exception as exc:
            ax.clear()
            ax.text(0.5, 0.5, f"Error generating chart:\n{exc}",
                    ha="center", va="center", transform=ax.transAxes, color="red")
            self.canvas.draw()

    # ---------------------------------------------------------------- refresh

    def refresh(self):
        self._apply_theme()
        self._refresh_statement()
        self.refresh_chart()
