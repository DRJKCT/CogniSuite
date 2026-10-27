# tax_calculator.py
"""
tax_calculator.py ver 3.0
For CogniFinance ver 1.9

ver 4.0 vocabulary (all declared by templates, none country-specific):
  * `threshold_rates` adjustments: sum / min / max of "rate x amount over a
    threshold" terms (surtaxes, phase-in surcharges), and a `post_credit`
    stage that acts on a layer's tax AFTER non-refundable credits.
  * `scope: "layer"` deduction / pre / scale adjustments: a layer's own
    taxable income (US-state / cantonal style).
  * `tariff`: polynomial-zone tax schedule as an alternative to `brackets`.
  * `alternatives`: a parallel calculation (own base, add-backs, exemption,
    schedule) compared with the regular tax -- max for minimum-tax rules,
    min for benefit caps.
  * `foreign_tax_credit` credits (limited to the foreign share of the tax)
    and declared `carryforwards` reported for the next year.

ver 3.0 vocabulary:
  * `separate_rate` bucket primitive: income taxed OUTSIDE the progressive
    brackets, per layer, at a flat `rate` or by its own bracket table
    stacked on top of the ordinary income. Optional `exempt_amount`.
  * `banded_rate` levy: a different rate per income band (e.g. payroll
    contributions with a main rate up to a ceiling and a lower rate above).
  * `refundable: true` credits: applied after the non-refundable ones and
    allowed to take a layer's tax below zero (a refund).
  * threshold_bucket credits name their own `income_reference`.

ver 2.0: the pipeline runs per jurisdiction LAYER (national / regional /
local -- however many the template set carries) instead of assuming
federal + provincial:
  * brackets, post adjustments and credits are applied to each layer's own
    tax, and non-refundable credits are floored at that layer's tax
    (a federal credit can no longer reduce provincial tax);
  * working precision and the final rounding policy come from the
    template set's currency / rounding, not a hard-coded "cents, half-up".
"""
import logging
from decimal import Decimal
from typing import Dict, Any, List, Tuple
from ledger import ZERO
from tax_format import ROUNDING_MODES, currency_minor_units, round_to_increment

logger = logging.getLogger(__name__)


class ComputationBlockedError(Exception):
    """
    Raised when a standalone value_source: user_input field (one with no
    account-derived fallback -- e.g. an age credit, a benefit clawback,
    retirement contributions, a threshold income reference) is
    present-but-blank: the key
    exists in scope for this template set but the user hasn't entered a
    value and hasn't explicitly skipped it either.

    Per brief: "empty user_input refuses to compute with an explanatory
    message." Distinct from a bucket's optional user_input override,
    where absence just means accounts drive -- that path never raises.
    """

    def __init__(self, field_name: str, message: str = None):
        self.field_name = field_name
        super().__init__(
            message
            or (
                f"'{field_name}' needs a value before this estimate can be computed. "
                f"Enter a value, or explicitly skip it (skipping treats it as 0 and "
                f"adds a footnote)."
            )
        )


def _as_decimal(val, default=None) -> Decimal | None:
    """Best-effort conversion; returns default on failure."""
    if val is None:
        return default
    try:
        return Decimal(str(val))
    except Exception:
        return default


