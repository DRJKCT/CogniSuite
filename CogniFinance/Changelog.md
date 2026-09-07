CogniFinance Changelog

## [0.9.3] - Unreleased (The Rust Crypto Core & Forensic Testing)
*This release represents the finalization of the security architecture, moving the heavy cryptographic lifting to a compiled, memory-hardened Rust core, and introducing forensic-grade automated testing to guarantee ledger immutability.*

### 🚨 Breaking Changes
*   **Transitioned to DEK/KEK Envelope Encryption:** Moved away from direct passphrase-based SQLCipher encryption. The database is now encrypted with a random 256-bit Data Encryption Key (DEK), which is wrapped by Key Encryption Keys (KEKs) derived from user passphrases.
*   **Transitioned to Append-Only Immutable Ledger:** Replaced standard `UPDATE` and `DELETE` SQL operations. Transactions are now "voided" via perfect mathematical reversing entries, and "edited" by voiding the original and posting a new one.
*   **Automated Legacy Migration:** Added a seamless migration routine in `main.py` that detects older passphrase-direct databases, securely exports them to a new DEK-encrypted file, and generates the metadata envelope on the user's next launch.

### 🔒 Security & Cryptography
*   **Rust Crypto Core Integration (`cogni_vault`):** Replaced Python-based PBKDF2 and AESGCM with a compiled Rust module (via PyO3/maturin). The core now uses memory-hard **Argon2id** for key derivation, **AES-256-GCM** for envelope wrapping, **`mlock`** to prevent the locked memory region from being swapped under supported OS semantics, and **`zeroize`** to provide secure memory wiping on drop.
*   **Instant Passphrase Rotation:** Because the DB is encrypted with the DEK, changing the primary passphrase no longer requires re-encrypting the entire database. It now takes milliseconds by simply re-wrapping the DEK in the metadata envelope.
*   **Native Multi-Key Recovery:** The recovery passphrase now derives its own KEK to unwrap the exact same DEK, natively supporting multiple unlock methods without the legacy Fernet-based `.recovery_escrow.json` workaround.
*   **Cryptographic Hash Chaining:** Added `prev_hash` and `row_hash` columns to `sql_transaction_history`. Every audit log entry now includes a SHA-256 hash of its contents combined with the previous row's hash, creating an unbroken, tamper-evident chain.
*   **Automated Supply Chain Scanning:** Integrated `pip-audit` into the `TestRunnerManager`. The quarterly background `pytest` suite now automatically checks for known CVEs in third-party libraries and routes findings to the Notification Center.

### 🧪 Testing & Forensics
*   **Forensic Integrity Tests:** Added `test_forensic_integrity.py` to the quarterly deep audit. This test actively simulates a malicious actor using raw SQL to alter historical records, verifying that the cryptographic hash chain successfully detects the tampering and that the append-only voiding model functions flawlessly.
*   **Crypto Pipeline Tests:** Added `test_crypto_envelope_pipeline.py` to verify the Rust-backed DEK/KEK wrap/unwrap flow, instant passphrase rotation, and AES-GCM rejection of invalid keys.
*   **Test Suite Modernization:** Refactored legacy test suites (`test_workflows_and_backups`, `test_advanced_features`, `test_tax_report_export`) to use the new DEK hex architecture and append-only `edit_transaction`/`void_transaction` methods. Removed obsolete `rekey` and Fernet escrow tests.

### 🐛 Bug Fixes
*   **Tax Template Manager Isolation:** Fixed `TaxTemplateManager` ignoring the `templates_dir` argument, ensuring proper isolated testing and dynamic JSON template loading without overwriting user data.
*   **Quarterly Deep Audit Integration:** Integrated the hash chain verification (`verify_audit_chain_integrity`) into the `DeepIntegrityChecker` to automatically detect any raw SQL tampering or external database manipulation.

[0.9.2] - Unreleased (The Immutable Ledger & Envelope Encryption)
This release represents a massive paradigm shift in how CogniFinance handles data integrity and cryptography, moving from standard CRUD operations to a professional, append-only accounting model.
🚨 Breaking Changes
Transitioned to DEK/KEK Envelope Encryption: Moved away from direct passphrase-based SQLCipher encryption. The database is now encrypted with a random 256-bit Data Encryption Key (DEK), which is wrapped by Key Encryption Keys (KEKs) derived from user passphrases via PBKDF2-HMAC-SHA256 (600,000 iterations).
Transitioned to Append-Only Immutable Ledger: Replaced standard UPDATE and DELETE SQL operations. Transactions are now "voided" via perfect mathematical reversing entries, and "edited" by voiding the original and posting a new one.
Automated Legacy Migration: Added a seamless migration routine in main.py that detects older passphrase-direct databases, securely exports them to a new DEK-encrypted file, and generates the metadata envelope on the user's next launch.
🔒 Security & Cryptography
Instant Passphrase Rotation: Because the DB is encrypted with the DEK, changing the primary passphrase no longer requires re-encrypting the entire database. It now takes milliseconds by simply re-wrapping the DEK in the metadata envelope.
Native Multi-Key Recovery: The recovery passphrase now derives its own KEK to unwrap the exact same DEK, natively supporting multiple unlock methods without the legacy Fernet-based .recovery_escrow.json workaround.
Cryptographic Hash Chaining: Added prev_hash and row_hash columns to sql_transaction_history. Every audit log entry now includes a SHA-256 hash of its contents combined with the previous row's hash, creating an unbroken, tamper-evident chain.
Automated Supply Chain Scanning: Integrated pip-audit into the TestRunnerManager. The quarterly background pytest suite now automatically checks for known CVEs in third-party libraries and routes findings to the Notification Center.
Quarterly Deep Audit Integration: Integrated the hash chain verification (verify_audit_chain_integrity) into the DeepIntegrityChecker to automatically detect any raw SQL tampering or external database manipulation.
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
