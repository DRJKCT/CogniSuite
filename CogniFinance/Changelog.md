CogniFinance Changelog

[0.9.66] - Investments & Crypto Engine & Tab

This release introduces a dedicated, ledger-first investment engine with multi-jurisdiction cost-basis tracking, a unified Investments & Crypto tab, and a deliberate architectural boundary between traditional securities and crypto workflows. It also ships a transparent, opt-in price lookup system and a reusable CSV import pipeline adapted for both asset classes.

🏗️ Architectural
*   **Investment & Crypto Engines Deliberately Separated:** `investment_engine.py` handles traditional securities (buy/sell/dividend model) while `crypto_engine.py` manages wallets, on-chain transfers, and staking/airdrop income. They share only `lot_matcher.py` (pure FIFO/specific-lot math) and post through `gl.record_transaction()`. This boundary prevents crypto's messy feature surface from bending the simpler investment model.
*   **Ledger-First Transaction Posting:** Every dollar movement routes through the main ledger. The engine never writes dr/cr amounts directly; quantities, per-unit cost basis, and lot state live in dedicated tables (`sql_investment_holdings`, `sql_investment_lots`, `sql_investment_realized`) within the same encrypted database, following the ledger's own balancing/validation logic.
*   **Cost-Basis Method Support:** Holdings track `FIFO` or `SPECIFIC` lot identification. The UI and engine enforce method selection at holding creation; `SPECIFIC` sales require explicit lot selection via the engine API (UI gap noted).
*   **Shared CSV Import Pipeline:** `investment_import_manager.py` mirrors `import_manager.py`'s thread/dialog plumbing (`QProgressDialog`, `TransactionCSVImportThread`, `CSVColumnMappingDialog`) but is engine-agnostic. It accepts a `poster` callback, enabling both Investments and Crypto sub-tabs to reuse the same background import experience without coupling.
*   **Opt-In Price Lookup with Transparency:** `PriceRefreshPanel` is explicitly manual-trigger-only (no timers, no auto-refresh). Users must toggle "Enable price lookups" to fetch live prices. A "View Network Call Log" button opens `NetworkCallLogDialog`, surfacing every fetch request, source, symbols, and outcome for auditability.

✨ Functional
*   **Holdings & Lot Tracking:** `add_holding()` creates the backing Asset account and holding row. `get_holdings_summary()` computes quantity held, average cost, and total cost basis. `get_open_lots()` and `get_quantity_held()` feed the UI table.
*   **Buy / Sell / Dividend Recording:** Buys fold fees into the lot's cost basis per unit. Sells consume lots per the selected method, compute realized gain/loss with cent-accurate rounding (proceeds vs. cost basis), and post to fixed Revenue/Expense accounts. Dividends post directly to `Dividend Income — Investments` with no lot effect.
*   **Realized Gains / Losses Table:** Tracks every disposal with symbol, date, quantity, cost basis, proceeds, and realized gain/loss. Negative gains render in red. Filterable by date range via engine API.
*   **CSV Import with Auto-Holding Creation:** Picking a CSV opens the column-mapping dialog. On import, unseen symbols are auto-added as new holdings using the user-selected report line and cost basis method. Skipped/rejected rows are summarized in a post-import dialog with detailed text.
*   **Fixed Account Provisioning:** The engine idempotently creates `Realized Gains — Investments`, `Realized Losses — Investments`, and `Dividend Income — Investments` on initialization, ensuring the ledger has the required Revenue/Expense accounts before any transaction posts.

