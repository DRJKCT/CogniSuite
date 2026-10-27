"""
ui_config.py
For CogniFinance -- tiny read-modify-write helper for ui_config.json
(config.UI_CONFIG_PATH, i.e. ...\\CogniSuite\\SystemState\\ui_config.json).

That one file is shared: ui_main.py keeps "theme", "window_width" and
"window_height" in it, and SettingsTab keeps the rest under "settings".
Every writer must read the whole file, change only its own keys, and
write the whole file back -- never replace it with just its own keys.
Writes go through config.atomic_secure_write_json, so a crash mid-write
can't leave a half-written (corrupt) file.
"""
import json
import logging
import os

from config import UI_CONFIG_PATH, atomic_secure_write_json

logger = logging.getLogger(__name__)


def load_ui_config() -> dict:
    """Whole config as a dict; {} if missing, unreadable or not a JSON object."""
    path = str(UI_CONFIG_PATH)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"ui_config: could not read {path}: {e}")
        return {}


def update_ui_config(updates: dict) -> None:
    """Merge top-level keys into the config. Raises on write failure so the
    caller can tell the user instead of claiming success."""
    data = load_ui_config()
    data.update(updates)
    atomic_secure_write_json(UI_CONFIG_PATH, data)


def load_section(section: str) -> dict:
    value = load_ui_config().get(section, {})
    return value if isinstance(value, dict) else {}


def update_section(section: str, values: dict) -> None:
    """Merge keys into one nested section (e.g. "settings"), leaving every
    other top-level key (theme, window size, ...) untouched."""
    data = load_ui_config()
    current = data.get(section, {})
    if not isinstance(current, dict):
        current = {}
    current.update(values)
    data[section] = current
    atomic_secure_write_json(UI_CONFIG_PATH, data)
