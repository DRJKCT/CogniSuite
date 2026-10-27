# ui_main.py ver 2.0 - Modernized with side navigation
# For CogniFinance ver 0.9.6

import os
import io
import json
import threading
import urllib.error
import urllib.request
import logging
import qrcode
import time
import html_renderers
from typing import List, Dict, Any, Optional
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTextEdit, QPushButton, QFileDialog, QMessageBox,
    QGridLayout, QLineEdit, QFormLayout, QScrollArea, QSplitter,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QComboBox, QDateEdit, QGroupBox, QDialog, QDialogButtonBox, QSizePolicy,
    QAbstractItemView, QStyledItemDelegate, QTreeWidget, QTreeWidgetItem,
    QProgressDialog, QStyleOptionViewItem, QStyle, QStackedWidget, QListWidget,
    QListWidgetItem, QSpacerItem, QFrame, QCheckBox, QToolButton
)
from PyQt6.QtCore import Qt, QDate, QEvent, QTimer, pyqtSignal, QSize
from PyQt6.QtGui import QColor, QFont, QPixmap, QImage, QDoubleValidator, QStandardItemModel, QStandardItem, QPalette, QBrush, QIcon, QPainter, QPen
from pathlib import Path
from datetime import datetime

from ledger import GeneralLedger, Budget, ZERO
from parser_integration import parse_statement_to_transactions
from tax_manager import TaxTemplateManager
from pdf_import import PDFImportThread
from decimal import Decimal
from notification_center import NotificationCenter, NotificationStore, Severity
from notification_widgets import NotificationBell
from log_tab import LogTab
from audit_tab import AuditTab
from tax_tab import TaxTab
from investments_tab import InvestmentsAndCryptoTab
from ofx_import import parse_ofx_to_ledger_transactions, OFXImportThread
from TestRunnerManager import TestRunnerManager
from graphs import ChartsTab
from category_rules_tab import CategoryRulesTab
from backup_manager import BackupManager
from ui_helpers import (
    add_refresh_row, setup_table_headers, fit_table_columns,
    money_item, get_transaction_display_amount, create_account_combo
)
from transaction_dialogs import AddTransactionDialog, EditTransactionDialog
from ui_transactions_tab import TransactionsTab
from budgets_tab import BudgetsTab
from add_edit_tab import AddEditTab
from import_manager import ImportManager
from sync_tab import SyncTab
from reports_tab import ReportsTab
from reconcile_tab import ReconcileTab
from config import NOTIFICATIONS_PATH, BACKUP_DIR, UI_CONFIG_PATH
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from settings_tab import SettingsTab
from recurring_tab import RecurringTab
import simple_mode
import shell_config
from simple_home_tab import SimpleHomeTab
from simple_money_tab import SimpleMoneyTab
from simple_plans_tab import SimplePlansTab
from simple_reports_tab import SimpleReportsTab

logger = logging.getLogger(__name__)

TAB_ICONS = {
    "Overview": "overview.svg",
    "Simple Home": "overview.svg",
    "Simple Money": "transactions.svg",
    "Plans": "budget.svg",
    "Simple Reports": "report.svg",
    "Reconciliation": "reconciliation.svg",
    "Transactions": "transactions.svg",
    "Add/Edit": "add_edit.svg",
    "Budgets": "budget.svg",
    "Recurring": "recurring.svg",
    "Investments": "investments.svg",
    "Charts": "chart.svg",
    "Reports": "report.svg",
    "Accounts": "accounts.svg",
    "Balance Sheet": "balance.svg",
    "Category Rules": "rules.svg",
    "Taxes": "taxes.svg",
    "Mobile Sync": "sync.svg",
    "Logs": "log.svg",
    "Audit Log": "audit.svg",
    "Settings": "settings.svg"
}

# --- MASTER COLOR PALETTES ---
LIGHT_MODE_QSS = """
/* Base canvas is now a slightly darker off-white for depth */
QWidget { background-color: #F3F4F6; color: #111827; }
QMainWindow { background-color: #F3F4F6; }

/* Data loading areas (Inputs, Tables, Text Edits) use the softer off-white */
QTextEdit, QPlainTextEdit, QLineEdit, QSpinBox, QDateEdit {
    background-color: #F9FAFB; color: #111827; border: 1px solid #E5E7EB; border-radius: 6px; padding: 6px;
    selection-background-color: #10B981; selection-color: #FFFFFF;
}
QLineEdit:focus, QTextEdit:focus { border: 1px solid #10B981; }

QComboBox { background-color: #F9FAFB; color: #111827; border: 1px solid #E5E7EB; border-radius: 6px; padding: 5px 10px; }
QComboBox::drop-down { border: none; background-color: transparent; width: 24px; }
QComboBox QAbstractItemView { background-color: #F9FAFB; color: #111827; selection-background-color: #F3F4F6; selection-color: #10B981; border: 1px solid #E5E7EB; outline: none; }

QTableWidget, QTreeWidget, QTreeView, QTableView, QListWidget {
    background-color: #F9FAFB; alternate-background-color: #FFFFFF; color: #111827; gridline-color: #E5E7EB; border: 1px solid #E5E7EB; border-radius: 6px; outline: none;
}
QHeaderView::section { background-color: #E5E7EB; color: #6B7280; border: none; border-bottom: 1px solid #D1D5DB; border-right: 1px solid #D1D5DB; padding: 6px; font-weight: bold; }

QTableWidget::item:selected, QTreeWidget::item:selected { background-color: #ECFDF5; color: #059669; }

QPushButton { background-color: #10B981; color: #FFFFFF; border: none; padding: 8px 18px; border-radius: 6px; font-weight: bold; }
QPushButton:hover { background-color: #059669; }
QPushButton:pressed { background-color: #047857; }
QPushButton:disabled { background-color: #E5E7EB; color: #9CA3AF; }

QGroupBox { border: 1px solid #E5E7EB; border-radius: 8px; margin-top: 12px; color: #111827; font-weight: bold; background-color: #F9FAFB; }
QGroupBox::title { subcontrol-origin: margin; left: 16px; padding: 0 8px; color: #10B981; }

QScrollBar:vertical { background: #F3F4F6; width: 10px; margin: 0px; border-radius: 5px; }
QScrollBar::handle:vertical { background: #D1D5DB; min-height: 20px; border-radius: 5px; }
QScrollBar::handle:vertical:hover { background: #9CA3AF; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }

QCheckBox::indicator { width: 18px; height: 18px; border: 1px solid #D1D5DB; border-radius: 4px; background-color: #F9FAFB; }
QCheckBox::indicator:checked { background-color: #10B981; border: 1px solid #10B981; }

QLabel { color: #111827; background: transparent; }
"""