🐛 Bug Fixes & Scope Notes
*   **Corporate Actions Explicitly Out of v1 Scope:** `record_stock_split()` raises `NotImplementedError` with a clear reference to the planning brief. This prevents silent cost-basis corruption if a user encounters a split, spin-off, or DRIP before those features are implemented.
*   **Price Lookup Disabled by Default:** The panel starts unchecked on fresh installs (state persists via `price_fetch.py`'s settings). This avoids silent network calls or rate-limit failures on first launch.
*   **Specific-Lot UI Gap:** The Investments sub-tab's Sell form notes that specific-lot selection isn't available in the UI yet; `SPECIFIC`-method holdings must be sold via the engine API directly. This is a tracked follow-up, not a silent limitation.

🎨 UI/UX
*   **Unified Investments & Crypto Tab:** Split internally into `InvestmentsSubTab` and `CryptoSubTab` via `QTabWidget`, keeping engine logic isolated while providing a single navigation entry point.
*   **Real-Time Portfolio & Gains Tables:** Holdings table shows symbol, quantity, avg cost, cost basis, last price, and method. Realized gains table updates immediately after buys/sells/dividends. Both preserve selection across refreshes.
*   **Clean Form Layouts:** Grouped forms for Add Holding, Buy, Sell, Dividend, and CSV Import sit side-by-side with auto-populated symbol/cash account combos that refresh on every data change.

📝 Notes
*   **Ledger Integration:** The engine relies on `ledger.py`'s `VALID_REPORT_LINES_BY_TYPE["Asset"]` and `REPORT_LINE_METADATA`. New report lines for "Investments" and "Digital Assets" must exist before first use.
*   **Future Scope:** Corporate actions (splits, spin-offs, mergers, DRIP), in-kind crypto transfers, and automatic price polling are deferred to v2 per the planning brief. The architecture is deliberately laid to support them without refactoring the core lot math or ledger posting path.


[0.9.65] - Multi-Jurisdiction Tax Tab & Display-Surface Contract

This release rewrites the Tax tab around the multi-layer template model
that the calculator now runs under, adds currency- and locale-aware
money formatting, persists carry-forwards between tax years, and
localizes the tab's user-facing strings. It also formalizes the
display-surface contract in ui_helpers and repairs three latent defects
in the Audit Log tab.

📝 Scope note. The Tax tab shipped in 0.9.5 and earlier was a
single-jurisdiction T2125 estimator: an HTML summary in a QTextEdit, a
DynamicReportWidget for configurable sections, and a hardcoded
`estimate_tax_liability` for Ontario. The changes below replace that
implementation entirely. Earlier changelog entries (0.4.0, 0.5.0,
0.6.0, 0.9.4) describe the superseded implementation and are retained
for historical context; see Notes.

🏗️ Architectural
*   **Display-Surface vs. Input-Surface Contract Formalized:** Added
    `ui_helpers.setup_display_table(table, *, selection,
    in_place_editing, alternating_rows)` as the single explicit
    declaration that a table is a read-out rather than an input grid.
    Disables edit triggers, configures row selection, hides the
    vertical header. `setup_table_headers` remains a thin wrapper so
    existing call sites are unaffected. `in_place_editing=True` exists
    as an escape hatch but is unused anywhere in the app.

*   **Column Layout Single Source of Truth Helper:** Added
    `ui_helpers.apply_column_layout(table, headers)`, deriving
    `setColumnCount` from `len(headers)` so count and labels cannot
    silently disagree. Qt truncates excess labels and accepts
    out-of-range `setItem(row, col, ...)` writes without raising, so a
    mismatch previously never surfaced as an error.

*   **Named-Column Tooltip Helper:** Added
    `ui_helpers.set_header_tooltip(table, headers, column_name,
    tooltip)`, so tooltips follow columns by name rather than
    hard-coded indices.

*   **Currency and Rounding Extracted:** Added `tax_format.py` —
    `currency_minor_units`, `rounding_modes`, `round_to_increment`,
    `format_money` — so the calculator and the UI agree on
    currency-appropriate precision and payable-amount rounding without
    either hard-coding "cents, half-up". The calculator reads the
    template set's `currency` and `rounding` policy rather than
    assuming both.

*   **Tax Tab Now Multi-Layer and Country-Scoped:** The tab presents a
    `Country:` selector plus one combo per jurisdiction level
    (`National` / `Regional` / `Local`), replacing the previous fixed
    Federal/Provincial pair. Level labels come from the template's own
    `level_label` (e.g. "Province", "State") when present. A
    `Filing status:` combo appears only when a selected base declares
    variants, and the modules list is filtered to only the modules the
    selected bases actually accept.

*   **Profile Sections Are Country-Scoped and Variant-Aware:**
    `TaxProfileManager` now keys sections as `"<CC>/Default"` and
    remembers the user's chosen variant per section.
    `resolve_section_name()` adopts the legacy un-namespaced section for
    the legacy country (CA per `LEGACY_V1_DEFAULTS`) by copying it into
    the new namespace, leaving the original row in place so anything
    still reading it keeps working. `save_section()` gained `variant`
    and `carryforwards` parameters behind a `_KEEP` sentinel, so callers
    that only edit the category map can't clobber them.

*   **Carry-Forward Persistence:**
    `TaxProfileManager.save_carryforwards(section_name, tax_year,
    values)` records what a year's calculation hands to the next;
    `carryforward_inputs_for(section_name, tax_year)` feeds the prior
    year's values into the current year's inputs at compute time, shaped
    as user inputs with `source: "carryforward"`. User-entered values
    for the same field win over carried ones.

*   **Template Loader Extended to Format 4:** `tax_parser.py` now
    supports marker versions 1–4 and upgrades older files in memory
    (`v1 → v2 → v3 → v4`) without rewriting them on disk, so shipped
    hashes are unaffected. Format 2 introduced `jurisdiction_base` with
    `jurisdiction {country, region?, level}` and the `LEVELS` ordering
    (`national → regional → local`); format 3 added `separate_rate`
    buckets, `banded_rate` levies, `refundable` credits, and explicit
    `income_reference` on threshold_bucket credits; format 4 added
    `threshold_rates` adjustments, `post_credit` stage, `scope:
    "layer"` adjustments, `tariff`, `alternatives`,
    `foreign_tax_credit`, `carryforwards`, and load-time validation of
    adjustment `stage` / `type` / `scope` so a typo can no longer make
    an adjustment silently do nothing.

*   **Calculator Runs Per Layer:** `tax_calculator.py` now applies
    brackets, post adjustments and credits to each layer's own tax;
    non-refundable credits floor at that layer's tax so a national
    credit can no longer reduce regional tax. Separate-rate income is
    carved out of the bracket base and taxed by its own schedule per
    layer. Working precision is the currency's minor unit (at least
    cents); the template's rounding policy applies only to amounts
    actually payable. Alternatives (minimum-tax / benefit-cap rules)
    are compared against the regular tax in declaration order.

*   **Tax Tab Localized:** Every user-facing string in `tax_tab.py` and
    `tax_profile_dialog.py` is wrapped in `self.tr(...)`; static strings
    that live outside a QObject use `QCoreApplication.translate`.
    `tax_ui.ts` is the extracted catalog (currently all
    `type="unfinished"`).

✨ Functional
*   **Multi-Jurisdiction Selection:** Choose a country, one template
    per level (National mandatory; Regional and Local default to
    `(none)`), a filing-status variant where offered, and a set of
    compatible modules. Loaded bases must share country, currency, tax
    year and tax period; a mismatch reports the more useful error first
    (two "national" layers from different countries is a country
    problem, not a level problem).

*   **Currency- and Locale-Aware Display:** Amounts in the calculation
    tree are formatted with `tax_format.format_money` using the loaded
    set's currency and the user's locale separators, replacing the
    previous hard-coded `${amt:,.2f}`. The template's payable-amount
    rounding policy is respected end to end.

