# CogniFinance tax template reference (format 4)

A tax **set** is one or more *jurisdiction bases* (one per level: `national`, `regional`, `local`) plus *income modules*. Everything a country needs is data in these JSON files. Python changes only when a rule needs a mechanism that isn't listed here.

Formats 1–4 are upgraded in memory on load and never rewritten. A file that uses anything new must set `"cognifinance_tax_template": 4`; older app versions then refuse it instead of misreading it. Keys starting `_` or `x_` are reserved for extensions and ignored. Any other unknown key is a load error.

Text fields (`display_name`, `description`, `label`, …) are either a string or `{"en": "…", "fr": "…"}`.

## 1. Jurisdiction base

```json
{
  "cognifinance_tax_template": 5,
  "template_type": "jurisdiction_base",
  "template_id": "gb-national-2025",
  "jurisdiction": {"country": "GB", "region": "…", "level": "national",
                   "country_name": {"en": "United Kingdom"}, "level_label": {"en": "Federal"}},
  "currency": "GBP",
  "tax_year": 2025,
  "tax_period": {"label": "2025-26", "start": "2025-04-06", "end": "2026-04-05"},
  "rounding": {"mode": "down", "increment": "1"},
  "module_compatibility": {"gb-income-2025": [{"version": 2025, "valid_until": null}]},
  "brackets": [ … ]   // or "tariff": { … }  — exactly one
}
```

- `country` is ISO 3166-1 alpha-2, `currency` ISO 4217. A set's layers must share country, currency, tax year and period.
- `tax_year` is an integer key (the year the period starts). `tax_period` is optional and defaults to the calendar year.
- `rounding` applies only to amounts payable (tax after credits, each levy). Working precision stays at cents. Modes: `half_up` (default), `half_even`, `down`, `up` (toward / away from zero). The default increment is the currency's minor unit.
- Use ids like `<country>-<region>-<year>`. If the same id exists in several template folders, the highest-precedence folder (user, then community, then shipped) wins.

## 2. Tax schedules

**`brackets`**: `[{"threshold": 0, "rate": 0.10}, {"threshold": 50000, "rate": 0.20}]`. Each rate applies from its threshold to the next. The first threshold must be 0.

**`tariff`**: polynomial zones, for tariffs defined by formula. The zone used is the last whose `from` ≤ income; within it, `tax = Σ coefficients[k] · tᵏ` with `t = (income − offset) / divisor` (`offset` default 0, `divisor` default 1). `income_rounding` optionally rounds income first.

```json
"tariff": {"income_rounding": {"mode": "down", "increment": 1},
           "zones": [{"from": 0, "coefficients": [0]},
                     {"from": 12097, "offset": 12096, "divisor": 10000, "coefficients": [0, 1400, 932.30]}]}
```
Check every transcription by testing that the tariff is continuous at zone boundaries.

## 3. Income buckets (in modules)

`buckets` maps a name to a definition. Users map their own accounts to these names.

| `primitive` | Meaning |
|---|---|
| `inclusion_rate` | `rate` × amount joins taxable income (e.g. 0.5 for a 50% inclusion). |
| `gross_up_credit` | Amount grossed up by `gross_up_rate`, plus credits per layer: `credit_rates: {"national": 0.15, "regional": 0.10}`. `refundable: true` makes them refundable. |
| `separate_rate` | Taxed **outside** the brackets. `exempt_amount` is an allowance. `schedules` is keyed by level; each is `{"rate": 0.25}` (flat) or `{"brackets": […]}` (stacked on top of ordinary income: tax = T(base + amount) − T(base), so thresholds refer to total taxable income). |

Optional `label`. A bucket that resolves negative is clamped to zero with a warning; see `carryforwards` to keep the loss.

## 4. Adjustments

Order of `stage`: `deduction` → `pre` → `scale` → `post` → `post_credit`.

| `stage` | Acts on | Default direction |
|---|---|---|
| `deduction` | total income → net income | subtract |
| `pre` | net income → adjusted taxable income | subtract |
| `scale` | splitting: brackets/tariff run on income ÷ `factor`, result × `factor` | — |
| `post` | a layer's tax before credits | add |
| `post_credit` | a layer's tax after non-refundable credits (e.g. surtaxes) | add |

`type` (not used by `scale`): `flat` (`value`, or `applies_to` buckets, or `value_source: "user_input"`), `tapered` (`base` reduces by `reduction_rate` × (value − `threshold`), never below `floor`), `rate` (`rate` × value), `threshold_rates`.

**`threshold_rates`**: `components: [{"rate", "threshold"}]`, each worth `rate × max(0, value − threshold)`, combined by `combine`: `sum` (default; "20% over T1 plus 36% over T2"), `min` (a phase-in capped by a full charge), or `max`.

