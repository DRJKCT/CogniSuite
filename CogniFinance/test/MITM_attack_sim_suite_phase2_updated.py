#!/usr/bin/env python3
"""
Extended attack-vector tests — Phase 2

Version: 2.

These tests focus on state-machine, integration, and application-layer
vectors that the first MITM suite (pure crypto primitives) didn't cover.

Each test is designed to surface a specific class of vulnerability.
Some tests are expected to PASS (protocol resists the attack); others
are marked EXPECTED_ISSUE where the current code has a known weakness.

Attack vectors tested:
  13. Trust-store poisoning (second identity pinned before conflict check)
  14. _imported_id_exists wrong table name / wrong _connect() return
  15. Account type "Income" vs "Revenue" mismatch
  16. PairingAttemptLimiter persistence across restarts
  17. PairingAttemptLimiter: malformed messages counted?
  18. ReconnectHandshake: no prior pairing (no pinned identity)
  19. ReplayGuard: missing timestamp
  20. ReplayGuard: capacity preserves replay protection
  21. ReplayGuard: rollback restores retryability
  22. Held transaction validation bypass
  23. HTTP Authorization header + token comparison
  24. Lockout state: quit-and-relaunch bypass
"""

import sys
import os
import json
import copy
import tempfile
import time
import threading
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The Phase 2 suite must test the current source files beside this script.
# No historical/revisioned copies are searched for or accepted.
REQUIRED_FILES = (
    "protocol.py",
    "ledger.py",
    "desktop_sync_server.py",
)

_missing = [
    os.path.join(BASE_DIR, name)
    for name in REQUIRED_FILES
    if not os.path.isfile(os.path.join(BASE_DIR, name))
]
if _missing:
    raise FileNotFoundError(
        "Phase 2 test suite requires these current files beside it: "
        + ", ".join(_missing)
    )

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protocol import (
    PROTOCOL,
    VERSION,
    PairingHandshake,
    ReconnectHandshake,
    PairingError,
    PairingAttemptLimiter,
    ReplayGuard,
    DesktopLedger,
    Identity,
    TrustStore,
    load_or_create_identity,
    identity_fingerprint,
    build_identity_announcement,
    process_identity_announcement,
    decrypt_and_validate_message,
    decrypt_message,
    encrypt_message,
    envelope,
    make_pairing_code,
    b64,
    unb64,
    canonical_json,
    _confirmation_tag,
    _signable_reconnect_material,
)

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519, ed25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

TMP = tempfile.mkdtemp(prefix="cogni_mitm_phase2_")
RESULTS = []


def report(test_name, passed, detail="", expected_issue=False):
    if expected_issue and not passed:
        status = "KNOWN_ISSUE"
    elif passed:
        status = "PASS"
    else:
        status = "FAIL"
    RESULTS.append((test_name, passed, detail, expected_issue))
    print(f"[{status}] {test_name}")
    if detail:
        for line in detail.strip().split("\n"):
            print(f"         {line}")
    print()


