"""
graphs.py ver 2.0
Matplotlib chart generators for CogniFinance.
Updated to support custom date ranges, yearly views, and category-specific YoY comparisons.
"""
from typing import Optional, Dict, List, Any
from decimal import Decimal
from datetime import datetime, timedelta
from matplotlib.axes import Axes
import matplotlib.pyplot as plt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QComboBox, QDateEdit, QLabel, QHBoxLayout, QPushButton, QSizePolicy
from PyQt6.QtCore import QDate
from ui_helpers import AdvancedDisclosure
from ledger import GeneralLedger, ZERO

class ChartFactory:
    """Factory class for creating various financial charts directly onto an Axes."""

    @staticmethod
    def income_vs_expenses_over_time(ledger: GeneralLedger, ax: Axes, start_date=None, end_date=None, theme=None):
        """Chart 1: Income vs Expenses Over Time (Line Chart)"""
        if not start_date or not end_date:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=365)

        delta_days = (end_date - start_date).days
        group_by_day = delta_days <= 31
        months_data = {}

        for tx in ledger.transactions:
            try:
                tx_date = datetime.strptime(tx.date[:10], "%Y-%m-%d")
            except ValueError:
                continue
            if tx_date < start_date or tx_date > end_date:
                continue

            group_key = tx.date[:10] if group_by_day else tx.date[:7]
            if group_key not in months_data:
                months_data[group_key] = {"income": ZERO, "expenses": ZERO}

            for line in tx.lines:
                acc = ledger.accounts.get(line.account_name)
                if not acc: continue
                if acc.account_type == "Revenue" and line.cr > ZERO:
                    months_data[group_key]["income"] += line.cr
                elif acc.account_type == "Expense" and line.dr > ZERO:
                    months_data[group_key]["expenses"] += line.dr

        sorted_keys = sorted(months_data.keys())
        if not sorted_keys:
            ax.text(0.5, 0.5, 'No transaction data for the selected period.', ha='center', va='center', transform=ax.transAxes)
            return

        income_vals = [float(months_data[m]["income"]) for m in sorted_keys]
        expense_vals = [float(months_data[m]["expenses"]) for m in sorted_keys]
        x = range(len(sorted_keys))

        ax.plot(x, income_vals, marker='o', label='Income', linewidth=2, color='#10B981') # Updated to Emerald
        ax.plot(x, expense_vals, marker='s', label='Expenses', linewidth=2, color='#F87171') # Updated to Soft Red
        ax.fill_between(x, income_vals, expense_vals, alpha=0.2, where=[i>=e for i,e in zip(income_vals, expense_vals)], color='#10B981')
        ax.fill_between(x, income_vals, expense_vals, alpha=0.2, where=[i<e for i,e in zip(income_vals, expense_vals)], color='#F87171')

        ax.set_xticks(list(x))
        if group_by_day:
            labels = [k[5:] for k in sorted_keys]
        else:
            labels = sorted_keys
        ax.set_xticklabels(labels, rotation=45, ha='right')
        ax.set_xlabel('Day' if group_by_day else 'Month')
        ax.set_ylabel('Amount ($)')
        ax.set_title(f'Income vs Expenses ({start_date.strftime("%Y-%m-%d")} to {end_date.strftime("%Y-%m-%d")})')

        # Themed Legend and Grid
        leg = ax.legend()
        if theme and leg:
            leg.get_frame().set_facecolor(theme['legend_bg'])
            leg.get_frame().set_edgecolor(theme['grid'])
            for text in leg.get_texts():
                text.set_color(theme['legend_text'])
        if theme:
            ax.grid(True, alpha=0.3, color=theme['grid'])
        else:
            ax.grid(True, alpha=0.3)

    @staticmethod
    def expense_breakdown_by_category(ledger: GeneralLedger, ax: Axes, start_date=None, end_date=None, theme=None):
        """Chart 3: Expense Breakdown by Category (Donut Chart)"""
        if not start_date or not end_date:
            end_date = datetime.now()
            start_date = end_date.replace(day=1)

        spending = {}
        for tx in ledger.transactions:
            try:
                tx_date = datetime.strptime(tx.date[:10], "%Y-%m-%d")
            except ValueError:
                continue
            if tx_date < start_date or tx_date > end_date:
                continue
            for line in tx.lines:
                acc = ledger.accounts.get(line.account_name)
                if acc and acc.account_type == "Expense" and line.dr > ZERO:
                    spending[line.account_name] = spending.get(line.account_name, ZERO) + line.dr

        if not spending:
            ax.text(0.5, 0.5, 'No expense data for this period.', ha='center', va='center', transform=ax.transAxes)
            return

        categories = list(spending.keys())
        amounts = [float(spending[c]) for c in categories]

        # FIX: Use a proper color palette that works with any number of categories
        num_colors = len(categories)
        if num_colors <= 12:
            # Use Set3 colormap properly with normalized values
            colors = [plt.cm.Set3(i/num_colors) for i in range(num_colors)]
        else:
            # For many categories, use a different approach
             colors = [plt.cm.tab20(i / num_colors) for i in range(num_colors)]

        wedges, texts, autotexts = ax.pie(amounts, labels=categories, autopct='%1.1f%%',
                                        colors=colors, startangle=90, pctdistance=0.85)

        # Themed center circle and text
        centre_circle = plt.Circle((0,0), 0.70, fc=theme['ax_bg'] if theme else 'white')
        ax.add_artist(centre_circle)

        text_color = theme['text'] if theme else "black"
        plt.setp(autotexts, size=9, weight="bold", color="white")
        plt.setp(texts, size=8, color=text_color)

        title = f'Expense Breakdown ({start_date.strftime("%Y-%m-%d")} to {end_date.strftime("%Y-%m-%d")})'
        ax.set_title(title)

    @staticmethod
    def net_worth_trend(ledger: GeneralLedger, ax: Axes, start_date=None, end_date=None, theme=None):
        """Chart 4: Net Worth Trend (Line Chart)"""
        if not start_date or not end_date:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=365)

        delta_days = (end_date - start_date).days
        group_by_day = delta_days <= 31
        monthly_net_worth = {}

        for tx in ledger.transactions:
            try:
                tx_date = datetime.strptime(tx.date[:10], "%Y-%m-%d")
            except ValueError:
                continue
            if tx_date < start_date or tx_date > end_date:
                continue
            group_key = tx.date[:10] if group_by_day else tx.date[:7]
            if group_key not in monthly_net_worth:
                monthly_net_worth[group_key] = {"assets": ZERO, "liabilities": ZERO}
            for line in tx.lines:
                acc = ledger.accounts.get(line.account_name)
                if not acc: continue
                if acc.account_type == "Asset":
                    monthly_net_worth[group_key]["assets"] += (line.dr - line.cr)
                elif acc.account_type == "Liability":
                    monthly_net_worth[group_key]["liabilities"] += (line.cr - line.dr)

        sorted_keys = sorted(monthly_net_worth.keys())
        if not sorted_keys:
            ax.text(0.5, 0.5, 'No balance data for the selected period.', ha='center', va='center', transform=ax.transAxes)
            return

        net_worth_vals = []
        cum_assets, cum_liab = ZERO, ZERO
        for key in sorted_keys:
            cum_assets += monthly_net_worth[key]["assets"]
            cum_liab += monthly_net_worth[key]["liabilities"]
            net_worth_vals.append(float(cum_assets - cum_liab))

        x = range(len(sorted_keys))
        ax.plot(x, net_worth_vals, marker='o', linewidth=2, color='#3B82F6', markersize=6) # Updated to modern blue

        # Themed zero-line and grid
        ax.axhline(y=0, color=theme['grid'] if theme else 'gray', linestyle='--', alpha=0.5)
        ax.set_xticks(list(x))
        if group_by_day:
            labels = [k[5:] for k in sorted_keys]
        else:
            labels = sorted_keys
        ax.set_xticklabels(labels, rotation=45, ha='right')
        ax.set_xlabel('Day' if group_by_day else 'Month')
        ax.set_ylabel('Net Worth ($)')
        ax.set_title(f'Net Worth Trend ({start_date.strftime("%Y-%m-%d")} to {end_date.strftime("%Y-%m-%d")})')
        if theme:
            ax.grid(True, alpha=0.3, color=theme['grid'])
        else:
            ax.grid(True, alpha=0.3)

    @staticmethod
    def top_spending_categories(ledger: GeneralLedger, ax: Axes, start_date=None, end_date=None, top_n: int = 10, theme=None):
        """Chart 6: Top 10 Spending Categories (Horizontal Bar Chart)"""
        if not start_date or not end_date:
            end_date = datetime.now()
            start_date = end_date.replace(day=1)

        spending = {}
        for tx in ledger.transactions:
            try:
                tx_date = datetime.strptime(tx.date[:10], "%Y-%m-%d")
            except ValueError:
                continue
            if tx_date < start_date or tx_date > end_date:
                continue
            for line in tx.lines:
                acc = ledger.accounts.get(line.account_name)
                if acc and acc.account_type == "Expense" and line.dr > ZERO:
                    spending[line.account_name] = spending.get(line.account_name, ZERO) + line.dr

        if not spending:
            ax.text(0.5, 0.5, 'No expense data for this period.', ha='center', va='center', transform=ax.transAxes)
            return

        sorted_categories = sorted(spending.items(), key=lambda x: x[1], reverse=True)[:top_n]
        categories = [c[0] for c in sorted_categories][::-1]
        amounts = [float(c[1]) for c in sorted_categories][::-1]
        y = range(len(categories))

        bars = ax.barh(y, amounts, color='#10B981', alpha=0.8) # Updated to Emerald
        for bar, amount in zip(bars, amounts):
            ax.text(amount, bar.get_y() + bar.get_height()/2, f' ${amount:,.0f}',
                   va='center', ha='left', fontsize=9)

        ax.set_yticks(list(y))
        ax.set_yticklabels(categories, fontsize=9)
        ax.set_xlabel('Amount ($)')
        title = f'Top {top_n} Spending Categories ({start_date.strftime("%Y-%m-%d")} to {end_date.strftime("%Y-%m-%d")})'
        ax.set_title(title)
        if theme:
            ax.grid(True, alpha=0.3, axis='x', color=theme['grid'])
        else:
            ax.grid(True, alpha=0.3, axis='x')

    @staticmethod
    def year_over_year_comparison(ledger: GeneralLedger, ax: Axes, category: Optional[str] = None, theme=None):
        """Chart 8: Year-over-Year Comparison (Grouped Bar Chart)"""
        current_year = datetime.now().year
        years = [current_year - 1, current_year]
        year_month_data = {year: {m: ZERO for m in range(1, 13)} for year in years}

        for tx in ledger.transactions:
            try:
                tx_date = datetime.strptime(tx.date[:10], "%Y-%m-%d")
            except ValueError:
                continue
            year = tx_date.year
            if year not in years: continue
            month = tx_date.month
            for line in tx.lines:
                acc = ledger.accounts.get(line.account_name)
                if not acc: continue
                if category and line.account_name != category: continue
                if acc.account_type == "Expense" and line.dr > ZERO:
                    year_month_data[year][month] += line.dr

        months = list(range(1, 13))
        month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        x = range(len(months))
        width = 0.35
        year1_vals = [float(year_month_data[years[0]][m]) for m in months]
        year2_vals = [float(year_month_data[years[1]][m]) for m in months]

        ax.bar([i - width/2 for i in x], year1_vals, width, label=str(years[0]), alpha=0.8, color='#6B7280') # Updated to modern gray
        ax.bar([i + width/2 for i in x], year2_vals, width, label=str(years[1]), alpha=0.8, color='#10B981') # Updated to Emerald

        ax.set_xticks(list(x))
        ax.set_xticklabels(month_names)
        ax.set_xlabel('Month')
        ax.set_ylabel('Expenses ($)')
        title = f'Year-over-Year Comparison'
        if category:
            title += f' - {category}'
        else:
            title += ' (All Expenses)'
        ax.set_title(title)

        # Themed Legend and Grid
        leg = ax.legend()
        if theme and leg:
            leg.get_frame().set_facecolor(theme['legend_bg'])
            leg.get_frame().set_edgecolor(theme['grid'])
            for text in leg.get_texts():
                text.set_color(theme['legend_text'])
        if theme:
            ax.grid(True, alpha=0.3, axis='y', color=theme['grid'])
        else:
            ax.grid(True, alpha=0.3, axis='y')