**`scope`**: `"shared"` (default) adjusts the one base all layers use; `"layer"` adjusts only the layer whose file declares it, on top of the shared result (state/cantonal deductions). Only for `deduction`, `pre` and `scale`. At most one shared `scale` per set and one layer-scoped per layer.

## 5. Credits

Non-refundable credits reduce a layer's tax to zero, never below and never onto another layer. `refundable: true` credits apply after everything else and may make a layer negative (a refund). `value_source: "user_input"` reads a user-entered figure keyed by the entry's `name` (blank blocks the calculation until entered or skipped).

- `rate`: `amount × rate`.
- `capped_bucket`: `min(applies_to bucket total, max_amount)`.
- `threshold_bucket`: `credit_rate × max(0, bucket total − min(fixed_threshold, pct_threshold × income))`. **`income_reference` is required**: `"total_income"`, `"net_income"`, `"adjusted_taxable_income"`, or `{"user_input": "field_name"}`.
- `foreign_tax_credit`: `applies_to` the foreign-income bucket(s), `tax_paid: {"user_input": "field_name"}`. Credit = `min(tax paid, layer tax × foreign income ÷ total income)`.

## 6. Levies

Added to the total; not tied to a layer. `applies_to` lists buckets, `"*"` or `"total_income"` (gross), `"net_income"`, or `"adjusted_taxable_income"` (the levy's own layer's taxable base — the right basis for a levy defined on taxable income rather than gross income).

- `rate`: `rate × (min(income, cap) − exempt_amount)`.
- `tiered`: `tiers: [{"min_income", "premium"}]`; the premium of the highest tier the income exceeds.
- `banded_rate`: `bands: [{"threshold", "rate"}]`; each rate applies to the slice up to the next threshold (the first band may start above 0).
- `phased_bands`: `segments: [{"threshold", "rate", "cap"}]`, each an INDEPENDENT term worth `rate × (min(income, cap) − threshold)`, summed. A ramp up to a plateau, repeated — the shape of the Ontario Health Premium or Australia's Medicare levy — as one levy entry instead of several `rate` levies.

## 7. Alternatives (minimum tax, benefit caps)

A parallel calculation compared with the layer's regular tax (brackets/tariff plus separate-rate tax), before post adjustments and credits.

```json
"alternatives": [{"name": "minimum_tax", "combine": "max",
  "base": "adjusted_taxable_income", "add_back": ["preference_items"],
  "exemption": {"type": "tapered", "base": 40000, "threshold": 120000, "reduction_rate": 0.25, "floor": 0},
  "brackets": [{"threshold": 0, "rate": 0.20}], "subtract": 0}]
```
`combine`: `max` (the alternative sets a floor) or `min` (a ceiling). `base`: `total_income`, `net_income` or `adjusted_taxable_income` (the layer's own). `add_back` adds buckets' raw amounts; `exemption` is `flat` or `tapered` (phasing out against the base); `subtract` reduces the alternative's tax by a flat amount (a cap on a splitting benefit: no `brackets` given, so it reuses the layer's schedule unsplit). Own `brackets`/`tariff` optional.
*Not modeled:* restrictions on credits against the alternative tax.

## 8. Variants (filing status, tax class)

```json
"variants": {"label": {"en": "Filing status"}, "default": "single", "options": {
  "single": {"label": {"en": "Single"}},
  "joint":  {"label": {"en": "Joint"}, "brackets": [ … ], "adjustments": [ … ]}}}
```
An option may override `brackets` *or* `tariff`, plus `credits`, `levies`, `adjustments`, `alternatives`. **An overridden list replaces the base's list wholesale**, so repeat any entries you still want. The user's choice is stored per country.

## 9. Carryforwards

```json
"carryforwards": [
  {"name": "net_capital_loss_carryforward", "source": "negative_bucket", "bucket": "capital_gains"},
  {"name": "unused_credit_carryforward", "source": "unused_credits"}]
```
The calculation reports each amount; "Save Carry-forwards" stores them for the tax year. The next year's calculation receives them as **user inputs with the same `name`**, so next year's template consumes them with a `user_input` adjustment or credit of that name. An amount the user enters themselves overrides the carried one.

## 10. Calculation order

buckets → shared deductions → shared `pre` → layer-scoped deductions/`pre` → per layer: schedule on ordinary income (÷/× `scale`) + separate-rate tax → alternatives → `post` → non-refundable credits → `post_credit` → refundable credits → levies → total.

## 11. Not covered

A single-currency, single-year model: no multi-currency income conversion, no automatic multi-year loss tracking beyond the declared carryforwards, and no household-level joint computation across people.