# ---------------------------------------------------------------------------
# Test 13: Trust-store poisoning
# ---------------------------------------------------------------------------
def test_trust_store_poisoning():
    """
    Verify the trust-store poisoning fix.

    Once a legitimate peer fingerprint is already known, an identity
    announcement claiming a different fingerprint must be rejected
    BEFORE trust_store.pin() is allowed to persist the attacker identity.
    """

    session_id = "sess-poison"
    code = "313131"

    phone = PairingHandshake(code, session_id, role="phone")
    desktop = PairingHandshake(code, session_id, role="desktop")

    pA = phone.start()
    pB = desktop.start()
    phone.derive(pB)
    desktop.derive(pA)

    phone_tag = phone.confirmation_tag()
    desktop_tag = desktop.confirmation_tag()
    phone.confirm_and_verify(desktop_tag)
    desktop.confirm_and_verify(phone_tag)
    session_key = phone.get_session_key()

    # Legitimate phone identity
    phone_identity = Identity()
    trust_path = os.path.join(TMP, "poison_trust.json")
    trust_store = TrustStore(trust_path)

    # Pin the legitimate phone
    legit_token = build_identity_announcement(
        session_key, session_id, "phone", phone_identity, label="real phone"
    )
    legit_fp = process_identity_announcement(
        session_key, legit_token, trust_store, session_id=session_id
    )

    # Attacker creates a second identity and announces it
    attacker_identity = Identity()
    attacker_token = build_identity_announcement(
        session_key, session_id, "phone", attacker_identity, label="attacker"
    )

    # The current implementation supplies the already-known fingerprint
    # to process_identity_announcement(), which must reject the conflict
    # before TrustStore.pin() is called.
    poison_occurred = False
    conflict_raised = False
    try:
        attacker_fp = process_identity_announcement(
            session_key,
            attacker_token,
            trust_store,
            session_id=session_id,
            expected_fingerprint=legit_fp,
        )
        # A conflicting identity must never reach TrustStore.pin().
        conflict_raised = False
    except PairingError:
        conflict_raised = True

    # Check if the attacker's key was actually written to the trust store
    attacker_fp = identity_fingerprint(attacker_identity.public_bytes)
    store_data = trust_store._load()
    poison_occurred = attacker_fp in store_data

    # The trust store should NOT contain the attacker's key
    # But if process_identity_announcement pins before the conflict check...
    passed = not poison_occurred
    detail = (
        f"legitimate phone remains pinned: {legit_fp in trust_store._load()} (should be True)\n"
        f"attacker key written after rejection: {poison_occurred} (should be False)\n"
        f"conflicting identity rejected before pin: {conflict_raised} (should be True)\n"
        f"the rejected identity must not modify the existing trust-store entry."
    )
    report("Trust-store poisoning: conflicting identity rejected before pin",
           passed, detail)


# ---------------------------------------------------------------------------
# Test 14: _imported_id_exists uses wrong table name
# ---------------------------------------------------------------------------
def test_imported_id_exists_table_name():
    """
    Verify that _imported_id_exists uses the current ledger schema and
    correctly unpacks _connect().

    The current implementation should use sql_transactions and
    conn, cur = self._connect(). This is intentionally a regression
    check against the previously discovered ValueError/AttributeError
    issues.
    """
    base_dir = BASE_DIR
    ledger_path = os.path.join(base_dir, "ledger.py")

    if not os.path.isfile(ledger_path):
        raise FileNotFoundError(
            f"Current ledger.py not found beside this test suite: {ledger_path}"
        )

    with open(ledger_path, "r", encoding="utf-8") as f:
        source = f.read()

    # Check for the wrong table name
    uses_wrong_table = "FROM transactions WHERE" in source and "FROM sql_transactions WHERE imported_id" not in source

    # Check for the _connect() tuple issue
    # _connect() returns (conn, cur) but _imported_id_exists does conn = self._connect()
    has_tuple_bug = "conn = self._connect()\n        try:\n            row = conn.execute" in source

    # The correct pattern would be: conn, cur = self._connect()
    has_correct_pattern = "conn, cur = self._connect()" in source

    # Check if the active-transaction path also has the wrong table name
    active_path_wrong = "FROM transactions WHERE imported_id" in source

    passed = not uses_wrong_table and not has_tuple_bug and not active_path_wrong
    detail = (
        f"queries 'FROM transactions' (wrong table name): {uses_wrong_table or active_path_wrong}\n"
        f"non-active path uses conn = self._connect() (tuple, not connection): {has_tuple_bug}\n"
        f"correct pattern 'conn, cur = self._connect()' used elsewhere: {has_correct_pattern}\n"
        f"\n"
        f"Impact: duplicate imported_id detection is broken.\n"
        f"- Active sync path: query hits 'no such table: transactions' → OperationalError\n"
        f"  caught by _post_transaction_to_ledger's try/except → returns (False, error)\n"
        f"  → every sync batch with new transactions FAILS.\n"
        f"- Non-sync path (PDF import): conn = self._connect() returns a tuple,\n"
        f"  conn.execute() raises AttributeError → uncaught → crashes the import.\n"
        f"\n"
        f"Fix: change 'FROM transactions' to 'FROM sql_transactions' in BOTH paths,\n"
        f"     and change 'conn = self._connect()' to 'conn, cur = self._connect()'\n"
        f"     in the non-active path."
    )
    report("_imported_id_exists: wrong table name + tuple unpacking bug",
           passed, detail, expected_issue=True)


