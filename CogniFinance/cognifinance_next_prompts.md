# CogniFinance — Saved Implementation Prompts

Two independent prompts, each usable in its own fresh conversation. They reference
decisions already made in a prior design discussion; each prompt restates the
relevant context so it doesn't depend on that conversation's history.

---

## PROMPT 1: Statement-Based Reconciliation Tab (Advanced Mode)

**Context:** CogniFinance is a PyQt6 + SQLCipher personal/small-business ledger app.
`ledger.py`'s `GeneralLedger` is an append-only, double-entry ledger: transactions are
never updated/deleted in place — `edit_transaction` voids the old row and inserts a
replacement; `void_transaction` posts a reversing entry. Every transaction has a
`change_reason` column (nullable) used to tag *why* an edit happened (e.g.
`"reallocation"` for a same-total recategorization, vs. a brand-new entry when the
total genuinely changes). A cryptographic hash chain (`sql_transaction_history`,
`verify_audit_chain_integrity`) makes the history tamper-evident.

The current reconciliation tab (`reconcile_tab.py`) is a plain transaction list with a
checkbox to mark items reconciled — no statement context, no running total, no keyboard
navigation. This prompt replaces/extends it for **Advanced mode** users (see Prompt 2 —
if that mode-gating scaffolding doesn't exist yet when you pick this up, build this as
a new `AdvancedReconciliationTab` class alongside the existing simple one; don't block
on Prompt 2 being done first).

**Before writing any code:** ask the user to upload `ledger.py`, `reconcile_tab.py`,
`config.py`, and `import_manager.py` (specifically its `SuggestedMatchesDialog` class),
so the implementation extends real existing code rather than guessing at method
signatures. Do not assume — verify against the actual files.

### Goals

1. **Statement-based reconciliation session.** Instead of an open-ended checklist, the
   user picks an account, a statement start/end date, and an ending balance
   (`GeneralLedger.record_statement_balance` already exists for storing this — check
   its exact signature). The view then shows only transactions in that date range for
   that account, with a **live-updating difference**:
   `Cleared so far: $X.XX  |  Statement: $Y.YY  |  Difference: $Z.ZZ`
   — green at `$0.00`, red/amber otherwise. A "Finish Reconciliation" action is only
   enabled when the difference is exactly zero.

2. **Shared split-allocation widget.** Extract the split-line editor + running-total
   balance validator (originally designed for the import-merge dialog's
   fee/tax/category reallocation use case) into a standalone, reusable component
   — e.g. `split_allocation_widget.py` — used by both the import-merge dialog and this
   reconciliation tab. Design constraints learned from `ledger.py`'s
   `edit_transaction`, which this widget must not violate:
   - A transaction must have **at least 2 lines** after edit.
   - `total_dr == total_cr` is enforced server-side — the widget's "Apply" button
     must be disabled until the live sum matches the target total exactly; don't rely
     on `edit_transaction`'s rejection as the primary guard, it should be a backstop.
   - `edit_transaction`/`void_transaction` both reject a transaction that's already
     `reconciled` — any split edit must happen *before* `mark_reconciled` is called,
     never after, matching the existing order in `import_manager.py`'s
     `_process_confirmed_matches`.
   - When this widget is used for a same-total recategorization, call
     `edit_transaction(..., reason="reallocation")`. Don't invent a new mechanism —
     `change_reason` already exists for this.

