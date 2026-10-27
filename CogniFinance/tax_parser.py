# tax_parser.py
"""
tax_parser.py ver 5.0
For CogniFinance ver 1.9

Template loader/validator. ver 5.0 (template format 5) adds:
  * a levy's `applies_to` may name a computed reference -- "total_income",
    "net_income", "adjusted_taxable_income" -- instead of only bucket names
    or "*" (gross total income). "adjusted_taxable_income" resolves to the
    levy's OWN layer's taxable base (its declaring base's own deductions
    applied), falling back to the shared base when the layer has none of
    its own. Needed for levies legally based on taxable income (e.g. a
    provincial health premium) rather than gross income.
  * `phased_bands` levy type: a series of independent "rate x amount over a
    threshold, capped" segments summed together (a ramp up to a plateau,
    repeated) -- the shape of premiums like the Ontario Health Premium or
    Australia's Medicare levy, as ONE levy entry instead of several.
  * a levy's `type` is now validated at load time (previously only a
    compute-time error caught an unknown one).

ver 4.0 (template format 4) added:
  * `threshold_rates` adjustments (sum / min / max of "rate x amount over a
    threshold" terms -- surtaxes, phase-in surcharges) and a `post_credit`
    stage that runs after non-refundable credits;
  * `scope: "layer"` on deduction / pre / scale adjustments, so a layer can
    have its own taxable income (US-state / cantonal style);
  * `tariff` (polynomial zones) as an alternative to `brackets`;
  * `alternatives` (a parallel calculation compared with the regular tax:
    minimum-tax and benefit-cap style rules);
  * `foreign_tax_credit` credits and declared `carryforwards`;
  * load-time validation of adjustment `stage` / `type` / `scope`, so a
    typo can no longer make an adjustment silently do nothing.

ver 3.0 (template format 3) added:
  * filing-status style VARIANTS on a base (alternative brackets / credits /
    levies / adjustments the user picks between);
  * new calculation vocabulary validated at load: `separate_rate` buckets,
    `banded_rate` levies, `refundable` credits, and an explicit
    `income_reference` on threshold_bucket credits (the hard-coded
    Canadian "line 23600" input now lives only in the upgrade path);
  * optional `label` (string or {lang: text}) on buckets, levies, credits,
    adjustments and variants, and `country_name` on jurisdiction.
Formats 1 and 2 are upgraded in memory on load.

ver 2.0 (template format 2):
  * "federal_base" / "provincial_base" are replaced by a single
    "jurisdiction_base" type carrying jurisdiction {country, region?,
    level}; a set is an ordered list of layers (national -> regional ->
    local), one per level.
  * tax_year is still the integer key, but a base may declare a
    tax_period {label, start, end} for non-calendar years.
  * bases declare currency and (optionally) a rounding policy.
  * Format-1 files are still read: they are upgraded IN MEMORY (never
    rewritten on disk, so shipped-file hashes are unaffected).
"""
import os
import re
import copy
import json
import hashlib
import logging
from datetime import date, timedelta
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field

from shipped_manifest import SHIPPED_TEMPLATE_MANIFEST
from tax_format import ROUNDING_MODES

logger = logging.getLogger(__name__)

RESERVED_PREFIXES = ("_", "x_")

# cognifinance_tax_template — file-format version marker. Bumped on
# schema change; the loader supports up to this value.
SUPPORTED_MARKER_VERSION = 5
# Oldest format still accepted (upgraded in memory on load).
MIN_SUPPORTED_MARKER_VERSION = 1

# Format-1 templates carried no jurisdiction metadata. Every one written so
# far was Canadian (federal + provincial, CRA line numbers), so upgrading
# fills these in. Change here if your legacy files were for somewhere else.
LEGACY_V1_DEFAULTS = {
    "country": "CA",
    "currency": "CAD",
    # Older threshold_bucket credits implicitly read this user input as their
    # income reference. The upgrade makes that explicit in the template.
    "income_reference_input": "line_23600_net_income",
}

# Jurisdiction levels, outermost first. A set holds at most one layer per level.
LEVELS = ("national", "regional", "local")
DEFAULT_LEVEL_LABELS = {"national": "National", "regional": "Regional", "local": "Local"}
_LEVEL_RANK = {lv: i for i, lv in enumerate(LEVELS)}

_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")        # ISO 3166-1 alpha-2
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")       # ISO 4217
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TemplateValidationError(Exception):
    pass


@dataclass
class TemplateTier:
    """
    Hash-based trust classification for a loaded template file.

    tier: "shipped" | "shipped_modified" | "user_authored" | "community"
    hash_matches: True/False for shipped-dir files (vs. the build-time
    manifest); None where hash comparison doesn't apply (user/community).
    """
    tier: str
    path: str
    hash_matches: Optional[bool] = None


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

REQUIRED_BASE_KEYS = {
    "cognifinance_tax_template",
    "template_type",
    "template_id",
    "tax_year",
    "module_compatibility",
    "jurisdiction",
    "currency",
}   # plus exactly one of "brackets" / "tariff" (checked in validate_schema)

REQUIRED_MODULE_KEYS = {
    "cognifinance_tax_template",
    "template_type",
    "template_id",
    "valid_from",
    "buckets",
}

# Explicit allowed top-level keys per template type. Any key not in
# this set (and not carrying a reserved prefix) causes rejection, per
# the brief's fail-closed loader posture. Adding a top-level field
# requires bumping `cognifinance_tax_template`.
_COMMON_KEYS = {
    "cognifinance_tax_template",
    "template_type",
    "template_id",
    "display_name",
    "description",
}