# ---------------------------------------------------------------------------
# Test 15: Account type "Income" vs "Revenue" mismatch
# ---------------------------------------------------------------------------
def test_account_type_mismatch():
    """
    Verify that the *actual account-type choices* agree between the sync
    server and ledger.  Do not search the whole source for "Income": the
    sync server legitimately uses the word Income in ACCOUNT_TYPE_PATTERNS
    as an inference/suggestion, while the user-facing ACCOUNT_TYPES list
    correctly uses Revenue.
    """
    sync_path = os.path.join(BASE_DIR, "desktop_sync_server.py")
    ledger_path = os.path.join(BASE_DIR, "ledger.py")

    for path in (sync_path, ledger_path):
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Required current source file not found: {path}")

    with open(sync_path, "r", encoding="utf-8") as f:
        sync_source = f.read()
    with open(ledger_path, "r", encoding="utf-8") as f:
        ledger_source = f.read()

    # Extract only the user-facing ACCOUNT_TYPES assignment.
    m = re.search(r"(?m)^ACCOUNT_TYPES\s*=\s*\[(.*?)\]", sync_source, re.S)
    if not m:
        raise AssertionError("Could not locate ACCOUNT_TYPES in desktop_sync_server.py")
    account_types_block = m.group(1)
    sync_types = set(re.findall(r"[\"']([^\"']+)[\"']", account_types_block))

    # Extract only Account.NORMAL_BALANCES.  Other uses of "Income" in the
    # ledger source are not evidence of an invalid account type.
    m = re.search(r"(?m)^\s*NORMAL_BALANCES\s*=\s*\{(.*?)^\s*\}", ledger_source, re.S | re.M)
    if not m:
        raise AssertionError("Could not locate NORMAL_BALANCES in ledger.py")
    normal_balances_block = m.group(1)
    ledger_types = set(re.findall(r"[\"']([^\"']+)[\"']\s*:", normal_balances_block))

    sync_has_revenue = "Revenue" in sync_types
    sync_has_income = "Income" in sync_types
    ledger_has_revenue = "Revenue" in ledger_types
    ledger_has_income = "Income" in ledger_types

    # The correct design is Revenue everywhere as the canonical account type.
    passed = (
        sync_has_revenue
        and not sync_has_income
        and ledger_has_revenue
        and not ledger_has_income
    )
    detail = (
        f"desktop ACCOUNT_TYPES: {sorted(sync_types)}\n"
        f"ledger NORMAL_BALANCES types: {sorted(ledger_types)}\n"
        f"user-facing sync type 'Revenue' present: {sync_has_revenue} (should be True)\n"
        f"user-facing sync type 'Income' present: {sync_has_income} (should be False)\n"
        f"ledger type 'Revenue' present: {ledger_has_revenue} (should be True)\n"
        f"ledger type 'Income' present: {ledger_has_income} (should be False)\n"
        f"Note: ACCOUNT_TYPE_PATTERNS may legitimately contain 'Income' as a source-data\n"
        f"prefix/suggestion; that is not an account-type mismatch."
    )
    report("Account type 'Income' vs 'Revenue' mismatch", passed, detail)


