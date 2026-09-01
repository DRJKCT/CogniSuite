# tax_tab.py ver 0.1
# For CogniFinance ver 1.8

import os
from decimal import Decimal
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QLabel, QComboBox, QDateEdit, QPushButton, QTextEdit,
    QScrollArea, QMessageBox, QFileDialog
)
from PyQt6.QtCore import Qt, QDate
from PyQt6.QtGui import QFont

from ledger import GeneralLedger
from tax_manager import TaxTemplateManager


class TaxTab(QWidget):
    """
    Encapsulates the entire Taxes tab UI and logic.
    Keeps ui_main.py clean by handling template selection,
    summary rendering, dynamic sections, and XLSX export internally.
    """
    def __init__(self, ledger: GeneralLedger, parent=None):
        super().__init__(parent)
        self.gl = ledger
        self.template_manager = TaxTemplateManager()
        self.available_templates = self.template_manager.load_available_templates()

        self._init_ui()
        self.refresh_summary()

    def _init_ui(self):
        # We use a QScrollArea so the tab remains usable even if the user
        # adds many dynamic template sections.
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)

        container_widget = QWidget()
        layout = QVBoxLayout(container_widget)

        # --- Date & Template Selection ---
        range_box = QGroupBox("Tax Year / Period")
        rlayout = QFormLayout()

        self.combo_tax_template = QComboBox()
        self.combo_tax_template.addItem("Standard (No Template)", userData=None)
        for t_id, t_data in self.available_templates.items():
            self.combo_tax_template.addItem(t_data.get("title", t_id), userData=t_id)
        self.combo_tax_template.currentIndexChanged.connect(self.refresh_summary)

        # ==========================================
        # 2. NEW: Tax Rates / Year Selector (JSON)
        # ==========================================
        self.tax_year_combo = QComboBox()
        self._refresh_tax_year_dropdown() # Populate it with JSON files
        self.tax_year_combo.currentIndexChanged.connect(self.refresh_summary)
        rlayout.addRow("Tax Rates/Year:", self.tax_year_combo)
        # ==========================================

        today = QDate.currentDate()
        self.tax_start = QDateEdit()
        self.tax_start.setCalendarPopup(True)
        self.tax_start.setDate(QDate(today.year(), 1, 1))
        self.tax_start.dateChanged.connect(self.refresh_summary)

        self.tax_end = QDateEdit()
        self.tax_end.setCalendarPopup(True)
        self.tax_end.setDate(QDate(today.year(), 12, 31))
        self.tax_end.dateChanged.connect(self.refresh_summary)

        rlayout.addRow("Active Template:", self.combo_tax_template)
        rlayout.addRow("Start:", self.tax_start)
        rlayout.addRow("End:", self.tax_end)
        range_box.setLayout(rlayout)
        layout.addWidget(range_box)

        # --- Action Buttons ---
        btn_row = QHBoxLayout()

        btn_export = QPushButton("Export Tax Report (XLSX)")
        btn_export.clicked.connect(self.export_report)

        year_layout = QHBoxLayout()
        year_layout.addWidget(QLabel("Tax Year / Jurisdiction:"))

        self.tax_year_combo = QComboBox()
        self._refresh_tax_year_dropdown() # Populate it on startup
        year_layout.addWidget(self.tax_year_combo, stretch=1)

        # Connect it so the summary recalculates when the user changes the year
        self.tax_year_combo.currentIndexChanged.connect(self.refresh_summary)


        btn_add_template = QPushButton("Add Template Section")
        btn_add_template.clicked.connect(self.add_dynamic_section)

        btn_row.addWidget(btn_export)
        btn_row.addWidget(btn_add_template)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # --- Summary Text Boxes ---
        self.biz_summary = QTextEdit()
        self.biz_summary.setReadOnly(True)
        self.biz_summary.setFont(QFont("Monospace", 10))
        layout.addWidget(self.biz_summary)

        # self.med_summary = QTextEdit()
        # self.med_summary.setReadOnly(True)
        # self.med_summary.setFont(QFont("Monospace", 10))
        # layout.addWidget(self.med_summary)

        # --- Dynamic Templates Container ---
        self.dynamic_templates_layout = QVBoxLayout()
        layout.addLayout(self.dynamic_templates_layout)

        scroll_area.setWidget(container_widget)

        # Add the scroll area to the main tab layout
        main_layout = QVBoxLayout(self)
        main_layout.addWidget(scroll_area)

    # ------------------------------------------------------------------
    # Core Logic Methods
    # ------------------------------------------------------------------
    def refresh_summary(self):
        from tax_calculator import estimate_tax_liability

        """Recalculates and renders the business and medical summaries."""
        start = self.tax_start.date().toString("yyyy-MM-dd")
        end = self.tax_end.date().toString("yyyy-MM-dd")
        template_data = self._get_active_template_data()

        raw_cats = template_data.get("categories") or template_data.get("target_categories") or {}
        category_map = self._normalize_category_map(raw_cats)
        medical_accounts = template_data.get("medical_accounts", [])

        # Business Summary
        income = self.gl.business_income_total(start, end)
        expenses_by_cat = self.gl.expenses_by_category(start, end, category_map=category_map)
        total_expenses = sum(expenses_by_cat.values(), Decimal("0.00"))
        net = income - total_expenses

        template_title = template_data.get("title", "Tax Summary")

        # --- Build HTML Table for alignment, ease of use and consistency ---
        html = []
        html.append(f"<b>INCOME & EXPENSES ({template_title})</b><br>")
        html.append("<hr>")
        html.append(f"Period: {start} to {end}<br><br>")

        html.append('<table width="100%" cellspacing="0" cellpadding="3" style="border-collapse: collapse; font-family: \'Inter\', sans-serif; font-size: 10pt;">')

        row_idx = 0
        def add_row(label, amount, bold=False, indent=False, is_total=False):
            nonlocal row_idx
            # Zebra striping logic
            if is_total:
                bg = "#e8e8e8" if row_idx % 2 == 0 else "#f0f0f0" # Slightly darker for totals
            else:
                bg = "#f9f9f9" if row_idx % 2 == 0 else "#ffffff" # Standard zebra

            style = "font-weight: bold;" if bold else ""
            label_style = f"padding-left: 20px; {style}" if indent else style

            amt_str = f"{amount:,.2f}"

            html.append(f'<tr bgcolor="{bg}" style="{style}">')
            html.append(f'<td align="left" style="{label_style}">{label}</td>')
            html.append(f'<td align="right" style="{style}" width="120">{amt_str}</td>')
            html.append('</tr>')
            row_idx += 1

        add_row("Income", income, bold=True)

        # Section Header
        html.append('<tr><td colspan="2" style="padding-top: 10px; font-weight: bold;">Expenses by Category:</td></tr>')

        for cat, amt in expenses_by_cat.items():
            if amt != 0:
                add_row(cat, amt, indent=True)

        # Divider line
        html.append('<tr><td colspan="2"><hr></td></tr>')

        add_row("Total Expenses", total_expenses, bold=True, is_total=True)
        add_row("Net Income (Loss)", net, bold=True, is_total=True)

        html.append('</table>')
        html.append("<br><i>Note: This is a planning tool only. Confirm with your CPA.</i>")

        # Tax Liability Section
        if net > Decimal("0.00"):

            selected_template = "2025_2026_canada_ontario"
            if hasattr(self, 'tax_year_combo'):
                selected_template = self.tax_year_combo.currentData() or selected_template

            est = estimate_tax_liability(net, tax_year=selected_template)
            html.append("<br><br>")
            html.append("<b>ESTIMATED TAX LIABILITY (T2125)</b><br>")
            html.append("<hr>")
            html.append('<table width="100%" cellspacing="0" cellpadding="3" style="border-collapse: collapse; font-family: \'Inter\', sans-serif; font-size: 10pt;">')

            row_idx = 0 # Reset index for the new table
            add_row("Federal Tax", est['fed_tax'])
            add_row("Ontario Tax", est['prov_tax'])
            add_row("CPP (Self-Employed)", est['cpp'])
            add_row("Ontario Health Premium", est['ohp'])
            add_row("Less: Basic Personal Credits", est['credits'])

            html.append('<tr><td colspan="2"><hr></td></tr>')
            add_row("TOTAL ESTIMATED TAX", est['total_tax'], bold=True, is_total=True)

            html.append('</table>')
            html.append(f"<br>Effective Tax Rate: <b>{est['effective_rate']:.1f}%</b><br><br>")
            html.append("<i>(Assumes this T2125 is your total taxable income.<br>Does not account for RRSP, spouse, or other credits.)</i>")
        else:
            html.append("<br><br><i>No tax liability estimated (Net income is zero or negative).</i>")

        self.biz_summary.setHtml("".join(html))

        """
        "# Medical Summary
        med_total = self.gl.medical_expenses_total(start, end, medical_accounts=medical_accounts)
        mlines = [
            "OUT‑OF‑POCKET MEDICAL/DENTAL EXPENSES",
            "(for line 33099 / 33199 – planning only)", "=" * 50,
            f"Period: {start} to {end}", "",
            f"{'Total eligible medical expenses':<35} {med_total:>12,.2f}", "",
            "Remember:",
            "- Only amounts above the CRA threshold are creditable.",
            "- Confirm eligibility with your tax preparer."
        ]
        self.med_summary.setPlainText("\n".join(mlines))
        """

    def export_report(self):
        """Triggers the ledger's XLSX export for the current selection."""
        start = self.tax_start.date().toString("yyyy-MM-dd")
        end = self.tax_end.date().toString("yyyy-MM-dd")
        template_data = self._get_active_template_data()

        raw_cats = template_data.get("categories") or template_data.get("target_categories") or {}
        category_map = self._normalize_category_map(raw_cats)
        medical_accounts = template_data.get("medical_accounts", [])

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Tax Report", "", "Excel Files (*.xlsx);;All Files (*)"
        )
        if not path:
            return
        if not path.endswith(".xlsx"):
            path += ".xlsx"

        try:
            self.gl.export_tax_report_xlsx(
                start, end, path,
                category_map=category_map,
                include_medical=True,
                medical_accounts=medical_accounts,
            )
            QMessageBox.information(self, "Export complete", f"Tax report exported to:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "Export failed", str(e))

    def add_dynamic_section(self):
        """Adds a new configurable tax template block to the bottom of the tab."""
        if not self.available_templates:
            QMessageBox.warning(self, "No Templates", "No JSON tax templates found in 'tax_templates/' folder.")
            return

        group_box = QGroupBox("Configurable Tax Report Template")
        g_layout = QVBoxLayout(group_box)

        combo = QComboBox()
        for tid, tdata in self.available_templates.items():
            combo.addItem(tdata.get("title", tid), userData=tid)
        g_layout.addWidget(QLabel("Select Template Definition:"))
        g_layout.addWidget(combo)

        report_output = QTextEdit()
        report_output.setReadOnly(True)
        report_output.setFont(QFont("Monospace", 9))
        report_output.setPlaceholderText("Click generate to build report based on JSON schema criteria...")
        g_layout.addWidget(report_output)

        gen_btn = QPushButton("Generate Report for Template")

        def generate_report():
            selected_id = combo.currentData()
            template_data = self.available_templates.get(selected_id)
            if not template_data:
                return

            start = self.tax_start.date().toString("yyyy-MM-dd")
            end = self.tax_end.date().toString("yyyy-MM-dd")

            raw_cats = template_data.get("categories") or template_data.get("target_categories") or {}
            target_cats = self._normalize_category_map(raw_cats)
            expenses_by_cat = self.gl.expenses_by_category(start, end, category_map=target_cats)
            total_exp = sum(expenses_by_cat.values(), Decimal("0.00"))

            html = []
            html.append(f"<b>REPORT: {template_data.get('title', 'Tax Summary')}</b><br>")
            if template_data.get('description'):
                html.append(f"<i>{template_data.get('description')}</i><br>")
            html.append(f"Period: {start} to {end}<br><br>")
            html.append("<b>Aggregated Categories:</b><br>")

            html.append('<table width="100%" cellspacing="0" cellpadding="3" style="border-collapse: collapse; font-family:\'Inter\', sans-serif; font-size: 10pt;">')
            row_idx = 0
            for cat, amt in expenses_by_cat.items():
                bg = "#f9f9f9" if row_idx % 2 == 0 else "#ffffff"
                amt_str = f"{amt:,.2f}"
                html.append(f'<tr bgcolor="{bg}">')
                html.append(f'<td align="left" style="padding-left: 20px;">{cat}</td>')
                html.append(f'<td align="right" width="120">{amt_str}</td>')
                html.append('</tr>')
                row_idx += 1

            html.append('<tr><td colspan="2"><hr></td></tr>')
            bg_total = "#e8e8e8"
            html.append(f'<tr bgcolor="{bg_total}" style="font-weight: bold;">')
            html.append(f'<td align="left">Total Aggregated</td>')
            html.append(f'<td align="right" width="120">{total_exp:,.2f}</td>')
            html.append('</tr>')
            html.append('</table>')

            # Render as HTML
            report_output.setHtml("".join(html))

        gen_btn.clicked.connect(generate_report)
        combo.currentIndexChanged.connect(generate_report)

        btn_remove = QPushButton("Remove Section")
        btn_remove.clicked.connect(lambda: group_box.deleteLater())

        btn_box = QHBoxLayout()
        btn_box.addWidget(gen_btn)
        btn_box.addWidget(btn_remove)
        g_layout.addLayout(btn_box)

        self.dynamic_templates_layout.addWidget(group_box)

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------
    def _get_active_template_data(self) -> dict:
        if not self.available_templates:
            return {}
        selected_id = self.combo_tax_template.currentData()
        if not selected_id:
            return {}
        return self.available_templates.get(selected_id, {})

    @staticmethod
    def _normalize_category_map(raw_cats) -> dict:
        if isinstance(raw_cats, dict):
            return raw_cats
        if isinstance(raw_cats, list):
            mapped_cats = {}
            for item in raw_cats:
                if isinstance(item, dict):
                    cat_name = item.get("name") or item.get("category")
                    accs = item.get("accounts", [])
                    if cat_name:
                        mapped_cats[cat_name] = accs if isinstance(accs, list) else [accs]
                elif isinstance(item, str):
                    mapped_cats[item] = [item]
            return mapped_cats
        return {}
    def _refresh_tax_year_dropdown(self):
        """Populate the tax year selector with available JSON templates."""
        from tax_calculator import get_available_tax_templates

        self.tax_year_combo.clear()
        templates = get_available_tax_templates()

        # Sort them so the newest years appear at the top
        for template in sorted(templates, reverse=True):
            # Make it look pretty in the dropdown (e.g., "2025 2026 Canada Ontario")
            display_name = template.replace("_", " ").title()
            self.tax_year_combo.addItem(display_name, template)
