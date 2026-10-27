# dynamic_report_widget.py
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QComboBox, QPushButton, QTextEdit, QFileDialog, QMessageBox
)
from PyQt6.QtGui import QFont
from decimal import Decimal


class DynamicReportWidget(QWidget):
    """
    A standalone, reusable widget for rendering any JSON-defined report.
    Accepts a ledger, a dict of available templates, and a reference
    to the parent tab (for reading date ranges).
    """

    def __init__(self, ledger, available_templates, parent_tab,
                 is_dark_mode=False, parent=None):
        super().__init__(parent)
        self.gl = ledger
        self.available_templates = available_templates
        self.parent_tab = parent_tab
        self.is_dark_mode = is_dark_mode
        self._init_ui()
        self.generate_report()

    def _init_ui(self):
        group_box = QGroupBox("Configurable Tax Report Template")
        g_layout = QVBoxLayout(group_box)

        self.combo = QComboBox()
        for tid, tdata in self.available_templates.items():
            self.combo.addItem(tdata.get("title", tid), userData=tid)

        g_layout.addWidget(QLabel("Select Template Definition:"))
        g_layout.addWidget(self.combo)

        self.report_output = QTextEdit()
        self.report_output.setReadOnly(True)
        self.report_output.setFont(QFont("Monospace", 9))
        g_layout.addWidget(self.report_output)

        self.export_btn = QPushButton("Export Section to XLSX")
        self.export_btn.clicked.connect(self.export_to_xlsx)

        self.combo.currentIndexChanged.connect(self.generate_report)

        btn_remove = QPushButton("Remove Section")
        btn_remove.clicked.connect(self.deleteLater)

        btn_box = QHBoxLayout()
        btn_box.addWidget(self.export_btn)
        btn_box.addWidget(btn_remove)
        g_layout.addLayout(btn_box)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(group_box)

        self.generate_report()

    def set_dark_mode(self, is_dark: bool):
        self.is_dark_mode = is_dark
        self.generate_report()

    def generate_report(self):
        selected_id = self.combo.currentData()
        template_data = self.available_templates.get(selected_id)
        if not template_data:
            return

        # Get dates from the parent TaxTab
        start = self.parent_tab.tax_start.date().toString("yyyy-MM-dd")
        end = self.parent_tab.tax_end.date().toString("yyyy-MM-dd")

        raw_cats = template_data.get("categories") or template_data.get("target_categories") or {}
        target_cats = self.parent_tab._normalize_category_map(raw_cats)
        expenses_by_cat = self.gl.expenses_by_category(start, end, category_map=target_cats)
        total_exp = sum(expenses_by_cat.values(), Decimal("0.00"))

        # Dynamic Colors
        if getattr(self, 'is_dark_mode', False):
            text_color = "#F3F4F6"      # Off-white
            bg_light = "#1A1D24"        # Surface 1
            bg_dark = "#111318"         # Base Canvas (for zebra striping)
            bg_total = "#22262E"        # Surface 2
            border_color = "#2D323C"    # For <hr> tags
        else:
            text_color = "#111827"      # Dark Gray (not pure black)
            bg_light = "#F9FAFB"        # Very light gray
            bg_dark = "#FFFFFF"         # Pure white
            bg_total = "#F3F4F6"        # Header gray
            border_color = "#E5E7EB"    # Light border

        html = []
        html.append(f'<span style="color: {text_color};">')
        html.append(f'<span style="font-weight: bold;">REPORT: {template_data.get("title", "Tax Summary")}</span><br>')
        if template_data.get('description'):
            html.append(f'<span style="font-style: italic;">{template_data.get("description")}</span><br>')
        html.append(f"Period: {start} to {end}<br><br>")
        html.append('<span style="font-weight: bold;">Aggregated Categories:</span><br>')

        html.append(f'<table width="100%" cellspacing="0" cellpadding="3" style="border-collapse: collapse; font-family:\'Inter\', sans-serif; font-size: 10pt; color: {text_color};">')
        row_idx = 0
        for cat, amt in expenses_by_cat.items():
            bg = bg_light if row_idx % 2 == 0 else bg_dark
            html.append(f'<tr bgcolor="{bg}"><td align="left" style="padding-left: 20px;">{cat}</td><td align="right" width="120">{amt:,.2f}</td></tr>')
            row_idx += 1

        html.append('<tr><td colspan="2"><hr></td></tr>')
        html.append(f'<tr bgcolor="{bg_total}" style="font-weight: bold;"><td align="left">Total Aggregated</td><td align="right" width="120">{total_exp:,.2f}</td></tr>')
        html.append('</table></span>')

        self.report_output.setHtml("".join(html))

    def export_to_xlsx(self):
        """Exports the specific dynamic template report to an XLSX file, scaling to any number of rows."""
        selected_id = self.combo.currentData()
        template_data = self.available_templates.get(selected_id)
        if not template_data:
            QMessageBox.warning(self, "No Template", "Please select a template definition first.")
            return

        # 1. Get dates and category map
        start = self.parent_tab.tax_start.date().toString("yyyy-MM-dd")
        end = self.parent_tab.tax_end.date().toString("yyyy-MM-dd")

        raw_cats = template_data.get("categories") or template_data.get("target_categories") or {}
        target_cats = self.parent_tab._normalize_category_map(raw_cats)

        # 2. Query the ledger. This returns a dictionary of arbitrary length (5 rows, 15 rows, etc.)
        expenses_by_cat = self.gl.expenses_by_category(start, end, category_map=target_cats)

        # 3. File dialog setup
        template_title = template_data.get("title", "Tax_Report")
        safe_title = "".join(c for c in template_title if c.isalnum() or c in (' ', '-', '_')).rstrip()
        default_name = f"{safe_title}_{start}_to_{end}.xlsx"

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Template Report", default_name, "Excel Files (*.xlsx);;All Files (*)"
        )
        if not path:
            return
        if not path.endswith(".xlsx"):
            path += ".xlsx"

        # 4. Generate the XLSX dynamically
        try:
            import openpyxl
            from openpyxl.styles import Font, Alignment

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = template_title[:31] # Excel sheet name limit

            # --- Static Headers ---
            ws['A1'] = template_title
            ws['A1'].font = Font(bold=True, size=14)
            ws['A2'] = f"Period: {start} to {end}"
            ws['A2'].font = Font(italic=True)

            ws['A4'] = "Category"
            ws['B4'] = "Amount"
            ws['A4'].font = Font(bold=True)
            ws['B4'].font = Font(bold=True)
            ws['B4'].alignment = Alignment(horizontal='right')

            # --- DYNAMIC DATA ROWS ---
            current_row = 5
            running_total = Decimal("0.00")

            # This loop scales to exactly the number of categories in the JSON template
            for cat, amt in expenses_by_cat.items():
                ws.cell(row=current_row, column=1, value=cat)

                cell_amt = ws.cell(row=current_row, column=2, value=float(amt))
                cell_amt.number_format = '#,##0.00'
                cell_amt.alignment = Alignment(horizontal='right')

                running_total += amt
                current_row += 1 # Move to the next row down

            # --- DYNAMIC TOTAL ROW ---
            # Placed immediately after the last category, regardless of how many there were
            ws.cell(row=current_row, column=1, value="Total Aggregated").font = Font(bold=True)

            cell_total = ws.cell(row=current_row, column=2, value=float(running_total))
            cell_total.font = Font(bold=True)
            cell_total.number_format = '#,##0.00'
            cell_total.alignment = Alignment(horizontal='right')

            # Auto-fit column widths
            ws.column_dimensions['A'].width = 45
            ws.column_dimensions['B'].width = 18

            wb.save(path)
            QMessageBox.information(self, "Export Complete", f"Report exported to:\n{path}")

        except ImportError:
            QMessageBox.critical(self, "Missing Dependency", "The 'openpyxl' library is required to export XLSX files.")
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", f"An error occurred during export:\n{e}")
