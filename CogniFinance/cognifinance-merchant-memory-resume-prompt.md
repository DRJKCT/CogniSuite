# CogniFinance — Merchant Memory / Structural Learning Feature: Resume Context

Use this prompt to resume design/implementation work on this feature in a new session.

## Project Context

CogniFinance is a desktop double-entry accounting app (PyQt6 + SQLCipher3, Rust crypto core). Core principles:
- **Append-only ledger** with cryptographic hash chain (tamper-evident, amend-only, never edited/deleted in place)
- **Local-first, encrypted, privacy-focused** — positioning avoids "AI/ML" buzzwords
- **Governing UX philosophy**: the program simplifies without eliminating user control. Users can relinquish control (automation) if they want, but are never forced to. The one deliberate exception is the ledger's audit trail itself — that layer protects the user's control over *evidence* (provable to third parties: auditors, courts, estates, disputes), which is a different kind of control than control over *interpretation* (categorization, rules, confidence). Everything in this feature lives in the interpretation layer, so it's fully mutable/reversible by design.
- **Modes**: "Pro" is the only version currently built. "Simple Mode" (planned) works by **hiding/automating existing functionality, not removing or forking code**. Not a hard rule, but the default approach.
- Existing merge/reconciliation window already has a Green/Yellow/Red dot system indicating duplicate-transaction likelihood.

## Feature: Merchant Memory / Learned Categorization Patterns

Goal: passively learn merchant → category associations from user behavior, surface suggestions, let users promote strong patterns to explicit Category Rules — without ever silently overriding user control or touching ledger integrity.

### Decisions Made (in order, all considered settled)

1. **Merchant normalization is decoupled from categorization.** Raw bank string → normalized merchant is a pure, zero-risk lookup. It never affects ledger math, only how merchants are grouped for learning.

2. **Two-tier merchant identity, not one:**
   - `merchant_key` — granular (e.g., preserves store number: "STARBUCKS #123" vs "#456"). This is the actual learning/pattern key. **Always stored granular, for everyone, regardless of mode.**
   - `merchant_display` — collapsed/pooled name for UI display ("STARBUCKS"). Used for Simple Mode presentation only.

3. **Draft state prevents noise from polluting learned patterns.**
   - New table: `merchant_draft` — keyed by `tx_id` + `line_id`, holds `raw_string`, `merchant_key`, `merchant_display`, `category_suggested`, `created_at`.
   - Only promoted to the permanent pattern table when the user confirms the transaction (in Add/Edit) or reconciles it.
   - If a transaction is voided/edited before confirmation, its draft row is simply orphaned — never promoted. No cleanup logic needed beyond that (garbage collection of old orphans is an open minor question, not yet decided).

4. **Confidence formula — computed live, never cached/stored as a derived value:**
   ```
   consistency_ratio = count_this_category / count_total   (per merchant_key)
   volume_factor     = min(1.0, occurrences / VOLUME_CAP)   // VOLUME_CAP tunable constant, e.g. 10
   base_confidence   = consistency_ratio * volume_factor
   final_confidence  = base_confidence * user_modifier      // Green = 1.2x, Yellow = 1.0x
   ```
   - Store **raw counts** (`count_this_category`, `count_total`, occurrences) as plain integer columns — never a stored/cached confidence score, never JSON. Confidence is derived at read/query time so it can never drift from the raw counts (matches the "provable, not black-box" positioning). This app has no realistic data volume where live computation is a performance concern.
   - `final_confidence` is **not clamped at 1.0** — user rating (Green) is a stronger, deliberate signal and should be able to push a pattern over threshold even if raw math alone wouldn't. This is intentional, not a bug — should be documented as such in code.
   - Below a minimum occurrence floor (e.g., `occurrences < 2`), don't surface a suggestion at all — avoid showing high-confidence-looking scores from a single data point.
   - Two separate thresholds exist conceptually: a lower bar for showing a suggestion (low stakes, user still reviews), and a higher bar for activating "Promote to Rule" (high stakes, becomes a persistent auto-applied rule). Confirm which one any user-facing "confidence %" setting is actually gating.
   - Confidence threshold should be user-adjustable but presented as **stepped values** (95/90/85/80...) rather than a continuous slider, since consistency_ratio is inherently discrete at low occurrence counts — a fine-grained slider implies false precision.

5. **Rating system — Green/Yellow are modifiers, Red is not a modifier at all:**
   - **Green** = confidence modifier (1.2x), boosts toward promotion. User vouching is a strong, deliberate signal.
   - **Yellow** = neutral (1.0x), no opinion, just a data point.
   - **Red** = "this is wrong, don't count it." Triggers: (a) delete/scrub the pattern's accumulated counts, and (b) insert into a blacklist so the same wrong suggestion doesn't reappear once enough occurrences pile back up.
   - Rejected an earlier "windowed re-learn with a timestamp" approach as unnecessary complexity — delete-and-recount achieves the same outcome more simply, and reuses the app's existing delete-a-rule UI pattern from the Category Rules tab.

6. **Blacklist is keyed on `(merchant_key, category)` pairs — never the merchant alone.**
   - Blacklisting "STARBUCKS #123 → Dining" must not suppress "STARBUCKS #123 → Coffee" or any other category for that merchant.
   - **Must be visible and reversible in the UI** (e.g., a small "Blacklisted Patterns" list in Category Rules tab with its own un-blacklist action) — consistent with the app's "nothing silent or unaccountable" principle. This was the one place pooling (see #7) could otherwise silently blacklist a granular pattern the user never individually saw — visibility is the fix, not redesigning the pooling.