DARK_MODE_QSS = """
QWidget { background-color: #111318; color: #F3F4F6; }
QMainWindow { background-color: #111318; }
QTextEdit, QPlainTextEdit, QLineEdit, QSpinBox, QDateEdit {
    background-color: #1A1D24; color: #F3F4F6; border: 1px solid #2D323C; border-radius: 6px; padding: 6px;
    selection-background-color: #10B981; selection-color: #FFFFFF;
}
QLineEdit:focus, QTextEdit:focus { border: 1px solid #10B981; }
QComboBox { background-color: #1A1D24; color: #F3F4F6; border: 1px solid #2D323C; border-radius: 6px; padding: 5px 10px; }
QComboBox::drop-down { border: none; background-color: transparent; width: 24px; }
QComboBox QAbstractItemView { background-color: #22262E; color: #F3F4F6; selection-background-color: #2A2F3A; selection-color: #10B981; border: 1px solid #2D323C; outline: none; }
QTableWidget, QTreeWidget, QTreeView, QTableView, QListWidget {
    background-color: #1A1D24; alternate-background-color: #16191F; color: #F3F4F6; gridline-color: #2D323C; border: 1px solid #2D323C; border-radius: 6px; outline: none;
}
QHeaderView::section { background-color: #22262E; color: #9CA3AF; border: none; border-bottom: 1px solid #2D323C; border-right: 1px solid #2D323C; padding: 6px; font-weight: bold; }
QTableWidget::item:selected, QTreeWidget::item:selected { background-color: #2A2F3A; color: #10B981; }
QPushButton { background-color: #10B981; color: #FFFFFF; border: none; padding: 8px 18px; border-radius: 6px; font-weight: bold; }
QPushButton:hover { background-color: #059669; }
QPushButton:pressed { background-color: #047857; }
QPushButton:disabled { background-color: #2A2F3A; color: #6B7280; }
QGroupBox { border: 1px solid #2D323C; border-radius: 8px; margin-top: 12px; color: #F3F4F6; font-weight: bold; background-color: #1A1D24; }
QGroupBox::title { subcontrol-origin: margin; left: 16px; padding: 0 8px; color: #10B981; }
QScrollBar:vertical { background: #111318; width: 10px; margin: 0px; border-radius: 5px; }
QScrollBar::handle:vertical { background: #2D323C; min-height: 20px; border-radius: 5px; }
QScrollBar::handle:vertical:hover { background: #3B4252; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QCheckBox::indicator { width: 18px; height: 18px; border: 1px solid #2D323C; border-radius: 4px; background-color: #1A1D24; }
QCheckBox::indicator:checked { background-color: #10B981; border: 1px solid #10B981; }
QLabel { color: #F3F4F6; background: transparent; }
"""


def _save_window_geometry(width: int, height: int) -> None:
    """Persist current window size to ui_config.json for the next launch."""
    config_path = str(UI_CONFIG_PATH)
    try:
        config = {}
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                config = json.load(f)
        config["window_width"] = width
        config["window_height"] = height
        with open(config_path, "w") as f:
            json.dump(config, f)
    except Exception:
        # Best-effort — should not block shutdown
        pass

class ShrinkableStack(QStackedWidget):
    def minimumSizeHint(self):
        return QSize(0, 0)

