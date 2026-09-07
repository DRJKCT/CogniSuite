# reports_tab.py
"""
reports_tab.py ver 1.0
For CogniFinance ver 1.9
Encapsulates the Reports tab UI, including date selection,
HTML report rendering, and XLSX export functionality.
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QComboBox, QPushButton, QTextEdit, QFileDialog, QMessageBox
)
from PyQt6.QtCore import QDate
from PyQt6.QtGui import QFont

from ledger import GeneralLedger
from ui_helpers import add_refresh_row
import html_renderers

class ReportsTab(QWidget):
    def __init__(self, ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.gl = ledger
        self._init_ui()
        self.refresh_data()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        add_refresh_row(layout, self.refresh_data)

        # --- Date Selectors ---
        month_box = QGroupBox("Report Period")
        mlayout = QFormLayout()

        current_date = QDate.currentDate()
        current_year = current_date.year()
        current_month = current_date.month()

        self.report_year = QComboBox()
        for y in range(current_year - 5, current_year + 2):
            self.report_year.addItem(str(y), y)
        self.report_year.setCurrentText(str(current_year))

        self.report_month = QComboBox()
        for m in range(1, 13):
            self.report_month.addItem(f"{m:02d}", m)
        self.report_month.setCurrentText(f"{current_month:02d}")

        mlayout.addRow("Year:", self.report_year)
        mlayout.addRow("Month:", self.report_month)
        month_box.setLayout(mlayout)
        layout.addWidget(month_box)

        # --- Export Button ---
        btn_row = QHBoxLayout()
        btn_export = QPushButton("Export Report (XLSX)")
        btn_export.clicked.connect(self.export_report)
        btn_row.addWidget(btn_export)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # --- Report Display ---
        self.report_summary = QTextEdit()
        self.report_summary.setReadOnly(True)
        self.report_summary.setFont(QFont("Monospace", 10))
        layout.addWidget(self.report_summary)

    def refresh_data(self):
        """Fetches data from the ledger and renders the HTML report."""
        year = self.report_year.currentData()
        month = self.report_month.currentData()

        # 1. Fetch Data
        data = self.gl.monthly_income_expense_report(year, month)

        # 2. Render and Display
        html = html_renderers.render_monthly_report_html(year, month, data)
        self.report_summary.setHtml(html)

    def export_report(self):
        """Exports the currently selected month's report to an XLSX file."""
        year = self.report_year.currentData()
        month = self.report_month.currentData()

        default_name = f"monthly_report_{month:02d}_{year}.xlsx"

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Monthly Report", default_name,
            "Excel Files (*.xlsx);;All Files (*)"
        )
        if not path:
            return

        if not path.endswith(".xlsx"):
            path += ".xlsx"

        try:
            self.gl.export_monthly_income_expense_xlsx(year, month, path)
            QMessageBox.information(self, "Export complete", f"Monthly report exported to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))
