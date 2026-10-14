Project Overview
CogniFinance is a professional-grade, personal finance management (PFM) application. It is built with a focus on security, mathematical integrity, and privacy, utilizing a true double-entry accounting engine.

Key Architectural Pillars
Core Accounting Engine (ledger.py):
A strict double-entry system where every transaction must balance.
Uses SQLCipher for full-database encryption.
Maintains a cryptographic transaction history to provide an immutable, auditable trail.
Security & Encapsulation (crypto_envelope.py, crypto_engine.py):
Implements an "Envelope Encryption" model where the database is keyed by a primary passphrase, which in turn is protected by a recovery key.
Supports OS keyring integration for authorized convenience.
Automated Integrity Auditing (integrity_checker.py, deep_integrity_checker.py):
Performs continuous, background mathematical checks to ensure all accounts and totals are consistent.
Includes automated supply-chain auditing to check for vulnerabilities in software dependencies.
Modern Presentation (ui_main.py):
A sophisticated PyQt6 desktop interface with sidebar navigation, reactive data updates, and support for both Light and Dark themes.
Provides specialized tabs for Budgets, Investments (including Crypto), Tax Preparation, and Reconciliation.
Core Component Map
main.py: Entry point; manages secure startup, lockout mechanisms, and app initialization.
ui_main.py: The central orchestration hub (BudgetApp) for the entire user interface.
ledger.py: The single source of truth for all financial data, account hierarchies, and business rules.
backup_manager.py: Handles automated, encrypted database backups and restoration.
tax_manager.py: Manages tax-related data and profile templates.
index.json: A structural metadata file used to map every defined function and class to its usage across the codebase.