3. **Keyboard-driven workflow.** This is the main efficiency ask — bookkeepers work
   down a statement line by line and mouse-clicking every row is the biggest friction
   point. Implement:
   - `↑` / `↓` — move focus between transaction rows, no click required.
   - `Space` — toggle reconciled state on the focused row (prefer Space over Enter for
     the toggle, since Enter often means "confirm/close" elsewhere in this app —
     check `ui_main.py`'s existing dialog conventions and stay consistent).
   - `Shift + ↑ / ↓` — extend a contiguous range selection; `Space` then toggles the
     whole range at once.
   - Type-ahead — typing digits filters/jumps by amount; typing letters filters/jumps
     by description.
   - `Tab` or `Enter` on a row with a split marker opens the shared split-allocation
     widget **inline**, not as a blocking modal, so keyboard flow isn't interrupted;
     `Esc` cancels without applying; arrow keys move between split lines inside it.
   - `Ctrl+Enter` — triggers "Finish Reconciliation", only enabled at `$0.00`
     difference.

4. **Visual language reuse.** The reconciliation matcher's green/amber/yellow-red
   confidence-tier colors (see `reconciliation_matcher.py`'s `MatchTier`) should be
   reused for "cleared / uncertain / unmatched" states here rather than inventing a
   new color scheme.

5. **Period locking (groundwork for month-end close — build the mechanism, wire the UI
   minimally).**
   - New table:
     ```sql
     CREATE TABLE IF NOT EXISTS sql_period_locks (
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         account_name TEXT,
         start_date TEXT,
         end_date TEXT,
         locked_at TEXT,
         reconciled_balance TEXT
     )
     ```
     Add via the same `PRAGMA table_info` + conditional `ALTER TABLE`/`CREATE TABLE IF
     NOT EXISTS` pattern already used in `ledger.py`'s `_init_db` for additive schema
     changes.
   - Once a statement session hits `$0.00` and the user confirms "Finish
     Reconciliation," write a lock row for that account/date range.
   - Add a guard near the top of `record_transaction`, `edit_transaction`, and
     `void_transaction`: if the transaction's date falls inside a locked period for
     any account the transaction touches, reject with a clear message (e.g. "Date
     falls in a locked/reconciled period for {account}. Unlock first or use a
     prior-period adjustment.") unless the caller explicitly passes an override/reason
     indicating a deliberate prior-period adjustment
     (`reason="prior_period_adjustment"`).
   - An explicit "Unlock period" action (with a confirmation dialog — this is
     destructive to the guarantee a prior reconciliation gave the user) should delete
     the lock row. This doesn't need a full UI polish pass yet — a functional button
     is enough for this iteration.
   - **This locking check must live in `ledger.py` itself, unconditionally** — not
     gated behind "advanced mode" in the UI layer. Simple-mode users simply never
     create lock rows, so the check is always a no-op for them; but the enforcement
     code path must be identical regardless of which UI opened the database, so a
     locked/reconciled period can never be silently edited no matter which build or
     mode touches the file.

6. **Reconciliation summary/report** (can be minimal for this pass): once finished,
   show/export a simple "As of {end_date}, reconciled to ${balance}, {N} transactions
   cleared" summary. Keep this simple — a fuller month-end report is future scope, not
   part of this prompt.

### Explicitly out of scope for this prompt
- Full month-end close across multiple accounts simultaneously.
- License/tier gating of any of this — that's a separate, later decision layered on
  top of whatever mode system Prompt 2 builds.
- Any change to `SuggestedMatchesDialog`'s own behavior beyond factoring its split
  editor out into the shared widget — don't regress the import-merge flow.

### Verification checklist before considering this done
- Existing tests (`test_ledger_logic.py`, `test_forensic_integrity.py`,
  `test_split_transactions.py`, etc.) still pass — period-lock guards must not break
  normal unlocked-period transaction flows.
- A transaction inside a locked period is rejected by `record_transaction` /
  `edit_transaction` / `void_transaction`, confirmed with a new test.
- The split-allocation widget rejects "Apply" when `total_dr != total_cr`.
- Keyboard-only operation: reconcile an entire statement without touching the mouse.

---

## PROMPT 2: Simple / Advanced UI Mode Separation (UI Engine Refactor)

**Context:** Same CogniFinance codebase. `ui_main.py` currently constructs and wires up
all tabs directly (per the 0.9.0 changelog, tabs were already extracted into their own
modules: `reports_tab.py`, `reconcile_tab.py`, `import_manager.py`'s dialogs,
`sync_tab.py`, `add_edit_tab.py`). The goal now is to let the app run in two UI modes —
**Simple** (today's plain feature set, aimed at personal users who don't need or want
accounting-professional-grade tools) and **Advanced** (statement-based reconciliation,
period locks, split allocation, month-end tooling, eventually reporting for phase
3/4 small-business and accounting-professional users) — from a single codebase and
single database format, without forking into separate builds.

**Design decisions already made — do not re-litigate these:**
- **One shared core engine, always.** `ledger.py`'s rules (balance validation, period
  locks, the hash chain, `change_reason` tagging) must be enforced unconditionally,
  regardless of which UI mode is active. The mode flag controls *what the UI shows*,
  never *what the ledger allows*. This is a data-integrity requirement, not a
  preference — a Simple-mode session must never be able to silently violate a rule an
  Advanced-mode session relies on (e.g. editing inside a locked/reconciled period).
- **No `ui_lite.py` / `ui_pro.py` tier-based files.** Splitting UI code by tier
  recreates a monolith along a different axis and scatters logically-related feature
  code across unrelated files. Instead, keep the existing **feature-based** module
  boundary (one file per feature area) and let each feature module contain both its
  Simple and Advanced classes:
  ```python
  # reconcile_tab.py
  class SimpleReconciliationTab(QWidget): ...      # today's plain checklist, unchanged
  class AdvancedReconciliationTab(QWidget): ...     # statement/lock/split-aware version
  ```
  If an Advanced variant grows too large for one file, split it as e.g.
  `reconcile_tab_advanced.py` importing shared pieces from `reconcile_tab.py` — that's
  a per-feature decision made when a file actually gets unwieldy, not a blanket rule
  applied up front.
- **`ui_main.py` becomes a pure orchestration engine.** It should not contain any
  feature-specific logic or scattered `if simple_mode:` checks. It selects which class
  to instantiate per tab via one declarative registry:
  ```python
  TAB_REGISTRY = {
      "reconcile": (SimpleReconciliationTab, AdvancedReconciliationTab),
      # add more (key: (simple_cls, advanced_cls)) as each feature gets an Advanced split.
      # Features with only one implementation so far are simply not yet in this table
      # and continue to be constructed directly, unchanged.
  }

  def _build_tabs(self):
      advanced = is_advanced_mode_enabled()
      for key, (simple_cls, advanced_cls) in TAB_REGISTRY.items():
          cls = advanced_cls if advanced else simple_cls
          self.tabs[key] = cls(self.gl)
  ```
- **The mode flag is a user/app preference, not a per-database property.** It's about
  which UI a person wants to see, not a property of their financial data — the same
  `.db` file should open correctly regardless of which mode the app is currently in.
  Store it via the existing `config.py` pattern (a new path constant +
  `atomic_secure_write_json`, or a simple settings file — check what convention
  `config.py` already uses for non-sensitive app state, e.g. `NOTIFICATIONS_PATH`'s
  handling, and match it) with a function like `is_advanced_mode_enabled() -> bool`.
- **Don't build a license check yet** — just a plain settings toggle for now
  (e.g. a checkbox somewhere in app settings, "Enable advanced/professional
  features"). Structure `is_advanced_mode_enabled()` as the single choke point so a
  license/entitlement check can wrap or replace its internals later without touching
  any tab class or the registry.

**Before writing any code:** ask the user to upload `ui_main.py`, `config.py`, and
`reconcile_tab.py`, so the refactor is grounded in the actual current tab-construction
code rather than an assumed structure.

### Deliverables for this prompt
1. `is_advanced_mode_enabled()` (plus a corresponding setter/settings UI entry point)
   added following `config.py`'s existing conventions for non-sensitive app state.
2. `ui_main.py` refactored to build tabs via the `TAB_REGISTRY` pattern shown above,
   replacing however tabs are currently constructed inline.
3. `reconcile_tab.py` restructured to expose both `SimpleReconciliationTab` (today's
   existing behavior, unchanged) and a stub/minimal `AdvancedReconciliationTab` class
   — the full Advanced implementation is Prompt 1's job; this prompt only needs the
   scaffolding in place so Prompt 1 has something to slot into (or, if Prompt 1 has
   already been done in a separate session, wire the registry to the class that
   already exists rather than creating a stub).
4. No other existing tab needs a Simple/Advanced split yet — leave every other feature
   module untouched and constructed directly, exactly as today.
5. A short note/comment at the top of `TAB_REGISTRY` explaining the pattern, so future
   features know how to add themselves to it.

### Explicitly out of scope for this prompt
- Any actual Advanced-mode reconciliation logic (that's Prompt 1).
- License/entitlement enforcement.
- Splitting any tab other than reconciliation into Simple/Advanced variants — do that
  per-feature, later, only when that feature actually gets an Advanced version.

### Verification checklist before considering this done
- Toggling the mode setting and restarting the app switches which reconciliation tab
  class is constructed, with zero other behavior change.
- `ledger.py` is untouched by this prompt — this is purely a UI-layer refactor.
- Existing tests unaffected; if any test constructs `BudgetApp`/`ui_main` directly, it
  still passes regardless of mode default.