*   **Detailed Calculation Tree:** The tree surfaces `of which taxed
    separately` under adjusted taxable income, per-layer shares under
    income tax owed when there is more than one layer, `Alternative
    calculation applied: ...` notes when a `max`/`min` alternative set
    the tax, `Plus: Post-Credit Charges` and `Less: Refundable
    Credits`, a `Carry-forward to next year` section, and a
    net-refund vs. tax-payable distinction on the final row.

*   **Carry-Forward Save Button:** A new `Save Carry-forwards for Next
    Year` button persists the current calculation's carry-forwards
    into the section for reuse by the following year's computation.

🐛 Bug Fixes
*   **Audit Log Summary Column Silently Dropped:** Fixed the main audit
    table declaring `setColumnCount(5)` while providing six header
    labels. Qt discarded the sixth label and rejected the matching
    `setItem(row, 5, ...)` out of bounds, so the per-entry line summary
    was never displayed. Now built from a single six-element header
    list via `apply_column_layout`.

*   **History Dialog Tooltip on Wrong Table and Column:** Fixed
    `_show_multi_entry_dialog` calling
    `self.table.horizontalHeaderItem(4).setToolTip(...)`, which targeted
    the main audit table rather than the dialog's and landed on
    "Summary" rather than "Reason". Now uses `set_header_tooltip`
    against the correct table and named column.

*   **Single-Entry Audit Snapshot Accepted In-Place Edits:** Fixed the
    single-entry dialog's lines table never disabling edit triggers.
    The edit was non-persistent, but a read-only audit surface that
    visually accepts edits invites doubt about whether the snapshot is
    true. Configured via `setup_display_table(..., selection="none")`.

*   **Duplicated Reason Tooltip String:** Hoisted to a module-level
    `_REASON_TOOLTIP` constant in `audit_tab.py`.

📝 Notes
*   **Superseded tax-tab entries.** The following earlier changelog
    entries describe the pre-0.9.65 Tax tab (a single-jurisdiction
    Ontario T2125 estimator with an HTML summary and
    DynamicReportWidget sections). They are retained for historical
    context:
    - 0.4.0 "JSON Tax Templates" and "Dynamic Tax Loading" — JSON
      template loading is still the model, but through
      `TaxTemplateLoader` and the jurisdiction/module schema, not a
      flat per-year file.
    - 0.5.0 "HTML Tax Tab" — the Tax tab is no longer HTML-rendered; it
      is a native Qt widget tree.
    - 0.6.0 "Tax Liability Estimator" pull-down — the estimator is now
      driven by per-level template combos, variant selection, and the
      `TaxCalculator` pipeline.
    - 0.9.4 "Dynamic Report Extraction" and "Template XLSX Export" —
      `dynamic_report_widget.py` is no longer instantiated by
      `TaxTab`. Its parent-tab date-range coupling
      (`parent_tab.tax_start` / `parent_tab.tax_end`) references an
      interface the new Tax tab does not expose.
    - `tax_manager.py` (`TaxTemplateManager`) is likewise no longer
      called by `TaxTab`; it and `dynamic_report_widget.py` are
      candidates for retirement or rewiring, tracked as an open item.
    - `tax_calculator.py` ver 0.2's `estimate_tax_liability()` and
      `get_available_tax_templates()` are replaced by the class-based
      `TaxCalculator` and the loader's own enumeration. The old
      functions are no longer imported anywhere.


[0.9.6] - Envelope Budgeting Engine & Audit Reader Repair

This release wires the ZBB/envelope budgeting engine (previously implemented
in the ledger and in `budget_engine.py` but not exposed to the UI) through the
Budgets tab, retires the legacy flat-budget API, restores a formula-injection
guard that had been unreachable dead code since it was written, and fixes
three latent defects in the Audit Log reader that were silently breaking the
Audit Log tab and failing the weekly integrity audit.

🚨 Breaking Changes
*   **Legacy Flat-Budget API Retired:** `GeneralLedger.set_budget`,
    `delete_budget`, and `get_budget` have been removed. Budget data is now
    modelled as an append-only event ledger (`sql_budget_ledger`) with
    assignment, unassignment, and inter-envelope move events, and is read
    back through the envelope engine. Any external caller using the old
    methods will need to migrate to `assign_to_envelope` /
    `unassign_from_envelope` / `move_budget_money`.

🏗️ Architectural
*   **Budget Engine Now Wired Into the UI:** `budget_engine.py`'s pure
    derivation functions (`derive_expected_income`, `envelope_available`,
    `ready_to_assign`, `net_assignment_in_month`) are now imported and
    called by `budgets_tab.py`. Previously the module was fully implemented
    but had no importers, leaving the entire rollover and income-derivation
    layer unreachable from the running application.

*   **Budgets Tab Rewritten for Envelopes:** `budgets_tab.py` now drives
    the envelope engine end-to-end: income plan entry, Ready-to-Assign
    banner, per-envelope assignment and spend tracking, inter-envelope
    moves, and a live activity table of the month's budget-ledger events.
    The `refresh_budget_list()` and `refresh_account_dropdown()` entry
    points are preserved so `ui_main.py`'s existing refresher map and
    `hasattr` guards continue to work unchanged.

*   **Overview Auto-Refresh on Budget Changes:** Connected
    `tab_budgets.data_modified` to `BudgetApp.refresh_overview` so the
    Budget Alerts section and Ready-to-Assign figures on the Overview
    tab update immediately after any budget edit, matching the pattern
    already used by `tab_transactions2` and `tab_sync`.

*   **Audit Reader Column Tuple Constant:** Introduced
    `_AUDIT_ROW_COLUMNS` in `audit.py` as the single source of truth for
    the audit row shape, so every `SELECT` in the module is now built from
    one tuple and adding a column in the future requires editing one place
    instead of three.

