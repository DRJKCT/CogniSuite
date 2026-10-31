"""
notification_widgets.py ver 0.2
For CogniFinance ver 0.9.67

UI layer: a small always-present bell icon whose badge reflects
`unresolved_count()`, and a popover panel listing individual
notifications. Opening the panel marks items read (removes the "new"
styling) but never changes the badge count -- only NotificationCenter.resolve()
does that, and only the code that owns a given check should call it.

Dark-mode aware: call set_dark_mode() on NotificationPanel when the app
theme changes, or when the panel is first shown.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QPoint, QSize, pyqtSignal
from PyQt6.QtGui import QPainter, QColor, QFont, QPen, QIcon
from PyQt6.QtWidgets import (
    QToolButton, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QScrollArea, QSizePolicy, QPushButton,
)

from notification_center import NotificationCenter, Notification, Severity


import pathlib
from pathlib import Path

_ICONS_DIR = Path(__file__).resolve().parent / "icons"

def _icon_path(name: str) -> Path:
    """Resolve an icon file path from the app's icons directory."""
    return _ICONS_DIR / name


# ---------------------------------------------------------------------------
# Color palettes for light/dark themes
# ---------------------------------------------------------------------------

_LIGHT = {
    "panel_bg":     "#FFFFFF",
    "panel_border": "#D0D0D0",
    "row_border":   "#F0F0F0",
    "row_hover_bg": "#FAFAFA",
    "line_color":   "#E5E5E5",
    "empty_fg":     "#888888",
    "close_bg_hv":  "#e0e0e0",
    "close_fg":     "#888888",
    "msg_fg":       "#666666",
    "ts_fg":        "#AAAAAA",
}

_DARK = {
    "panel_bg":     "#1A1D24",
    "panel_border": "#2D323C",
    "row_border":   "#2D323C",
    "row_hover_bg": "#2A2F3A",
    "line_color":   "#2D323C",
    "empty_fg":     "#666666",
    "close_bg_hv":  "#3B4252",
    "close_fg":     "#9CA3AF",
    "msg_fg":       "#9CA3AF",
    "ts_fg":        "#777777",
}

_LIGHT_QSS = """
NotificationPanel { background: #FFFFFF; border: 1px solid #D0D0D0; border-radius: 6px; }
NotificationRow { border-bottom: 1px solid #F0F0F0; }
NotificationRow:hover { background: #FAFAFA; }
QFrame.line-frame { color: #E5E5E5; }
QLabel.empty-label { color: #888888; font-size: 12px; }
"""

_DARK_QSS = """
NotificationPanel { background: #1A1D24; border: 1px solid #2D323C; border-radius: 6px; }
NotificationRow { border-bottom: 1px solid #2D323C; }
NotificationRow:hover { background: #2A2F3A; }
QFrame.line-frame { color: #2D323C; }
QLabel.empty-label { color: #666666; font-size: 12px; }
"""


