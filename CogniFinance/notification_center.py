"""
notification_center.py ver 0.1
For CogniFinance ver 1.8

Notification backend: persisted state + the read/resolved distinction.

Design principles (from the "weekly integrity test" conversation):

1. State lives on disk, not just in memory. A notification created while
   the app happens to be open, but never persisted, is exactly the kind
   of silent failure this whole feature exists to prevent. Every mutation
   here writes through to a JSON file immediately.

2. "Read" and "resolved" are different things, and only one of them is
   allowed to actually make the badge count go down:
     - `read`     = the user has looked at it. Cosmetic only (controls
                    bold/pulse styling in the UI). Set by the UI when the
                    panel is opened.
     - `resolved` = the underlying problem is actually fixed. Only ever
                    set by the CODE THAT OWNS THE CHECK (e.g. the weekly
                    integrity test calling `resolve(source=...)` the next
                    time it passes). There is deliberately no user-facing
                    "dismiss" action for an unresolved item -- you cannot
                    make an active problem disappear by clicking a button,
                    only by it actually going away.

   The badge count is `unresolved_count()`, full stop. It does not care
   whether items have been read.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal


class Severity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def color(self) -> str:
        return {
            Severity.INFO: "#5B8DEF",       # blue -- fyi, no action needed
            Severity.WARNING: "#E0A62B",    # amber -- worth a look
            Severity.CRITICAL: "#D64545",   # red -- something is actually wrong
        }[self]

    @property
    def label(self) -> str:
        return self.value.capitalize()


@dataclass
class Notification:
    id: str
    severity: Severity
    title: str
    message: str
    source: str          # e.g. "weekly_integrity_check", "reconciliation"
    timestamp: str        # ISO 8601, UTC
    read: bool = False
    resolved: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Notification":
        d = dict(d)
        d["severity"] = Severity(d["severity"])
        return cls(**d)


class NotificationStore:
    """Plain JSON-file persistence. Swap this out for a SQLite table
    alongside your other app state if you'd rather not have a second
    small file sitting next to the encrypted database -- the interface
    (load/save a list of dicts) is the only thing that matters to
    NotificationCenter below."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable state file -- fail safe to "no
            # notifications" rather than crash the app on startup.
            return []

    def save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # atomic on POSIX and Windows


class NotificationCenter(QObject):
    """In-memory manager backed by NotificationStore. Emits a Qt signal
    on any change so widgets can just connect and repaint -- they never
    need to poll."""

    changed = pyqtSignal()

    def __init__(self, store: NotificationStore):
        super().__init__()
        self._store = store
        self._items: list[Notification] = [
            Notification.from_dict(d) for d in self._store.load()
        ]
        stale_sources = {"integrity_check_running", "integrity_check_complete"}
        changed = False
        for n in self._items:
            if n.source in stale_sources and not n.resolved:
                n.resolved = True
                changed = True
        if changed:
            self._persist()

    # -- persistence --------------------------------------------------

    def _persist(self) -> None:
        self._store.save([n.to_dict() for n in self._items])
        self.changed.emit()

    # -- writing --------------------------------------------------------

    def add(self, severity: Severity, title: str, message: str, source: str) -> Notification:
        note = Notification(
            id=str(uuid.uuid4()),
            severity=severity,
            title=title,
            message=message,
            source=source,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self._items.insert(0, note)  # newest first
        self._persist()
        return note

    def resolve(self, source: str) -> None:
        """Call this from the code that OWNS a given check when it next
        succeeds -- e.g. the weekly integrity QProcess, on a clean exit
        code, calls `notification_center.resolve("weekly_integrity_check")`.
        This is the only way an unresolved notification's badge weight
        goes away; there is no user-facing equivalent."""
        changed = False
        for n in self._items:
            if n.source == source and not n.resolved:
                n.resolved = True
                changed = True
        if changed:
            self._persist()

    def mark_all_read(self) -> None:
        changed = False
        for n in self._items:
            if not n.read:
                n.read = True
                changed = True
        if changed:
            self._persist()

    def resolve_by_id(self, note_id: str) -> None:
        """Resolves a specific notification by its unique ID."""
        changed = False
        for n in self._items:
            if n.id == note_id and not n.resolved:
                n.resolved = True
                changed = True
        if changed:
            self._persist()

    def clear_resolved(self) -> None:
        """Housekeeping: drop resolved items from history. Safe to call
        any time -- never touches anything still unresolved."""
        before = len(self._items)
        self._items = [n for n in self._items if not n.resolved]
        if len(self._items) != before:
            self._persist()

    # -- reading --------------------------------------------------------

    def all(self) -> list[Notification]:
        return list(self._items)

    def unresolved_count(self) -> int:
        return sum(1 for n in self._items if not n.resolved)

    def has_unread(self) -> bool:
        return any(not n.read for n in self._items)

    def highest_active_severity(self) -> Optional[Severity]:
        active = [n.severity for n in self._items if not n.resolved]
        if not active:
            return None
        order = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}
        return min(active, key=lambda s: order[s])