✨ Functional
*   **Envelope-Based Budgeting Workflow:** Assign money from Ready to
    Assign into envelopes, unassign back, move money between envelopes,
    and edit envelope configuration (linked Expense account, rollover
    policy, active state) from the Budgets tab. Available balances are
    computed live per envelope using the policy-configured rollover math
    (`carry_forward` accumulates across months; `reset_monthly` starts
    each month fresh).

*   **Income Plan with Historical Derivation:** Each month can have an
    expected income figure, derived automatically from last month's
    actual revenue, a 3-month rolling average, a 6-month rolling average,
    or entered manually. The derivation and its human-readable label are
    shown live in the UI as the source is changed.

*   **Ready to Assign Surface:** The Ready-to-Assign figure (income
    received minus assignments, plus unassignments, excluding envelope-to-
    envelope moves) is now displayed prominently at the top of the Budgets
    tab and updates on every edit.

*   **Budget Activity History:** The month's `sql_budget_ledger` rows are
    rendered as a live table, showing every assign, unassign, move-in, and
    move-out event with timestamps and optional notes — making the budget
    ledger auditable at the event level rather than only as a running
    total.

🔒 Security
*   **Excel Sanitizer Formula-Injection Guard Restored:** `_sanitize_for_excel`
    was returning immediately after stripping XML control characters,
    leaving its formula-injection prefix check as unreachable dead code.
    A cell value beginning with `= + - @` would have been written through
    to the exported `.xlsx` verbatim and evaluated on open. The function
    now strips control characters, then applies the prefix check on the
    cleaned value, then returns once.

*   **CSV Export Sanitizer Added:** Introduced `_sanitize_for_csv` and
    wired it into `GeneralLedger.export_journal_csv`. CSV cells whose
    text begins with `=`, `+`, `-`, `@`, tab, or CR are prefixed with a
    single quote, neutralizing formula evaluation in Excel, LibreOffice,
    and Google Sheets when the export is opened. The numeric Debit and
    Credit columns are left untouched so round-tripping through
    `import_ledger_csv` preserves amount typing. The accepted prefix set
    is declared as `_CSV_FORMULA_PREFIXES` in `ledger.py`.

🐛 Bug Fixes
*   **Audit Log Reader Unpack Mismatch:** Fixed `AuditLogReader._row_to_dict`
    raising `ValueError: too many values to unpack (expected 7, got 8)`.
    The `SELECT` statements in `get_audit_log` and `get_history_for_tx`
    include the `change_reason` column, but the row-unpack tuple was never
    updated to match. This broke the Audit Log tab in the running app and
    failed the weekly integrity audit (`test_audit_log_pagination`),
    which in turn made every weekly audit report a critical failure.

*   **Audit Log Search Parameter Mismatch:** Fixed `_build_where`
    supplying two SQL bind parameters for a three-placeholder search
    clause (`description LIKE ? OR lines_json LIKE ? OR change_reason LIKE ?`).
    The Audit Log tab's search box would have raised
    `sqlite3.ProgrammingError: Incorrect number of bindings supplied` on
    the first non-empty query.

*   **Undefined `change_reason` in Audit Reader:** Removed a dangling
    reference to an unassigned local in `_row_to_dict`. This was masked
    by the unpack mismatch above; it would have surfaced as `NameError`
    on the very next statement if the unpack had been fixed in isolation.

---

[0.9.5] - Reconciliation Forensics & Structural Memory

This release adds forensic error detection to the reconciliation workflow, introduces a learned merchant-categorization memory system, and surfaces the user-supplied change reason through the audit trail.

🏗️ Architectural
* Merchant Normalization Module: Added `merchant_normalization.py`, a pure, deterministic function that transforms a raw bank description into a granular `merchant_key` (preserving detected store numbers, e.g. `"STARBUCKS #0234"`) and a collapsed `merchant_display` (e.g. `"STARBUCKS"`). No network calls, no external merchant database, no side effects.
* Merchant Memory Tunables Centralization: All Merchant Memory thresholds (volume cap, minimum occurrences, suggestion threshold, promotion threshold, rating modifiers) are declared as named constants at the top of `ledger.py` rather than hardcoded inline, so they can be tuned without touching the classification logic.
* Ledger Reconciliation API: Added `cleared_balance_as_of()` (sum of reconciled transactions only, as of a given date) and `get_transactions_for_account()` (all transactions touching an account within a date window, regardless of reconciled state) to `GeneralLedger`. Both are read-only and support the reconciliation session's live cleared-balance calculation and statement-window rendering.
* Audit Reader Row Shape Constant: Introduced `_AUDIT_ROW_COLUMNS` in `audit.py` as the single source of truth for the audit row shape. Every SELECT in the module is now built from this tuple, so adding a new column requires editing one place instead of four. `_row_to_dict` defensively checks the returned row length and raises a clear error on mismatch rather than silently misaligning fields.
* Change Reason Tracking: Added an optional `change_reason` column to `sql_transactions` and `sql_transaction_history`, threaded through `edit_transaction(reason=...)`. The reason is folded into the row's cryptographic hash, so it becomes part of the tamper-evident audit trail.