class NavigationButton(QToolButton):
    """Custom button for sidebar navigation with hover/active states."""
    def __init__(self, text: str, icon_filename: str = None, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedHeight(45)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.full_text = text
        self.target_name = text   # the screen this button opens (never relabeled)

        # FIX 1: Set objectName to bypass global qt_material theme interference
        self.setObjectName("NavButton")

        if icon_filename:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            icon_path = os.path.join(base_dir, "icons", icon_filename)
            if os.path.exists(icon_path):
                icon = QIcon(icon_path)
                self.setIcon(icon)
                self.setIconSize(QSize(24, 24))

        self.setText(text)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._update_style()

    def apply_theme_colors(self, text_color: str, hover_bg: str):
        """Updates button colors based on the global theme."""
        self._text_color = text_color
        self._hover_bg = hover_bg
        self._update_style()

    def update_colors_for_theme(self, text_color: str, hover_bg: str):
        """Called by the sidebar to update colors when theme changes."""
        self._text_color = text_color
        self._hover_bg = hover_bg
        self._update_style() # Re-apply the style with new colors

    def _update_style(self):
        text_color = getattr(self, '_text_color', '#111827')
        hover_bg = getattr(self, '_hover_bg', '#F3F4F6')
        active_bg = "#10B981"
        active_border = "#059669"
        active_hover = "#059669"

        if self.isChecked():
            self.setStyleSheet(f"""
                QToolButton {{
                    background-color: {active_bg}; color: white; border: none;
                    border-left: 4px solid {active_border}; font-weight: bold;
                    text-align: left; padding-left: 16px;
                }}
                QToolButton:hover {{ background-color: {active_hover}; color: white; }}
            """)
        else:
            self.setStyleSheet(f"""
                QToolButton {{
                    background-color: transparent; color: {text_color}; border: none;
                    border-left: 4px solid transparent; padding-left: 16px; text-align: left;
                }}
                QToolButton:hover {{ background-color: {hover_bg}; color: {text_color}; }}
            """)

    def set_label(self, text: str):
        """Change the visible label without changing which screen opens.

        Simple Mode calls the Overview screen "Home", for example. While
        the sidebar is collapsed the text stays empty and only the
        tooltip carries the label.
        """
        self.full_text = text
        if self.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly:
            self.setToolTip(text)
        else:
            self.setText(text)

    def set_collapsed(self, collapsed: bool):
        """Toggle between icon-only and icon+text modes."""
        text_color = getattr(self, '_text_color', '#111827')
        hover_bg = getattr(self, '_hover_bg', '#F3F4F6')
        active_bg = "#10B981"
        active_border = "#059669"
        active_hover = "#059669"

        if collapsed:
            self.setText("")
            self.setToolTip(self.full_text)
            self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            self.setStyleSheet(f"""
                QToolButton {{
                    background-color: transparent;
                    border: none;
                    border-left: 4px solid transparent;
                    padding-left: 14px;
                    text-align: center;
                }}
                QToolButton:hover {{
                    background-color: {hover_bg};
                }}
                QToolButton:checked {{
                    background-color: {active_bg};
                    border-left: 4px solid {active_border};
                    padding-left: 14px;
                }}
                QToolButton:checked:hover {{
                    background-color: {active_hover};
                }}
            """)
        else:
            self.setText(self.full_text)
            self.setToolTip("")
            self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self._update_style()

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self._update_style()

class CollapsibleSidebar(QWidget):
    """Collapsible navigation sidebar."""
    tab_changed = pyqtSignal(int)

    def __init__(self, tab_names: List[str], parent=None):
        super().__init__(parent)
        self.setFixedWidth(220)
        self.is_collapsed = False
        self.buttons = []
        self.tab_names = tab_names

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header with collapse button
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 10, 10, 10)

        self.app_label = QLabel("CogniFinance")
        self.app_label.setStyleSheet("""
            color: #007d32;
            font-family: 'Inter', sans-serif;
            font-size: 18px;
            font-weight: bold;
        """)

        # Create collapse button with proper icon
        self.collapse_btn = QPushButton()
        self.collapse_btn.setFixedSize(30, 30)
        self.collapse_btn.setIcon(self._create_collapse_icon(False))
        self.collapse_btn.setIconSize(self.collapse_btn.size())
        self.collapse_btn.setStyleSheet("""
            QPushButton {
                background-color: #e8f5e9;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #c8e6c9;
            }
        """)
        self.collapse_btn.clicked.connect(self.toggle_collapse)

        header_layout.addWidget(self.app_label)
        header_layout.addStretch()
        header_layout.addWidget(self.collapse_btn)

        layout.addWidget(header)

        # Navigation buttons container
        self.nav_scroll = QScrollArea()
        self.nav_scroll.setWidgetResizable(True)
        self.nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav_scroll.setStyleSheet("border: none; background-color: transparent;")

        self.nav_container = QWidget()
        self.nav_layout = QVBoxLayout(self.nav_container)
        self.nav_layout.setContentsMargins(0, 0, 0, 0)
        self.nav_layout.setSpacing(2)
        self.nav_layout.addStretch()

        self.nav_scroll.setWidget(self.nav_container)
        layout.addWidget(self.nav_scroll, 1)

        # Create navigation buttons with icons
        tab_icons = self._create_tab_icons()
        for i, name in enumerate(tab_names):
            icon_file = TAB_ICONS.get(name)
            btn = NavigationButton(name, icon_file)
            btn.clicked.connect(lambda checked, idx=i: self._on_button_clicked(idx))
            self.buttons.append(btn)
            self.nav_layout.insertWidget(i, btn)

        # Footer with version
        self.footer_label = QLabel("v0.9.3")
        self.footer_label.setStyleSheet("color: #999; padding: 10px; font-size: 11px;")
        self.footer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.footer_label)

        # Select first tab by default
        if self.buttons:
            self.buttons[0].setChecked(True)
            self.buttons[0]._update_style()

    def apply_theme(self, is_dark: bool):
        """Updates sidebar colors to match the global theme."""
        if is_dark:
            bg_color = "#111318"
            text_color = "#F3F4F6"
            hover_bg = "#2A2F3A"
            collapse_bg = "#1A1D24"
            collapse_hover = "#22262E"
        else:
            bg_color = "#FFFFFF"
            text_color = "#111827"
            hover_bg = "#F3F4F6"
            collapse_bg = "#F9FAFB"
            collapse_hover = "#E5E7EB"

        self.setStyleSheet(f"background-color: {bg_color};")
        self.app_label.setStyleSheet("color: #10B981; font-size: 22px; font-weight: bold;")

        self.collapse_btn.setStyleSheet(f"""
            QPushButton {{ background-color: {collapse_bg}; border: none; border-radius: 4px; color: #10B981; }}
            QPushButton:hover {{ background-color: {collapse_hover}; }}
        """)

        for btn in self.buttons:
            btn.apply_theme_colors(text_color, hover_bg)

    def _create_collapse_icon(self, is_collapsed: bool) -> QIcon:
        """Create a collapse/expand icon using QPainter."""
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#10B981"), 2))

        if is_collapsed:
            # Right arrow (expand)
            painter.drawLine(8, 6, 16, 12)
            painter.drawLine(8, 18, 16, 12)
        else:
            # Left arrow (collapse)
            painter.drawLine(16, 6, 8, 12)
            painter.drawLine(16, 18, 8, 12)

        painter.end()
        return QIcon(pixmap)

    def _make_more_toggle(self, count: int) -> QToolButton:
        """A single expand/collapse row that hides the group below it."""
        toggle = QToolButton()
        toggle.setObjectName("MoreToggle")
        toggle.setCheckable(True)
        toggle.setChecked(False)
        toggle.setFixedHeight(32)
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle.setText(f"More ({count})  \u25be")
        toggle.setStyleSheet("""
            QToolButton {
                background: transparent;
                border: none;
                color: #9CA3AF;
                font-size: 11px;
                font-weight: bold;
                text-align: left;
                padding: 4px 16px;
            }
            QToolButton:hover {
                background: rgba(0,0,0,0.04);
                color: #6B7280;
            }
            QToolButton:checked {
                color: #10B981;
            }
        """)
        return toggle

    def _create_tab_icons(self) -> dict:
        """Create icons for each tab using Qt's built-in icons."""
        from PyQt6.QtWidgets import QStyle
        icons = {}

        # Map tab names to Qt standard pixmaps
        icon_map = {
            "Overview": QStyle.StandardPixmap.SP_ComputerIcon,
            "Reconciliation": QStyle.StandardPixmap.SP_DialogApplyButton,
            "Transactions": QStyle.StandardPixmap.SP_ArrowForward,
            "Add/Edit": QStyle.StandardPixmap.SP_FileDialogNewFolder,
            "Budgets": QStyle.StandardPixmap.SP_DialogSaveButton,
            "Charts": QStyle.StandardPixmap.SP_FileDialogDetailedView,
            "Reports": QStyle.StandardPixmap.SP_FileDialogContentsView,
            "Accounts": QStyle.StandardPixmap.SP_DirIcon,
            "Balance Sheet": QStyle.StandardPixmap.SP_FileDialogInfoView,
            "Category Rules": QStyle.StandardPixmap.SP_ArrowDown,
            "Taxes": QStyle.StandardPixmap.SP_FileDialogListView,
            "Mobile Sync": QStyle.StandardPixmap.SP_BrowserReload,
            "Logs": QStyle.StandardPixmap.SP_FileDialogDetailedView,
            "Audit Log": QStyle.StandardPixmap.SP_DialogResetButton,
            "Settings": QStyle.StandardPixmap.SP_FileDialogDetailedView
        }

        app = QApplication.instance()
        style = app.style() if app else None

        for name, pixmap_type in icon_map.items():
            if style:
                icon = style.standardIcon(pixmap_type)
                icons[name] = icon

        return icons

    def _on_button_clicked(self, index: int):
        """Handle button click."""
        for i, btn in enumerate(self.buttons):
            if i == index:
                btn.setChecked(True)
            else:
                btn.setChecked(False)
            btn._update_style()
        self.tab_changed.emit(index)

    def toggle_collapse(self):
        """Toggle sidebar collapse state."""
        self.is_collapsed = not self.is_collapsed

        if self.is_collapsed:
            self.setFixedWidth(60)
            self.collapse_btn.setIcon(self._create_collapse_icon(True))
            self.app_label.setVisible(False)
            self.footer_label.setVisible(False)
            for btn in self.buttons:
                btn.set_collapsed(True)
        else:
            self.setFixedWidth(220)
            self.collapse_btn.setIcon(self._create_collapse_icon(False))
            self.app_label.setVisible(True)
            self.footer_label.setVisible(True)
            for btn in self.buttons:
                btn.set_collapsed(False)
        self._sync_headings()

    def apply_entries(self, entries):
        """Show these screens, in this order, and hide every other button.

        ``entries`` is an ordered list of (stack_index, label, group,
        heading). Button i always opens stack page i, exactly as before;
        only visibility, order and label change. A heading (or a thin
        divider when the heading is empty) is drawn wherever the group
        changes.
        """
        for widget in getattr(self, "_group_widgets", []):
            self.nav_layout.removeWidget(widget)
            widget.deleteLater()
        self._group_widgets = []
        self._heading_labels = []

        for btn in self.buttons:
            self.nav_layout.removeWidget(btn)
            btn.setVisible(False)

        position = 0
        last_group = None
        more_toggle = None
        more_members = []

        for stack_index, label, group, heading in entries:
            # Insert a separator whenever the group changes.
            if last_group is not None and group != last_group:
                if group == "more" and more_toggle is None:
                    # Count how many entries belong to the "more" group.
                    more_count = sum(
                        1 for _, _, g, _ in entries if g == "more"
                    )
                    toggle = self._make_more_toggle(more_count)
                    self.nav_layout.insertWidget(position, toggle)
                    self._group_widgets.append(toggle)
                    more_toggle = toggle
                    position += 1
                elif heading:
                    spacer = QLabel(heading.upper())
                    spacer.setStyleSheet(
                        "color: #9CA3AF; font-size: 10px; font-weight: bold; "
                        "padding: 10px 16px 2px 16px;")
                    spacer.setVisible(not self.is_collapsed)
                    self._heading_labels.append(spacer)
                    self.nav_layout.insertWidget(position, spacer)
                    self._group_widgets.append(spacer)
                    position += 1
                else:
                    spacer = QFrame()
                    spacer.setFrameShape(QFrame.Shape.HLine)
                    spacer.setStyleSheet("color: #E5E7EB; margin: 6px 12px;")
                    self.nav_layout.insertWidget(position, spacer)
                    self._group_widgets.append(spacer)
                    position += 1

            last_group = group
            btn = self.buttons[stack_index]
            btn.set_label(label)
            self.nav_layout.insertWidget(position, btn)
            btn.setVisible(True)
            if group == "more":
                btn.setVisible(False)      # hidden until the toggle is checked
                more_members.append(btn)
            position += 1

        if more_toggle is not None:
            def _toggle(checked, members=more_members, t=more_toggle):
                for b in members:
                    b.setVisible(checked)
                t.setText(
                    f"More ({len(members)})  "
                    f"{'\u25b4' if checked else '\u25be'}"
                )
            more_toggle.toggled.connect(_toggle)

    def _sync_headings(self):
        for label in getattr(self, "_heading_labels", []):
            label.setVisible(not self.is_collapsed)
        for widget in getattr(self, "_group_widgets", []):
            if isinstance(widget, QToolButton):
                widget.setVisible(not self.is_collapsed)

    def visible_stack_indexes(self):
        return [i for i, b in enumerate(self.buttons) if b.isVisibleTo(self)]

    def set_current_index(self, index: int):
        """Programmatically set the current tab."""
        if 0 <= index < len(self.buttons):
            self._on_button_clicked(index)

