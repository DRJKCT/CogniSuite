ADR-001: Month-End Close and Ledger Archiving Strategy
Status: Proposed (Staged Implementation)
Date: 2026-09-09
Deciders: CogniFinance Product Lead
Tags: Architecture, Performance, Compliance, GAAP, Data Retention

1. Context and Problem Statement
CogniFinance currently operates on a 100% local, append-only immutable ledger. All transactions are loaded into memory (self.transactions) on application startup.

The Problem:

As the ledger grows over years of use, startup time and memory usage scale linearly with transaction count (O(n)).

For SMBs and long-term users, a single database file will eventually become unwieldy.

Professional accounting standards (GAAP/IFRS) require the ability to close accounting periods (months/quarters/years) to prevent retroactive modifications.

There is currently no mechanism to archive "cold" historical data without breaking the cryptographic audit chain.

The Opportunity:

The newly implemented Reconciliation workflow (v0.9.4) provides a natural gatekeeper: a period is only considered "closed" when the cleared balance exactly matches the statement balance.

The append-only ledger model is perfectly suited for reversing entries (instead of destructive edits).

2. Decision Drivers
Performance: Maintain sub-second startup times for users with 10+ years of transaction history.

Compliance: Support GAAP-compliant period closures (hard close with reversing entries).

Privacy: Data must never leave the local machine; archiving must use the same DEK encryption as the live ledger.

Auditability: The cryptographic hash chain (prev_hash/row_hash) must remain unbroken across archives.

Incremental Delivery: This is a complex feature; it must be implemented in stages without breaking existing core functionality.

3. Considered Options
Option	Description	Decision
A. Do Nothing	Keep the monolithic ledger; ignore scaling issues.	Rejected - The app will become unusable for SMBs (Phase 3 target).
B. Rewrite Ledger with ORM	Replace in-memory lists with SQLAlchemy and query on-demand.	Rejected - Massive rewrite (~3 months), risk of introducing bugs, violates "one step at a time" principle.
C. Staged Hard Close + Hot/Cold Archive (Selected)	Use Reconciliation as the close trigger. Add metadata locks first, then reversing entries, then physical archiving to separate encrypted files.	Accepted - Minimizes risk, leverages existing UI, preserves privacy, and scales seamlessly.
4. Decision
We will implement a Staged Month-End Close and Archiving System:

Phase 1: Metadata Lock (Month-End Close V1)
Goal: Prevent edits/voids on historical periods.

Implementation:

Add sql_closed_periods table to track closed accounts and date ranges.

Reconciliation session detects a balanced state (Cleared Balance == Statement Balance) and enables a "Close Period" button.

When clicked, the app inserts a record into sql_closed_periods.

Enforcement: Future edit_transaction and void_transaction calls check the transaction's date against closed periods. If locked, the operation is rejected with a user-facing message.

Impact: Minimal code changes; immediately gives users control over their books.

Phase 2: Reversing Entries (Month-End Close V2)
Goal: Allow corrections to closed periods without breaking the lock.

Implementation:

When a user attempts to edit a locked transaction, the app offers to create a "Reversing Entry" on the next open day (or a user-specified date).

This effectively voids the effect of the old transaction by posting a new, perfectly balanced transaction that offsets it.

Impact: Brings CogniFinance in line with GAAP standards for accrual accounting.

Phase 3: Physical Archiving (Hot/Cold Storage) - Future
Goal: Remove archived transactions from the "Hot" ledger to maintain performance.

Implementation:

On period lock, optionally export all transactions within that period to a new, encrypted SQLite file (archive_{account}_{period_end}.db).

Replace the archived transactions in the "Hot" ledger with a single "Summary Anchor" transaction (maintaining the closing balance).

Include the archive file's SHA-256 checksum in sql_closed_periods to ensure integrity.

Hash Chain Continuity: The first transaction in the new hot period references the row_hash of the last transaction in the archive (stored in the summary anchor), ensuring an unbroken forensic chain.

5. Consequences
Positive Consequences
Performance: The Hot ledger remains lean (only current + last 2 years), ensuring instant startup for SMBs.

Compliance: Period locking and reversing entries are standard GAAP/IFRS requirements.

Marketability: This feature is a key differentiator against standard personal finance apps and competes directly with QuickBooks/Xero.

Privacy: All archival and cryptographic operations happen entirely on the local machine.

Negative Consequences (Trade-offs)
Increased UI Complexity: The Reconciliation tab needs a prominent "Lock" button, and the Settings/Archive Manager requires a new interface for browsing archived periods.

Code Complexity: The GeneralLedger will need to check sql_closed_periods on every edit/void call, adding a small overhead.

Backup Strategy: Users must be educated to back up both the Hot ledger and the Archive folder, as the data is now split.

6. Compliance Standards
GAAP (Generally Accepted Accounting Principles):

Supports the "Period of Performance" concept.

Enforces the distinction between "Adjusting Entries" (reversing) and "Correcting Entries" (editing).

Audit Standards (SOC 2 / SOX):

The cryptographic hash chain provides tamper-evident logging.

The archive checksum provides proof that exported data has not been altered post-export.

7. Related Artifacts
UI: reconcile_tab.py (contains the core statement session logic and the trigger for closing).

Backend: ledger.py (GeneralLedger.close_period(), GeneralLedger.archive_period()).

Schema: sql_closed_periods (new table) and modifications to sql_transaction_history (retains hash chain continuity).

Dependencies: This ADR depends on the successful implementation of the Reconciliation session controls ([0.9.4] - Professional Style Reconciliation Workflow).

8. Implementation Roadmap
Milestone	Target Version	Deliverable	Estimated Effort
Phase 1	v0.9.5	Metadata lock table + UI enforcement	1 week
Phase 2	v0.9.6	Reversing entry workflow in Add/Edit tab	1 week
Phase 3	v1.0.0	Archive Manager UI + Hot/Cold split logic	2 weeks
9. Open Questions
Should the Archive Manager be a standalone desktop tool, or an integrated tab within the main app? (Decision: Integrated tab - keeps UX consistent).

How should the system handle archiving of multi-currency transactions? (Decision: Archive each currency's sub-ledger separately, maintaining the FX rates logged at the time of the transaction).

This ADR will be updated as phases are implemented and user feedback is collected.