✨ Functional
* Forensic Error Detection (Mod 9 / Delta-2): Added a Ctrl+E "Find Errors" scan to the Reconciliation tab that uses modulo-9 (digit transposition) and delta/2 (sign reversal) heuristics to identify the specific reconciled transaction(s) responsible for an out-of-balance reconciliation. Flagged rows are highlighted with an explanatory tooltip, and F4 cycles between them. The diagnostic is a pure read-only method that filters to transactions actually feeding `cleared_balance_as_of` for the current account and date window, so candidates are always ones that could plausibly explain the difference.
* Reconciliation PDF Export: Added one-click PDF export of the current reconciliation session (account, date range, cleared balance, statement balance, matched status) via ReportLab, suitable for client review or audit working papers.
* Structural Merchant Memory (Learned Categorization): Introduced a three-tier learned-categorization system that observes how the user categorizes transactions and suggests the same category for future transactions from the same merchant. Fully offline, deterministic, and isolated from ledger math and the cryptographic audit trail.
  - Learning flows through three isolated tables: `sql_merchant_draft` stages a per-transaction candidate; `sql_merchant_pattern` accumulates confirmed occurrences with a Green/Yellow rating; `sql_merchant_blacklist` suppresses patterns the user has explicitly rejected.
  - Blacklisting is keyed per `(merchant_key, category)`, so a rejected category for one merchant never suppresses that merchant's other patterns. Blacklisted pairs still accumulate occurrence counts silently in the background so they can re-cross the suggestion threshold someday — the blacklist only suppresses *surfacing*, not *counting*.
  - Confidence is computed live (consistency ratio × volume factor × rating modifier) and never cached. Green ratings intentionally apply a 1.2× modifier without clamping, so a deliberate vouch can push a pattern over the promotion threshold even where raw statistics alone wouldn't.
  - Drafts are promoted on reconciliation (`mark_reconciled`) or an edit-confirm — the two moments a user actually validates a categorization.
  - Simple Mode pools patterns by `merchant_display`; Pro Mode operates on the granular `merchant_key`. Both read from the same underlying granular storage, so Simple → Pro is non-destructive: nothing has to be re-trained.
  - Patterns that clear a 95% confidence threshold can be promoted to a permanent Category Rule, after which they no longer rely on memory. Rule keywords in Simple Mode use the pooled `merchant_display`; in Pro Mode they use the granular `merchant_key`.
* Blacklisted Patterns Panel: Added a Blacklisted Patterns section to the Category Rules tab, surfacing every `(merchant_key, category)` pair for inspection and one-click reversal. This is the only place a Simple-Mode pooled Red rating can be individually un-done, since the pooled view hides the granular entries.
* Audit Trail Surfacing: Added a "Reason" column to the audit log table, a "Change reason" row to the single-entry detail dialog, and a "Reason" column to the per-transaction history dialog. Searching the audit log now also matches against `change_reason`, so a reason like "corrected transposition" is findable.

🎨 UI/UX
* Reconciliation Flag Affordances: Flagged rows use a distinct background tint (light amber in Light Mode, dark amber in Dark Mode) that resets to Qt's default palette rather than a fixed color when cleared — otherwise alternating-row coloring would be permanently overridden for those rows after the first flag/unflag cycle.
* Reconciliation Info Banner: The info banner above the tree now lists every supported keyboard shortcut (Space, Shift+Arrows, Ctrl+Enter, Ctrl+E, F4, Tab/F3, Esc) so the workflow is discoverable without a manual.

🐛 Bug Fixes
* Reconciliation Selection Preservation Across Refresh: Row selection and current-item focus are now preserved across `refresh_data()` by transaction ID. Qt cannot preserve selection across `clear()` on its own, so any refresh (e.g. after toggling a row) previously lost the user's place.

---

[0.9.4] - Custom Theming Engine, Settings Module & UI Modularization

This release removes the reliance on third-party UI theming libraries in favor of a custom Qt Style Sheet (QSS) engine. It also introduces a dedicated, standalone Settings module for application preferences and improves the modularity of the reporting and navigation tools.

🏗️ Architectural
* Removed `qt_material` Dependency: Replaced the third-party theming library with a custom, comprehensive QSS engine for both Light and Dark modes. This reduces external dependencies and provides precise control over widget styling.
* New Settings Module: Added a dedicated, standalone `SettingsTab` module (`settings_tab.py`) to manage application preferences (themes, date/currency formats, security toggles, and backup configurations), keeping the main UI file clean from the start.
* Navigation Refactor: Replaced the standard `QTabWidget` with a custom `CollapsibleSidebar` and `QStackedWidget` for primary navigation, improving screen real estate.
* Dynamic Report Extraction: Extracted the JSON tax template renderer into a standalone, reusable `DynamicReportWidget` module, decoupling the rendering logic from the main tax tab.

✨ Functional
* Professional Style Reconciliation Workflow: Overhauled the Reconciliation tab with statement session controls (Account, Date Range, Ending Balance), real-time cleared vs. statement balance matching, multi-select toggling, and a keyboard-driven workflow featuring Space to toggle, Shift+Arrows for range selection, and Ctrl+Enter to finish.
  - Type-ahead search distinguishes amount searches (digits, `.`, `-`, `,`) from description searches (letters). Digits search split-line amounts; letters search descriptions.
  - The search buffer auto-resets after 1.5 seconds, shrinks on Backspace, and clears entirely on Esc. Tab/F3 jumps to the next match without retyping.
* Comprehensive Dark Mode: Implemented full Dark Mode support across the application. HTML-rendered views (Overview, Balance Sheet, Taxes) and Matplotlib charts now dynamically update their inline styles to match the active theme.
* Theme Persistence: Added logic to save and load the user's theme preference via a local configuration file (`ui_config.json`), ensuring the app remembers the selected theme across sessions.
* Themed Data Visualization: Matplotlib charts now accept a theme dictionary, allowing figure backgrounds, axes, gridlines, legends, and text colors to automatically adapt to Light or Dark mode.
* Template XLSX Export: Added direct XLSX export functionality to the dynamic tax template sections, allowing users to export specific aggregated category reports independently of the main tax estimator.
* Reconciliation Matching Enhancements: Added expandable parent and child rows (transaction split lines) to the Suggested Matches window. Users can now compare the exact line-item details of potential duplicates side-by-side directly within the app, eliminating the need to cross-reference external bank statements.

