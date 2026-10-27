# tax_profile.py
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from ledger import GeneralLedger
from tax_parser import LEGACY_V1_DEFAULTS

logger = logging.getLogger(__name__)

# save_section(variant=_KEEP) leaves the stored variant alone, so callers that
# only edit the category map / inputs (e.g. the profile dialog) can't clobber it.
_KEEP = object()

# Which country / template layers / modules the Tax tab had selected. Kept in
# its own row (not inside a country section) so country-scoped profile data
# and save_section()'s fixed shape are untouched.
_UI_STATE_SECTION = "_ui_state"


def record_selection(state, country, layers, modules):
    """Return `state` with this country's layer picks and module on/off
    switches recorded. `layers` maps level -> template_id ("" = the user chose
    "(none)"); `modules` maps template_id -> bool. Modules not in `modules`
    (hidden by the current layers) keep whatever was stored for them."""
    state = dict(state or {})
    all_layers = dict(state.get("layers") or {})
    all_layers[country] = dict(layers)
    all_modules = dict(state.get("modules") or {})
    merged = dict(all_modules.get(country) or {})
    merged.update({tid: bool(on) for tid, on in modules.items()})
    all_modules[country] = merged
    state.update({"country": country, "layers": all_layers, "modules": all_modules})
    return state


def stored_layer(state, country, level):
    """Template id stored for a level; "" if the user chose (none); None if nothing stored."""
    return ((state or {}).get("layers") or {}).get(country, {}).get(level)


def stored_module_enabled(state, country, template_id) -> bool:
    """A module is on unless the user switched it off (new modules start on)."""
    return bool((((state or {}).get("modules") or {}).get(country) or {}).get(template_id, True))