7. **Mode-gated pooling (Simple vs Pro), no schema fork, no migration needed:**
   - Storage is **always granular** (`merchant_key`-based), for both modes, all the time.
   - **Simple Mode**: pools/aggregates granular patterns sharing a `merchant_display` for display and rating purposes. A user rating a pooled suggestion (Green/Red) fans out that action to *every* granular pattern underneath it sharing that `(merchant_display, category)` — this is a one-to-many write, needs its own explicit operation (not just a single-row update).
   - **Pro Mode**: shows granular patterns individually, no pooling, no fan-out — ratings apply to one `merchant_key` at a time.
   - Because storage never differs by mode, **switching Simple → Pro requires no rebuild/migration** — granular rows already exist; Pro just stops pooling them for display and stops fanning out ratings.
   - Tradeoff acknowledged: pooling in Simple Mode trades precision for simplicity (a wrong pooled Red rating can blacklist a granular pattern that was individually fine) — considered acceptable given Simple Mode's audience (individuals) doesn't need or want store-level precision anyway.

8. **Target audience split confirmed the granularity design, not an adaptive/heuristic one:**
   - Individuals (Simple Mode) don't need store-level granularity — low transaction volume per location, no real intent-based distinction, granularity would just add noise with no benefit.
   - Bookkeepers/small business (Pro/Phase 3) genuinely need it — franchise/cost-center distinctions, sufficient volume for granular patterns to reach confidence thresholds in reasonable time.
   - Mode determines pooling, not an adaptive per-merchant splitting heuristic — simpler and matches audience segmentation already planned in the roadmap.

9. **UI integration: fold into the existing merge/reconciliation window rather than a new modal.**
   - Reuses an existing natural interruption point in the workflow instead of adding a new one; both transaction-confirmation and pattern-promotion happen on the same user action.
   - **Progressive disclosure** to satisfy "informative and efficient, not overwhelming":
     - Default: category suggestion shown as a **color-coded cell/tag** (no separate confidence column) — reuses the merge window's *existing* Green/Yellow/Red dot semantics (dupe-likelihood), extended to mean confidence for category suggestions. Confirmed safe to reuse this exact palette because the merge window already has this as its own local color grammar (not colliding with anything on that screen).
     - **Hover** reveals a lightweight, read-only preview (occurrence count, consistency ratio) — answers "why," no interactive controls.
     - **Click-to-expand** reveals the full interactive view — actual numbers, the Green/Yellow/Red rating control, promote-to-rule affordance. Click-to-expand chosen as the primary mechanism (not hover) because rating controls need to be stable/clickable, which hover tooltips don't support well.
   - Correcting a category during merge review always feeds the raw occurrence counts (real data, always counted) but does **not** auto-apply a Green/Red rating — ratings stay reserved for deliberate user action, since a rating is a stronger claim than a one-off correction.
   - Confidence-tint color and duplicate-likelihood dots are visually separated by **position/shape**, not palette — e.g., dots stay as small circular indicators in their existing location (far right), confidence tint lives on the category cell itself. Same palette, different visual location, so nothing is ambiguous about which "confidence" a colored element refers to. Exact layout/spacing is flagged as easy to adjust later (not a schema-level decision).
   - Explicitly rejected using this same green/yellow/red palette to also carry the Add/Edit tab's account-type color coding (green=asset, red=expense, blue=equity, black=liability, purple=revenue) in the same context — those are a different, unrelated semantic system elsewhere in the app; kept separate to avoid cross-screen ambiguity.

10. **Auto-reconciliation (Simple Mode, future)**: should be explicit/visible when it happens (e.g., an activity log line: "Auto-reconciled 12 transactions"), not a silent background action — same "nothing silent" principle applied to reconciliation as to merchant memory.
    - Possible future refinement (explicitly flagged as very late-stage): letting a Simple Mode user selectively unhide/reactivate the full reconciliation tab without leaving Simple Mode. Low risk to defer — pure UI/settings toggle, zero backend or schema impact, since Simple Mode never removes underlying functionality, only hides it.

### Open / Not Yet Decided
- Garbage collection policy for orphaned `merchant_draft` rows (do they ever get cleaned up, and when).
- Exact schema/SQL for `merchant_draft`, `merchant_pattern`, `merchant_blacklist` tables — discussed at a column-list level, not yet finalized as DDL.
- Exact promotion workflow: what happens step-by-step when "Promote to Rule" is clicked (pre-fills explicit rule creation dialog per the original concept — not yet re-verified against later decisions).
- Default value for `VOLUME_CAP` and default/initial confidence threshold — treat as tunable constants in one config location, not hardcoded.
- Exact hover-preview vs click-to-expand interaction details/wireframe.
- Whether audit-trail/evidentiary positioning ("provable to third parties") should be reflected more explicitly in Phase 1/2 marketing — raised as a tangent, not decided.

### Next Step (as of this document)
Move from prose design to actual schema/DDL for `merchant_draft`, `merchant_pattern`, and `merchant_blacklist`, then define the promotion workflow step-by-step.
