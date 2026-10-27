# reports_tab.py
"""
reports_tab.py ver 2.1
For CogniFinance ver 0.9.6
Financial statements on the Reports tab: Income Statement (P&L) and
Balance Sheet, each with optional prior-period comparison.
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QDateEdit, QCheckBox, QPushButton, QTextEdit, QFileDialog,
    QMessageBox, QLabel, QComboBox,
)
from PyQt6.QtCore import QDate
from PyQt6.QtGui import QFont

from ledger import GeneralLedger
from ui_helpers import add_refresh_row
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
import html_renderers


class ReportsTab(QWidget):
    def __init__(self, ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.gl = ledger
        self.is_dark_mode = False
        self._init_ui()
        self.refresh_data()

    def set_dark_mode(self, is_dark: bool):
        self.is_dark_mode = is_dark
        self.refresh_data()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        add_refresh_row(layout, self.refresh_data)

        # --- Report type ---
        type_box = QGroupBox("Report Type")
        tl = QFormLayout()
        self.report_type_combo = QComboBox()
        self.report_type_combo.addItem("Income Statement", "income_statement")
        self.report_type_combo.addItem("Trial Balance", "trial_balance")
        self.report_type_combo.addItem("Balance Sheet", "balance_sheet")
        self.report_type_combo.addItem("Statement of Changes in Equity", "equity_statement")
        self.report_type_combo.currentIndexChanged.connect(self._on_report_type_changed)
        tl.addRow("Report:", self.report_type_combo)
        type_box.setLayout(tl)
        layout.addWidget(type_box)

        # --- Period controls ---
        period_box = QGroupBox("Report Period")
        pl = QFormLayout()

        today = QDate.currentDate()
        first_of_year = QDate(today.year(), 1, 1)

        self.start_label = QLabel("Start:")
        self.start_edit = QDateEdit()
        self.start_edit.setCalendarPopup(True)
        self.start_edit.setDate(first_of_year)
        self.start_edit.dateChanged.connect(self.refresh_data)

        self.end_label = QLabel("End:")
        self.end_edit = QDateEdit()
        self.end_edit.setCalendarPopup(True)
        self.end_edit.setDate(today)
        self.end_edit.dateChanged.connect(self.refresh_data)

        self.compare_checkbox = QCheckBox(
            "Show same-period comparison"
        )
        self.compare_checkbox.setChecked(False)
        self.compare_checkbox.toggled.connect(self.refresh_data)

        self.compare_mode_combo = QComboBox()
        # The entries are re-populated when the report type changes,
        # because the meaningful comparison modes differ between P&L
        # (period-based) and Balance Sheet (point-in-time).
        self.compare_mode_combo.currentIndexChanged.connect(self.refresh_data)
        self.compare_mode_combo.setEnabled(False)

        pl.addRow(self.start_label, self.start_edit)
        pl.addRow(self.end_label, self.end_edit)
        pl.addRow("", self.compare_checkbox)
        pl.addRow("Comparison mode:", self.compare_mode_combo)
        period_box.setLayout(pl)
        layout.addWidget(period_box)

        # --- Export ---
        btn_row = QHBoxLayout()
        btn_export = QPushButton("Export Report (XLSX)")
        btn_export.clicked.connect(self.export_report)
        btn_row.addWidget(btn_export)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # --- Display ---
        self.report_display = QTextEdit()
        self.report_display.setReadOnly(True)
        self.report_display.setFont(QFont("Monospace", 10))
        layout.addWidget(self.report_display)

        # Initial configuration
        self._on_report_type_changed()

    def _on_report_type_changed(self):
        """Adjust the period controls and comparison modes to match the
        selected report type, then refresh."""
        report_type = self.report_type_combo.currentData()

        # Repopulate the comparison-mode combo without triggering refresh
        self.compare_mode_combo.blockSignals(True)
        self.compare_mode_combo.clear()
        if report_type == "balance_sheet":
            self.compare_mode_combo.addItem("Prior year, same date", "prior_year")
            self.compare_mode_combo.addItem("Prior year-end (Dec 31)", "prior_year_end")
        elif report_type in ("trial_balance", "equity_statement"):
            pass
        else:
            self.compare_mode_combo.addItem("Prior year, same dates", "prior_year")
            self.compare_mode_combo.addItem("Prior period, same length", "prior_period")
        self.compare_mode_combo.setCurrentIndex(0)
        self.compare_mode_combo.blockSignals(False)

        # Balance sheet: hide the start date, relabel end to "As of:"
        if report_type in ("balance_sheet", "trial_balance"):
            self.start_label.setVisible(False)
            self.start_edit.setVisible(False)
            self.end_label.setText("As of:")
        else:
            self.start_label.setVisible(True)
            self.start_edit.setVisible(True)
            self.end_label.setText("End:")

        show_cmp = report_type in ("income_statement", "balance_sheet")
        self.compare_checkbox.setVisible(show_cmp)
        self.compare_checkbox.setEnabled(show_cmp)
        self.compare_mode_combo.setVisible(show_cmp)
        self.compare_mode_combo.setEnabled(show_cmp and self.compare_checkbox.isChecked())
        if not show_cmp:
            self.compare_checkbox.setChecked(False)

        self.refresh_data()

    def refresh_data(self):
        report_type = self.report_type_combo.currentData()
        include_cmp = self.compare_checkbox.isChecked()
        comparison_mode = self.compare_mode_combo.currentData() or "prior_year"

        if report_type == "balance_sheet":
            as_of = self.end_edit.date().toString("yyyy-MM-dd")
            built = build_balance_sheet(
                self.gl, as_of,
                include_comparison=include_cmp,
                comparison_mode=comparison_mode,
            )
            period_label = as_of
            compare_label = built.get("compare_end") if include_cmp else None
            html = html_renderers.render_balance_sheet_statement_html(
                built, period_label, compare_label,
            )
        elif report_type == "trial_balance":
            as_of = self.end_edit.date().toString("yyyy-MM-dd")
            built = build_trial_balance(self.gl, as_of)
            html = html_renderers.render_trial_balance_html(built)
        elif report_type == "equity_statement":
            start = self.start_edit.date().toString("yyyy-MM-dd")
            end = self.end_edit.date().toString("yyyy-MM-dd")
            built = build_equity_statement(self.gl, start, end)
            html = html_renderers.render_equity_statement_html(built)
        else:
            start = self.start_edit.date().toString("yyyy-MM-dd")
            end = self.end_edit.date().toString("yyyy-MM-dd")
            built = build_income_statement(
                self.gl, start, end,
                include_comparison=include_cmp,
                comparison_mode=comparison_mode,
            )
            period_label = f"{start} – {end}"
            compare_label = None
            if include_cmp:
                compare_label = f"{built['compare_start']} – {built['compare_end']}"
            html = html_renderers.render_income_statement_html(
                built["sections"], period_label, compare_label,
            )

        if getattr(self, "is_dark_mode", False):
            html = self._apply_dark_css(html)

        self.report_display.setHtml(html)

    def export_report(self):
        report_type = self.report_type_combo.currentData()
        include_cmp = self.compare_checkbox.isChecked()
        comparison_mode = self.compare_mode_combo.currentData() or "prior_year"

        if report_type == "trial_balance":
            as_of = self.end_edit.date().toString("yyyy-MM-dd")
            built = build_trial_balance(self.gl, as_of)
            default_name = f"trial_balance_{as_of}.xlsx"
            exporter = lambda p: export_trial_balance_xlsx(built, p)
        elif report_type == "equity_statement":
            start = self.start_edit.date().toString("yyyy-MM-dd")
            end = self.end_edit.date().toString("yyyy-MM-dd")
            built = build_equity_statement(self.gl, start, end)
            default_name = f"equity_statement_{start}_to_{end}.xlsx"
            exporter = lambda p: export_equity_statement_xlsx(built, p)
        elif report_type == "balance_sheet":
            as_of = self.end_edit.date().toString("yyyy-MM-dd")
            built = build_balance_sheet(
                self.gl, as_of,
                include_comparison=include_cmp,
                comparison_mode=comparison_mode,
            )
            period_label = as_of
            compare_label = built.get("compare_end") if include_cmp else None
            default_name = f"balance_sheet_{as_of}.xlsx"
            exporter = lambda p: export_balance_sheet_xlsx(
                built, p, period_label=period_label, compare_label=compare_label,
            )
        else:
            start = self.start_edit.date().toString("yyyy-MM-dd")
            end = self.end_edit.date().toString("yyyy-MM-dd")
            built = build_income_statement(
                self.gl, start, end,
                include_comparison=include_cmp,
                comparison_mode=comparison_mode,
            )
            period_label = f"{start} – {end}"
            compare_label = None
            if include_cmp:
                compare_label = f"{built['compare_start']} – {built['compare_end']}"
            default_name = f"income_statement_{start}_to_{end}.xlsx"
            exporter = lambda p: export_income_statement_xlsx(
                built, p, period_label=period_label, compare_label=compare_label,
            )

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
            QMessageBox.information(self, "Export Complete", f"Exported to:\n{path}")
        except ImportError:
            QMessageBox.critical(
                self, "Missing Dependency",
                "The 'openpyxl' library is required to export XLSX files."
            )
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", str(e))

    @staticmethod
    def _apply_dark_css(html: str) -> str:
        dark_css = """
        <style>
            body, html { background-color: #111318 !important; color: #F3F4F6 !important; }
            table, td, th, p, h1, h2, h3, h4, span, div, li { color: #F3F4F6 !important; background-color: #111318 !important; }
            .header, .title, h1, h2 { color: #10B981 !important; }
        </style>
        """
        return dark_css + html