class TaxCalculator:
    """
    Executes the 8-stage tax calculation pipeline defined in the brief:

      1. Sum income buckets              -> total income
      2. Apply deductions                -> net income
      3. Apply pre adjustments           -> adjusted taxable income
      4. Bracket calculation             -> income tax owed
      5. Scale adjustment (if any)
      6. Post adjustments                -> adjusted income tax owed
      7. Credits applied                 -> income tax after credits
      8. Levies computed and added       -> total tax

    Pure interpreter: knows only the closed primitive set from the brief.
    Every rate, threshold, and bucket declaration comes from the loaded
    template set; every user-specific figure comes from the profile.

    compute() may raise ComputationBlockedError if a standalone
    value_source: user_input field (no account-derived fallback) is
    present in the template set but neither entered nor explicitly
    skipped. Callers should catch this and prompt the user rather than
    treating it as a computation failure.
    """

    def __init__(self, template_set, profile_data: Dict, ledger_aggregates: Dict):
        self.ts = template_set
        self.profile = profile_data
        self.aggregates = ledger_aggregates

        # Pipeline state
        self.total_income = ZERO
        self.net_income = ZERO
        self.adjusted_taxable_income = ZERO
        # Income taxed outside the brackets (separate_rate buckets) is carved
        # out of the bracket base: ordinary = adjusted taxable - separate.
        self.ordinary_taxable_income = ZERO
        self._separate: Dict[str, Dict[str, Any]] = {}
        # Per-layer bases: a layer may narrow the shared base further.
        self._layer_base: Dict[str, Decimal] = {}
        self._layer_ordinary: Dict[str, Decimal] = {}
        # Pre-clamp size of buckets that came in negative (loss carryforwards).
        self._negative_raw: Dict[str, Decimal] = {}
        # Carried-in amounts a bucket (via `carryforward_in`) did not use up,
        # keyed by carryforward name; they roll on to next year.
        self._carry_unused: Dict[str, Decimal] = {}
        self.carryforwards_out: Dict[str, Decimal] = {}
        self.income_tax_owed = ZERO
        self.tax_after_credits = ZERO
        self.total_tax = ZERO

        # Currency-aware precision. Intermediate math keeps at least cents
        # (more for 3-decimal currencies); the template's rounding policy
        # applies only to the amounts actually payable (see _final).
        self.currency = getattr(template_set, "currency", None)
        minor = currency_minor_units(self.currency)
        self._work_quantum = Decimal(1).scaleb(-max(2, minor))
        rounding = getattr(template_set, "rounding", None) or {}
        self._final_mode = rounding.get("mode", "half_up")
        if self._final_mode not in ROUNDING_MODES:
            raise ValueError(f"Unknown rounding mode: {self._final_mode}")
        self._final_increment = (
            _as_decimal(rounding.get("increment")) or Decimal(1).scaleb(-minor)
        )

        # Detailed breakdown for UI/Attribution
        self.breakdown = {
            "buckets": {},
            "deductions": ZERO,
            "pre_adjustments": ZERO,
            "post_adjustments": ZERO,
            "levies": {},
            "credits_applied": ZERO,
            "credits_unused": ZERO,
            "refundable_credits": ZERO,
            "post_credit_adjustments": ZERO,
            "carryforwards": {},
            "separate_income": ZERO,
            "ordinary_taxable_income": ZERO,
            "layers": {},
            "skipped_inputs": profile_data.get("skipped_inputs", []),
            "warnings": [],
        }

    # ------------------------------------------------------------------
    # Rounding
    # ------------------------------------------------------------------
    def _q(self, val: Decimal) -> Decimal:
        """Working precision: half-up at the currency's minor unit (>= cents)."""
        from decimal import ROUND_HALF_UP
        return Decimal(val).quantize(self._work_quantum, rounding=ROUND_HALF_UP)

    def _final(self, val: Decimal) -> Decimal:
        """The template's payable-amount rounding (default: half-up to the minor unit)."""
        return round_to_increment(Decimal(val), self._final_increment, self._final_mode)

    # ------------------------------------------------------------------
    # Standalone user_input fields (no account-derived fallback)
    # ------------------------------------------------------------------
    def _resolve_user_input_only(self, field_name: str) -> Decimal:
        """
        Three-state resolution for a value_source: user_input field that
        has no account-derived source at all (age credit, clawbacks,
        retirement contributions, threshold income references).

          - skipped -> 0, footnote already carried via breakdown["skipped_inputs"]
          - entered (including explicit 0) -> that value
          - absent -> blocked; caller should surface the message to the
            user with an option to enter or skip
        """
        if field_name in self.profile.get("skipped_inputs", []):
            return ZERO

        entry = self.profile.get("user_inputs", {}).get(field_name)
        if isinstance(entry, dict):
            entry = entry.get("value")
        dec = _as_decimal(entry)
        if dec is None:
            raise ComputationBlockedError(field_name)
        return self._q(dec)

    def _carried_in(self, name: str) -> Decimal:
        """Amount carried in from earlier years under `name`. Never blocks:
        a user with nothing carried forward simply has none (absent or
        skipped -> 0), and a negative figure is treated as 0."""
        if name in self.profile.get("skipped_inputs", []):
            return ZERO
        entry = self.profile.get("user_inputs", {}).get(name)
        if isinstance(entry, dict):
            entry = entry.get("value")
        dec = _as_decimal(entry)
        if dec is None or dec < ZERO:
            return ZERO
        return self._q(dec)

    # ------------------------------------------------------------------
    # Bucket resolution (three-state: absent / entered / skipped)
    # ------------------------------------------------------------------
    def _get_bucket_raw_amount(self, bucket_name: str) -> Decimal:
        """Resolve user_input override vs ledger aggregate per the brief."""
        user_inputs = self.profile.get("user_inputs", {})

        # 1. Skipped — computes as 0, footnote fires elsewhere
        if bucket_name in self.profile.get("skipped_inputs", []):
            return ZERO

        # 2. Entered — override (including explicit 0)
        if bucket_name in user_inputs:
            entry = user_inputs[bucket_name]
            if isinstance(entry, dict):
                val = entry.get("value")
            else:
                val = entry  # tolerate a bare value shape
            dec = _as_decimal(val)
            if dec is not None:
                return self._q(dec)

        # 3. Absent — fall back to ledger aggregate
        return self._q(self.aggregates.get(bucket_name, ZERO))

    # ------------------------------------------------------------------
    # Primitive dispatch
    # ------------------------------------------------------------------
    @staticmethod
    def _credit_rates(primitive_def: Dict) -> Dict[str, Decimal]:
        """
        Per-layer credit rates for a gross_up_credit: {level: rate}.
        Reads `credit_rates` ({"national": 0.15, "regional": 0.10}); falls
        back to the format-1 fields federal_credit_rate / provincial_credit_rate.
        """
        rates = {
            lv: _as_decimal(r, ZERO)
            for lv, r in (primitive_def.get("credit_rates") or {}).items()
        }
        for legacy_key, level in (
            ("federal_credit_rate", "national"),
            ("provincial_credit_rate", "regional"),
        ):
            if level not in rates and legacy_key in primitive_def:
                rates[level] = _as_decimal(primitive_def[legacy_key], ZERO)
        return rates

    def _apply_primitive(
        self, bucket_name: str, raw_amount: Decimal, primitive_def: Dict
    ) -> Tuple[Decimal, Dict[str, Decimal]]:
        """
        Apply the closed-set calculation primitive.

        Returns (taxable_amount, credit_by_level). Returning credits rather
        than mutating instance state keeps pipeline data flow explicit --
        a bucket-processing step shouldn't silently alter a pool that a
        later stage sums.
        """
        p_type = primitive_def.get("primitive", "inclusion_rate")

        if p_type == "inclusion_rate":
            rate = _as_decimal(primitive_def.get("rate", 1.0), Decimal("1.0"))
            taxable = self._q(raw_amount * rate)
            self.breakdown["buckets"][bucket_name] = {"raw": raw_amount, "taxable": taxable}
            return taxable, {}

        if p_type == "gross_up_credit":
            gu_rate = _as_decimal(primitive_def.get("gross_up_rate", 0.0), ZERO)
            grossed_up = self._q(raw_amount * (1 + gu_rate))
            credits = {
                level: self._q(grossed_up * rate)
                for level, rate in self._credit_rates(primitive_def).items()
            }
            self.breakdown["buckets"][bucket_name] = {"raw": raw_amount, "taxable": grossed_up}
            return grossed_up, credits

        if p_type == "separate_rate":
            exempt = _as_decimal(primitive_def.get("exempt_amount", 0), ZERO)
            taxable = self._q(max(ZERO, raw_amount - exempt))
            self.breakdown["buckets"][bucket_name] = {"raw": raw_amount, "taxable": taxable}
            self._separate[bucket_name] = {
                "amount": taxable,
                "schedules": primitive_def.get("schedules") or {},
            }
            return taxable, {}

        raise ValueError(f"Unknown primitive: {p_type}")

    def _separate_tax(self, level: str) -> Decimal:
        """
        Tax on the separate_rate buckets for one layer. A schedule is either a
        flat {"rate"} or {"brackets"} STACKED on the income below it: the tax
        is T(base + amount) - T(base), where the base starts at the ordinary
        taxable income and grows as each bucket (in name order) is added, so
        the bracket thresholds refer to total taxable income. A bucket with
        no schedule for this layer is untaxed here but still occupies room.
        """
        running = self._layer_ordinary.get(level, self.ordinary_taxable_income)
        total = ZERO
        for name in sorted(self._separate):
            info = self._separate[name]
            amount = info["amount"]
            sched = (info["schedules"] or {}).get(level)
            if sched is not None and amount > ZERO:
                if "rate" in sched:
                    tax = self._q(amount * _as_decimal(sched["rate"], ZERO))
                elif "brackets" in sched:
                    tax = self._q(
                        self._calc_brackets(running + amount, sched["brackets"])
                        - self._calc_brackets(running, sched["brackets"])
                    )
                else:
                    raise ValueError(f"separate_rate schedule for '{name}' needs 'rate' or 'brackets'.")
                total += tax
                self.breakdown["buckets"][name].setdefault("separate_tax", {})[level] = tax
            running += amount
        return total

    # ------------------------------------------------------------------
    # Bracket calculation
    # ------------------------------------------------------------------
    def _calc_brackets(self, income: Decimal, brackets: List[Dict]) -> Decimal:
        """
        Standard progressive bracket calculation, deterministic rounding.

        Each bracket's `threshold` is the LOWER bound of the income range
        that rate applies to (schema requires the first at 0, strictly
        ascending thereafter) -- the upper bound of a bracket is the next
        bracket's threshold, or open-ended for the last one. Each band's
        own rate applies to the slice between its lower and upper bound.
        """
        if income <= ZERO or not brackets:
            return ZERO

        sorted_brackets = sorted(brackets, key=lambda b: Decimal(str(b["threshold"])))

        tax = ZERO
        for i, b in enumerate(sorted_brackets):
            lower = Decimal(str(b["threshold"]))
            if income <= lower:
                break

            rate = Decimal(str(b["rate"]))
            upper = (
                Decimal(str(sorted_brackets[i + 1]["threshold"]))
                if i + 1 < len(sorted_brackets)
                else None
            )
            taxable_in_bracket = (min(income, upper) - lower) if upper is not None else (income - lower)

            if taxable_in_bracket > ZERO:
                tax += self._q(taxable_in_bracket * rate)

        return tax

    def _calc_tariff(self, income: Decimal, tariff: Dict) -> Decimal:
        """
        Polynomial-zone tariff. The zone used is the last one whose `from`
        is <= income; within it  tax = sum(c_k * t^k)  with
        t = (income - offset) / divisor. Optional `income_rounding` is
        applied to the income first (some tariffs are defined on whole units).
        """
        rounding = tariff.get("income_rounding")
        if rounding:
            income = round_to_increment(
                income,
                _as_decimal(rounding.get("increment"), Decimal(1)) or Decimal(1),
                rounding.get("mode", "half_up"),
            )
        if income <= ZERO:
            return ZERO
        zone = None
        for z in sorted(tariff["zones"], key=lambda z: Decimal(str(z["from"]))):
            if income >= Decimal(str(z["from"])):
                zone = z
            else:
                break
        if zone is None:
            return ZERO
        offset = Decimal(str(zone.get("offset", 0)))
        divisor = Decimal(str(zone.get("divisor", 1)))
        t = (income - offset) / divisor
        total, power = ZERO, Decimal(1)
        for coefficient in zone["coefficients"]:
            total += Decimal(str(coefficient)) * power
            power *= t
        return self._q(max(ZERO, total))

    def _schedule_tax(self, holder: Dict, income: Decimal) -> Decimal:
        """Tax on `income` under a layer's (or alternative's) tariff or brackets."""
        if "tariff" in holder:
            return self._calc_tariff(income, holder["tariff"])
        return self._calc_brackets(income, holder.get("brackets", []))

    def _scale_factor_for(self, level: str):
        """Income-splitting factor for a layer: its own scale adjustment, else the shared one."""
        chosen = None
        for adj in self.ts.all_adjustments:
            if adj.get("stage") != "scale":
                continue
            if adj.get("scope") == "layer":
                if adj.get("_layer") == level:
                    chosen = adj
                    break
            elif chosen is None:
                chosen = adj
        if chosen is None:
            return None
        factor = _as_decimal(chosen.get("factor", 1), Decimal("1"))
        if factor <= ZERO:
            raise ValueError("Scale adjustment factor must be positive.")
        return factor

    def _alternative_tax(self, alt: Dict, layer: Dict, layer_base: Decimal) -> Decimal:
        """A parallel calculation: own base (+ add-backs - exemption), own or the layer's schedule."""
        bases = {
            "total_income": self.total_income,
            "net_income": self.net_income,
            "adjusted_taxable_income": layer_base,
        }
        value = bases[alt.get("base", "adjusted_taxable_income")]
        for bucket in alt.get("add_back", []) or []:
            value += self.breakdown["buckets"].get(bucket, {}).get("raw", ZERO)
        exemption = alt.get("exemption")
        if exemption:
            # A tapered exemption phases out against the base BEFORE it is applied.
            value -= self._resolve_adjustment_amount(exemption, value)
        value = max(ZERO, value)
        holder = alt if ("brackets" in alt or "tariff" in alt) else layer
        tax = self._schedule_tax(holder, value)
        return self._q(max(ZERO, tax - _as_decimal(alt.get("subtract", 0), ZERO)))

    def _apply_alternatives(self, layer: Dict, regular: Decimal, layer_base: Decimal):
        """Compare the regular tax with each alternative in order; returns (tax, [names that won])."""
        tax, won = regular, []
        for alt in layer.get("alternatives") or []:
            alt_tax = self._alternative_tax(alt, layer, layer_base)
            if (alt["combine"] == "max" and alt_tax > tax) or (alt["combine"] == "min" and alt_tax < tax):
                tax = alt_tax
                won.append(alt["name"])
        return tax, won

    # ------------------------------------------------------------------
    # Adjustment helpers
    # ------------------------------------------------------------------
    def _resolve_adjustment_amount(
        self, adj: Dict, current_value: Decimal
    ) -> Decimal:
        """
        Resolve an adjustment entry's magnitude, regardless of stage.

        Handles:
          - flat: literal value, or applies_to bucket total, or user_input
          - tapered: base amount reducing toward a floor as current_value
            exceeds a threshold
          - rate: rate × current_value
        """
        a_type = adj.get("type", "flat")

        if a_type == "flat":
            # Bucket-sourced (e.g. union dues)
            if adj.get("applies_to"):
                total = ZERO
                for ref in adj["applies_to"]:
                    total += self.breakdown["buckets"].get(ref, {}).get("raw", ZERO)
                return self._q(total)
            # User input sourced (e.g. RRSP contributions) -- no account
            # fallback exists for these, so blank (non-skipped) blocks.
            if adj.get("value_source") == "user_input":
                field_name = adj.get("name", "")
                return self._resolve_user_input_only(field_name)
            # Literal
            dec = _as_decimal(adj.get("value", 0), ZERO)
            return self._q(dec)

        if a_type == "tapered":
            base = _as_decimal(adj.get("base", 0), ZERO)
            threshold = _as_decimal(adj.get("threshold", 0), ZERO)
            reduction_rate = _as_decimal(adj.get("reduction_rate", 0), ZERO)
            floor = _as_decimal(adj.get("floor", 0), ZERO)

            if current_value <= threshold:
                return self._q(base)
            excess = current_value - threshold
            reduced = base - (excess * reduction_rate)
            return self._q(max(floor, reduced))

        if a_type == "rate":
            rate = _as_decimal(adj.get("rate", 0), ZERO)
            return self._q(rate * current_value)

        if a_type == "threshold_rates":
            # Each component is  rate x max(0, value - threshold);  combined
            # by sum (a surtax written as "20% over T1 plus 36% over T2"),
            # min (a phase-in capped by a full-rate charge) or max.
            parts = [
                self._q(
                    _as_decimal(c.get("rate", 0), ZERO)
                    * max(ZERO, current_value - _as_decimal(c.get("threshold", 0), ZERO))
                )
                for c in adj.get("components", [])
            ]
            if not parts:
                return ZERO
            combine = adj.get("combine", "sum")
            if combine == "sum":
                return self._q(sum(parts, ZERO))
            if combine == "min":
                return min(parts)
            if combine == "max":
                return max(parts)
            raise ValueError(f"Unknown threshold_rates combine mode: {combine}")

        raise ValueError(f"Unknown adjustment type: {a_type}")

    def _apply_adjustments_at_stage(
        self, stage: str, current_value: Decimal, layer: str = None, layer_scoped: bool = False
    ) -> Tuple[Decimal, Decimal]:
        """
        Apply all adjustments at a given stage.

        Returns (new_value, total_magnitude_applied). Direction is explicit
        per the brief: pre adjustments default to subtracting from income;
        post adjustments default to adding to tax owed. `direction` in the
        template can override either default.

        Which adjustments run:
          * post / post_credit: only the given layer's (they act on that
            layer's tax);
          * deduction / pre / scale, layer_scoped=False: the SHARED ones
            (scope != "layer"), applied once to the common base;
          * deduction / pre / scale, layer_scoped=True: only those scoped
            to `layer`, applied on top of the shared result.
        """
        total_applied = ZERO
        default_direction = "subtract" if stage in ("deduction", "pre") else "add"
        default_layer = self.ts.layers[0]["jurisdiction"]["level"] if self.ts.layers else None
        per_layer_stage = stage in ("post", "post_credit")

        for adj in self.ts.all_adjustments:
            if adj.get("stage") != stage:
                continue
            own_layer = adj.get("_layer") or default_layer
            if per_layer_stage:
                if layer is not None and own_layer != layer:
                    continue
            elif layer_scoped:
                if adj.get("scope") != "layer" or own_layer != layer:
                    continue
            elif adj.get("scope") == "layer":
                continue

            magnitude = self._resolve_adjustment_amount(adj, current_value)
            direction = adj.get("direction", default_direction)

            if direction == "subtract":
                current_value = current_value - magnitude
                total_applied -= magnitude
            elif direction == "add":
                current_value = current_value + magnitude
                total_applied += magnitude
            else:
                raise ValueError(f"Unknown adjustment direction: {direction}")

        return current_value, total_applied

    def _credit_amount(self, credit: Dict, layer_tax: Decimal = ZERO) -> Decimal:
        """The amount of one template credit (blank user_input blocks)."""
        c_type = credit.get("type")
        credit_name = credit.get("name", "unnamed_credit")

        # value_source: user_input escape hatch — no account fallback
        # exists for these, so blank (non-skipped) blocks computation.
        if credit.get("value_source") == "user_input":
            return self._resolve_user_input_only(credit_name)

        if c_type == "rate":
            amount = _as_decimal(credit.get("amount", 0), ZERO)
            rate = _as_decimal(credit.get("rate", 1.0), Decimal("1.0"))
            return self._q(amount * rate)

        if c_type == "capped_bucket":
            bucket_val = ZERO
            for b in credit.get("applies_to", []):
                bucket_val += self.breakdown["buckets"].get(b, {}).get("raw", ZERO)
            max_amount = _as_decimal(credit.get("max_amount", 0), ZERO)
            return self._q(min(bucket_val, max_amount))

        if c_type == "threshold_bucket":
            # The credit names its own income reference: a computed figure
            # (total_income / net_income / adjusted_taxable_income) or a
            # user_input field. A user_input has no account fallback, so a
            # blank one blocks computation rather than silently
            # substituting a computed figure.
            net_inc_for_thresh = self._income_reference(credit)

            bucket_val = ZERO
            for b in credit.get("applies_to", []):
                bucket_val += self.breakdown["buckets"].get(b, {}).get("raw", ZERO)

            fixed_threshold = _as_decimal(credit.get("fixed_threshold", 0), ZERO)
            pct_threshold = _as_decimal(credit.get("pct_threshold", 0), ZERO)
            credit_rate = _as_decimal(credit.get("credit_rate", 0), ZERO)

            threshold = min(fixed_threshold, net_inc_for_thresh * pct_threshold)
            eligible = max(ZERO, bucket_val - threshold)
            return self._q(eligible * credit_rate)

        if c_type == "foreign_tax_credit":
            # Foreign tax paid is credited up to the layer's tax attributable
            # to the foreign income: tax x (foreign income / total income).
            paid = self._resolve_user_input_only(credit["tax_paid"]["user_input"])
            if self.total_income <= ZERO:
                return ZERO
            foreign = sum(
                (self.breakdown["buckets"].get(b, {}).get("taxable", ZERO) for b in credit.get("applies_to", [])),
                ZERO,
            )
            limit = self._q(layer_tax * min(Decimal(1), foreign / self.total_income))
            return self._q(max(ZERO, min(paid, limit)))

        raise ValueError(f"Unknown credit type: {c_type}")

    def _income_reference(self, credit: Dict) -> Decimal:
        ref = credit.get("income_reference")
        computed = {
            "total_income": self.total_income,
            "net_income": self.net_income,
            "adjusted_taxable_income": self.adjusted_taxable_income,
        }
        if isinstance(ref, str) and ref in computed:
            return computed[ref]
        if isinstance(ref, dict) and isinstance(ref.get("user_input"), str):
            return self._resolve_user_input_only(ref["user_input"])
        raise ValueError(
            f"threshold_bucket credit '{credit.get('name', 'unnamed_credit')}' "
            f"needs a valid income_reference."
        )

    def _levy_key(self, name: str, levy: Dict) -> str:
        """Breakdown key for a levy; disambiguated if two layers reuse a name."""
        if name in self.breakdown["levies"]:
            return f"{name} ({levy.get('_layer') or 'other'})"
        return name

    # ------------------------------------------------------------------
    # Full pipeline
    # ------------------------------------------------------------------
    def compute(self) -> Dict[str, Any]:
        """Execute the full 8-stage pipeline."""

        levels = [layer["jurisdiction"]["level"] for layer in self.ts.layers]
        default_layer = levels[0] if levels else None
        layer_credits: Dict[str, Decimal] = {lv: ZERO for lv in levels}
        layer_refundable: Dict[str, Decimal] = {lv: ZERO for lv in levels}

        # ==============================================================
        # STAGE 1: Sum income buckets
        # ==============================================================
        for bucket_name, prim_def in self.ts.all_buckets.items():
            raw = self._get_bucket_raw_amount(bucket_name)

            # Bucket values are non-negative by schema, with an offset_group
            # exception. offset_group netting/clamping is reserved but not
            # implemented (per brief) -- a grouped bucket's raw value passes
            # through unchanged for now. An ungrouped bucket that resolves
            # negative is clamped to zero immediately, with a warning.
            if raw < ZERO and not prim_def.get("offset_group"):
                self._negative_raw[bucket_name] = -raw
                msg = f"'{bucket_name}' resolved to a negative amount ({raw}); clamped to zero."
                logger.warning(msg)
                self.breakdown["warnings"].append(msg)
                raw = ZERO

            # Losses carried in from earlier years reduce this bucket first,
            # but never below zero; the unused part keeps carrying forward.
            raw_before_carry = raw
            cf_name = prim_def.get("carryforward_in")
            carry_applied = ZERO
            if cf_name:
                carried = self._carried_in(cf_name)
                carry_applied = min(max(raw, ZERO), carried)
                raw = raw - carry_applied
                self._carry_unused[cf_name] = self._q(carried - carry_applied)

            taxable, bucket_credits = self._apply_primitive(bucket_name, raw, prim_def)
            if cf_name:
                # Show the bucket's own amount, with what was carried in on top.
                self.breakdown["buckets"][bucket_name]["raw"] = raw_before_carry
                self.breakdown["buckets"][bucket_name]["carryforward_applied"] = carry_applied
                if carry_applied > ZERO:
                    self.breakdown["warnings"].append(
                        f"Applied {carry_applied} of carried-forward '{cf_name}' against '{bucket_name}'."
                    )
            self.total_income += taxable
            pool = layer_refundable if prim_def.get("refundable") else layer_credits
            for level, amount in bucket_credits.items():
                if level in pool:
                    pool[level] += amount
                # A credit aimed at a level that isn't loaded has no tax to
                # reduce; it is dropped, not redirected.
        self.total_income = self._q(self.total_income)

        # ==============================================================
        # STAGE 2: Apply SHARED deductions -> net income
        # ==============================================================
        self.net_income, deduction_magnitude = self._apply_adjustments_at_stage(
            "deduction", self.total_income
        )
        self.net_income = self._q(self.net_income)
        self.breakdown["deductions"] = self._q(deduction_magnitude)

        # ==============================================================
        # STAGE 3: Apply SHARED pre adjustments -> adjusted taxable income,
        # then each layer's own (scope: "layer") deductions / pre adjustments
        # ==============================================================
        self.adjusted_taxable_income, pre_magnitude = self._apply_adjustments_at_stage(
            "pre", self.net_income
        )
        self.adjusted_taxable_income = self._q(max(ZERO, self.adjusted_taxable_income))
        separate_total = self._q(sum((i["amount"] for i in self._separate.values()), ZERO))
        self.ordinary_taxable_income = self._q(
            max(ZERO, self.adjusted_taxable_income - separate_total)
        )
        self.breakdown["separate_income"] = separate_total
        self.breakdown["ordinary_taxable_income"] = self.ordinary_taxable_income
        self.breakdown["pre_adjustments"] = self._q(pre_magnitude)

        # A layer with no layer-scoped adjustments taxes the shared base.
        for level in levels:
            _, layer_ded = self._apply_adjustments_at_stage(
                "deduction", self.total_income, layer=level, layer_scoped=True
            )
            _, layer_pre = self._apply_adjustments_at_stage(
                "pre", self.net_income + layer_ded, layer=level, layer_scoped=True
            )
            base = self._q(max(ZERO, self.adjusted_taxable_income + layer_ded + layer_pre))
            self._layer_base[level] = base
            self._layer_ordinary[level] = self._q(max(ZERO, base - separate_total))

        # ==============================================================
        # STAGE 4 & 5: Tax schedule per layer (brackets or tariff), optional
        # scale, separate-rate income, then any alternative calculation
        # ==============================================================
        layer_tax: Dict[str, Decimal] = {}
        layer_parts: Dict[str, Tuple[Decimal, Decimal]] = {}
        layer_alt: Dict[str, List[str]] = {}
        for layer in self.ts.layers:
            level = layer["jurisdiction"]["level"]
            ordinary = self._layer_ordinary[level]
            factor = self._scale_factor_for(level)
            if factor is None:
                bracket_tax = self._schedule_tax(layer, ordinary)
            else:
                # Stage 4 result is discarded when scale is present — only the
                # scaled-and-multiplied figure is authoritative (per brief).
                # Scaling applies to ordinary income only.
                bracket_tax = self._q(self._schedule_tax(layer, ordinary / factor) * factor)
            separate_tax = self._separate_tax(level)
            regular = self._q(bracket_tax + separate_tax)
            layer_tax[level], layer_alt[level] = self._apply_alternatives(
                layer, regular, self._layer_base[level]
            )
            layer_parts[level] = (bracket_tax, separate_tax)

        # ==============================================================
        # STAGE 6: Post adjustments (each on its own layer's tax)
        # ==============================================================
        post_total = ZERO
        for level in levels:
            adjusted, magnitude = self._apply_adjustments_at_stage(
                "post", layer_tax[level], layer=level
            )
            layer_tax[level] = self._q(max(ZERO, adjusted))
            post_total += magnitude
        self.income_tax_owed = self._q(sum(layer_tax.values(), ZERO))
        self.breakdown["post_adjustments"] = self._q(post_total)

        # ==============================================================
        # STAGE 7: Credits, per layer -> income tax after credits
        # ==============================================================
        # Non-refundable credits reduce a layer's tax to zero but never
        # below, and never spill onto another layer. Then post_credit
        # adjustments (e.g. a surtax on tax AFTER non-refundable credits),
        # then refundable credits, which may take the layer below zero.
        for credit in self.ts.all_credits:
            level = credit.get("_layer") or default_layer
            pool = layer_refundable if credit.get("refundable") else layer_credits
            pool[level] = pool.get(level, ZERO) + self._credit_amount(credit, layer_tax.get(level, ZERO))

        layer_after: Dict[str, Decimal] = {}
        layer_unused: Dict[str, Decimal] = {}
        applied_total = ZERO
        unused_total = ZERO
        refundable_total = ZERO
        post_credit_total = ZERO
        for layer in self.ts.layers:
            level = layer["jurisdiction"]["level"]
            available = self._q(layer_credits.get(level, ZERO))
            refundable = self._q(layer_refundable.get(level, ZERO))
            applied = min(available, layer_tax[level])
            after_nonrefundable = layer_tax[level] - applied
            adjusted, pc_magnitude = self._apply_adjustments_at_stage(
                "post_credit", after_nonrefundable, layer=level
            )
            after_nonrefundable = self._q(max(ZERO, adjusted))
            layer_after[level] = after_nonrefundable - refundable
            layer_unused[level] = available - applied
            applied_total += applied
            unused_total += available - applied
            refundable_total += refundable
            post_credit_total += pc_magnitude
            self.breakdown["layers"][level] = {
                "template_id": layer.get("template_id"),
                "taxable_income": self._layer_base[level],
                "bracket_tax": self._q(layer_parts[level][0]),
                "separate_tax": self._q(layer_parts[level][1]),
                "alternatives_applied": list(layer_alt[level]),
                "income_tax": layer_tax[level],
                "credits_available": available,
                "credits_applied": self._q(applied),
                "post_credit_adjustments": self._q(pc_magnitude),
                "credits_refundable": refundable,
                "tax_after_credits": self._q(layer_after[level]),
            }

        self.breakdown["credits_applied"] = self._q(applied_total)
        self.breakdown["credits_unused"] = self._q(unused_total)
        self.breakdown["refundable_credits"] = self._q(refundable_total)
        self.breakdown["post_credit_adjustments"] = self._q(post_credit_total)
        # Net position across layers; negative means a net refund.
        self.tax_after_credits = self._final(sum(layer_after.values(), ZERO))

        # Amounts the template says carry into next year's return.
        for layer in self.ts.layers:
            level = layer["jurisdiction"]["level"]
            for cf in layer.get("carryforwards") or []:
                if cf["source"] == "unused_credits":
                    amount = layer_unused[level]
                else:
                    amount = (
                        self._negative_raw.get(cf["bucket"], ZERO)
                        + self._carry_unused.get(cf["name"], ZERO)
                    )
                self.carryforwards_out[cf["name"]] = self._q(amount)
        self.breakdown["carryforwards"] = dict(self.carryforwards_out)

        # ==============================================================
        # STAGE 8: Levies computed and added -> total tax
        # ==============================================================
        total_levies = ZERO
        for levy in self.ts.all_levies:
            l_type = levy.get("type")
            levy_name = levy.get("name", "unnamed_levy")

            # value_source: user_input on a levy (e.g. OAS clawback) — same
            # treatment as credits: no account fallback, blank blocks.
            if levy.get("value_source") == "user_input":
                levy_contribution = self._final(self._resolve_user_input_only(levy_name))
                total_levies += levy_contribution
                self.breakdown["levies"][self._levy_key(levy_name, levy)] = levy_contribution
                continue

            own_layer = levy.get("_layer") or default_layer
            relevant_income = ZERO
            for b in levy.get("applies_to", []):
                if b == "*" or b == "total_income":
                    relevant_income += self.total_income
                elif b == "net_income":
                    relevant_income += self.net_income
                elif b == "adjusted_taxable_income":
                    # The levy's OWN layer's taxable base (its own layer-scoped
                    # deductions applied), falling back to the shared base.
                    relevant_income += self._layer_base.get(own_layer, self.adjusted_taxable_income)
                else:
                    relevant_income += self.breakdown["buckets"].get(b, {}).get("raw", ZERO)

            levy_contribution = ZERO

            if l_type == "rate":
                rate = _as_decimal(levy.get("rate", 0), ZERO)
                exempt = _as_decimal(levy.get("exempt_amount", 0), ZERO)
                cap_dec = _as_decimal(levy.get("cap"))  # None if absent
                capped_income = relevant_income if cap_dec is None else min(relevant_income, cap_dec)
                contribution = max(ZERO, rate * (capped_income - exempt))
                levy_contribution = self._final(contribution)

            elif l_type == "tiered":
                tiers = sorted(
                    levy.get("tiers", []),
                    key=lambda t: Decimal(str(t.get("min_income", 0))),
                )
                applicable_premium = ZERO
                for tier in tiers:
                    if relevant_income > Decimal(str(tier.get("min_income", 0))):
                        applicable_premium = Decimal(str(tier.get("premium", 0)))
                levy_contribution = self._final(applicable_premium)

            elif l_type == "phased_bands":
                # A series of INDEPENDENT capped segments summed together: a
                # ramp up to a plateau, repeated (Ontario Health Premium,
                # Australia's Medicare levy shape). Unlike banded_rate, each
                # segment has its own cap rather than ending where the next begins.
                total = ZERO
                for seg in levy.get("segments", []):
                    threshold = _as_decimal(seg["threshold"], ZERO)
                    cap = _as_decimal(seg["cap"], ZERO)
                    rate = _as_decimal(seg["rate"], ZERO)
                    total += max(ZERO, rate * (min(relevant_income, cap) - threshold))
                levy_contribution = self._final(total)

            elif l_type == "banded_rate":
                # Each band's rate applies to the slice of relevant income
                # between its threshold and the next (like brackets, but the
                # first band need not start at 0).
                levy_contribution = self._final(
                    self._calc_brackets(relevant_income, levy.get("bands", []))
                )

            else:
                raise ValueError(f"Unknown levy type: {l_type}")

            total_levies += levy_contribution
            self.breakdown["levies"][self._levy_key(levy_name, levy)] = levy_contribution

        # Every term is already on the rounding grid, so the sum is too.
        self.total_tax = self.tax_after_credits + total_levies

        return {
            "total_income": self.total_income,
            "net_income": self.net_income,
            "adjusted_taxable_income": self.adjusted_taxable_income,
            "income_tax_owed": self.income_tax_owed,
            "tax_after_credits": self.tax_after_credits,
            "total_tax": self.total_tax,
            "breakdown": self.breakdown,
            "skipped_inputs": self.profile.get("skipped_inputs", []),
            "currency": self.currency,
            "tax_period": getattr(self.ts, "tax_period", None),
            "variant": getattr(self.ts, "variant", None),
            "carryforwards": dict(self.carryforwards_out),
        }
