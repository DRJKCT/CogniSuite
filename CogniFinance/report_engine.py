"""
report_engine.py ver 1.0
For CogniFinance ver 0.9.67
Centralized PDF generation engine using ReportLab.
"""
import os
from datetime import datetime
from decimal import Decimal
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
from ledger import GeneralLedger

class ReportEngine:
    """Generates formal PDF deliverables for CogniFinance."""

    @staticmethod
    def generate_reconciliation_pdf(ledger: GeneralLedger, account_name: str,
                                    start_date: str, end_date: str,
                                    statement_balance: Decimal, output_path: str):
        """
        Generates a professional Bank Reconciliation Report.
        """
        doc = SimpleDocTemplate(output_path, pagesize=letter,
                                rightMargin=0.75*inch, leftMargin=0.75*inch,
                                topMargin=0.75*inch, bottomMargin=0.75*inch)

        styles = getSampleStyleSheet()

        # Custom Styles for Accounting Reports
        styles.add(ParagraphStyle(name='ReportTitle', parent=styles['Heading1'], fontSize=16, spaceAfter=12, alignment=TA_CENTER, textColor=colors.HexColor('#111827')))
        styles.add(ParagraphStyle(name='SectionHeader', parent=styles['Heading2'], fontSize=12, spaceBefore=12, spaceAfter=6, textColor=colors.HexColor('#059669'))) # Emerald
        styles.add(ParagraphStyle(name='RightAlign', parent=styles['Normal'], alignment=TA_RIGHT))
        styles.add(ParagraphStyle(name='BoldRight', parent=styles['Normal'], alignment=TA_RIGHT, fontName='Helvetica-Bold'))
        styles.add(ParagraphStyle(name='TotalRow', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=11, textColor=colors.HexColor('#111827')))

        elements = []

        # --- 1. HEADER ---
        elements.append(Paragraph("Bank Reconciliation Report", styles['ReportTitle']))
        elements.append(Paragraph(f"<b>Account:</b> {account_name}", styles['Normal']))
        elements.append(Paragraph(f"<b>Statement Period:</b> {start_date} to {end_date}", styles['Normal']))
        elements.append(Paragraph(f"<b>Prepared On:</b> {datetime.now().strftime('%Y-%m-%d %H:%M')}", styles['Normal']))
        elements.append(Spacer(1, 0.2*inch))
        elements.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor('#E5E7EB')))
        elements.append(Spacer(1, 0.2*inch))

        # --- 2. DATA GATHERING ---
        txs = ledger.get_transactions_for_account(account_name, start_date, end_date)

        cleared_txs = [tx for tx in txs if tx.reconciled]
        uncleared_txs = [tx for tx in txs if not tx.reconciled]

        # Calculate totals (simplified logic for display)
        # In a real app, you'd calculate the specific impact on the bank account balance
        # For now, we'll just list them and sum their display amounts

        from ui_helpers import get_transaction_display_amount

        cleared_amount = sum(get_transaction_display_amount(ledger, tx) for tx in cleared_txs)
        uncleared_amount = sum(get_transaction_display_amount(ledger, tx) for tx in uncleared_txs)

        # The "Cleared Ledger Balance" is effectively the Statement Balance if reconciled
        # But mathematically: Book Balance + Uncleared = Statement Balance (roughly)
        # We will display the user's inputs and the counts.

        # --- 3. SUMMARY SECTION ---
        elements.append(Paragraph("Reconciliation Summary", styles['SectionHeader']))

        summary_data = [
            ["Statement Ending Balance:", "", f"${statement_balance:,.2f}"],
            ["", "", ""],
            ["Less: Uncleared Transactions:", f"({len(uncleared_txs)})", f"(${uncleared_amount:,.2f})"],
            ["", "", ""],
            ["Cleared Ledger Balance:", "", f"${statement_balance - uncleared_amount:,.2f}"] # Simplified math
        ]

        summary_table = Table(summary_data, colWidths=[3*inch, 1*inch, 1.5*inch])
        summary_table.setStyle(TableStyle([
            ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'), # Bold the final line
            ('LINEABOVE', (0, -1), (-1, -1), 1, colors.black), # Line above total
            ('LINEBELOW', (0, -1), (-1, -1), 2, colors.black), # Double line under total
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
        ]))
        elements.append(summary_table)
        elements.append(Spacer(1, 0.3*inch))

        # --- 4. STATISTICS ---
        stats_data = [
            ["Total Transactions in Period:", str(len(txs))],
            ["Cleared Transactions:", str(len(cleared_txs))],
            ["Uncleared (Outstanding) Transactions:", str(len(uncleared_txs))]
        ]
        stats_table = Table(stats_data, colWidths=[4*inch, 1.5*inch])
        stats_table.setStyle(TableStyle([
            ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E5E7EB')),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F9FAFB')),
        ]))
        elements.append(stats_table)
        elements.append(Spacer(1, 0.3*inch))

        # --- 5. UNCLEARED TRANSACTIONS (The "Outstanding" List) ---
        if uncleared_txs:
            elements.append(Paragraph("Uncleared Transactions (Outstanding)", styles['SectionHeader']))

            uncleared_data = [["Date", "Description", "Amount"]]
            for tx in uncleared_txs:
                amt = get_transaction_display_amount(ledger, tx)
                uncleared_data.append([
                    tx.date,
                    Paragraph(tx.description, styles['Normal']),
                    Paragraph(f"${amt:,.2f}", styles['RightAlign'])
                ])

            uncleared_table = Table(uncleared_data, colWidths=[1*inch, 3.5*inch, 1*inch])
            uncleared_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#E5E7EB')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#111827')),
                ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
                ('GRID', (0, 1), (-1, -1), 0.5, colors.HexColor('#E5E7EB')),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F9FAFB')]),
            ]))
            elements.append(uncleared_table)

        # Build PDF
        doc.build(elements)
        return True