class NotificationBell(QToolButton):
    """Fixed-position indicator. Quiet by default; badge only appears
    when unresolved_count() > 0, so it never becomes wallpaper you stop
    noticing, but it's always in the same place so you know where to
    look."""

    def __init__(self, center: NotificationCenter, parent=None):
        super().__init__(parent)
        self._center = center
        self._dark_mode = False
        self.setIcon(QIcon(str(_icon_path("notifications.svg"))))
        self.setIconSize(QSize(28, 28))
        self.setToolTip("Notifications")
        self.setFixedSize(48, 48)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("QToolButton { border: none; }")

        self._panel: NotificationPanel | None = None
        self.clicked.connect(self._toggle_panel)
        self._center.changed.connect(self.update)  # repaint on any change

    def sizeHint(self) -> QSize:
        return QSize(48, 48)

    def set_dark_mode(self, dark: bool):
        self._dark_mode = dark

    def _update_icon(self):
        """Swap the bell icon to reflect the current severity state."""
        count = self._center.unresolved_count()
        if count == 0:
            icon_name = "notifications.svg"
        else:
            severity = self._center.highest_active_severity() or Severity.INFO
            icon_name = {
                Severity.INFO: "notifications.svg",
                Severity.WARNING: "notifications_unread.svg",
                Severity.CRITICAL: "notifications_red.svg",
            }[severity]
        self.setIcon(QIcon(str(_icon_path(icon_name))))

    def paintEvent(self, event):
        self._update_icon()
        super().paintEvent(event)
        count = self._center.unresolved_count()
        if count == 0:
            return

        severity = self._center.highest_active_severity() or Severity.INFO
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        badge_d = 20
        x = self.width() - badge_d - 4
        y = 4
        painter.setBrush(QColor(severity.color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(x, y, badge_d, badge_d)

        painter.setPen(QPen(QColor("white")))
        font = QFont()
        font.setPixelSize(12)
        font.setBold(True)
        painter.setFont(font)
        text = str(count) if count < 10 else "9+"
        painter.drawText(x, y, badge_d, badge_d, Qt.AlignmentFlag.AlignCenter, text)
        painter.end()

    def _toggle_panel(self):
        if self._panel is not None and self._panel.isVisible():
            self._panel.close()
            return
        self._panel = NotificationPanel(self._center, self)
        self._panel.set_dark_mode(self._dark_mode)
        global_pos = self.mapToGlobal(QPoint(0, self.height()))
        # Right-align the panel under the bell rather than left-align,
        # so it doesn't run off the edge of the window if the bell sits
        # near the top-right corner (the usual spot for this kind of icon).
        self._panel.move(global_pos.x() - self._panel.width() + self.width(), global_pos.y())
        self._panel.show()
        self._center.mark_all_read()


class NotificationPanel(QWidget):
    """A lightweight popup: Qt.WindowType.Popup auto-closes on any click
    outside it, so there's no manual focus-tracking to manage."""

    def __init__(self, center: NotificationCenter, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self._center = center
        self._dark_mode = False
        self.setFixedWidth(360)
        self.setStyleSheet(_LIGHT_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(12, 10, 12, 10)
        title = QLabel("Notifications")
        title.setStyleSheet("font-weight: bold; font-size: 13px;")
        header.addWidget(title)
        header.addStretch()
        clear_btn = QPushButton("Clear resolved")
        clear_btn.setFlat(True)
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.setStyleSheet("QPushButton { color: #5B8DEF; border: none; font-size: 11px; }")
        clear_btn.clicked.connect(self._clear_resolved)
        header.addWidget(clear_btn)
        outer.addLayout(header)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #E5E5E5;")
        outer.addWidget(line)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(400)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._content = QWidget()
        self._list_layout = QVBoxLayout(self._content)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(0)

        scroll.setWidget(self._content)
        outer.addWidget(scroll)

        # Connect model changes to refresh the UI view dynamically
        self._center.changed.connect(self._refresh_list)
        self._refresh_list()

    def _refresh_list(self):
        """Clears and rebuilds the notification list when data updates."""
        while self._list_layout.count():
            child = self._list_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        items = self._center.all()
        if not items:
            empty = QLabel("Nothing to report.")
            empty.setStyleSheet("color: #888; padding: 24px; font-size: 12px;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._list_layout.addWidget(empty)
        else:
            for note in items:
                row_widget = NotificationRow(note, self._dark_mode)
                row_widget.dismiss_requested.connect(self._center.resolve_by_id)
                self._list_layout.addWidget(row_widget)

        self._list_layout.addStretch()

    def set_dark_mode(self, dark: bool):
        """Apply dark or light colour palette to the panel and every row."""
        self._dark_mode = dark
        self.setStyleSheet(_DARK_QSS if dark else _LIGHT_QSS)
        # Update all existing rows
        for idx in range(self._list_layout.count()):
            child = self._list_layout.itemAt(idx)
            if child and child.widget() and isinstance(child.widget(), NotificationRow):
                child.widget().set_dark_mode(dark)

    def _clear_resolved(self):
        self._center.clear_resolved()
        self.close()


class NotificationRow(QWidget):
    dismiss_requested = pyqtSignal(str)
    def __init__(self, note: Notification, is_dark: bool = False, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._is_dark = is_dark
        self._note = note
        p = (_LIGHT, _DARK)[is_dark]
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)

        dot = QLabel()
        dot.setFixedSize(10, 10)
        color = "#8FBF8F" if note.resolved else note.severity.color
        dot.setStyleSheet(f"background: {color}; border-radius: 5px;")
        dot.setContentsMargins(0, 4, 0, 0)
        row.addWidget(dot, alignment=Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)

        title_weight = "normal" if note.read else "bold"
        status_suffix = "  \u2713 resolved" if note.resolved else ""
        title = QLabel(note.title + status_suffix)
        title.setWordWrap(True)
        title.setStyleSheet(f"font-weight: {title_weight}; font-size: 12.5px;")
        text_col.addWidget(title)

        message = QLabel(note.message)
        message.setWordWrap(True)
        message.setStyleSheet(f"color: {p['msg_fg']}; font-size: 11.5px;")
        text_col.addWidget(message)

        ts = QLabel(_relative_time(note.timestamp))
        ts.setStyleSheet(f"color: {p['ts_fg']}; font-size: 10.5px;")
        text_col.addWidget(ts)

        row.addLayout(text_col, stretch=1)

        # 1. Create the individual 'X' button for this specific row
        close_btn = QPushButton("\u2715")
        close_btn.setFixedSize(24, 24)
        close_btn.setFlat(True)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{ border: none; background: transparent; color: {p['close_fg']}; font-weight: bold; font-size: 13px; padding: 0px; margin: 0px;}}
            QPushButton:hover {{ color: #d32f2f; background: {p['close_bg_hv']}; border-radius: 12px;}}
        """)

        close_btn.clicked.connect(lambda: self.dismiss_requested.emit(note.id))

        row.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignTop)

        # Own stylesheet for hover bg + bottom border
        self._update_row_style()

    def _update_row_style(self):
        if self._is_dark:
            self.setStyleSheet(
                f"NotificationRow {{ border-bottom: 1px solid {_DARK['row_border']}; }}"
                f"NotificationRow:hover {{ background: {_DARK['row_hover_bg']}; }}"
            )
        else:
            self.setStyleSheet(
                f"NotificationRow {{ border-bottom: 1px solid {_LIGHT['row_border']}; }}"
                f"NotificationRow:hover {{ background: {_LIGHT['row_hover_bg']}; }}"
            )

    def set_dark_mode(self, dark: bool):
        self._is_dark = dark
        self._update_row_style()
        # Update children colour labels
        labels = list(self.findChildren(QLabel))
        buttons = list(self.findChildren(QPushButton))
        if len(labels) >= 3:
            # labels[2] = message, labels[3] = timestamp
            msg_lbl = labels[2]
            ts_lbl = labels[3] if len(labels) >= 4 else None
            p = (_LIGHT, _DARK)[dark]
            msg_lbl.setStyleSheet(f"color: {p['msg_fg']}; font-size: 11.5px;\nbackground: transparent;")
            if ts_lbl:
                ts_lbl.setStyleSheet(f"color: {p['ts_fg']}; font-size: 10.5px;\nbackground: transparent;")
        if buttons:
            p = (_LIGHT, _DARK)[dark]
            buttons[0].setStyleSheet(f"""
                QPushButton {{ border: none; background: transparent; color: {p['close_fg']}; font-weight: bold; font-size: 13px; padding: 0px; margin: 0px; }}
                QPushButton:hover {{ color: #d32f2f; background: {p['close_bg_hv']}; border-radius: 12px; }}
            """)


def _relative_time(iso_timestamp: str) -> str:
    from datetime import datetime, timezone
    try:
        ts = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return iso_timestamp
    delta = datetime.now(timezone.utc) - ts
    seconds = delta.total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"