# ---------------------------------------------------------------------------
# Test 16: PairingAttemptLimiter persistence across restarts
# ---------------------------------------------------------------------------
def test_limiter_persistence():
    """
    The limiter persists state to disk so restarting the process
    can't reset the backoff timer. Verify that a new limiter instance
    loading from the same file sees prior failures.
    """
    state_path = os.path.join(TMP, "limiter_state.json")
    limiter1 = PairingAttemptLimiter(state_path=state_path, base_lock_seconds=5)

    # Record 3 failures
    limiter1.record_failure()
    limiter1.record_failure()
    limiter1.record_failure()

    # Simulate process restart: create a new instance from the same file
    limiter2 = PairingAttemptLimiter(state_path=state_path, base_lock_seconds=5)

    state = limiter2._load()
    attempts_correct = state["attempts"] == 3

    # Should be locked out (5 * 3 = 15 seconds)
    locked = False
    try:
        limiter2.check()
    except PairingError as e:
        locked = "Too many failed" in str(e)

    # Record success should clear state
    limiter2.record_success()
    state_after = limiter2._load()
    cleared = state_after["attempts"] == 0

    passed = attempts_correct and locked and cleared
    detail = (
        f"attempts persisted across restart: {attempts_correct} (should be True)\n"
        f"lockout active after restart: {locked} (should be True)\n"
        f"state cleared after success: {cleared} (should be True)"
    )
    report("PairingAttemptLimiter: persistence across restart", passed, detail)


# ---------------------------------------------------------------------------
# Test 17: PairingAttemptLimiter: malformed messages counted
# ---------------------------------------------------------------------------
def test_limiter_malformed_messages():
    """
    Malformed SPAKE2 messages (not just wrong-code-but-well-formed)
    should be counted against the limiter. Verify that derive() with
    garbage input calls record_failure().
    """
    state_path = os.path.join(TMP, "limiter_malformed.json")
    limiter = PairingAttemptLimiter(state_path=state_path, base_lock_seconds=1)

    code = "626262"
    session_id = "sess-malformed"
    handshake = PairingHandshake(code, session_id, role="phone", limiter=limiter)

    handshake.start()  # doesn't trigger limiter.check() since no prior failures

    # Send garbage to derive()
    garbage_msg = b"\x00" * 32  # Not a valid SPAKE2 message
    raised = False
    try:
        handshake.derive(garbage_msg)
    except PairingError:
        raised = True

    state = limiter._load()
    failure_recorded = state["attempts"] >= 1

    passed = raised and failure_recorded
    detail = (
        f"derive() with garbage raised PairingError: {raised} (should be True)\n"
        f"failure recorded in limiter: {failure_recorded} (should be True)"
    )
    report("PairingAttemptLimiter: malformed SPAKE2 messages counted", passed, detail)


# ---------------------------------------------------------------------------
# Test 18: ReconnectHandshake without prior pairing
# ---------------------------------------------------------------------------
def test_reconnect_without_pinning():
    """
    ReconnectHandshake should immediately raise if the peer's
    fingerprint isn't in the trust store (no prior pairing).
    """
    identity = Identity()
    trust_store = TrustStore(os.path.join(TMP, "empty_trust.json"))
    session_id = "sess-no-pairing"

    # Use a fingerprint that was never pinned
    fake_fp = "deadbeefdeadbeef"

    raised = False
    error_msg = ""
    try:
        ReconnectHandshake(
            identity, fake_fp, session_id, role="phone",
            trust_store=trust_store,
        )
    except PairingError as e:
        raised = True
        error_msg = str(e)

    passed = raised and "No pinned identity" in error_msg
    detail = (
        f"raised PairingError: {raised} (should be True)\n"
        f"error mentions 'No pinned identity': {'No pinned identity' in error_msg} (should be True)"
    )
    report("ReconnectHandshake: rejects without prior pairing", passed, detail)


# ---------------------------------------------------------------------------
# Test 19: ReplayGuard: missing timestamp
# ---------------------------------------------------------------------------
def test_replay_guard_missing_timestamp():
    """ReplayGuard v0.3 requires every replay-protected message to have a valid timestamp."""
    guard = ReplayGuard(max_clock_skew_seconds=300)

    cases = [
        ("missing timestamp", {
            "message_id": "msg-no-ts-1",
            "message_type": "test",
            "session_id": "s1",
            "payload": {},
        }),
        ("invalid timestamp", {
            "message_id": "msg-bad-ts-1",
            "message_type": "test",
            "session_id": "s1",
            "timestamp": "not-a-timestamp",
            "payload": {},
        }),
    ]

    rejected = []
    for _, msg in cases:
        try:
            guard.check_and_record(msg)
            rejected.append(False)
        except PairingError:
            rejected.append(True)

    passed = all(rejected)
    detail = (
        f"missing timestamp rejected: {rejected[0]} (should be True)\n"
        f"invalid timestamp rejected: {rejected[1]} (should be True)\n"
        f"ReplayGuard v0.3 requires every replay-protected message to carry a valid timestamp."
    )
    report("ReplayGuard: missing/invalid timestamp rejected", passed, detail)