class TaxProfileManager:
    """
    Manages the private, per-client tax profile stored in the SQLCipher DB.

    Sections are scoped per country: "<CC>/Default" (see resolve_section_name),
    so someone who files in two countries keeps two independent profiles.
    Each section also remembers the user's chosen variant (e.g. filing status).

    Two tables:
      - tax_profile            — per-section category maps + user figures
      - tax_estimate_snapshots — attribution records, one per computed
                                 estimate (template IDs, tax_year,
                                 valid_from for modules, computed_at)

    Both tables are created lazily via _ensure_schema() so this module is
    self-contained — a caller that reaches for the profile manager gets
    the schema without needing to touch ledger.py. IF NOT EXISTS makes
    this safe even when ledger._init_db already created tax_profile.
    """

    def __init__(self, ledger: GeneralLedger):
        self.gl = ledger
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        conn, cur = self.gl._connect()
        try:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS tax_profile ("
                "section_name TEXT PRIMARY KEY, "
                "data_json TEXT NOT NULL, "
                "updated_at TEXT NOT NULL)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS tax_estimate_snapshots ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "computed_at TEXT NOT NULL, "
                "template_set_json TEXT NOT NULL, "
                "notes TEXT)"
            )
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to ensure tax profile schema: {e}")
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Per-section profile data
    # ------------------------------------------------------------------
    def save_section(
        self,
        section_name: str,
        category_map: Dict,
        user_inputs: Dict[str, Any],
        skipped_inputs: List[str],
        variant: Any = _KEEP,
        carryforwards: Any = _KEEP,
    ) -> bool:
        if variant is _KEEP or carryforwards is _KEEP:
            existing = self.load_section(section_name) or {}
            if variant is _KEEP:
                variant = existing.get("variant")
            if carryforwards is _KEEP:
                carryforwards = existing.get("carryforwards") or {}
        data = {
            "category_map": category_map,
            "user_inputs": user_inputs,
            "skipped_inputs": skipped_inputs,
            "variant": variant,
            "carryforwards": carryforwards,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        conn, cur = self.gl._connect()
        try:
            cur.execute(
                "INSERT INTO tax_profile (section_name, data_json, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(section_name) DO UPDATE SET "
                "data_json = excluded.data_json, updated_at = excluded.updated_at",
                (section_name, json.dumps(data), data["updated_at"]),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to save tax profile section '{section_name}': {e}")
            return False
        finally:
            conn.close()

    def load_section(self, section_name: str) -> Optional[Dict[str, Any]]:
        conn, cur = self.gl._connect()
        try:
            cur.execute(
                "SELECT data_json FROM tax_profile WHERE section_name = ?",
                (section_name,),
            )
            row = cur.fetchone()
            return json.loads(row[0]) if row else None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Country scoping + variant choice
    # ------------------------------------------------------------------
    @staticmethod
    def section_name_for(country: str, name: str = "Default") -> str:
        return f"{country}/{name}"

    def resolve_section_name(self, country: str, name: str = "Default") -> str:
        """
        The profile section for a country. First use in the legacy country
        adopts the old un-namespaced section: its data is COPIED to
        "<CC>/Default" (the original row is left untouched, so anything that
        still reads it keeps working). Every other country starts empty.
        """
        key = self.section_name_for(country, name)
        if country == LEGACY_V1_DEFAULTS["country"] and self.load_section(key) is None:
            legacy = self.load_section(name)
            if legacy is not None:
                self.save_section(
                    key,
                    legacy.get("category_map", {}),
                    legacy.get("user_inputs", {}),
                    legacy.get("skipped_inputs", []),
                    variant=legacy.get("variant"),
                )
        return key

    # ------------------------------------------------------------------
    # Carryforwards: amounts a tax year hands to the next one
    # ------------------------------------------------------------------
    def save_carryforwards(self, section_name: str, tax_year: int, values: Dict[str, Any]) -> bool:
        """Record what `tax_year`'s calculation says carries into the next year
        (replaces that year's earlier record; other years are kept)."""
        existing = self.load_section(section_name) or {}
        all_years = dict(existing.get("carryforwards") or {})
        all_years[str(tax_year)] = {name: str(v) for name, v in values.items()}
        return self.save_section(
            section_name,
            existing.get("category_map", {}),
            existing.get("user_inputs", {}),
            existing.get("skipped_inputs", []),
            carryforwards=all_years,
        )

    def carryforward_inputs_for(self, section_name: str, tax_year: int) -> Dict[str, Dict[str, Any]]:
        """
        Last year's saved carryforwards, shaped as user_inputs for `tax_year`
        (so the calculator consumes them like any entered figure). Values the
        user enters themselves take precedence -- the caller merges them on top.
        """
        data = self.load_section(section_name) or {}
        prior = (data.get("carryforwards") or {}).get(str(tax_year - 1), {})
        return {
            name: {"value": v, "entered_for": tax_year, "source": "carryforward"}
            for name, v in prior.items()
        }

    # ------------------------------------------------------------------
    # Tax-tab selection (country / layers / modules)
    # ------------------------------------------------------------------
    def load_ui_state(self) -> Dict[str, Any]:
        """Last selection; {} if none yet. Never raises -- an unreadable row
        must not stop the tab from opening."""
        try:
            return self.load_section(_UI_STATE_SECTION) or {}
        except Exception as e:
            logger.warning(f"Could not read saved tax tab selection: {e}")
            return {}

    def save_ui_state(self, state: Dict[str, Any]) -> bool:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn, cur = self.gl._connect()
        try:
            cur.execute(
                "INSERT INTO tax_profile (section_name, data_json, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(section_name) DO UPDATE SET "
                "data_json = excluded.data_json, updated_at = excluded.updated_at",
                (_UI_STATE_SECTION, json.dumps(state), now),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to save tax tab selection: {e}")
            return False
        finally:
            conn.close()

    def get_variant(self, section_name: str) -> Optional[str]:
        data = self.load_section(section_name)
        return data.get("variant") if data else None

    def set_variant(self, section_name: str, variant: Optional[str]) -> bool:
        existing = self.load_section(section_name) or {}
        return self.save_section(
            section_name,
            existing.get("category_map", {}),
            existing.get("user_inputs", {}),
            existing.get("skipped_inputs", []),
            variant=variant,
        )

    @staticmethod
    def find_stale_inputs(section_data: Dict[str, Any], target_year: int) -> List[str]:
        """
        Three-state stale marker (B: stale user_input). Returns the names
        of user_input fields in this section whose entered_for doesn't
        match target_year -- amber '!' indicator territory in the UI.

        Only fires on a genuine mismatch; a field with no entered_for
        recorded is left alone rather than assumed stale.
        """
        stale = []
        for name, entry in (section_data or {}).get("user_inputs", {}).items():
            if isinstance(entry, dict):
                entered_for = entry.get("entered_for")
                if entered_for is not None and entered_for != target_year:
                    stale.append(name)
        return stale

    def clear_section(self, section_name: str) -> bool:
        conn, cur = self.gl._connect()
        try:
            cur.execute(
                "DELETE FROM tax_profile WHERE section_name = ?", (section_name,)
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to clear tax profile section '{section_name}': {e}")
            return False
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Attribution snapshots (B9)
    # ------------------------------------------------------------------
    def save_estimate_snapshot(
        self,
        template_set,  # ResolvedTemplateSet
        notes: Optional[str] = None,
    ) -> Optional[int]:
        """
        Record the effective template set used to produce an estimate.
        The snapshot is what makes "computed with X, Y, Z" durable.
        """
        tp = template_set.tax_period
        snapshot = {
            # Current shape: every layer that took part, outermost first.
            "layers": [
                {
                    "level": l["jurisdiction"]["level"],
                    "country": l["jurisdiction"]["country"],
                    "region": l["jurisdiction"].get("region"),
                    "template_id": l.get("template_id"),
                    "tax_year": l.get("tax_year"),
                }
                for l in template_set.layers
            ],
            "currency": template_set.currency,
            "variant": template_set.variant,
            "variants_applied": dict(template_set.variants_applied),
            "tax_year": template_set.tax_year,
            "tax_period": {"label": tp.label, "start": tp.start, "end": tp.end},
            # Kept so anything reading older-style snapshots keeps working.
            "federal_base": template_set.federal_base.get("template_id"),
            "federal_tax_year": template_set.federal_base.get("tax_year"),
            "provincial_base": template_set.provincial_base.get("template_id"),
            "provincial_tax_year": template_set.provincial_base.get("tax_year"),
            "modules": [
                {
                    "template_id": m.get("template_id"),
                    "valid_from": m.get("valid_from"),
                }
                for m in template_set.modules
            ],
        }
        computed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn, cur = self.gl._connect()
        try:
            cur.execute(
                "INSERT INTO tax_estimate_snapshots (computed_at, template_set_json, notes) "
                "VALUES (?, ?, ?)",
                (computed_at, json.dumps(snapshot), notes),
            )
            snapshot_id = cur.lastrowid
            conn.commit()
            return snapshot_id
        except Exception as e:
            logger.error(f"Failed to save tax estimate snapshot: {e}")
            return None
        finally:
            conn.close()

    def load_estimate_snapshot(self, snapshot_id: int) -> Optional[Dict[str, Any]]:
        conn, cur = self.gl._connect()
        try:
            cur.execute(
                "SELECT computed_at, template_set_json, notes "
                "FROM tax_estimate_snapshots WHERE id = ?",
                (snapshot_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": snapshot_id,
                "computed_at": row[0],
                "template_set": json.loads(row[1]),
                "notes": row[2],
            }
        finally:
            conn.close()

    def delete_estimate_snapshot(self, snapshot_id: int) -> bool:
        conn, cur = self.gl._connect()
        try:
            cur.execute(
                "DELETE FROM tax_estimate_snapshots WHERE id = ?", (snapshot_id,)
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete tax estimate snapshot {snapshot_id}: {e}")
            return False
        finally:
            conn.close()