class BudgetApp(QMainWindow):
    def __init__(self, db_path: str, dek_hex: str, target_width: Optional[int] = None, target_height: Optional[int] = None):
        super().__init__()
        self.setWindowTitle("CogniFinance")
        base_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(base_dir, "icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.gl = GeneralLedger(db_path, dek_hex)
        # Simple Mode: presentation policy loaded before any tab is
        # built so the first render of every tab is in the right mode.
        self.is_simple_mode = simple_mode.load_simple_mode()
        self._tx_table_id_map = {}
        self.db_dir = os.path.dirname(self.gl.db_path)
        self._default_width = target_width
        self._default_height = target_height
        store_path = NOTIFICATIONS_PATH
        self.notifications = NotificationCenter(NotificationStore(store_path))
        self.notifications.clear_resolved()
        self.backup_manager = BackupManager(self.db_dir)
        self.import_manager = ImportManager(self.gl, self)
        self.import_manager.data_imported.connect(self._refresh_all_data)

        avail = (self.screen() or QApplication.primaryScreen()).availableGeometry()
        w = target_width or 1200
        h = target_height or 750
        self.resize(min(w, int(avail.width() * 0.95)),
                    min(h, int(avail.height() * 0.95)))
        self.setMinimumSize(min(900, avail.width()), min(600, avail.height()))

        # Create central widget with horizontal layout
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Define tab names
        tab_names = [
            "Overview", "Reconciliation", "Transactions", "Add/Edit",
            "Budgets", "Recurring", "Investments", "Charts", "Reports", "Accounts", "Balance Sheet",
            "Category Rules", "Taxes", "Mobile Sync", "Logs", "Audit Log", "Settings",
            "Simple Home",   # appended last so every earlier index is unchanged
            "Simple Money",
            "Plans",
            "Simple Reports",
        ]
        self._tab_names = list(tab_names)

        # Create sidebar
        self.sidebar = CollapsibleSidebar(tab_names)
        self.sidebar.tab_changed.connect(self._on_tab_changed)
        root.addWidget(self.sidebar)

        # Create stacked widget for pages
        self.stacked_widget = ShrinkableStack()
        # self.stacked_widget.setStyleSheet("background-color: white;")
        # A one-line note above the pages, used in Simple Mode when a
        # full Pro screen is standing in for a simpler one.
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        self.mode_banner = QLabel("")
        self.mode_banner.setWordWrap(True)
        self.mode_banner.setStyleSheet(
            "color: #6B7280; font-size: 11px; padding: 6px 12px;")
        self.mode_banner.setVisible(False)
        content_layout.addWidget(self.mode_banner)
        content_layout.addWidget(self.stacked_widget)
        root.addWidget(content, 1)

        # Create all tabs
        self.tab_overview = QWidget()
        self.tab_reconcile = ReconcileTab(self.gl)
        self.tab_transactions = TransactionsTab(self.gl)
        self.tab_transactions2 = AddEditTab(self.gl)
        self.tab_budgets = BudgetsTab(self.gl)
        self.tab_recurring = RecurringTab(self.gl)
        self.tab_investments = InvestmentsAndCryptoTab(self.gl)
        self.tab_charts = ChartsTab(self.gl)
        self.tab_reports = ReportsTab(self.gl)
        self.tab_accounts = QWidget()
        self.tab_balance = QWidget()
        self.tab_rules = CategoryRulesTab(self.gl)
        self.tab_taxes = TaxTab(self.gl)
        self.tab_sync = SyncTab(self.gl)
        self.tab_log = LogTab()
        self.tab_audit = AuditTab(db_path=self.gl.db_path, dek_hex=self.gl.dek_hex)
        self.tab_settings = SettingsTab(gl=self.gl)
        self.tab_simple_home = SimpleHomeTab(self.gl)
        # Money brings in files through the same ImportManager Pro uses. The
        # lambdas look it up when clicked, and pass what Pro passes when its
        # bank picker is left on "Auto Detect".
        self.tab_simple_money = SimpleMoneyTab(self.gl, import_actions={
            "Bank file (OFX / QFX)": lambda: self.import_manager.start_ofx_import(),
            "Statement (PDF)": lambda: self.import_manager.start_pdf_import(None, "auto"),
            "Ledger CSV": lambda: self.import_manager.import_ledger_csv(),
        })
        self.tab_simple_plans = SimplePlansTab(self.gl)
        self.tab_simple_reports = SimpleReportsTab(self.gl)

        self.tab_settings.theme_changed.connect(self._apply_theme)  # Connect settings signals
        self._load_saved_theme() # Load saved theme on startup

        # Add all tabs to stacked widget
        self.stacked_widget.addWidget(self.tab_overview)
        self.stacked_widget.addWidget(self.tab_reconcile)
        self.stacked_widget.addWidget(self.tab_transactions)
        self.stacked_widget.addWidget(self.tab_transactions2)
        self.stacked_widget.addWidget(self.tab_budgets)
        self.stacked_widget.addWidget(self.tab_recurring)
        self.stacked_widget.addWidget(self.tab_investments)
        self.stacked_widget.addWidget(self.tab_charts)
        self.stacked_widget.addWidget(self.tab_reports)
        self.stacked_widget.addWidget(self.tab_accounts)
        self.stacked_widget.addWidget(self.tab_balance)
        self.stacked_widget.addWidget(self.tab_rules)
        self.stacked_widget.addWidget(self.tab_taxes)
        self.stacked_widget.addWidget(self.tab_sync)
        self.stacked_widget.addWidget(self.tab_log)
        self.stacked_widget.addWidget(self.tab_audit)
        self.stacked_widget.addWidget(self.tab_settings)
        self.stacked_widget.addWidget(self.tab_simple_home)   # index 17: after every Pro screen
        self.stacked_widget.addWidget(self.tab_simple_money)  # index 18
        self.stacked_widget.addWidget(self.tab_simple_plans)  # index 19
        self.stacked_widget.addWidget(self.tab_simple_reports)  # index 20

        # Initialize individual tabs
        self._init_overview()
        self._init_accounts()
        self._init_balance_sheet()

        logging.getLogger().addHandler(self.tab_log.log_handler)

        self._closing = False

        # Setup test manager
        base_dir = os.path.dirname(os.path.abspath(__file__))
        test_dir = os.path.join(base_dir, "test")
        req_path = os.path.join(base_dir, "requirements.txt")

        self.test_manager = TestRunnerManager(self.db_dir, test_dir, req_path, self.gl.dek_hex, self)
        self.test_manager.log_output.connect(self._log_audit_message)
        self.test_manager.audit_completed.connect(self._on_audit_completed)
        self.test_manager.supply_chain_warning.connect(self._handle_supply_chain_warning)

        QTimer.singleShot(5000, self._start_audit_sequence)

        # Setup backup timer
        self.backup_timer = QTimer(self)
        self.backup_timer.timeout.connect(lambda: self._create_safety_backup("periodic"))
        self.backup_timer.start(4 * 60 * 60 * 1000)

        # Connect signals
        self.tab_transactions2.history_requested.connect(self.tab_audit.show_history_for_tx_id)
        self.tab_transactions2.data_modified.connect(self._refresh_all_data)
        self.tab_simple_home.data_modified.connect(self._refresh_all_data)
        self.tab_simple_money.data_modified.connect(self._refresh_all_data)
        self.tab_simple_plans.data_modified.connect(self._refresh_all_data)
        self.tab_simple_home.navigate_requested.connect(self._navigate_to_target)
        self.tab_sync.data_synced.connect(self._refresh_all_data)
        self.tab_budgets.data_modified.connect(self.refresh_overview)
        self.tab_investments.data_modified.connect(self._refresh_all_data)
        self.tab_settings.simple_mode_changed.connect(self._apply_simple_mode)
        if hasattr(self.tab_settings, 'show_reconcile_check'):
            self.tab_settings.show_reconcile_check.toggled.connect(self._apply_sidebar_for_mode)

        # Push the mode into every participating tab.
        self._audit_simple_mode_registration()
        self._apply_simple_mode(self.is_simple_mode, persist=False)

    # ------------------------------------------------------------------
    # Simple Mode (app-wide presentation policy)
    # ------------------------------------------------------------------

    def _mode_aware_tabs(self):
        return [
            self.tab_overview, self.tab_reconcile, self.tab_transactions,
            self.tab_transactions2, self.tab_budgets, self.tab_recurring,
            self.tab_investments, self.tab_charts, self.tab_reports,
            self.tab_accounts, self.tab_balance, self.tab_rules,
            self.tab_taxes, self.tab_sync, self.tab_log, self.tab_audit,
            self.tab_settings,
        ]

    def _audit_simple_mode_registration(self):
        for name in shell_config.PRO_TABS:    # Simple-only screens have no Pro behaviour to declare
            if name not in simple_mode.TAB_MODE_REGISTRY:
                logger.error(
                    f"Simple Mode: tab '{name}' is not registered in "
                    "simple_mode.TAB_MODE_REGISTRY. It will ignore Simple Mode."
                )

    def _apply_simple_mode(self, simple: bool, persist: bool = True):
        self.is_simple_mode = bool(simple)
        if persist:
            try:
                simple_mode.save_simple_mode(self.is_simple_mode)
            except Exception as e:
                logger.warning(f"Could not persist simple mode: {e}")
        for tab in self._mode_aware_tabs():
            setter = getattr(tab, "set_simple_mode", None)
            if callable(setter):
                setter(self.is_simple_mode)
        self._apply_sidebar_for_mode()
        # Explicitly land on the mode's home screen when the mode is set.
        home = shell_config.HOME_TARGET["simple" if self.is_simple_mode else "pro"]
        index_of = {name: i for i, name in enumerate(self._tab_names)}
        self.sidebar.set_current_index(index_of[home])
        self._refresh_current_tab(self.stacked_widget.currentIndex())

    def _show_advanced_screens(self) -> bool:
        """Whether Simple Mode should list the advanced screens.

        Reads the Settings checkbox. Until that checkbox is renamed
        ("Show advanced screens"), the existing Show Reconciliation
        checkbox plays the same role.
        """
        check = (getattr(self.tab_settings, 'show_advanced_check', None)
                 or getattr(self.tab_settings, 'show_reconcile_check', None))
        return bool(check is not None and check.isChecked())

    def _apply_sidebar_for_mode(self, *_ignored):
        """Point the sidebar at the right screens for the current mode.

        Every screen already exists in the stacked widget; this only
        decides which sidebar entries show, in what order, with what
        label. Pro shows all 17 as before. Nothing is created or
        destroyed, so no signal needs rewiring.
        """
        simple = self.is_simple_mode
        entries = shell_config.sidebar_entries(simple, self._show_advanced_screens())
        index_of = {name: i for i, name in enumerate(self._tab_names)}

        payload = []
        for entry in entries:
            idx = index_of.get(entry.target)
            if idx is None:
                logger.error(f"Sidebar entry '{entry.label}' targets unknown screen "
                             f"'{entry.target}'; skipped.")
                continue
            heading = shell_config.GROUP_HEADINGS.get(entry.group, "") if simple else ""
            payload.append((idx, entry.label, entry.group, heading))
        self.sidebar.apply_entries(payload)
        if hasattr(self, 'tab_simple_home'):
            # Only offer a "See all" link whose screen is in this sidebar.
            self.tab_simple_home.set_navigable_targets({e.target for e in entries})

        shown = {item[0] for item in payload}
        current = self.stacked_widget.currentIndex()
        if current not in shown:
            home = shell_config.HOME_TARGET["simple" if simple else "pro"]
            self.sidebar.set_current_index(index_of[home])
        self._update_mode_banner()

    def _navigate_to_target(self, target: str):
        """Open a screen by name (used by Home's "See all" links)."""
        try:
            index = self._tab_names.index(target)
        except ValueError:
            logger.error(f"Cannot navigate: unknown screen '{target}'")
            return
        self.sidebar.set_current_index(index)

    def _update_mode_banner(self):
        """Show the placeholder note when a full Pro screen stands in for a
        simpler one, and nothing otherwise."""
        entry = None
        if self.is_simple_mode:
            current = self.stacked_widget.currentIndex()
            if 0 <= current < len(self._tab_names):
                entry = shell_config.entry_for_target(
                    True, self._show_advanced_screens(), self._tab_names[current])
        if entry is not None and entry.placeholder and entry.note:
            self.mode_banner.setText(entry.note)
            self.mode_banner.setVisible(True)
        else:
            self.mode_banner.setVisible(False)

    def _on_tab_changed(self, index: int):
        """Handle tab change from sidebar."""
        self.stacked_widget.setCurrentIndex(index)
        widget = self.stacked_widget.widget(index)
        if widget is not None and widget.layout() is not None:
            widget.layout().activate()
        self._update_mode_banner()

        # Refresh the current tab if needed
        self._refresh_current_tab(index)

    def _apply_theme(self, mode: str):
        """Applies the selected theme to the entire application."""
        app = QApplication.instance()

        if mode == "dark":
            self.is_dark_mode = True
            app.setStyleSheet(DARK_MODE_QSS)
        else:
            self.is_dark_mode = False
            app.setStyleSheet(LIGHT_MODE_QSS)

        # Notify the sidebar to update its custom colors
        if hasattr(self, 'sidebar'):
            self.sidebar.apply_theme(self.is_dark_mode)

        # Notify HTML-rendering tabs to update their inline colors
        if hasattr(self, 'tab_taxes'):
            self.tab_taxes.set_dark_mode(self.is_dark_mode)
        if hasattr(self, 'tab_reconcile'):
            self.tab_reconcile.set_dark_mode(self.is_dark_mode)
        if hasattr(self, 'overview_text'):
            self.refresh_overview()
        if hasattr(self, 'balance_text'):
            self.refresh_balance_sheet()
        if hasattr(self, 'tab_charts'):
            self.tab_charts.set_dark_mode(self.is_dark_mode)
        if hasattr(self, 'tab_simple_home'):
            self.tab_simple_home.set_dark_mode(self.is_dark_mode)
        if hasattr(self, 'tab_simple_money'):
            self.tab_simple_money.set_dark_mode(self.is_dark_mode)
        if hasattr(self, 'tab_simple_plans'):
            self.tab_simple_plans.set_dark_mode(self.is_dark_mode)
        if hasattr(self, 'tab_simple_reports'):
            self.tab_simple_reports.set_dark_mode(self.is_dark_mode)

        # Notify the notification bell so the popup panel matches the theme
        if hasattr(self, 'notification_bell'):
            self.notification_bell.set_dark_mode(self.is_dark_mode)
            if self.notification_bell._panel is not None:
                self.notification_bell._panel.set_dark_mode(self.is_dark_mode)

        self._save_theme_preference(mode)

    def _save_theme_preference(self, mode: str):
        """Saves the theme preference to a local JSON file."""
        config_path = str(UI_CONFIG_PATH)
        try:
            config = {}
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
            config["theme"] = mode
            with open(config_path, 'w') as f:
                json.dump(config, f)
        except Exception as e:
            logging.warning(f"Failed to save UI config: {e}")

    def _load_saved_theme(self):
        """Loads the theme preference on startup."""
        config_path = str(UI_CONFIG_PATH)
        mode = "light"

        if os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    mode = config.get("theme", "light")
            except Exception:
                pass

        if mode == "dark":
            self.tab_settings.theme_combo.setCurrentText("Dark Mode")
        elif mode == "system":
            self.tab_settings.theme_combo.setCurrentText("System Default")
        else:
            self.tab_settings.theme_combo.setCurrentText("Light Teal")

        self._apply_theme(mode)

    def safe_trigger_audit(self):
        """Safely initiates scheduled integrity checks."""
        self._start_audit_sequence()

    def _create_safety_backup(self, trigger: str = "manual"):
        """Bridge interface for timed/event-based silent backups."""
        self._run_silent_auto_backup(trigger=trigger)

    def _start_audit_sequence(self):
        """Wrapper to show the UI notification before kicking off the manager."""
        if getattr(self.tab_sync, 'sync_server', None) is not None:
            QTimer.singleShot(30000, self.safe_trigger_audit)
            self.statusBar().showMessage("Integrity audit deferred: Mobile sync currently active.", 4000)
            return

        self.tab_sync.set_start_button_enabled(False)
        self.notifications.add(
            Severity.INFO,
            "Running Background Integrity Checks",
            "CogniFinance is verifying your ledger's math and security in the background. "
            "Program might experience slow down during tests.",
            "integrity_check_running"
        )
        self.test_manager.start_scheduled_audit()

    def _handle_supply_chain_warning(self, total: int, critical: int, details: list):
        """Routes pip-audit findings to the Notification Center."""
        if critical > 0:
            severity = Severity.CRITICAL
            title = "Critical Supply Chain Vulnerabilities Detected"
            msg = (f"{critical} dependencies have critical vulnerabilities with no available patch. "
                f"({total} total vulnerabilities found). Please review the Log Tab and update dependencies immediately.")
        else:
            severity = Severity.WARNING
            title = "Dependency Vulnerabilities Found"
            msg = f"{total} dependencies have known vulnerabilities. Please run 'pip-audit' and update your requirements.txt."
        self.notifications.add(severity, title, msg, "supply_chain_audit")

    def _log_audit_message(self, message: str):
        """Routes the test output directly into the Log Tab."""
        if not message.strip():
            return

        if message.startswith(("platform ", "rootdir:", "configfile:", "plugins:", "cache_dir:")):
            return

        if message.startswith("collected "):
            return

        if message.startswith("===") and not any(k in message.lower() for k in ["passed", "failed", "error"]):
            return

        if any(keyword in message for keyword in ["FAILED", "ERROR", "CRITICAL", "❌", "___"]):
            logging.error(message)
        else:
            logging.info(message)

    def _on_audit_completed(self, success: bool, message: str):
        """Called when the entire audit sequence (Weekly + Deep) finishes."""
        self.notifications.resolve("integrity_check_running")
        self.tab_sync.set_start_button_enabled(True)

        if success:
            if "No audits currently due" not in message:
                self.notifications.resolve("integrity_check_complete")
                self.notifications.add(
                    Severity.INFO,
                    "Integrity Checks Complete",
                    message,
                    "integrity_check_complete"
                )
            self._log_audit_message(f"✅ {message}")
            self.notifications.resolve("weekly_integrity_check")
            self.notifications.resolve("deep_integrity_check")
        else:
            self.notifications.add(
                Severity.CRITICAL,
                "Integrity Check Failed",
                message,
                "integrity_check_failed"
            )
            self._log_audit_message(f"❌ {message}")
            QMessageBox.critical(
                self,
                "CRITICAL LEDGER FAILURE",
                f"The background integrity check FAILED.\n\n{message}\n\n"
                "Please open the 'Logs' tab to see exactly which invariants were violated."
            )

    def _refresh_current_tab(self, index: int):
        """Refresh only tabs whose primary purpose is displaying ledger data."""
        refreshers = {
            self.tab_overview: self.refresh_overview,
            self.tab_accounts: self.refresh_accounts_table,
            self.tab_transactions: self.tab_transactions.refresh_data,
            self.tab_transactions2: self.tab_transactions2.refresh_data,
            self.tab_budgets: self.tab_budgets.refresh_budget_list,
            self.tab_recurring: self.tab_recurring.refresh_tables,
            self.tab_charts: self.tab_charts.refresh_charts,
            self.tab_reports: self.tab_reports.refresh_data,
            self.tab_reconcile: self.tab_reconcile.refresh_data,
            self.tab_balance: self.refresh_balance_sheet,
            self.tab_taxes: self.tab_taxes.refresh_summary,
            self.tab_audit: self.tab_audit.refresh,
            self.tab_simple_home: self.tab_simple_home.refresh,
            self.tab_simple_money: self.tab_simple_money.refresh,
            self.tab_simple_plans: self.tab_simple_plans.refresh,
            self.tab_simple_reports: self.tab_simple_reports.refresh,
        }

        widget = self.stacked_widget.widget(index)
        refresher = refreshers.get(widget)

        if refresher is not None:
            refresher()

        if widget is not None:
            for table in widget.findChildren(QTableWidget):
                fit_table_columns(table)

    def _refresh_all_data(self):
        """Master refresh triggered when data is modified in a tab."""
        self.tab_transactions.refresh_data()
        self.refresh_overview()
        self.refresh_accounts_table()
        if hasattr(self, 'tab_budgets'):
            self.tab_budgets.refresh_budget_list()
        if self.is_simple_mode and hasattr(self, 'tab_simple_home'):
            self.tab_simple_home.refresh()
        if self.is_simple_mode and hasattr(self, 'tab_simple_money'):
            self.tab_simple_money.refresh()
        if self.is_simple_mode and hasattr(self, 'tab_simple_plans'):
            self.tab_simple_plans.refresh()

    def showEvent(self, event):
        """Clamp window to screen bounds after realization to prevent Windows overflow."""
        super().showEvent(event)
        try:
            screen = QGuiApplication.primaryScreen().availableGeometry()
            w, h = self.width(), self.height()
            if w > screen.width() or h > screen.height():
                self.centralWidget().setMinimumSize(0, 0)
                self.resize(min(w, int(screen.width())), min(h, int(screen.height())))
        except Exception:
            pass

    def _position_notification_bell(self):
        """Pins the notification bell to the top-right corner of the HTML area."""
        if not hasattr(self, 'overview_text') or not hasattr(self, 'notification_bell'):
            return

        padding = 15
        x = self.overview_text.width() - self.notification_bell.width() - padding
        y = padding

        self.notification_bell.move(x, y)
        self.notification_bell.raise_()

    def eventFilter(self, obj, event):
        """Intercepts resize events to keep the bell pinned to the corner."""
        if obj == getattr(self, 'overview_text', None) and event.type() == QEvent.Type.Resize:
            self._position_notification_bell()

        return super().eventFilter(obj, event)

    def _init_accounts(self):
        layout = QVBoxLayout(self.tab_accounts)

        info = QLabel("Manage your Chart of Accounts. Categories used in CogniCapture and Budgets are 'Expense' accounts.")
        info.setWordWrap(True)
        layout.addWidget(info)

        accounts_box = QGroupBox("Chart of Accounts")
        box_layout = QVBoxLayout(accounts_box)

        self.accounts_table = QTableWidget()
        self.accounts_table.setColumnCount(2)
        self.accounts_table.setHorizontalHeaderLabels(["Account Name", "Type"])
        setup_table_headers(self.accounts_table, [0.7, 0.3])
        box_layout.addWidget(self.accounts_table)

        btn_row = QHBoxLayout()
        btn_add = QPushButton("Add New Account")
        btn_add.clicked.connect(self.add_new_account)
        btn_edit = QPushButton("Edit Selected")
        btn_edit.clicked.connect(self.edit_selected_account)
        btn_delete = QPushButton("Delete Selected")
        btn_delete.clicked.connect(self.delete_selected_account)

        btn_row.addWidget(btn_add)
        btn_row.addWidget(btn_edit)
        btn_row.addWidget(btn_delete)
        btn_row.addStretch()
        box_layout.addLayout(btn_row)

        layout.addWidget(accounts_box)

        self.refresh_accounts_table()

    def refresh_accounts_table(self):
        self.accounts_table.setRowCount(len(self.gl.accounts))
        for row, (name, acc) in enumerate(sorted(self.gl.accounts.items())):
            self.accounts_table.setItem(row, 0, QTableWidgetItem(name))
            self.accounts_table.setItem(row, 1, QTableWidgetItem(acc.account_type))

    def add_new_account(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Add New Account")
        layout = QVBoxLayout(dialog)

        form = QFormLayout()
        name_edit = QLineEdit()
        type_combo = QComboBox()
        for t in ["Asset", "Liability", "Equity", "Revenue", "Expense"]:
            type_combo.addItem(t)
        type_combo.setCurrentText("Expense")

        form.addRow("Account Name:", name_edit)
        form.addRow("Account Type:", type_combo)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            name = name_edit.text().strip()
            acc_type = type_combo.currentText()
            if not name:
                QMessageBox.warning(self, "Error", "Account name cannot be empty.")
                return
            ok, msg = self.gl.create_account(name, acc_type)
            if ok:
                self.refresh_accounts_table()
                self.refresh_account_combos()
                QMessageBox.information(self, "Success", msg)
            else:
                QMessageBox.warning(self, "Error", msg)

    def delete_selected_account(self):
        selected = self.accounts_table.selectedItems()
        if not selected:
            QMessageBox.information(self, "No selection", "Please select an account to delete.")
            return
        name = selected[0].text()
        ok, msg = self.gl.delete_account(name)
        if ok:
            self.refresh_accounts_table()
            self.refresh_account_combos()
            QMessageBox.information(self, "Success", msg)
        else:
            QMessageBox.warning(self, "Error", msg)

    def edit_selected_account(self):
        selected = self.accounts_table.selectedItems()
        if not selected:
            QMessageBox.information(self, "No selection", "Please select an account to edit.")
            return

        old_name = selected[0].text()
        acc = self.gl.accounts.get(old_name)
        if not acc:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Edit Account")
        layout = QVBoxLayout(dialog)

        form = QFormLayout()
        name_edit = QLineEdit(old_name)
        type_combo = QComboBox()
        for t in ["Asset", "Liability", "Equity", "Revenue", "Expense"]:
            type_combo.addItem(t)
        type_combo.setCurrentText(acc.account_type)

        form.addRow("Account Name:", name_edit)
        form.addRow("Account Type:", type_combo)
        layout.addLayout(form)

        warning = QLabel("<b>Note:</b> Changing the name will automatically update all historical transactions, budgets, and rules associated with this account.")
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #d68910;")
        layout.addWidget(warning)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_name = name_edit.text().strip()
            new_type = type_combo.currentText()

            if not new_name:
                QMessageBox.warning(self, "Error", "Account name cannot be empty.")
                return

            ok, msg = self.gl.update_account(old_name, new_name, new_type)
            if ok:
                self.refresh_accounts_table()
                self.refresh_account_combos()
                self.tab_rules.refresh_rules_table()
                QMessageBox.information(self, "Success", msg)
            else:
                QMessageBox.warning(self, "Error", msg)

    def _init_overview(self):
        layout = QVBoxLayout(self.tab_overview)
        self.overview_text = QTextEdit()
        self.overview_text.setReadOnly(True)
        self.overview_text.setFont(QFont("Monospace", 10))

        self.notification_bell = NotificationBell(self.notifications, self)
        self.notification_bell.set_dark_mode(self.is_dark_mode)
        self.notification_bell.setParent(self.overview_text)

        self.overview_text.installEventFilter(self)
        self._position_notification_bell()
        self.notification_bell.raise_()
        self.notification_bell.show()

        layout.addWidget(self.overview_text)
        add_refresh_row(layout, self.refresh_overview)

        btn_row = QHBoxLayout()
        self.btn_import_pdf = QPushButton("Import Statement (PDF)")
        self.btn_import_pdf.clicked.connect(lambda: self.import_manager.start_pdf_import(
            self.import_bank.currentData(),
            self.import_statement_type.currentData() or "auto"
        ))

        statement_row = QHBoxLayout()
        statement_row.addWidget(QLabel("Bank:"))
        self.import_bank = QComboBox()
        self.import_bank.addItem("Auto Detect", None)
        self.import_bank.addItem("CIBC", "cibc")
        self.import_bank.addItem("Tangerine", "tangerine")
        statement_row.addWidget(self.import_bank)

        statement_row.addWidget(QLabel("Statement Type:"))
        self.import_statement_type = QComboBox()
        self.import_statement_type.addItem("Auto Detect", "auto")
        self.import_statement_type.addItem("Credit Card", "credit_card")
        self.import_statement_type.addItem("Bank Account", "bank")
        statement_row.addWidget(self.import_statement_type)

        statement_row.addStretch()
        layout.addLayout(statement_row)

        btn_export = QPushButton("Export Ledger (CSV)")
        btn_export.clicked.connect(self.import_manager.export_ledger_csv)

        btn_import = QPushButton("Import Ledger (CSV)")
        btn_import.clicked.connect(self.import_manager.import_ledger_csv)

        btn_import_ofx = QPushButton("Import OFX/QFX (Bank Feed)")
        btn_import_ofx.clicked.connect(self.import_manager.start_ofx_import)

        btn_manual_backup = QPushButton("Manual Backup to External Drive")
        btn_manual_backup.clicked.connect(self.manual_external_backup)

        btn_row.addWidget(self.btn_import_pdf)
        btn_row.addWidget(btn_import_ofx)
        btn_row.addWidget(btn_import)
        btn_row.addWidget(btn_export)
        btn_row.addWidget(btn_manual_backup)

        btn_restore = QPushButton("Restore from Backup")
        btn_restore.clicked.connect(self.restore_from_backup)
        btn_restore.setStyleSheet("color: #c62828; font-weight: bold;")
        btn_row.addWidget(btn_restore)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.refresh_overview()

    def refresh_overview(self):
        today = QDate.currentDate()
        month = today.toString("yyyy-MM")

        # 1. Fetch Data
        report = self.gl.monthly_income_expense_report(today.year(), today.month())
        budgets = [b for b in self.gl.get_all_budgets() if b.month == month]
        spending = self.gl.spending_by_category(month)
        unreconciled_count = len(self.gl.get_unreconciled_transactions())

        # 2. Prepare Recent Transactions (Pre-calculate display amounts)
        recent_txs_data = []
        for tx in list(reversed(self.gl.transactions[-10:])):
            recent_txs_data.append({
                "date": tx.date,
                "description": tx.description,
                "amount": get_transaction_display_amount(self.gl, tx)
            })

        # 3. Prepare Backup Snippet (Kept in UI layer as it touches BackupManager)
        backup_color, backup_msg = self.backup_manager.get_backup_health(len(self.gl.transactions))
        backup_html_snippet = f'<font color="{backup_color}" size="4">●</font>&nbsp;&nbsp;{backup_msg}'

        # 4. Render and Display
        html = html_renderers.render_overview_html(
            today_str=today.toString('MMMM yyyy'),
            month_str=month,
            report=report,
            budgets=budgets,
            spending=spending,
            recent_txs=recent_txs_data,
            unreconciled_count=unreconciled_count,
            backup_html_snippet=backup_html_snippet
        )

        # INJECT DARK MODE CSS IF ACTIVE
        if getattr(self, 'is_dark_mode', False):
            dark_css = """
            <style>
                body, html { background-color: #111318 !important; color: #F3F4F6 !important; }
                table, td, th, p, h1, h2, h3, h4, span, div, li { color: #F3F4F6 !important; background-color: #111318 !important; }
                a { color: #10B981 !important; }
                .header, .title, h1, h2 { color: #10B981 !important; }
            </style>
            """
            html = dark_css + html

        self.overview_text.setHtml(html)
        # Delegate dropdown refreshes to the respective tabs
        if hasattr(self, 'tab_rules') and hasattr(self.tab_rules, '_populate_category_combo'):
            self.tab_rules._populate_category_combo()
        if hasattr(self, 'tab_transactions') and hasattr(self.tab_transactions, 'refresh_account_dropdown'):
            self.tab_transactions.refresh_account_dropdown()
        if hasattr(self, 'tab_budgets') and hasattr(self.tab_budgets, 'refresh_account_dropdown'):
            self.tab_budgets.refresh_account_dropdown()
        if hasattr(self, 'tab_transactions2') and hasattr(self.tab_transactions2, 'refresh_account_dropdown'):
            self.tab_transactions2.refresh_account_dropdown()

    def _init_balance_sheet(self):
        layout = QVBoxLayout(self.tab_balance)
        add_refresh_row(layout, self.refresh_balance_sheet)

        self.balance_text = QTextEdit()
        self.balance_text.setReadOnly(True)
        self.balance_text.setFont(QFont("Monospace", 10))
        layout.addWidget(self.balance_text)

        self.refresh_balance_sheet()

    def refresh_balance_sheet(self):
        # 1. Fetch both the accounting totals and the presentation grouping.
        bs = self.gl.balance_sheet()
        grouped = self.gl.get_accounts_grouped_for_report()

        # 2. Render. simple_mode is False today; when the mode flag lands,
        # replace it with self.is_simple_mode (or wherever the flag lives).
        html = html_renderers.render_balance_sheet_html(
            bs,
            grouped=grouped,
            simple_mode=getattr(self, 'is_simple_mode', False),
        )

        # 3. Dark-mode CSS injection -- keep exactly as it is today.
        if getattr(self, 'is_dark_mode', False):
            dark_css = """
            <style>
                body, html { background-color: #111318 !important; color: #F3F4F6 !important; }
                table, td, th, p, h1, h2, h3, h4, span, div, li { color: #F3F4F6 !important; background-color: #111318 !important; }
                a { color: #10B981 !important; }
                .header, .title, h1, h2 { color: #10B981 !important; }
            </style>
            """
            html = dark_css + html

        self.balance_text.setHtml(html)

    def update_status_bar(self, percent: int, message: str):
        self.statusBar().showMessage(message)

    def _run_silent_auto_backup(self, trigger: str = "close"):
        """Creates a timestamped encrypted backup in ~/BudgetData/backups/ and prunes backups older than 30 days."""
        try:
            backup_dir = str(BACKUP_DIR)
            os.makedirs(backup_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            backup_path = os.path.join(backup_dir, f"auto_backup_{trigger}_{timestamp}.db")

            self.gl.checkpoint()
            self.gl.create_encrypted_backup(backup_path)

            cutoff_time = time.time() - (30 * 86400)
            for filename in os.listdir(backup_dir):
                file_path = os.path.join(backup_dir, filename)
                if os.path.isfile(file_path) and os.path.getmtime(file_path) < cutoff_time:
                    try:
                        os.remove(file_path)
                    except OSError:
                        pass
        except Exception as e:
            logging.warning(f"Silent auto-backup on {trigger} failed: {e}")
            self.notifications.add(
                Severity.WARNING,
                f"{trigger.replace('_', ' ').title()} Backup Failed",
                f"CogniFinance could not create a safety backup. Your data is safe, "
                f"but not backed up to disk. Error: {e}",
                "backup_failure"
            )

    def manual_external_backup(self):
        """UI wrapper for manual external backup."""
        timestamp = datetime.now().strftime("%Y-%m-%d")
        default_name = f"cognifinance_manual_backup_{timestamp}.db"

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Encrypted Backup", default_name,
            "Database Files (*.db);;All Files (*)"
        )
        if not path:
            return

        success, msg = self.backup_manager.execute_manual_backup(self.gl, path)

        if success:
            self.refresh_overview()
            QMessageBox.information(self, "Backup Successful", f"Encrypted backup saved to:\n{msg}")
        else:
            QMessageBox.critical(self, "Backup Failed", f"Could not save backup:\n{msg}")

    def restore_from_backup(self):
        """UI wrapper for database restore."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Backup to Restore", "",
            "Database Files (*.db *.cfb);;All Files (*)"
        )
        if not path:
            return

        reply = QMessageBox.warning(
            self, "Confirm Restore",
            "Restoring from a backup will OVERWRITE your current database.\n\n"
            "The app will close your current database and force a restart.\n\n"
            "WARNING: Ensure the backup was created with the SAME passphrase.\n\n"
            "Are you sure you want to continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        success, msg = self.backup_manager.execute_restore(self.gl, path)

        if success:
            QMessageBox.information(
                self, "Restore Successful",
                "The database has been successfully restored.\n\n"
                "CogniFinance will now close. Please reopen the application "
                "to load your restored data."
            )
            # Gracefully shut down background threads before quitting
            self.tab_sync.stop_sync()
            QApplication.instance().quit()
        else:
            QMessageBox.critical(self, "Restore Failed", msg)

    def closeEvent(self, event):
        self._closing = True
        try:
            self.tab_sync.stop_sync()
        except Exception:
            pass

        if hasattr(self, 'test_manager'):
            self.test_manager.cleanup()
        self._run_silent_auto_backup("close")

        try:
            self.gl.close()
        except Exception:
            pass

        # Save window geometry for the next launch
        _save_window_geometry(self.width(), self.height())

        super().closeEvent(event)

    def add_new_transaction(self):
        dlg = AddTransactionDialog(self.gl, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_date, new_desc, lines_data = dlg.get_data()
            ok, msg = self.gl.record_transaction(new_date, new_desc, lines_data)
            if not ok:
                QMessageBox.warning(self, "Transaction Not Added", msg)
                return
            self.statusBar().showMessage("Transaction added.", 3000)
            self.tab_transactions2.refresh_data()
            self.tab_transactions.refresh_data()
            self.refresh_overview()
            self.refresh_accounts_table()