🎨 UI/UX
* Modernized Color Palette: Standardized the application color palette using a Slate/Emerald theme for Dark Mode and a soft off-white/gray palette for Light Mode to reduce eye strain and improve visual hierarchy.
* SVG Navigation Icons: Integrated scalable SVG icons for all sidebar navigation tabs, replacing the default system icons for a more cohesive layout.
* Add/Edit Tab Layout Optimization: Reorganized the Add/Edit tab to display the "Add Transaction" and "Filters" forms side-by-side with horizontal scroll protection. Optimized the data table column sizing so that only the "Description" column stretches, keeping dates, accounts, and amounts fixed.

🐛 Bug Fixes
* Matplotlib Colormap Crash: Fixed a `ValueError` crash in pie charts caused by passing unnormalized integers to colormaps when rendering varying numbers of expense categories.
* Chart Theme Propagation: Fixed an issue where chart gridlines and legends would fail to render or crash if the theme dictionary was missing or not passed correctly to the chart factory.
* HTML Text Inheritance: Fixed text color inconsistencies in the Tax tab where standard HTML formatting tags (`<b>`, `<i>`) were incorrectly inheriting accent colors from the global theme instead of the base text color.
* Dark Mode Artifacts: Fixed visual inconsistencies where certain data loading areas, dropdowns, and buttons retained pure white backgrounds or illegible text colors when Dark Mode was activated.

---

[0.9.3] - Unreleased (The Rust Crypto Core & Forensic Testing)

*This release represents the finalization of the security architecture, moving the heavy cryptographic lifting to a compiled, memory-hardened Rust core, and introducing forensic-grade automated testing to guarantee ledger immutability.*

🚨 Breaking Changes
*   **Transitioned to DEK/KEK Envelope Encryption:** Moved away from direct passphrase-based SQLCipher encryption. The database is now encrypted with a random 256-bit Data Encryption Key (DEK), which is wrapped by Key Encryption Keys (KEKs) derived from user passphrases.
*   **Transitioned to Append-Only Immutable Ledger:** Replaced standard `UPDATE` and `DELETE` SQL operations. Transactions are now "voided" via perfect mathematical reversing entries, and "edited" by voiding the original and posting a new one.
*   **Automated Legacy Migration:** Added a seamless migration routine in `main.py` that detects older passphrase-direct databases, securely exports them to a new DEK-encrypted file, and generates the metadata envelope on the user's next launch.

🔒 Security & Cryptography
*   **Rust Crypto Core Integration (`cogni_vault`):** Replaced Python-based PBKDF2 and AESGCM with a compiled Rust module (via PyO3/maturin). The core now uses memory-hard **Argon2id** for key derivation, **AES-256-GCM** for envelope wrapping, **`mlock`** to prevent the locked memory region from being swapped under supported OS semantics, and **`zeroize`** to provide secure memory wiping on drop.
*   **Instant Passphrase Rotation:** Because the DB is encrypted with the DEK, changing the primary passphrase no longer requires re-encrypting the entire database. It now takes milliseconds by simply re-wrapping the DEK in the metadata envelope.
*   **Native Multi-Key Recovery:** The recovery passphrase now derives its own KEK to unwrap the exact same DEK, natively supporting multiple unlock methods without the legacy Fernet-based `.recovery_escrow.json` workaround.
*   **Cryptographic Hash Chaining:** Added `prev_hash` and `row_hash` columns to `sql_transaction_history`. Every audit log entry now includes a SHA-256 hash of its contents combined with the previous row's hash, creating an unbroken, tamper-evident chain.
*   **Automated Supply Chain Scanning:** Integrated `pip-audit` into the `TestRunnerManager`. The quarterly background `pytest` suite now automatically checks for known CVEs in third-party libraries and routes findings to the Notification Center.

🧪 Testing & Forensics
*   **Forensic Integrity Tests:** Added `test_forensic_integrity.py` to the quarterly deep audit. This test actively simulates a malicious actor using raw SQL to alter historical records, verifying that the cryptographic hash chain successfully detects the tampering and that the append-only voiding model functions flawlessly.
*   **Crypto Pipeline Tests:** Added `test_crypto_envelope_pipeline.py` to verify the Rust-backed DEK/KEK wrap/unwrap flow, instant passphrase rotation, and AES-GCM rejection of invalid keys.
*   **Test Suite Modernization:** Refactored legacy test suites (`test_workflows_and_backups`, `test_advanced_features`, `test_tax_report_export`) to use the new DEK hex architecture and append-only `edit_transaction`/`void_transaction` methods. Removed obsolete `rekey` and Fernet escrow tests.

🐛 Bug Fixes
*   **Tax Template Manager Isolation:** Fixed `TaxTemplateManager` ignoring the `templates_dir` argument, ensuring proper isolated testing and dynamic JSON template loading without overwriting user data.
*   **Quarterly Deep Audit Integration:** Integrated the hash chain verification (`verify_audit_chain_integrity`) into the `DeepIntegrityChecker` to automatically detect any raw SQL tampering or external database manipulation.

---

[0.9.2] - Unreleased (The Immutable Ledger & Envelope Encryption)

This release represents a massive paradigm shift in how CogniFinance handles data integrity and cryptography, moving from standard CRUD operations to a professional, append-only accounting model.