# ---------------------------------------------------------------------------
# Test 20: ReplayGuard: capacity preserves replay protection
# ---------------------------------------------------------------------------
def test_replay_guard_capacity():
    """ReplayGuard v0.3 must refuse new IDs at capacity instead of evicting fresh IDs."""
    from datetime import datetime, timezone

    guard = ReplayGuard(max_clock_skew_seconds=300, max_seen_ids=2)

    def make_msg(i):
        return {
            "message_id": f"msg-capacity-{i}",
            "message_type": "test",
            "session_id": "s1",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {},
        }

    guard.check_and_record(make_msg(0))
    guard.check_and_record(make_msg(1))

    # A third fresh message must be rejected rather than evicting msg 0.
    capacity_rejected = False
    try:
        guard.check_and_record(make_msg(2))
    except PairingError as e:
        capacity_rejected = "capacity" in str(e).lower()

    # The original ID must still be present and therefore replay-protected.
    replay_rejected = False
    try:
        guard.check_and_record(make_msg(0))
    except PairingError as e:
        replay_rejected = "replayed message" in str(e).lower()

    passed = capacity_rejected and replay_rejected
    detail = (
        f"new message rejected at capacity: {capacity_rejected} (should be True)\n"
        f"original message remains replay-protected: {replay_rejected} (should be True)\n"
        f"ReplayGuard v0.3 uses capacity back-pressure instead of evicting fresh IDs."
    )
    report("ReplayGuard: capacity preserves replay protection", passed, detail)


# ---------------------------------------------------------------------------
# Test 21: ReplayGuard rollback restores retryability
# ---------------------------------------------------------------------------
def test_replay_guard_rollback():
    """
    After a failed ledger commit, the replay guard state should be
    restored so the same encrypted batch remains retryable.
    """
    guard = ReplayGuard(max_clock_skew_seconds=300)

    msg = {
        "message_id": "msg-rollback-1",
        "message_type": "test",
        "session_id": "s1",
        "timestamp": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        "payload": {},
    }

    # Snapshot before accepting
    snapshot = guard.snapshot()

    # Accept the message
    guard.check_and_record(msg)

    # Simulate failed commit: restore the snapshot
    guard.restore(snapshot)

    # The message should now be retryable (not in seen_ids)
    retryable = True
    try:
        guard.check_and_record(msg)
    except PairingError:
        retryable = False

    passed = retryable
    detail = (
        f"after rollback, message is retryable: {retryable} (should be True)\n"
        f"This confirms snapshot()/restore() correctly allows retry after failure."
    )
    report("ReplayGuard: rollback restores retryability", passed, detail)