class ChartsTab(QWidget):
    """Enhanced Charts Tab with flexible timeframes and category filtering."""

    def __init__(self, ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.ledger = ledger
        self.is_dark_mode = False
        self.is_simple_mode = False
        self._init_ui()
        self.refresh_charts()

    def set_dark_mode(self, is_dark: bool):
        self.is_dark_mode = is_dark
        self.refresh_charts()

    def set_simple_mode(self, simple: bool):
        """Simple Mode: keep chart type and timeframe visible; the
        specialist category filter hides behind a reveal.

        The period stays user-chosen (a decision, not an expert
        control). "Custom Range" stays in the dropdown; only the
        category filter collapses, and the chart title continues to
        name the selected category, so hiding the control does not
        hide the fact.
        """
        simple = bool(simple)
        if simple == self.is_simple_mode:
            return
        self.is_simple_mode = simple
        # In Simple Mode the category filter is hidden unconditionally;
        # Pro Mode lets _update_controls_visibility manage it.
        if simple:
            self.category_combo.setVisible(False)
            self.category_label.setVisible(False)
        else:
            # Hand back to the tab's own visibility rules.
            self._update_controls_visibility()
        self.refresh_charts()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(5)  # ✅ Reduce spacing between rows

        # --- ROW 1: Chart Type & Category Filter ---
        row1 = QHBoxLayout()
        row1.setSpacing(10)  # ✅ Reduce spacing within row
        row1.addWidget(QLabel("Chart Type:"))
        self.chart_type_combo = QComboBox()
        self.chart_type_combo.addItem("Income vs Expenses Over Time", "income_expenses")
        self.chart_type_combo.addItem("Expense Breakdown (Donut)", "expense_pie")
        self.chart_type_combo.addItem("Net Worth Trend", "net_worth")
        self.chart_type_combo.addItem("Top Spending Categories", "top_categories")
        self.chart_type_combo.addItem("Year-over-Year Comparison", "yoy_comparison")
        self.chart_type_combo.currentIndexChanged.connect(self._update_controls_visibility)
        self.chart_type_combo.currentIndexChanged.connect(self.refresh_charts)
        self.chart_type_combo.adjustSize()
        self.chart_type_combo.setMinimumWidth(250)
        self.chart_type_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)  # ✅ Prevent expansion
        row1.addWidget(self.chart_type_combo)

        self.category_label = QLabel("Category (YoY):")
        row1.addWidget(self.category_label)
        self.category_combo = QComboBox()
        self.category_combo.addItem("All Expenses", None)
        for name, acc in sorted(self.ledger.accounts.items()):
            if acc.account_type == "Expense":
                self.category_combo.addItem(name, name)
        self.category_combo.adjustSize()
        self.category_combo.setMinimumWidth(200)
        self.category_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)  # ✅ Prevent expansion
        self.category_combo.currentIndexChanged.connect(self.refresh_charts)
        row1.addWidget(self.category_combo)

        row1.addStretch()
        layout.addLayout(row1)

        # --- ROW 2: Timeframe & Date Pickers ---
        row2 = QHBoxLayout()
        row2.setSpacing(10)  # ✅ Reduce spacing within row
        row2.addWidget(QLabel("Timeframe:"))
        self.timeframe_combo = QComboBox()
        self.timeframe_combo.addItems(["Current Month", "Current Year", "Custom Range"])
        self.timeframe_combo.currentIndexChanged.connect(self._update_controls_visibility)
        self.timeframe_combo.currentIndexChanged.connect(self.refresh_charts)
        self.timeframe_combo.adjustSize()
        self.timeframe_combo.setMinimumWidth(150)
        self.timeframe_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)  # ✅ Prevent expansion
        row2.addWidget(self.timeframe_combo)

        # Month Picker
        self.month_edit = QDateEdit()
        self.month_edit.setCalendarPopup(True)
        self.month_edit.setDisplayFormat("yyyy-MM")
        self.month_edit.setDate(QDate.currentDate())
        self.month_edit.dateChanged.connect(self.refresh_charts)
        self.month_edit.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)  # ✅ Prevent expansion
        row2.addWidget(self.month_edit)

        # Year Picker
        self.year_edit = QDateEdit()
        self.year_edit.setCalendarPopup(True)
        self.year_edit.setDisplayFormat("yyyy")
        self.year_edit.setDate(QDate.currentDate())
        self.year_edit.dateChanged.connect(self.refresh_charts)
        self.year_edit.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)  # ✅ Prevent expansion
        row2.addWidget(self.year_edit)

        # Custom Range Pickers
        self.start_edit = QDateEdit()
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDate(QDate.currentDate().addMonths(-1))
        self.start_edit.dateChanged.connect(self.refresh_charts)
        self.start_edit.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)  # ✅ Prevent expansion
        row2.addWidget(self.start_edit)

        row2.addWidget(QLabel("to"))

        self.end_edit = QDateEdit()
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDate(QDate.currentDate())
        self.end_edit.dateChanged.connect(self.refresh_charts)
        self.end_edit.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)  # ✅ Prevent expansion
        row2.addWidget(self.end_edit)

        row2.addStretch()
        layout.addLayout(row2)

        # --- Canvas ---
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
        from matplotlib.figure import Figure
        self.figure = Figure(figsize=(10, 6), dpi=100)
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)  # ✅ Canvas should expand
        layout.addWidget(self.canvas, 1)  # ✅ Add stretch factor of 1

        # Initialize visibility
        self._update_controls_visibility()

    def _update_controls_visibility(self):
        """Shows/hides date pickers and category filter based on selections."""
        timeframe = self.timeframe_combo.currentText()
        self.month_edit.setVisible(timeframe == "Current Month")
        self.year_edit.setVisible(timeframe == "Current Year")
        self.start_edit.setVisible(timeframe == "Custom Range")
        self.end_edit.setVisible(timeframe == "Custom Range")

        # Category filter only applies to YoY comparison.
        # In Simple Mode the disclosure owns the filter; don't override.
        chart_type = self.chart_type_combo.currentData()
        is_yoy = (chart_type == "yoy_comparison")
        in_simple = getattr(self, 'is_simple_mode', False)
        self.category_combo.setVisible(is_yoy and not in_simple)
        self.category_label.setVisible(is_yoy and not in_simple)

    def refresh_charts(self):
        """Refresh the current chart by calculating the exact start/end dates."""

        if getattr(self, 'is_dark_mode', False):
            theme = {
                'fig_bg': '#111318', 'ax_bg': '#1A1D24', 'text': '#F3F4F6',
                'grid': '#2D323C', 'tick': '#9CA3AF', 'legend_bg': '#1A1D24', 'legend_text': '#F3F4F6'
            }
        else:
            theme = {
                'fig_bg': '#F3F4F6', 'ax_bg': '#FFFFFF', 'text': '#111827',
                'grid': '#E5E7EB', 'tick': '#6B7280', 'legend_bg': '#FFFFFF', 'legend_text': '#111827'
            }

        self.figure.clear()

        # 2. APPLY THEME TO FIGURE AND AXES
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
        timeframe = self.timeframe_combo.currentText()

        # 1. Calculate Start and End Dates based on Timeframe
        start_date = None
        end_date = None

        if timeframe == "Current Month":
            qd = self.month_edit.date()
            start_date = datetime(qd.year(), qd.month(), 1)
            # ✅ FIX: Properly handle December and calculate end of month
            if qd.month() == 12:
                end_date = datetime(qd.year() + 1, 1, 1) - timedelta(days=1)
            else:
                end_date = datetime(qd.year(), qd.month() + 1, 1) - timedelta(days=1)
            # ✅ FIX: Set end_date to end of day for proper filtering
            end_date = end_date.replace(hour=23, minute=59, second=59)

        elif timeframe == "Current Year":
            qd = self.year_edit.date()
            start_date = datetime(qd.year(), 1, 1)
            end_date = datetime(qd.year(), 12, 31, 23, 59, 59)

        elif timeframe == "Custom Range":
            qd_start = self.start_edit.date()
            qd_end = self.end_edit.date()
            start_date = datetime(qd_start.year(), qd_start.month(), qd_start.day())
            end_date = datetime(qd_end.year(), qd_end.month(), qd_end.day(), 23, 59, 59)

        # 2. Get Category for YoY
        category = self.category_combo.currentData()

        try:
            if chart_type == "income_expenses":
                ChartFactory.income_vs_expenses_over_time(self.ledger, ax, start_date=start_date, end_date=end_date, theme=theme)
            elif chart_type == "expense_pie":
                ChartFactory.expense_breakdown_by_category(self.ledger, ax, start_date=start_date, end_date=end_date, theme=theme)
            elif chart_type == "net_worth":
                ChartFactory.net_worth_trend(self.ledger, ax, start_date=start_date, end_date=end_date, theme=theme)
            elif chart_type == "top_categories":
                ChartFactory.top_spending_categories(self.ledger, ax, start_date=start_date, end_date=end_date, top_n=10, theme=theme)
            elif chart_type == "yoy_comparison":
                ChartFactory.year_over_year_comparison(self.ledger, ax, category=category, theme=theme)

            self.figure.tight_layout()
            self.canvas.draw()
        except Exception as e:
            ax.clear()
            ax.text(0.5, 0.5, f'Error generating chart:\n{str(e)}', ha='center', va='center', transform=ax.transAxes, color='red')
            self.canvas.draw()