🚨 Breaking Changes
*   Transitioned to DEK/KEK Envelope Encryption: Moved away from direct passphrase-based SQLCipher encryption. The database is now encrypted with a random 256-bit Data Encryption Key (DEK), which is wrapped by Key Encryption Keys (KEKs) derived from user passphrases via PBKDF2-HMAC-SHA256 (600,000 iterations).
*   Transitioned to Append-Only Immutable Ledger: Replaced standard UPDATE and DELETE SQL operations. Transactions are now "voided" via perfect mathematical reversing entries, and "edited" by voiding the original and posting a new one.
*   Automated Legacy Migration: Added a seamless migration routine in main.py that detects older passphrase-direct databases, securely exports them to a new DEK-encrypted file, and generates the metadata envelope on the user's next launch.

🔒 Security & Cryptography
*   Instant Passphrase Rotation: Because the DB is encrypted with the DEK, changing the primary passphrase no longer requires re-encrypting the entire database. It now takes milliseconds by simply re-wrapping the DEK in the metadata envelope.
*   Native Multi-Key Recovery: The recovery passphrase now derives its own KEK to unwrap the exact same DEK, natively supporting multiple unlock methods without the legacy Fernet-based .recovery_escrow.json workaround.
*   Cryptographic Hash Chaining: Added prev_hash and row_hash columns to sql_transaction_history. Every audit log entry now includes a SHA-256 hash of its contents combined with the previous row's hash, creating an unbroken, tamper-evident chain.
*   Automated Supply Chain Scanning: Integrated pip-audit into the TestRunnerManager. The quarterly background pytest suite now automatically checks for known CVEs in third-party libraries and routes findings to the Notification Center.
*   Quarterly Deep Audit Integration: Integrated the hash chain verification (verify_audit_chain_integrity) into the DeepIntegrityChecker to automatically detect any raw SQL tampering or external database manipulation.
[0.9.1] - Centralized Config & Security Hardening
🔒 Security
Atomic Secure File I/O: Implemented atomic_secure_write and atomic_secure_write_json using a write-to-temp-then-replace strategy (O_CREAT | O_EXCL) to guarantee 0o600 permissions and prevent data corruption from interrupted writes.
Strict Permission Enforcement: Enforced strict 0o700 directory permissions and 0o600 file permissions for all sensitive state files (recovery escrow, sync identity, pairing lockouts, cooldowns) at the exact moment of creation.
🏗️ Architectural
Centralized Configuration: Created config.py to centralize all file paths, directory definitions, and secure file I/O operations.
Directory Reorganization: Reorganized the default data directory from a single ~/BudgetData folder into a clean ~/CogniSuite layout: BudgetData/ (ledger), BackupData/ (encrypted backups), SystemState/ (non-sensitive info), and .SecurityBackup/ (hidden state/security files).
Path Consolidation: Removed scattered path definitions across main.py, ui_main.py, backup_manager.py, TestRunnerManager.py, integrity_checker.py, deep_integrity_checker.py, passphrase_recovery.py, protocol.py, desktop_sync_server.py, and ledger.py, replacing them with centralized imports.
[0.9.0] - Tab Extraction & UI Stability
🏗️ Architectural
Module Extraction: Extracted Reports Tab, Reconcile Tab, Import/Match Processing (PDF/OFX threads, progress dialogs), Mobile Sync Tab, and Add/Edit Tab into dedicated, standalone modules (reports_tab.py, reconcile_tab.py, import_manager.py, sync_tab.py, add_edit_tab.py).
🐛 Bug Fixes
QGroupBox Borders: Fixed visual inconsistency where Accounts and Reconcile tabs lacked the native OS border present in other tabs.
Garbage Collection Crashes: Fixed RuntimeError (wrapped C/C++ object has been deleted) in Reconcile and Accounts tabs caused by Qt garbage-collecting unparented widgets by explicitly parenting them to their QGroupBox containers.
Sync Button API: Fixed AttributeError crash in the audit sequence caused by reaching directly into self.sync_start_button instead of using the new self.tab_sync.set_start_button_enabled() API.
Orphaned Init Calls: Fixed AttributeError crash on startup caused by orphaned _init_transactions_tab call after extracting the Add/Edit tab.
[0.8.5] - Massive UI Refactor, Graphs, & Import Fixes
🏗️ Architectural
UI Layer Decoupling: Massive refactor of ui_main.py. Extracted Transaction List Tab, Add/Edit Dialogs, and Budgets Tab. Created ui_helpers.py for shared utilities, html_renderers.py for pure-function HTML report generation, and backup_manager.py for isolated backup logic.
Charting Engine: Created graphs.py with ChartFactory and ChartsTab. Includes five chart types: Income vs Expenses, Expense Breakdown (Donut), Net Worth Trend, Top 10 Spending Categories, and Year-over-Year grouped bar comparison.
Dynamic Time Grouping: Charts automatically switch between daily grouping (for ranges ≤31 days) and monthly grouping to prevent the "Current Month" view from rendering as a single point.
✨ Functional
Account Editing: Added safe account rename and type change with automatic cascading updates to all historical transactions, budgets, and category rules.
Manual External Backups: Added "Manual Backup to External Drive" and "Restore from Backup" features with safety snapshots, WAL/SHM file purging, and forced app restart.
Backup Health Tracking: Added visual Backup Status indicator (Red/Yellow/Green) to the Overview tab, tracking health based on days since last backup and number of new entries.
Category Rules Expansion: Removed artificial 'Expense only' restriction on Category Rules. Rules can now target any account type, enabling automatic routing of transfers and misrouted imports.
Bank-to-Bank Merges: Updated _process_confirmed_matches to handle Bank-to-Bank merges by marking the existing transaction as reconciled and voiding the duplicate import.
🐛 Bug Fixes
Notification Accumulation: Fixed old 'integrity_check_complete' notifications persisting across app restarts. Added stale source auto-resolve on startup.
Notification Spam: Suppressed notification for 'No audits currently due' messages caused by the 30-second sync polling loop.
TestRunnerManager Crash: Fixed silent crash caused by undefined CONFIG_PATH variable in _set_infra_failures.
Progress Dialog Race Condition: Fixed NoneType AttributeError when background thread emitted late signals after dialog was closed.
Test Suite 4-Tuple Fix: Fixed test_encrypted_backup_and_restore cascading failure caused by test suite still using old 3-tuple format for update_transaction calls.
Transfer Display Amount: Fixed Add/Edit tab displaying 0.00 for pure Asset-to-Asset transfers by replacing hardcoded Expense/Revenue logic with the robust _transaction_display_amount fallback.
OFX Pipeline Fixes: Fixed OFX import bypassing Category Rules, fixed OFX double-credit bug for Liability inflows, and fixed Reconciliation Matcher falsely identifying recurring monthly subscriptions as duplicates by enforcing a hard 14-day maximum date window (MAX_DATE_WINDOW_DAYS).
UI Polish: Fixed "Delete" button text clipping (locked to 120px), fixed Add/Edit dialogs opening too small (set explicit min sizes), and replaced blocking "Success" popups with non-blocking status bar messages for batch-entry workflows.
[0.8.0] - Advanced Reconciliation & Import Threading
✨ Functional
Fuzzy Reconciliation Engine: Added a reconciliation step when importing statements (OFX/QFX/PDF) using fuzzy heuristics to match duplicate statement entries and mobile pending entries. Returns a 3-tier match list (Green/Yellow/Red) for manual verification.
OFX Deduplication: Fixed OFX import returning unfiltered transaction lists and silently skipping fuzzy duplicate detection by calculating amounts from the lines array as a fallback.
🎨 UI/UX
Import Progress: Added visual progress bars (QProgressDialog) for both PDF and OFX imports with real-time file and transaction count updates. Moved parsing and DB writes to a background QThread to prevent UI freezing.
Match Indicators: Replaced text-based tier labels in Suggested Matches dialog with colored circle indicators (green/amber/red). Added 'Date Diff' and 'Merchant Match' columns with color-coded percentages and hover tooltips explaining match confidence.
Interactive Tables: Made Suggested Matches dialog columns user-adjustable via interactive header resizing.
[0.7.0] - Architectural Math Validation
🏗️ Architectural
UI Math Isolation: Began the process of rewriting the add/edit tab, moving math validation completely out of the UI. Major architectural flaw having any math validation conducted within ui_main.py. Math validation moved to ledger.py (consistent with the rest of the design).
Sync Lockout: Disabled the sync button in the mobile tab during automated tests to prevent database lock conflicts.
[0.6.0] - Notifications & Automated Audits
✨ Functional
Notification Center: Added a persistent notification area allowing messages to be displayed to users without the use of intrusive pop-ups.
Automated Backups: Added timed background backups (every 4 hours) and post-sync backups.
Quarterly Systems Check: Added an automated systems check (test suite excluding ledger checks) that runs every 3 months.
TestRunnerManager: Added a separate module to handle running weekly and quarterly tests, tracking cooldowns via secure JSON files.
Tax Liability Estimator: Added a pull-down menu for the tax liability estimator, allowing JSON templates for tax years and rates to be loaded dynamically.
[0.5.0] - Import Pipeline & Background Testing
🔒 Security & Structural
WAL & Backup Safety: Verified main database is complete and flushed WAL (Write Ahead Logging) before backup DB encryption to prevent dropped transactions. Added WAL + busy timeout to the audit tab to prevent lock collisions.
Formula Injection Prevention: Sanitizes JSON templates for illegal XML characters before exporting to .xlsx (includes protection from basic code injection).
🏗️ Architectural
OFX/QFX Support: Added full architectural support for OFX/QFX bank feeds.
Background Integrity Tests: Added the ledger logic tax from the test suite to the start of the program as a QProcess. Integrity tests run weekly. Added a QTimer to delay test start, ensuring the program loads immediately for the user.
HTML Tax Tab: Changed tax tab to HTML to be consistent with other tabs and improve presentation.
🐛 Bug Fixes
Keyring Fallback: Fixed OS keyring failing silently and disabling on the login screen.
[0.4.0] - Tax Estimation Engine
🏗️ Architectural
JSON Tax Templates: Removed hard-coded tax logic and moved it to external JSON templates. Placed the Tax tab into a separate module.
Dynamic Tax Loading: Tax-tab now supports loading multiple templates (JSON) and exporting them to the same .xlsx file.
Tax Liability Estimator: Added a comprehensive estimator currently using the 2025/2026 CRA tax code (Federal, Provincial, CPP/CPP2, OHP).
[0.3.0] - Auditing & Logging
✨ Functional
Audit Trail: Added the audit tab, making the transaction audit trail visible and searchable. Extracted into a separate module.
Actionable Logs: Added an actionable log and log tab as a separate module for real-time debugging.
Encrypted Backup on Close: Fixed create_encrypted_backup to ensure it is actually being called during program closing.
[0.2.0] - UI/UX Polish & Overview
✨ Functional
Unified Refresh: Unified the refresh button for tabs needing refresh and added auto-refresh on tab changes.
Overview Enhancements: Added budget alerts, reconciliation status, and recent transactions to the Overview tab.
Status: All major core functions stabilized.
[0.1.0] - Initial Sync & Core Foundation
✨ Functional
Mobile Sync Handshake: Implemented the initial SPAKE2 handshake between Android and Desktop.
Sync Logging: Added dedicated logs for desktop_sync_server.
Core Ledger: Established the foundational double-entry SQLite ledger, SQLCipher encryption, and basic PyQt6 UI layout.