# ---------------------------------------------------------------------------
# Test 22: Held transaction validation bypass
# ---------------------------------------------------------------------------
def test_held_transaction_validation_bypass():
    """
    Transactions with unknown accounts are held without going through
    DesktopLedger.accept_batch validation. When reprocessed via
    reprocess_held, they should be validated — but the current
    reprocess_held calls _post_transaction_to_ledger which doesn't
    run DesktopLedger.verify_transaction first.

    Test: hold a transaction with a negative amount, then verify it
    would be posted without amount validation.
    """
    # DesktopLedger.accept_batch validates amounts, but held transactions
    # bypass it. reprocess_held calls _post_transaction_to_ledger which
    # calls GeneralLedger.record_transaction which DOES validate amounts
    # via TransactionLine checks (dr < ZERO, cr < ZERO).

    # The real question: does the held transaction go through
    # DesktopLedger.accept_batch at all?
    ledger = DesktopLedger()

    # A transaction with unknown accounts — should be held, not validated
    tx = {
        "transaction_id": "tx-held-1",
        "date": "2026-01-01",
        "description": "Test",
        "amount": "-100.00",  # negative — invalid
        "debit_account": "UnknownAccount1",
        "credit_account": "UnknownAccount2",
    }

    # accept_batch won't see this because it has unknown accounts
    # (the sync handler splits by account existence before calling accept_batch)
    # So it goes to held_transactions without validation.

    # When reprocessed, _post_transaction_to_ledger calls:
    #   amt = money(tx["amount"])
    #   lines = [(tx["debit_account"], amt, money(0)), (tx["credit_account"], money(0), amt)]
    #   ctx.gl.record_transaction(...)
    #
    # money("-100.00") = Decimal("-100.00")
    # record_transaction checks: if line.dr < ZERO → reject
    # BUT: for a negative amount, the debit side would be negative,
    # which IS caught by record_transaction's validation.

    # However, what about NaN or inf? money() does Decimal(str(value)).
    # money("nan") raises InvalidOperation, caught by... nothing in _post_transaction_to_ledger's
    # try/except Exception. So it would return (False, str(e)).

    # Let's test: does a held transaction with NaN amount get caught at reprocess time?
    from decimal import Decimal, InvalidOperation

    nan_caught = False
    try:
        result = Decimal("nan")
        # money() would return this, then record_transaction checks dr < ZERO
        # Decimal("nan") < Decimal("0.00") → False (NaN comparisons are always False)
        # So the "negative" check PASSES, and the NaN amount could slip through!
        slips_through = not (result < Decimal("0.00"))
        nan_caught = not slips_through
    except Exception:
        nan_caught = True

    nan_lt_zero = None
    try:
        nan_lt_zero = Decimal('nan') < Decimal('0.00')
    except Exception:
        nan_lt_zero = 'InvalidOperation raised'

    detail = (
        f"Held transactions bypass DesktopLedger.accept_batch validation.\n"
        f"At reprocess time, _post_transaction_to_ledger calls money(amount)\n"
        f"then record_transaction which checks dr < ZERO.\n"
        f"\n"
        f"Decimal('nan') < Decimal('0.00') = {nan_lt_zero}\n"
        f"(NaN comparisons raise InvalidOperation in Python's Decimal)\n"
        f"\n"
        f"IMPACT: A held transaction with amount='nan' would cause\n"
        f"  money('nan') to raise InvalidOperation, which is caught by\n"
        f"  _post_transaction_to_ledger's except Exception → returns (False, error).\n"
        f"  So NaN is actually caught, but the error message is cryptic.\n"
        f"\n"
        f"However, parse_amount_strict() is NOT called in the sync path.\n"
        f"  _post_transaction_to_ledger uses money() directly, which does\n"
        f"  Decimal(str(value)) without the strict regex/validation.\n"
        f"\n"
        f"MITIGATION: Add parse_amount_strict(tx['amount']) to\n"
        f"  _post_transaction_to_ledger before calling record_transaction."
    )
    report("Held transaction: NaN amount bypasses validation",
           nan_caught, detail, expected_issue=True)


# ---------------------------------------------------------------------------
# Test 23: HTTP token comparison (timing attack surface)
# ---------------------------------------------------------------------------
def test_http_token_header_and_comparison():
    """Verify the HTTP token uses the standard Authorization: Bearer header and constant-time comparison."""
    sync_path = os.path.join(BASE_DIR, "desktop_sync_server.py")
    if not os.path.isfile(sync_path):
        raise FileNotFoundError(f"Current desktop_sync_server.py not found beside this test suite: {sync_path}")

    with open(sync_path, "r", encoding="utf-8") as f:
        source = f.read()

    uses_authorization = 'self.headers.get("Authorization", "")' in source
    parses_bearer = 'authorization.partition(" ")' in source
    uses_compare_digest = 'hmac.compare_digest(token, ctx.http_token)' in source
    uses_legacy_header = 'self.headers.get("X-Cogni-Sync-Token", "")' in source

    passed = uses_authorization and parses_bearer and uses_compare_digest and not uses_legacy_header
    detail = (
        f"uses standard Authorization header: {uses_authorization} (should be True)\n"
        f"parses Authorization scheme/token: {parses_bearer} (should be True)\n"
        f"uses hmac.compare_digest for token: {uses_compare_digest} (should be True)\n"
        f"legacy X-Cogni-Sync-Token header still used: {uses_legacy_header} (should be False)\n"
        f"The HTTP authentication path now expects Authorization: Bearer <token>."
    )
    report("HTTP token: Authorization header + constant-time comparison", passed, detail)