_ALLOWED_KEYS_BY_TYPE = {
    "jurisdiction_base": _COMMON_KEYS | {
        "tax_year",
        "tax_period",
        "jurisdiction",
        "currency",
        "rounding",
        "brackets",
        "tariff",
        "alternatives",
        "carryforwards",
        "variants",
        "module_compatibility",
        "levies",
        "credits",
        "adjustments",
    },
    "income_module": _COMMON_KEYS | {
        "valid_from",
        "buckets",
    },
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _is_reserved(key: str) -> bool:
    return key.startswith(RESERVED_PREFIXES)


def _is_numeric(val) -> bool:
    try:
        from decimal import Decimal, InvalidOperation
        Decimal(str(val))
        return True
    except (ValueError, TypeError, InvalidOperation):
        return False


def compute_sha256(path: str) -> str:
    """SHA-256 hex digest of a file's bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Module-level validation (callable without a loader instance)
# ---------------------------------------------------------------------------

def _require_keys(data: Dict, keys: List[str]) -> None:
    missing = [k for k in keys if k not in data]
    if missing:
        raise TemplateValidationError(
            f"Template '{data.get('template_id', '?')}' missing required keys: {missing}"
        )


def _validate_brackets(brackets: Any, field_name: str, require_zero_start: bool = True) -> None:
    """Shape validation for bracket lists (levy bands may start above 0)."""
    if not isinstance(brackets, list) or not brackets:
        raise TemplateValidationError(f"'{field_name}' must be a non-empty list.")

    from decimal import Decimal

    prev_threshold = None
    for i, b in enumerate(brackets):
        if not isinstance(b, dict):
            raise TemplateValidationError(f"'{field_name}[{i}]' is not an object.")
        if "threshold" not in b or "rate" not in b:
            raise TemplateValidationError(
                f"'{field_name}[{i}]' missing threshold or rate."
            )
        if not _is_numeric(b["threshold"]) or not _is_numeric(b["rate"]):
            raise TemplateValidationError(
                f"'{field_name}[{i}]' threshold/rate must be numeric."
            )
        threshold = Decimal(str(b["threshold"]))
        if prev_threshold is not None and threshold <= prev_threshold:
            raise TemplateValidationError(
                f"'{field_name}' thresholds must be strictly ascending."
            )
        prev_threshold = threshold

    # Lower-bound convention: the first rate applies to [0, next_threshold).
    if require_zero_start and Decimal(str(brackets[0]["threshold"])) != Decimal("0"):
        raise TemplateValidationError(
            f"'{field_name}' first threshold must be 0."
        )


def localized(value: Any, lang: Optional[str] = None, default: str = "") -> str:
    """
    Resolve a text field that is either a plain string or a {lang: text}
    map. Tries the exact language, then its base ("fr_CA" -> "fr"), then
    "en", then whatever is first.
    """
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if lang:
            for key in (lang, lang.replace("-", "_").split("_")[0]):
                if key in value:
                    return value[key]
        if "en" in value:
            return value["en"]
        return next(iter(value.values()), default)
    return str(value)


def pretty_name(name: str) -> str:
    """'capital_gains' -> 'Capital Gains' (fallback text when a template gives no label)."""
    return name.replace("_", " ").title()


def display_label(entry: Any, fallback: str, lang: Optional[str] = None) -> str:
    """The user-facing label for a bucket / levy / credit / adjustment: its
    `label` (string or {lang: text}) if it has one, else `fallback` as given."""
    label = entry.get("label") if isinstance(entry, dict) else None
    return localized(label, lang, default=fallback) if label is not None else fallback


def _upgrade_v1_to_v2(data: Dict) -> Dict:
    out = copy.deepcopy(data)
    t_type = out.get("template_type")
    if t_type in ("federal_base", "provincial_base"):
        if t_type == "federal_base":
            level, level_label, old_key = "national", "Federal", "federal_brackets"
        else:
            level, level_label, old_key = "regional", "Provincial", "provincial_brackets"
        out["template_type"] = "jurisdiction_base"
        if old_key in out:
            out["brackets"] = out.pop(old_key)
        year = out.get("tax_year")
        if isinstance(year, str) and year.strip().isdigit():
            out["tax_year"] = int(year)
        out.setdefault("jurisdiction", {
            "country": LEGACY_V1_DEFAULTS["country"],
            "level": level,
            "level_label": level_label,
        })
        out.setdefault("currency", LEGACY_V1_DEFAULTS["currency"])
    out["cognifinance_tax_template"] = 2
    out["x_upgraded_from"] = 1
    return out


def _upgrade_v2_to_v3(data: Dict) -> Dict:
    out = copy.deepcopy(data)
    if out.get("template_type") == "jurisdiction_base":
        # threshold_bucket credits used to read a fixed user input; state it.
        for credit in out.get("credits") or []:
            if (
                isinstance(credit, dict)
                and credit.get("type") == "threshold_bucket"
                and "income_reference" not in credit
            ):
                credit["income_reference"] = {
                    "user_input": LEGACY_V1_DEFAULTS["income_reference_input"]
                }
    out["cognifinance_tax_template"] = 3
    out.setdefault("x_upgraded_from", 2)
    return out


def _upgrade_v3_to_v4(data: Dict) -> Dict:
    """Format 4 only adds optional vocabulary; existing content is unchanged."""
    out = copy.deepcopy(data)
    out["cognifinance_tax_template"] = 4
    out.setdefault("x_upgraded_from", 3)
    return out


def _upgrade_v4_to_v5(data: Dict) -> Dict:
    """Format 5 only adds optional vocabulary; existing content is unchanged."""
    out = copy.deepcopy(data)
    out["cognifinance_tax_template"] = 5
    out.setdefault("x_upgraded_from", 4)
    return out


def upgrade_template(data: Any) -> Any:
    """
    Return `data` in the current format. Older supported formats are
    upgraded on a copy (v1 -> v2 -> v3 -> v4); current-format, non-dict, or
    unrecognized-marker input is returned untouched (validate_schema
    reports the problem).
    """
    if not isinstance(data, dict):
        return data
    marker = data.get("cognifinance_tax_template")
    if not isinstance(marker, int) or isinstance(marker, bool):
        return data
    if marker < MIN_SUPPORTED_MARKER_VERSION or marker >= SUPPORTED_MARKER_VERSION:
        return data
    out = data
    while marker < SUPPORTED_MARKER_VERSION:
        step = {1: _upgrade_v1_to_v2, 2: _upgrade_v2_to_v3, 3: _upgrade_v3_to_v4, 4: _upgrade_v4_to_v5}[marker]
        out = step(out)
        marker = out["cognifinance_tax_template"]
    return out


def _validate_localized(value: Any, field_name: str, tid: str) -> None:
    if isinstance(value, str):
        if not value.strip():
            raise TemplateValidationError(f"'{field_name}' in '{tid}' must not be empty.")
        return
    if isinstance(value, dict) and value and all(
        isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in value.items()
    ):
        return
    raise TemplateValidationError(
        f"'{field_name}' in '{tid}' must be a string or a non-empty {{language: text}} map."
    )


def _validate_jurisdiction(j: Any, tid: str) -> None:
    if not isinstance(j, dict):
        raise TemplateValidationError(f"'jurisdiction' in '{tid}' must be an object.")
    allowed = {"country", "region", "country_name", "level", "level_label"}
    extra = set(j) - allowed
    if extra:
        raise TemplateValidationError(
            f"Unknown key(s) {sorted(extra)} in jurisdiction of '{tid}'."
        )
    country = j.get("country")
    if not isinstance(country, str) or not _COUNTRY_RE.match(country):
        raise TemplateValidationError(
            f"jurisdiction.country in '{tid}' must be an ISO 3166-1 alpha-2 code (e.g. 'CA')."
        )
    if j.get("level") not in LEVELS:
        raise TemplateValidationError(
            f"jurisdiction.level in '{tid}' must be one of {list(LEVELS)}."
        )
    if "region" in j and (not isinstance(j["region"], str) or not j["region"].strip()):
        raise TemplateValidationError(f"jurisdiction.region in '{tid}' must be a non-empty string.")
    if "level_label" in j:
        _validate_localized(j["level_label"], "jurisdiction.level_label", tid)
    if "country_name" in j:
        _validate_localized(j["country_name"], "jurisdiction.country_name", tid)


def _validate_tax_period(data: Dict) -> None:
    tid = data.get("template_id", "?")
    tp = data["tax_period"]
    if not isinstance(tp, dict) or "start" not in tp or "end" not in tp:
        raise TemplateValidationError(f"'tax_period' in '{tid}' needs 'start' and 'end'.")
    extra = set(tp) - {"label", "start", "end"}
    if extra:
        raise TemplateValidationError(f"Unknown key(s) {sorted(extra)} in tax_period of '{tid}'.")
    try:
        if not (_ISO_DATE_RE.match(str(tp["start"])) and _ISO_DATE_RE.match(str(tp["end"]))):
            raise ValueError
        start = date.fromisoformat(tp["start"])
        end = date.fromisoformat(tp["end"])
    except (TypeError, ValueError):
        raise TemplateValidationError(
            f"tax_period start/end in '{tid}' must be YYYY-MM-DD dates."
        )
    if end <= start:
        raise TemplateValidationError(f"tax_period end must be after start in '{tid}'.")
    if (end - start).days > 400:
        raise TemplateValidationError(f"tax_period in '{tid}' spans more than a year.")
    if start.year != data["tax_year"]:
        raise TemplateValidationError(
            f"tax_year in '{tid}' must equal the year the tax_period starts ({start.year})."
        )
    if "label" in tp and (not isinstance(tp["label"], str) or not tp["label"].strip()):
        raise TemplateValidationError(f"tax_period.label in '{tid}' must be a non-empty string.")


def _validate_rounding(r: Any, tid: str) -> None:
    from decimal import Decimal, InvalidOperation
    if not isinstance(r, dict):
        raise TemplateValidationError(f"'rounding' in '{tid}' must be an object.")
    extra = set(r) - {"mode", "increment"}
    if extra:
        raise TemplateValidationError(f"Unknown key(s) {sorted(extra)} in rounding of '{tid}'.")
    if "mode" in r and r["mode"] not in ROUNDING_MODES:
        raise TemplateValidationError(
            f"rounding.mode in '{tid}' must be one of {sorted(ROUNDING_MODES)}."
        )
    if "increment" in r:
        try:
            ok = Decimal(str(r["increment"])) > 0
        except (InvalidOperation, ValueError):
            ok = False
        if not ok:
            raise TemplateValidationError(f"rounding.increment in '{tid}' must be a positive number.")


KNOWN_PRIMITIVES = ("inclusion_rate", "gross_up_credit", "separate_rate")
KNOWN_LEVY_TYPES = ("rate", "tiered", "banded_rate", "phased_bands")
# What a variant option may override on a base (replaced wholesale).
_CALC_KEYS = ("brackets", "tariff", "credits", "levies", "adjustments", "alternatives")

ADJUSTMENT_STAGES = ("deduction", "pre", "scale", "post", "post_credit")
_LAYER_SCOPABLE_STAGES = ("deduction", "pre", "scale")
ADJUSTMENT_TYPES = ("flat", "tapered", "rate", "threshold_rates")
COMBINE_MODES = ("sum", "min", "max")
CARRYFORWARD_SOURCES = ("unused_credits", "negative_bucket")
# Computed references a levy's applies_to may use instead of a bucket name.
LEVY_INCOME_REFERENCES = ("total_income", "net_income", "adjusted_taxable_income")
_COMPUTED_INCOME_REFERENCES = ("total_income", "net_income", "adjusted_taxable_income")


def _validate_income_reference(ref: Any, loc: str, tid: str) -> None:
    ok = (isinstance(ref, str) and ref in _COMPUTED_INCOME_REFERENCES) or (
        isinstance(ref, dict)
        and set(ref) == {"user_input"}
        and isinstance(ref["user_input"], str)
        and ref["user_input"].strip()
    )
    if not ok:
        raise TemplateValidationError(
            f"{loc}.income_reference in '{tid}' must be one of "
            f"{list(_COMPUTED_INCOME_REFERENCES)} or {{\"user_input\": \"<field name>\"}}."
        )


def _validate_threshold_rates(adj: Dict, loc: str, tid: str) -> None:
    from decimal import Decimal
    comps = adj.get("components")
    if not isinstance(comps, list) or not comps:
        raise TemplateValidationError(f"{loc}.components in '{tid}' must be a non-empty list.")
    for i, c in enumerate(comps):
        if not isinstance(c, dict) or set(c) - {"rate", "threshold"} or "rate" not in c:
            raise TemplateValidationError(
                f"{loc}.components[{i}] in '{tid}' needs 'rate' (and optionally 'threshold')."
            )
        for k in ("rate", "threshold"):
            if k in c and (not _is_numeric(c[k]) or Decimal(str(c[k])) < 0):
                raise TemplateValidationError(f"{loc}.components[{i}].{k} in '{tid}' must be a number >= 0.")
    if adj.get("combine", "sum") not in COMBINE_MODES:
        raise TemplateValidationError(f"{loc}.combine in '{tid}' must be one of {list(COMBINE_MODES)}.")


def _validate_tariff(tariff: Any, loc: str, tid: str) -> None:
    """Polynomial-zone tariff: tax = sum(c_k * t^k), t = (income - offset) / divisor,
    using the last zone whose `from` is <= income."""
    from decimal import Decimal
    if not isinstance(tariff, dict) or set(tariff) - {"zones", "income_rounding"}:
        raise TemplateValidationError(f"{loc} in '{tid}' must be an object with 'zones' (and optionally 'income_rounding').")
    zones = tariff.get("zones")
    if not isinstance(zones, list) or not zones:
        raise TemplateValidationError(f"{loc}.zones in '{tid}' must be a non-empty list.")
    prev = None
    for i, z in enumerate(zones):
        zl = f"{loc}.zones[{i}]"
        if not isinstance(z, dict) or set(z) - {"from", "offset", "divisor", "coefficients"} or "from" not in z or "coefficients" not in z:
            raise TemplateValidationError(f"{zl} in '{tid}' needs 'from' and 'coefficients' (optional: 'offset', 'divisor').")
        for k in ("from", "offset", "divisor"):
            if k in z and not _is_numeric(z[k]):
                raise TemplateValidationError(f"{zl}.{k} in '{tid}' must be numeric.")
        if "divisor" in z and Decimal(str(z["divisor"])) <= 0:
            raise TemplateValidationError(f"{zl}.divisor in '{tid}' must be > 0.")
        co = z["coefficients"]
        if not isinstance(co, list) or not co or not all(_is_numeric(c) for c in co):
            raise TemplateValidationError(f"{zl}.coefficients in '{tid}' must be a non-empty list of numbers.")
        frm = Decimal(str(z["from"]))
        if prev is None and frm != 0:
            raise TemplateValidationError(f"{loc}.zones[0].from in '{tid}' must be 0.")
        if prev is not None and frm <= prev:
            raise TemplateValidationError(f"{loc}.zones in '{tid}' must have strictly ascending 'from'.")
        prev = frm
    if "income_rounding" in tariff:
        _validate_rounding(tariff["income_rounding"], tid)


def _validate_alternatives(alts: Any, tid: str, where: str = "") -> None:
    from decimal import Decimal
    if not isinstance(alts, list):
        raise TemplateValidationError(f"'{where}alternatives' in '{tid}' must be a list.")
    seen = set()
    for i, a in enumerate(alts):
        loc = f"{where}alternatives[{i}]"
        allowed = {"name", "label", "combine", "base", "add_back", "exemption", "subtract", "brackets", "tariff"}
        if not isinstance(a, dict) or set(a) - allowed:
            raise TemplateValidationError(f"{loc} in '{tid}' has unknown keys: {sorted(set(a) - allowed) if isinstance(a, dict) else a}.")
        if not isinstance(a.get("name"), str) or not a["name"].strip() or a["name"] in seen:
            raise TemplateValidationError(f"{loc}.name in '{tid}' must be a unique non-empty string.")
        seen.add(a["name"])
        if a.get("combine") not in ("max", "min"):
            raise TemplateValidationError(f"{loc}.combine in '{tid}' must be 'max' or 'min'.")
        if "label" in a:
            _validate_localized(a["label"], f"{loc}.label", tid)
        if a.get("base", "adjusted_taxable_income") not in _COMPUTED_INCOME_REFERENCES:
            raise TemplateValidationError(f"{loc}.base in '{tid}' must be one of {list(_COMPUTED_INCOME_REFERENCES)}.")
        if "add_back" in a and (not isinstance(a["add_back"], list) or not all(isinstance(b, str) for b in a["add_back"])):
            raise TemplateValidationError(f"{loc}.add_back in '{tid}' must be a list of bucket names.")
        if "subtract" in a and (not _is_numeric(a["subtract"]) or Decimal(str(a["subtract"])) < 0):
            raise TemplateValidationError(f"{loc}.subtract in '{tid}' must be a number >= 0.")
        if "brackets" in a and "tariff" in a:
            raise TemplateValidationError(f"{loc} in '{tid}' may have 'brackets' or 'tariff', not both.")
        if "brackets" in a:
            _validate_brackets(a["brackets"], f"{loc}.brackets")
        if "tariff" in a:
            _validate_tariff(a["tariff"], f"{loc}.tariff", tid)
        ex = a.get("exemption")
        if ex is not None:
            if not isinstance(ex, dict) or ex.get("type", "flat") not in ("flat", "tapered"):
                raise TemplateValidationError(f"{loc}.exemption in '{tid}' must be a flat or tapered amount.")


def _validate_carryforwards(cfs: Any, tid: str) -> None:
    if not isinstance(cfs, list):
        raise TemplateValidationError(f"'carryforwards' in '{tid}' must be a list.")
    seen = set()
    for i, c in enumerate(cfs):
        loc = f"carryforwards[{i}]"
        if not isinstance(c, dict) or set(c) - {"name", "label", "source", "bucket"}:
            raise TemplateValidationError(f"{loc} in '{tid}' has unknown keys.")
        if not isinstance(c.get("name"), str) or not c["name"].strip() or c["name"] in seen:
            raise TemplateValidationError(f"{loc}.name in '{tid}' must be a unique non-empty string.")
        seen.add(c["name"])
        if c.get("source") not in CARRYFORWARD_SOURCES:
            raise TemplateValidationError(f"{loc}.source in '{tid}' must be one of {list(CARRYFORWARD_SOURCES)}.")
        if c["source"] == "negative_bucket" and not (isinstance(c.get("bucket"), str) and c["bucket"].strip()):
            raise TemplateValidationError(f"{loc} in '{tid}': negative_bucket needs a 'bucket'.")
        if "label" in c:
            _validate_localized(c["label"], f"{loc}.label", tid)


def _validate_calc_entries(container: Dict, tid: str, where: str = "") -> None:
    """
    Validate the fields the calculator relies on for credits / levies /
    adjustments. Deliberately narrow: only fields with defined meaning are
    checked, so existing files with other fields keep loading.
    """
    for kind in ("credits", "levies", "adjustments"):
        entries = container.get(kind)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise TemplateValidationError(f"'{where}{kind}' in '{tid}' must be a list.")
        for i, entry in enumerate(entries):
            loc = f"{where}{kind}[{i}]"
            if not isinstance(entry, dict):
                raise TemplateValidationError(f"'{loc}' in '{tid}' is not an object.")
            if "label" in entry:
                _validate_localized(entry["label"], f"{loc}.label", tid)
            if kind == "adjustments":
                stage = entry.get("stage")
                if stage not in ADJUSTMENT_STAGES:
                    raise TemplateValidationError(
                        f"{loc}.stage in '{tid}' must be one of {list(ADJUSTMENT_STAGES)} (got {stage!r})."
                    )
                if stage != "scale" and entry.get("type", "flat") not in ADJUSTMENT_TYPES:
                    raise TemplateValidationError(
                        f"{loc}.type in '{tid}' must be one of {list(ADJUSTMENT_TYPES)} (got {entry.get('type')!r})."
                    )
                if "direction" in entry and entry["direction"] not in ("add", "subtract"):
                    raise TemplateValidationError(f"{loc}.direction in '{tid}' must be 'add' or 'subtract'.")
                if "scope" in entry:
                    if entry["scope"] not in ("shared", "layer"):
                        raise TemplateValidationError(f"{loc}.scope in '{tid}' must be 'shared' or 'layer'.")
                    if stage not in _LAYER_SCOPABLE_STAGES:
                        raise TemplateValidationError(
                            f"{loc}.scope in '{tid}' only applies to stages {list(_LAYER_SCOPABLE_STAGES)}; "
                            f"'{stage}' adjustments already act on their own layer."
                        )
                if stage != "scale" and entry.get("type") == "threshold_rates":
                    _validate_threshold_rates(entry, loc, tid)
            if kind == "credits":
                if "refundable" in entry and not isinstance(entry["refundable"], bool):
                    raise TemplateValidationError(f"{loc}.refundable in '{tid}' must be true or false.")
                if entry.get("type") == "threshold_bucket":
                    _validate_income_reference(entry.get("income_reference"), loc, tid)
                if entry.get("type") == "foreign_tax_credit":
                    ap = entry.get("applies_to")
                    if not isinstance(ap, list) or not ap or not all(isinstance(b, str) for b in ap):
                        raise TemplateValidationError(f"{loc}.applies_to in '{tid}' must list the foreign-income bucket(s).")
                    tp = entry.get("tax_paid")
                    if not (isinstance(tp, dict) and set(tp) == {"user_input"} and isinstance(tp["user_input"], str) and tp["user_input"].strip()):
                        raise TemplateValidationError(
                            f"{loc}.tax_paid in '{tid}' must be {{\"user_input\": \"<field name>\"}} (the foreign tax paid)."
                        )
            if kind == "levies":
                if "value_source" not in entry and entry.get("type") not in KNOWN_LEVY_TYPES:
                    raise TemplateValidationError(
                        f"{loc}.type in '{tid}' must be one of {list(KNOWN_LEVY_TYPES)} "
                        f"(got {entry.get('type')!r})."
                    )
                if entry.get("type") == "banded_rate":
                    _validate_brackets(entry.get("bands"), f"{loc}.bands", require_zero_start=False)
                if entry.get("type") == "phased_bands":
                    _validate_phased_bands(entry.get("segments"), loc, tid)


def _validate_phased_bands(segments: Any, loc: str, tid: str) -> None:
    from decimal import Decimal
    if not isinstance(segments, list) or not segments:
        raise TemplateValidationError(f"{loc}.segments in '{tid}' must be a non-empty list.")
    prev_threshold = None
    for i, seg in enumerate(segments):
        sl = f"{loc}.segments[{i}]"
        if not isinstance(seg, dict) or set(seg) - {"threshold", "rate", "cap"} or not {"threshold", "rate", "cap"} <= set(seg):
            raise TemplateValidationError(f"{sl} in '{tid}' needs 'threshold', 'rate' and 'cap'.")
        for k in ("threshold", "rate", "cap"):
            if not _is_numeric(seg[k]):
                raise TemplateValidationError(f"{sl}.{k} in '{tid}' must be a number.")
        threshold, rate, cap = (Decimal(str(seg[k])) for k in ("threshold", "rate", "cap"))
        if rate <= 0:
            raise TemplateValidationError(f"{sl}.rate in '{tid}' must be > 0.")
        if cap <= threshold:
            raise TemplateValidationError(f"{sl}.cap in '{tid}' must be greater than its threshold.")
        if prev_threshold is not None and threshold < prev_threshold:
            raise TemplateValidationError(f"{loc}.segments in '{tid}' must have non-decreasing thresholds.")
        prev_threshold = threshold


def _validate_bucket_defs(buckets: Dict, tid: str) -> None:
    from decimal import Decimal
    for name, bdef in buckets.items():
        loc = f"bucket '{name}'"
        if not isinstance(bdef, dict):
            raise TemplateValidationError(f"{loc} in '{tid}' must be an object.")
        if "label" in bdef:
            _validate_localized(bdef["label"], f"{loc}.label", tid)
        primitive = bdef.get("primitive", "inclusion_rate")
        if primitive not in KNOWN_PRIMITIVES:
            raise TemplateValidationError(
                f"{loc} in '{tid}' uses unknown primitive {primitive!r}; known: {list(KNOWN_PRIMITIVES)}."
            )
        if "refundable" in bdef and not isinstance(bdef["refundable"], bool):
            raise TemplateValidationError(f"{loc}.refundable in '{tid}' must be true or false.")
        if "carryforward_in" in bdef:
            # Net losses carried in from earlier years are applied against
            # this bucket before the primitive runs (capped at the bucket's
            # amount); what is not used carries on. Only meaningful where
            # the bucket is a plain inclusion, so refuse it elsewhere
            # rather than let it silently do nothing.
            if primitive != "inclusion_rate":
                raise TemplateValidationError(
                    f"{loc}.carryforward_in in '{tid}' is only supported on inclusion_rate buckets."
                )
            if not isinstance(bdef["carryforward_in"], str) or not bdef["carryforward_in"].strip():
                raise TemplateValidationError(
                    f"{loc}.carryforward_in in '{tid}' must be the name of a declared carryforward."
                )
        if primitive == "separate_rate":
            if "exempt_amount" in bdef and (
                not _is_numeric(bdef["exempt_amount"]) or Decimal(str(bdef["exempt_amount"])) < 0
            ):
                raise TemplateValidationError(f"{loc}.exempt_amount in '{tid}' must be a number >= 0.")
            schedules = bdef.get("schedules")
            if not isinstance(schedules, dict) or not schedules:
                raise TemplateValidationError(
                    f"{loc} in '{tid}': separate_rate needs 'schedules' keyed by level "
                    f"(e.g. {{\"national\": {{\"rate\": 0.25}}}})."
                )
            for level, sched in schedules.items():
                if level not in LEVELS:
                    raise TemplateValidationError(
                        f"{loc}.schedules in '{tid}': unknown level {level!r}; use {list(LEVELS)}."
                    )
                if not isinstance(sched, dict) or len(set(sched) & {"rate", "brackets"}) != 1 or set(sched) - {"rate", "brackets"}:
                    raise TemplateValidationError(
                        f"{loc}.schedules.{level} in '{tid}' needs exactly one of 'rate' or 'brackets'."
                    )
                if "rate" in sched and (not _is_numeric(sched["rate"]) or Decimal(str(sched["rate"])) < 0):
                    raise TemplateValidationError(f"{loc}.schedules.{level}.rate in '{tid}' must be a number >= 0.")
                if "brackets" in sched:
                    _validate_brackets(sched["brackets"], f"{loc}.schedules.{level}.brackets")


def _validate_variants(spec: Any, tid: str) -> None:
    if not isinstance(spec, dict):
        raise TemplateValidationError(f"'variants' in '{tid}' must be an object.")
    extra = set(spec) - {"label", "default", "options"}
    if extra:
        raise TemplateValidationError(f"Unknown key(s) {sorted(extra)} in variants of '{tid}'.")
    options = spec.get("options")
    if not isinstance(options, dict) or not options:
        raise TemplateValidationError(f"variants.options in '{tid}' must be a non-empty object.")
    if "label" in spec:
        _validate_localized(spec["label"], "variants.label", tid)
    for name, opt in options.items():
        loc = f"variants.options.{name}"
        if not isinstance(name, str) or not name.strip():
            raise TemplateValidationError(f"Variant names in '{tid}' must be non-empty strings.")
        if not isinstance(opt, dict):
            raise TemplateValidationError(f"{loc} in '{tid}' must be an object.")
        extra = set(opt) - ({"label"} | set(_CALC_KEYS))
        if extra:
            raise TemplateValidationError(f"Unknown key(s) {sorted(extra)} in {loc} of '{tid}'.")
        if "label" in opt:
            _validate_localized(opt["label"], f"{loc}.label", tid)
        if "brackets" in opt and "tariff" in opt:
            raise TemplateValidationError(f"{loc} in '{tid}' may override 'brackets' or 'tariff', not both.")
        if "brackets" in opt:
            _validate_brackets(opt["brackets"], f"{loc}.brackets")
        if "tariff" in opt:
            _validate_tariff(opt["tariff"], f"{loc}.tariff", tid)
        if "alternatives" in opt:
            _validate_alternatives(opt["alternatives"], tid, f"{loc}.")
        _validate_calc_entries(opt, tid, f"{loc}.")
    if "default" in spec and spec["default"] not in options:
        raise TemplateValidationError(f"variants.default in '{tid}' is not one of its options.")


def validate_schema(data: Dict) -> None:
    """
    Validate a single template file's schema.

    Module-level (not a loader method) so a future template editor can
    call it directly to validate a candidate dict before saving.
    """
    if not isinstance(data, dict):
        raise TemplateValidationError("Template must be a JSON object.")

    marker = data.get("cognifinance_tax_template")
    if marker is None:
        # Directory-scanning callers treat a missing marker as "not a
        # template file at all" and skip it silently (INFO). This
        # exception is for callers loading a specific, already-chosen
        # template_id, where the absence is worth surfacing.
        raise TemplateValidationError(
            "Template has no 'cognifinance_tax_template' marker; not a "
            "recognized template file."
        )
    if not isinstance(marker, int) or isinstance(marker, bool):
        raise TemplateValidationError(f"Invalid template marker: {marker!r}")
    if marker > SUPPORTED_MARKER_VERSION:
        raise TemplateValidationError(
            f"Template '{data.get('template_id', '?')}' requires format version "
            f"{marker}, but this app supports up to {SUPPORTED_MARKER_VERSION}. "
            f"Update the app to use this template."
        )
    if marker < MIN_SUPPORTED_MARKER_VERSION:
        raise TemplateValidationError(
            f"Unsupported (too old) template marker: {marker!r}"
        )
    if marker < SUPPORTED_MARKER_VERSION:
        # Older but supported format: validate its upgraded form so
        # callers (e.g. a template editor) don't need to know about it.
        data = upgrade_template(data)

    t_type = data.get("template_type")
    if t_type not in _ALLOWED_KEYS_BY_TYPE:
        raise TemplateValidationError(f"Unknown template_type: {t_type!r}")

    # Required-key presence (reserved-prefixed keys don't count).
    if t_type == "jurisdiction_base":
        _require_keys(data, REQUIRED_BASE_KEYS)
    else:
        _require_keys(data, REQUIRED_MODULE_KEYS)

    # Fail-closed on unrecognized keys (excluding reserved prefixes).
    allowed = _ALLOWED_KEYS_BY_TYPE[t_type]
    for key in data.keys():
        if _is_reserved(key):
            continue
        if key not in allowed:
            raise TemplateValidationError(
                f"Unknown key '{key}' in {t_type} '{data.get('template_id', '?')}'. "
                f"Add it to the schema or prefix it with '_' / 'x_' for a reserved extension."
            )

    # Localizable text fields.
    for text_key in ("display_name", "description"):
        if text_key in data:
            _validate_localized(data[text_key], text_key, data.get("template_id", "?"))

    # Type-specific content validation.
    if t_type == "jurisdiction_base":
        tid = data["template_id"]
        year = data["tax_year"]
        if not isinstance(year, int) or isinstance(year, bool):
            raise TemplateValidationError(f"tax_year in '{tid}' must be an integer.")
        if ("brackets" in data) == ("tariff" in data):
            raise TemplateValidationError(
                f"'{tid}' must define exactly one of 'brackets' or 'tariff'."
            )
        if "brackets" in data:
            _validate_brackets(data["brackets"], "brackets")
        else:
            _validate_tariff(data["tariff"], "tariff", tid)
        if "alternatives" in data:
            _validate_alternatives(data["alternatives"], tid)
        if "carryforwards" in data:
            _validate_carryforwards(data["carryforwards"], tid)
        _validate_jurisdiction(data["jurisdiction"], tid)
        if not isinstance(data["currency"], str) or not _CURRENCY_RE.match(data["currency"]):
            raise TemplateValidationError(
                f"currency in '{tid}' must be an ISO 4217 code (e.g. 'CAD')."
            )
        if "tax_period" in data:
            _validate_tax_period(data)
        if "rounding" in data:
            _validate_rounding(data["rounding"], tid)
        _validate_calc_entries(data, tid)
        if "variants" in data:
            _validate_variants(data["variants"], tid)
    elif t_type == "income_module":
        _require_keys(data, ["template_id", "valid_from", "buckets"])
        if not isinstance(data["buckets"], dict) or not data["buckets"]:
            raise TemplateValidationError(
                f"Income module '{data['template_id']}' must declare a non-empty "
                f"'buckets' map."
            )
        _validate_bucket_defs(data["buckets"], data["template_id"])


def _module_accepted_by_base(base: Dict, module: Dict) -> bool:
    """
    True if `base` accepts this module version for its tax_year.

    Per brief: V <= base.tax_year AND (valid_until is null OR base.tax_year <= valid_until).
    Both bounds matter — `valid_until` alone misses modules published
    after the base.
    """
    mod_id = module.get("template_id")
    mod_version = module.get("valid_from")
    if mod_version is None:
        return False

    compat_map = base.get("module_compatibility", {})
    if mod_id not in compat_map:
        return False

    base_year = base.get("tax_year")
    if base_year is None:
        return False

    try:
        version = int(mod_version)
        year = int(base_year)
    except (ValueError, TypeError):
        return False

    for entry in compat_map[mod_id]:
        if not isinstance(entry, dict):
            continue
        if entry.get("version") != mod_version:
            continue
        valid_until = entry.get("valid_until")
        if version > year:
            continue  # module predates the base's tax_year
        if valid_until is None or year <= valid_until:
            return True
    return False


def validate_applies_to(declared_buckets: set, entries: List[Dict]) -> None:
    """Every applies_to reference must resolve to a declared bucket."""
    for entry in entries:
        for ref in entry.get("applies_to", []) or []:
            if ref == "*" or ref in LEVY_INCOME_REFERENCES:
                continue
            if ref not in declared_buckets:
                name = entry.get("name") or entry.get("template_id") or "unnamed entry"
                raise TemplateValidationError(
                    f"Undeclared bucket '{ref}' referenced by '{name}'."
                )


def validate_at_most_one_scale(adjustments: List[Dict]) -> None:
    """At most one `scale` adjustment, per brief."""
    scale_count = sum(1 for a in adjustments if a.get("stage") == "scale")
    if scale_count > 1:
        raise TemplateValidationError(
            f"At most one 'scale' adjustment permitted; found {scale_count}."
        )


def validate_category_map(declared_buckets: set, category_map: Dict[str, Any]) -> None:
    """
    Every category map key references a declared bucket. Account-level
    validation against the chart of accounts lives with the ledger and
    isn't performed here.
    """
    for bucket_ref in category_map.keys():
        if bucket_ref not in declared_buckets:
            raise TemplateValidationError(
                f"Category map references undeclared bucket '{bucket_ref}'."
            )


def classify_base_recency(
    base: Dict[str, Any],
    current_year: Optional[int] = None,
    today: Optional[date] = None,
) -> str:
    """
    "Outdated" (amber, non-dismissible, computes normally) vs "Current"
    (hidden). A base is outdated once today is more than a year past the
    END of its tax period (calendar year unless tax_period is declared).

    Reference date: `today` if given, else Jan 1 of `current_year` if
    given (the old integer rule, kept for callers that only know a year),
    else the real date. For calendar-year bases this is identical to the
    old rule (current_year - tax_year > 1).

    "Incompatible" (red, blocks computation) isn't signaled here -- it's
    a hard load-time failure when a module's valid_from isn't accepted
    by any loaded base.
    """
    try:
        tax_year = int(base.get("tax_year"))
    except (TypeError, ValueError):
        return "current"
    tp = base.get("tax_period")
    try:
        end = date.fromisoformat(tp["end"]) if isinstance(tp, dict) else date(tax_year, 12, 31)
    except (KeyError, TypeError, ValueError):
        return "current"

    if today is None:
        today = date(int(current_year), 1, 1) if current_year is not None else date.today()
    try:
        cutoff = end.replace(year=end.year + 1)
    except ValueError:  # Feb 29
        cutoff = end + timedelta(days=365)
    return "outdated" if today > cutoff else "current"


# ---------------------------------------------------------------------------
# Tax period
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaxPeriod:
    """The date range a tax year covers. Dates are ISO strings, inclusive."""
    label: str      # "2025" or "2025-26"
    start: str
    end: str


def resolve_tax_period(base: Dict[str, Any]) -> TaxPeriod:
    """A base's tax period: its declared tax_period, else the calendar year."""
    year = int(base["tax_year"])
    tp = base.get("tax_period")
    if isinstance(tp, dict):
        start_year, end_year = int(tp["start"][:4]), int(tp["end"][:4])
        label = tp.get("label") or (
            str(start_year) if start_year == end_year else f"{start_year}-{str(end_year)[-2:]}"
        )
        return TaxPeriod(label=label, start=tp["start"], end=tp["end"])
    return TaxPeriod(label=str(year), start=f"{year}-01-01", end=f"{year}-12-31")


# ---------------------------------------------------------------------------
# Resolved set
# ---------------------------------------------------------------------------

@dataclass
class ResolvedTemplateSet:
    # Jurisdiction layers, outermost first (national -> regional -> local),
    # at most one per level.
    layers: List[Dict[str, Any]]
    modules: List[Dict[str, Any]]
    tax_year: int
    tax_period: TaxPeriod
    currency: Optional[str] = None
    # Final-rounding policy {mode, increment}; {} means "half_up at the
    # currency's minor unit".
    rounding: Dict[str, Any] = field(default_factory=dict)
    # The variant (e.g. filing status) requested, and which one each layer
    # actually applied ({level: name}; layers without variants are absent).
    variant: Optional[str] = None
    variants_applied: Dict[str, str] = field(default_factory=dict)

    # Flattened views for the calculator. Credits/levies/adjustments are
    # copies tagged with `_layer` (the level of the base they came from).
    all_buckets: Dict[str, Any] = field(default_factory=dict)
    all_levies: List[Dict] = field(default_factory=list)
    all_credits: List[Dict] = field(default_factory=list)
    all_adjustments: List[Dict] = field(default_factory=list)

    # template_id -> TemplateTier, for attribution/UI badges
    tiers: Dict[str, Any] = field(default_factory=dict)

    def layer(self, level: str) -> Optional[Dict[str, Any]]:
        return next((l for l in self.layers if l["jurisdiction"]["level"] == level), None)

    # Compatibility views for code written against the two-tier model.
    @property
    def federal_base(self) -> Dict[str, Any]:
        return self.layer("national") or {}

    @property
    def provincial_base(self) -> Dict[str, Any]:
        return self.layer("regional") or {}


def module_accepted_by_bases(bases: List[Dict], module: Dict) -> bool:
    """True if ANY of the bases accepts this module (same rule load_layers applies)."""
    return any(_module_accepted_by_base(b, module) for b in bases)


def _apply_variant(base: Dict, variant: Optional[str]) -> Tuple[Dict, Optional[str]]:
    """
    Resolve a base's variant. An option's brackets / credits / levies /
    adjustments REPLACE the base's wholesale; anything it doesn't mention is
    inherited. `variant=None` uses the base's own default (or no variant).
    Returns (layer_dict, applied_name_or_None).
    """
    spec = base.get("variants")
    if not spec:
        return base, None
    options = spec["options"]
    chosen = variant if variant is not None else spec.get("default")
    if chosen is None:
        return base, None
    if chosen not in options:
        raise TemplateValidationError(
            f"'{base.get('template_id')}' has no variant {chosen!r} "
            f"(available: {sorted(options)})."
        )
    out = dict(base)
    opt = options[chosen]
    # A variant that supplies one kind of schedule replaces the other kind.
    if "brackets" in opt:
        out.pop("tariff", None)
    if "tariff" in opt:
        out.pop("brackets", None)
    for key in _CALC_KEYS:
        if key in opt:
            out[key] = copy.deepcopy(opt[key])
    out["x_variant"] = chosen
    return out, chosen


def variant_choices(entries: List[Dict], lang: Optional[str] = None):
    """
    For the UI: the variants that EVERY selected base defining variants
    offers. `entries` are list_available_templates() entries. Returns
    (heading_or_None, [(name, label), ...], default_or_None). Empty choices
    means no selected base has variants (hide the control).
    """
    specs = [e["variants"] for e in entries if e.get("variants")]
    if not specs:
        return None, [], None
    first = specs[0]
    names = [n for n in first["options"] if all(n in s["options"] for s in specs[1:])]
    choices = [
        (n, localized(first["options"][n].get("label"), lang, default=pretty_name(n)))
        for n in names
    ]
    heading = localized(first["label"], lang) if first.get("label") is not None else None
    default = first.get("default") if first.get("default") in names else None
    return heading, choices, default


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

class TaxTemplateLoader:
    def __init__(
        self,
        shipped_dir: str,
        user_dir: str,
        community_dir: Optional[str] = None,
        shipped_manifest: Optional[Dict[str, str]] = None,
        allow_community: bool = False,
    ):
        self.shipped_dir = shipped_dir
        self.user_dir = user_dir
        # Community dir + signing are deferred to Phase 3+ per the brief;
        # allow_community defaults off ("refused until opt-in").
        self.community_dir = community_dir
        self.allow_community = allow_community
        # Build-time artifact: template_id -> sha256 hex digest. Sourced
        # from shipped_manifest.py by default; an explicit argument
        # overrides (useful for tests).
        self.shipped_manifest = (
            shipped_manifest if shipped_manifest is not None
            else SHIPPED_TEMPLATE_MANIFEST
        )

        # Populated as a side effect of _load_json, keyed by template_id.
        self._tiers: Dict[str, TemplateTier] = {}
        # Other (lower-precedence) copies of the same template_id found
        # on disk, for the "newer version elsewhere" hint.
        self._shadowed: Dict[str, List[Tuple[str, str, Dict]]] = {}

    # ------------------------------------------------------------------
    # Trust tiers
    # ------------------------------------------------------------------
    def _classify_tier(self, base_dir: str, path: str, template_id: str) -> TemplateTier:
        if base_dir == self.shipped_dir:
            expected = self.shipped_manifest.get(template_id)
            if expected is None:
                # No manifest entry for a file in the shipped dir -- can't
                # vouch for it. Treat conservatively as modified.
                return TemplateTier(tier="shipped_modified", path=path, hash_matches=False)
            actual = compute_sha256(path)
            if actual == expected:
                return TemplateTier(tier="shipped", path=path, hash_matches=True)
            return TemplateTier(tier="shipped_modified", path=path, hash_matches=False)
        if self.community_dir and base_dir == self.community_dir:
            return TemplateTier(tier="community", path=path, hash_matches=None)
        return TemplateTier(tier="user_authored", path=path, hash_matches=None)

    def get_tier(self, template_id: str) -> Optional[TemplateTier]:
        """Tier of the most recently loaded version of this template_id."""
        return self._tiers.get(template_id)

    def _classify_for_listing(
        self, base_dir: str, path: str, template_id: str
    ) -> TemplateTier:
        """
        Tier classification used by list_available_templates.

        Differs from _classify_tier in one case: when the winning file
        is in user_dir and a shipped copy of the same template_id
        exists, compares their bytes -- byte-identical means the user
        hasn't actually modified anything (report as shipped), byte-
        differing means shipped_modified. Falls back to user_authored
        if the shipped copy is unreadable.
        """
        if base_dir != self.user_dir:
            return self._classify_tier(base_dir, path, template_id)

        shipped_path = os.path.join(self.shipped_dir, f"{template_id}.json")
        if not os.path.exists(shipped_path):
            return TemplateTier(tier="user_authored", path=path, hash_matches=None)

        try:
            with open(shipped_path, "rb") as f:
                shipped_bytes = f.read()
            with open(path, "rb") as f:
                user_bytes = f.read()
        except OSError:
            return TemplateTier(tier="user_authored", path=path, hash_matches=None)

        if shipped_bytes == user_bytes:
            # Byte-identical -- functionally the shipped file, even
            # though it lives in user_dir.
            return self._classify_tier(self.shipped_dir, shipped_path, template_id)

        return TemplateTier(tier="shipped_modified", path=path, hash_matches=False)

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------
    def _precedence_dirs(self) -> List[str]:
        # user > shipped > community; community only if opted in.
        dirs = [self.user_dir, self.shipped_dir]
        if self.community_dir and self.allow_community:
            dirs.append(self.community_dir)
        return dirs

    def _load_json(self, template_id: str) -> Dict:
        found: List[Tuple[str, str, Dict]] = []
        for base_dir in self._precedence_dirs():
            path = os.path.join(base_dir, f"{template_id}.json")
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, json.JSONDecodeError) as e:
                    raise TemplateValidationError(
                        f"Failed to load template '{template_id}' from '{path}': {e}"
                    )
                found.append((base_dir, path, data))

        if not found:
            raise TemplateValidationError(f"Template '{template_id}' not found.")

        winning_dir, winning_path, winning_data = found[0]
        # Everything downstream sees the current format; older files are
        # upgraded on a copy (the file on disk is never touched).
        winning_data = upgrade_template(winning_data)

        # A user-local file must not shadow a shipped file of a different
        # template_type -- silent type confusion is worse than a missing
        # file. Checked specifically against the shipped copy, per brief.
        if winning_dir == self.user_dir:
            shipped_path = os.path.join(self.shipped_dir, f"{template_id}.json")
            if os.path.exists(shipped_path) and shipped_path != winning_path:
                try:
                    with open(shipped_path, "r", encoding="utf-8") as f:
                        shipped_data = json.load(f)
                    def _identity(d):
                        if not isinstance(d, dict):
                            return None
                        d = upgrade_template(d)
                        return (d.get("template_type"), (d.get("jurisdiction") or {}).get("level"))

                    shipped_id = _identity(shipped_data)
                    if shipped_id and shipped_id[0] and shipped_id != _identity(winning_data):
                        raise TemplateValidationError(
                            f"User-local '{template_id}' ({_identity(winning_data)}) shadows a "
                            f"shipped template of a different type/level ({shipped_id}). "
                            f"Rename one of the files."
                        )
                except (OSError, json.JSONDecodeError):
                    pass  # shipped file unreadable -- not this check's concern

        self._tiers[template_id] = self._classify_tier(
            winning_dir, winning_path, template_id
        )
        self._shadowed[template_id] = [
            (d, pth, upgrade_template(raw)) for d, pth, raw in found[1:]
        ]

        return winning_data

    # ------------------------------------------------------------------
    # Picker enumeration
    # ------------------------------------------------------------------
    def list_available_templates(self, lang: Optional[str] = None) -> Dict[str, List]:
        """
        Scan shipped/user (and community, if opted in) dirs for candidate
        template files, returning only ones that validate.

        Never raises. Marker semantics per brief:
          - no marker at all -> not a template file; skip silently (INFO)
          - marker newer than supported -> skip, but surface an
            actionable message (the picker should show this, not just
            drop it silently)
          - any other validation failure -> skip, logged + surfaced
        """
        accepted: List[Dict] = []
        skip_messages: List[str] = []
        seen_ids = set()

        for base_dir in self._precedence_dirs():
            if not os.path.isdir(base_dir):
                continue
            for fname in sorted(os.listdir(base_dir)):
                if not fname.endswith(".json"):
                    continue
                template_id = fname[:-5]
                if template_id in seen_ids:
                    continue  # higher-precedence dir already resolved this id
                path = os.path.join(base_dir, fname)

                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, json.JSONDecodeError) as e:
                    logger.info(f"Skipping unreadable file '{path}': {e}")
                    continue
                if not isinstance(data, dict):
                    continue

                marker = data.get("cognifinance_tax_template")
                if marker is None:
                    logger.info(f"Skipping '{path}': no template marker (not a template file).")
                    continue
                if (
                    isinstance(marker, int)
                    and not isinstance(marker, bool)
                    and marker > SUPPORTED_MARKER_VERSION
                ):
                    msg = (
                        f"'{template_id}' requires a newer app version (format "
                        f"{marker}; this app supports up to {SUPPORTED_MARKER_VERSION})."
                    )
                    logger.warning(msg)
                    skip_messages.append(msg)
                    continue

                data = upgrade_template(data)
                try:
                    validate_schema(data)
                except TemplateValidationError as e:
                    logger.warning(f"Skipping invalid template '{path}': {e}")
                    skip_messages.append(f"'{template_id}': {e}")
                    continue

                is_base = data.get("template_type") == "jurisdiction_base"
                seen_ids.add(template_id)
                accepted.append({
                    "template_id": template_id,
                    # Older-format files report their current-format type.
                    "template_type": data.get("template_type"),
                    "display_name": localized(data.get("display_name"), lang, template_id),
                    "tax_year": data.get("tax_year"),
                    "valid_from": data.get("valid_from"),
                    "jurisdiction": data.get("jurisdiction"),
                    "currency": data.get("currency"),
                    "tax_period": resolve_tax_period(data) if is_base else None,
                    "module_compatibility": data.get("module_compatibility"),
                    "variants": data.get("variants"),
                    "tier": self._classify_for_listing(base_dir, path, template_id),
                    "path": path,
                })

        return {"accepted": accepted, "skipped": skip_messages}

    # ------------------------------------------------------------------
    # Set loading
    # ------------------------------------------------------------------
    def load_set(
        self,
        federal_id: str,
        provincial_id: str,
        module_ids: List[str],
        ledger_currency: Optional[str] = None,
    ) -> ResolvedTemplateSet:
        """Two-tier convenience wrapper around load_layers()."""
        return self.load_layers([federal_id, provincial_id], module_ids, ledger_currency)

    def load_layers(
        self,
        base_ids: List[str],
        module_ids: List[str],
        ledger_currency: Optional[str] = None,
        variant: Optional[str] = None,
    ) -> ResolvedTemplateSet:
        """
        Load a set: one or more jurisdiction bases (at most one per level,
        all for the same country, tax year, period and currency) plus
        income modules. Layers are returned outermost first regardless of
        the order given.
        """
        if not base_ids:
            raise TemplateValidationError("At least one jurisdiction template is required.")
        if len(set(base_ids)) != len(base_ids):
            raise TemplateValidationError("The same jurisdiction template was selected twice.")

        layers: List[Dict[str, Any]] = []
        for b_id in base_ids:
            base = self._load_json(b_id)
            validate_schema(base)
            if base.get("template_type") != "jurisdiction_base":
                raise TemplateValidationError(
                    f"Template '{b_id}' is a {base.get('template_type')!r}, "
                    f"not a jurisdiction base."
                )
            layers.append(base)
        # Filing-status style variants, resolved per layer that defines them.
        variants_applied: Dict[str, str] = {}
        for i, base in enumerate(layers):
            layers[i], applied = _apply_variant(base, variant)
            if applied:
                variants_applied[base["jurisdiction"]["level"]] = applied
        layers.sort(key=lambda b: _LEVEL_RANK[b["jurisdiction"]["level"]])

        levels = [b["jurisdiction"]["level"] for b in layers]
        first = layers[0]
        for other in layers[1:]:
            if other["jurisdiction"]["country"] != first["jurisdiction"]["country"]:
                raise TemplateValidationError(
                    f"Layers must be for the same country ('{first['template_id']}' is "
                    f"{first['jurisdiction']['country']}, '{other['template_id']}' is "
                    f"{other['jurisdiction']['country']})."
                )
            if other["currency"] != first["currency"]:
                raise TemplateValidationError(
                    f"Layers must share one currency ('{first['template_id']}' uses "
                    f"{first['currency']}, '{other['template_id']}' uses {other['currency']})."
                )
            if (
                other["tax_year"] != first["tax_year"]
                or resolve_tax_period(other) != resolve_tax_period(first)
            ):
                raise TemplateValidationError(
                    "Jurisdiction layers must share the same tax year and period."
                )

        # Checked after country/currency/year so a mismatch reports the more
        # useful message (two "national" layers from different countries is
        # a country problem first).
        if len(set(levels)) != len(levels):
            raise TemplateValidationError(
                f"Only one layer per level is allowed; got levels {levels}."
            )

        tax_year = first["tax_year"]
        tax_period = resolve_tax_period(first)
        currency = first["currency"]
        if ledger_currency and ledger_currency.upper() != currency:
            raise TemplateValidationError(
                f"These templates compute in {currency}, but the ledger is in "
                f"{ledger_currency.upper()}. Choose templates for the ledger's currency."
            )
        rounding = next((b["rounding"] for b in layers if b.get("rounding")), {})

        # Load and validate modules
        modules = []
        for m_id in module_ids:
            mod = self._load_json(m_id)
            validate_schema(mod)

            # A4: module is accepted if ANY loaded base declares it
            if not module_accepted_by_bases(layers, mod):
                # Multiple versions on disk: a newer accepted copy might be
                # sitting in a lower-precedence directory, shadowed by the
                # one we just rejected. Surface that as an actionable hint
                # rather than making the user guess.
                hint = ""
                for _, alt_path, alt_data in self._shadowed.get(m_id, []):
                    if module_accepted_by_bases(layers, alt_data):
                        hint = (
                            f" A compatible version of '{m_id}' exists at '{alt_path}' "
                            f"in a lower-precedence directory -- move it up or delete "
                            f"the stale copy."
                        )
                        break
                raise TemplateValidationError(
                    f"Module '{m_id}' (valid_from={mod.get('valid_from')}) "
                    f"not accepted by any loaded base for tax year {tax_year}.{hint}"
                )
            modules.append(mod)

        # Flatten buckets, checking uniqueness across all modules
        all_buckets: Dict[str, Any] = {}
        for mod in modules:
            for bucket_name, bucket_def in mod.get("buckets", {}).items():
                if bucket_name in all_buckets:
                    raise TemplateValidationError(
                        f"Bucket '{bucket_name}' declared in multiple modules."
                    )
                all_buckets[bucket_name] = bucket_def

        # A bucket that consumes a carry-forward needs a base that also
        # produces it from that same bucket; otherwise losses would be
        # applied but never carried on (or carried out under a name nothing
        # reads). Fail loudly rather than silently lose a loss.
        declared_carryforwards = {
            cf.get("name"): cf
            for layer in layers for cf in (layer.get("carryforwards") or [])
        }
        for bucket_name, bucket_def in all_buckets.items():
            ref = bucket_def.get("carryforward_in")
            if not ref:
                continue
            cf = declared_carryforwards.get(ref)
            if cf is None or cf.get("source") != "negative_bucket" or cf.get("bucket") != bucket_name:
                raise TemplateValidationError(
                    f"Bucket '{bucket_name}' applies carry-forward '{ref}', but no loaded base declares "
                    f"a negative_bucket carryforward named '{ref}' for that bucket. Add one to the "
                    f"jurisdiction base (or update it to the version that includes it)."
                )

        # Collect credits/levies/adjustments from every layer. Each entry is
        # a copy tagged with its layer (`_layer` = the base's level): the
        # calculator applies credits and post adjustments to that layer's
        # own tax rather than to one pooled figure.
        def _tagged(key: str) -> List[Dict]:
            out = []
            for base in layers:
                level = base["jurisdiction"]["level"]
                out.extend(dict(e, _layer=level) for e in base.get(key, []) or [])
            return out

        all_credits = _tagged("credits")
        all_levies = _tagged("levies")
        all_adjustments = _tagged("adjustments")

        # Scale (income-splitting style) adjustments: one shared across the
        # set, and at most one layer-scoped per layer.
        shared_scale = [a for a in all_adjustments if a.get("stage") == "scale" and a.get("scope") != "layer"]
        if len(shared_scale) > 1:
            raise TemplateValidationError("Only one shared 'scale' adjustment is allowed per template set.")
        for level in levels:
            own = [a for a in all_adjustments
                   if a.get("stage") == "scale" and a.get("scope") == "layer" and a.get("_layer") == level]
            if len(own) > 1:
                raise TemplateValidationError(f"Only one layer-scoped 'scale' adjustment is allowed on the {level} layer.")

        # Cross-file validation
        # B5: validate applies_to references.
        #
        # A base's own applies_to references count as declarations of
        # those bucket names. If the national base declares a CPP employee
        # levy that applies to "employment", then "employment" is a
        # legitimate bucket in this jurisdiction's vocabulary -- whether
        # or not a module loaded this session populates it. A levy
        # against an unpopulated bucket simply contributes zero, which is
        # the correct behavior for a user with no such income.
        #
        # The validation still catches genuine typos: a base referencing
        # a bucket that no module and no base ever declares is a mistake
        # worth surfacing loudly.
        declared = set(all_buckets.keys())
        for entry in (*all_credits, *all_levies, *all_adjustments):
            for ref in entry.get("applies_to", []) or []:
                if ref != "*" and ref not in LEVY_INCOME_REFERENCES:
                    declared.add(ref)

        validate_applies_to(declared, all_credits)
        validate_applies_to(declared, all_levies)
        validate_applies_to(declared, all_adjustments)

        tiers = {
            tid: self._tiers[tid]
            for tid in [*base_ids, *module_ids]
            if tid in self._tiers
        }

        return ResolvedTemplateSet(
            layers=layers,
            modules=modules,
            tax_year=tax_year,
            tax_period=tax_period,
            currency=currency,
            rounding=rounding,
            variant=variant,
            variants_applied=variants_applied,
            all_buckets=all_buckets,
            all_levies=all_levies,
            all_credits=all_credits,
            all_adjustments=all_adjustments,
            tiers=tiers,
        )

    # ------------------------------------------------------------------
    # Cross-section validation (B8)
    # ------------------------------------------------------------------
    @staticmethod
    def find_user_input_conflicts(
        sets: List[ResolvedTemplateSet],
    ) -> Dict[str, List[int]]:
        """
        Given multiple loaded template sets (one per profile section),
        return a mapping of user_input field name -> list of section
        indices that declare it. Any field appearing more than once is
        a load-time error per the brief.
        """
        seen: Dict[str, List[int]] = {}
        for idx, ts in enumerate(sets):
            refs = set()
            for credit in ts.all_credits:
                if credit.get("value_source") == "user_input":
                    refs.add(credit.get("name", ""))
            for levy in ts.all_levies:
                if levy.get("value_source") == "user_input":
                    refs.add(levy.get("name", ""))
            for adj in ts.all_adjustments:
                if adj.get("value_source") == "user_input":
                    refs.add(adj.get("name", ""))
            for name in refs:
                if not name:
                    continue
                seen.setdefault(name, []).append(idx)
        return {k: v for k, v in seen.items() if len(v) > 1}