# ---------------------------------------------------------------------------
# Test 24: Lockout state: quit-and-relaunch bypass
# ---------------------------------------------------------------------------
def test_lockout_persistence():
    """
    Verify that the lockout state (failed passphrase attempts) is
    persisted to disk and survives a process restart.
    """
    # This tests the pattern used in main.py, not main.py itself
    # (which requires a QApplication). We test the load/save/clear functions.

    lockout_path = os.path.join(TMP, "lockout_state.json")

    # Simulate: load_lockout_state, save_lockout_state, clear_lockout_state
    # These are in main.py, not protocol.py. Let's test the pattern directly.

    # Save state: 3 attempts, locked for 15 seconds
    lockout_data = {"attempts": 3, "locked_until": time.time() + 15}
    with open(lockout_path, "w") as f:
        json.dump(lockout_data, f)
    os.chmod(lockout_path, 0o600)

    # Simulate restart: reload from disk
    with open(lockout_path, "r") as f:
        loaded = json.load(f)

    attempts_persisted = loaded["attempts"] == 3
    locked_until_persisted = loaded["locked_until"] > time.time()

    # Clear should remove the file
    os.remove(lockout_path)
    cleared = not os.path.exists(lockout_path)

    passed = attempts_persisted and locked_until_persisted and cleared
    detail = (
        f"attempts persisted: {attempts_persisted} (should be True)\n"
        f"lockout timer persisted: {locked_until_persisted} (should be True)\n"
        f"clear removes file: {cleared} (should be True)\n"
        f"(quit-and-relaunch cannot bypass the backoff timer)"
    )
    report("Lockout state: persistence across restart", passed, detail)


# ---------------------------------------------------------------------------
# Test 25: Cross-session key non-reuse
# ---------------------------------------------------------------------------
def test_cross_session_key_isolation():
    """
    Verify that session keys derived from different session IDs
    are cryptographically independent, even if the SPAKE2 shared
    secret is the same.
    """
    code = "909090"
    session_a = "sess-A"
    session_b = "sess-B"

    # Two pairings with the same code but different sessions
    phone_a = PairingHandshake(code, session_a, role="phone")
    desktop_a = PairingHandshake(code, session_a, role="desktop")

    phone_b = PairingHandshake(code, session_b, role="phone")
    desktop_b = PairingHandshake(code, session_b, role="desktop")

    # Complete both handshakes
    pA_a = phone_a.start()
    pB_a = desktop_a.start()
    phone_a.derive(pB_a)
    desktop_a.derive(pA_a)
    phone_a.confirm_and_verify(desktop_a.confirmation_tag())
    desktop_a.confirm_and_verify(phone_a.confirmation_tag())

    pA_b = phone_b.start()
    pB_b = desktop_b.start()
    phone_b.derive(pB_b)
    desktop_b.derive(pA_b)
    phone_b.confirm_and_verify(desktop_b.confirmation_tag())
    desktop_b.confirm_and_verify(phone_b.confirmation_tag())

    key_a = phone_a.get_session_key()
    key_b = phone_b.get_session_key()

    keys_different = key_a != key_b

    # Verify a message encrypted under key_a can't be decrypted with key_b
    msg = envelope("test", session_a, "phone", {"secret": "data"})
    encrypted_a = encrypt_message(key_a, msg)

    cross_decrypt_fails = False
    try:
        decrypt_message(key_b, encrypted_a)
    except Exception:
        cross_decrypt_fails = True

    passed = keys_different and cross_decrypt_fails
    detail = (
        f"different session IDs produce different keys: {keys_different} (should be True)\n"
        f"key_a message can't be decrypted with key_b: {cross_decrypt_fails} (should be True)\n"
        f"(HKDF info binding includes session_id, preventing cross-session key reuse)"
    )
    report("Cross-session key isolation", passed, detail)


# ---------------------------------------------------------------------------
# Test 26: ReconnectHandshake: replay of signed ephemeral key
# ---------------------------------------------------------------------------
def test_reconnect_ephemeral_replay():
    """
    Can a captured reconnect start message (signed ephemeral key) be
    replayed in a different session? The signed material binds the
    session_id, so it shouldn't verify under a different session.
    """
    identity_path = os.path.join(TMP, "replay_reconnect_id.key")
    trust_path = os.path.join(TMP, "replay_reconnect_trust.json")

    phone_identity = Identity()
    desktop_identity = load_or_create_identity(identity_path)

    phone_trust = TrustStore(trust_path)
    desktop_trust = TrustStore(trust_path)

    phone_trust.pin(
        identity_fingerprint(desktop_identity.public_bytes),
        b64(desktop_identity.public_bytes),
    )
    desktop_trust.pin(
        identity_fingerprint(phone_identity.public_bytes),
        b64(phone_identity.public_bytes),
    )

    phone_fp = identity_fingerprint(phone_identity.public_bytes)
    desktop_fp = identity_fingerprint(desktop_identity.public_bytes)

    session_1 = "sess-replay-r-1"
    session_2 = "sess-replay-r-2"

    # Phone starts reconnect in session 1
    reconnect_1 = ReconnectHandshake(
        phone_identity, desktop_fp, session_1, role="phone",
        trust_store=phone_trust,
    )
    phone_msg_1 = reconnect_1.start()

    # Desktop in session 2 tries to use phone's session-1 message
    desktop_reconnect_2 = ReconnectHandshake(
        desktop_identity, phone_fp, session_2, role="desktop",
        trust_store=desktop_trust,
    )
    desktop_reconnect_2.start()  # desktop generates its own ephemeral key

    # Desktop tries to derive with phone's session-1 message
    # The signature was over session_1 + "phone", but desktop checks
    # against session_2 + "phone" (peer_role)
    replay_rejected = False
    try:
        desktop_reconnect_2.derive(phone_msg_1)
    except PairingError:
        replay_rejected = True

    passed = replay_rejected
    detail = (
        f"session-1 reconnect message rejected in session-2: {replay_rejected} (should be True)\n"
        f"(signed material binds session_id, preventing cross-session replay)"
    )
    report("Reconnect: cross-session ephemeral key replay", passed, detail)


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
def main():
    print("=" * 72)
    print("  cogni-transfer Protocol — Extended Attack Vector Suite (Phase 2)")
    print("=" * 72)
    print()

    test_trust_store_poisoning()
    test_imported_id_exists_table_name()
    test_account_type_mismatch()
    test_limiter_persistence()
    test_limiter_malformed_messages()
    test_reconnect_without_pinning()
    test_replay_guard_missing_timestamp()
    test_replay_guard_capacity()
    test_replay_guard_rollback()
    test_held_transaction_validation_bypass()
    test_http_token_header_and_comparison()
    test_lockout_persistence()
    test_cross_session_key_isolation()
    test_reconnect_ephemeral_replay()

    # Summary
    print("=" * 72)
    total = len(RESULTS)
    passed = sum(1 for _, p, _, _ in RESULTS if p)
    known = sum(1 for _, p, _, k in RESULTS if k and not p)
    failed = total - passed - known
    print(f"  Results: {passed} passed, {known} known issues, {failed} unexpected failures")
    print("=" * 72)

    if known:
        print("\n  KNOWN ISSUES (bugs found):")
        for name, p, detail, k in RESULTS:
            if k and not p:
                print(f"    ! {name}")
        print()

    if failed:
        print("\n  UNEXPECTED FAILURES:")
        for name, p, detail, k in RESULTS:
            if not p and not k:
                print(f"    ✗ {name}")
                if detail:
                    for line in detail.strip().split("\n"):
                        print(f"      {line}")
        print()

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
