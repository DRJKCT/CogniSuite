"""
Tests for the bug fixes made after the initial protocol review:

1. Decimal-based amount validation (was float(), which silently accepted
   NaN/Infinity/scientific notation as valid transaction amounts)
2. Retry-conflict detection (a resent transaction ID with different
   content used to be silently discarded with no signal to the caller)
3. Malformed input to derive() on both handshake classes used to raise
   raw library exceptions AND bypass the rate limiter entirely
4. process_identity_announcement used to never check message_type at all
5. Replay/session-id validation via decrypt_and_validate_message
6. HKDF-based session key derivation (was a bare SHA256 concatenation)
"""
import os
import shutil
import tempfile
import unittest
import uuid

from protocol import (
    DesktopLedger,
    parse_amount_strict,
    PairingHandshake,
    ReconnectHandshake,
    TrustStore,
    Identity,
    identity_fingerprint,
    PairingAttemptLimiter,
    PairingError,
    ReplayGuard,
    decrypt_and_validate_message,
    process_identity_announcement,
    encrypt_message,
    envelope,
    derive_session_key,
    b64,
)


class TestParseAmountStrict(unittest.TestCase):
    def test_valid_plain_amounts(self):
        self.assertEqual(parse_amount_strict("5.00"), parse_amount_strict("5.00"))
        parse_amount_strict("5")       # should not raise
        parse_amount_strict("1234.56")  # should not raise

    def test_nan_rejected(self):
        with self.assertRaises(ValueError):
            parse_amount_strict("nan")

    def test_infinity_rejected(self):
        with self.assertRaises(ValueError):
            parse_amount_strict("inf")
        with self.assertRaises(ValueError):
            parse_amount_strict("-inf")

    def test_scientific_notation_rejected(self):
        with self.assertRaises(ValueError):
            parse_amount_strict("1e10")

    def test_excess_precision_rejected(self):
        with self.assertRaises(ValueError):
            parse_amount_strict("10.999999999999")

    def test_zero_and_negative_rejected(self):
        with self.assertRaises(ValueError):
            parse_amount_strict("0")
        with self.assertRaises(ValueError):
            parse_amount_strict("0.00")
        with self.assertRaises(ValueError):
            parse_amount_strict("-5.00")

    def test_non_string_rejected(self):
        with self.assertRaises(ValueError):
            parse_amount_strict(5.00)
        with self.assertRaises(ValueError):
            parse_amount_strict(None)

    def test_garbage_string_rejected(self):
        with self.assertRaises(ValueError):
            parse_amount_strict("not a number")
        with self.assertRaises(ValueError):
            parse_amount_strict("")


class TestVerifyTransactionAmounts(unittest.TestCase):
    def setUp(self):
        self.dl = DesktopLedger()

    def _tx(self, amount):
        return {"transaction_id": "tx-1", "date": "2026-08-08", "description": "x", "amount": amount, "debit_account": "A", "credit_account": "B"}

    def test_nan_rejected(self):
        ok, reason = self.dl.verify_transaction(self._tx("nan"))
        self.assertFalse(ok)

    def test_infinity_rejected(self):
        ok, reason = self.dl.verify_transaction(self._tx("inf"))
        self.assertFalse(ok)

    def test_scientific_notation_rejected(self):
        ok, reason = self.dl.verify_transaction(self._tx("1e10"))
        self.assertFalse(ok)

    def test_normal_amount_accepted(self):
        ok, reason = self.dl.verify_transaction(self._tx("54.23"))
        self.assertTrue(ok)

    def test_split_transaction_conflict_detection():
    """
    If a split transaction is resent with the SAME ID but DIFFERENT splits,
    the DesktopLedger must flag it as a conflict, not silently accept it
    as an identical retry.
    """
    ledger = DesktopLedger()
    tx1 = {
        "transaction_id": "tx-split-1",
        "date": "2026-01-01",
        "description": "Costco run",
        "lines": [
            {"account": "Groceries", "type": "DEBIT", "amount": "60.00"},
            {"account": "Gas", "type": "DEBIT", "amount": "40.00"},
            {"account": "Credit Card", "type": "CREDIT", "amount": "100.00"}
        ]
    }
    ledger.accept_batch([tx1])

    # Resend with DIFFERENT splits (e.g. $50/$50 instead of $60/$40)
    tx2 = copy.deepcopy(tx1)
    tx2["lines"][0]["amount"] = "50.00"
    tx2["lines"][1]["amount"] = "50.00"

    result = ledger.accept_batch([tx2])

    # It MUST be flagged as a conflict
    assert len(result["conflicted"]) == 1
    assert result["conflicted"][0]["transaction_id"] == "tx-split-1"
    # And the original $60/$40 split must be preserved
    assert ledger.accepted["tx-split-1"]["lines"][0]["amount"] == "60.00"


class TestAcceptBatchConflictDetection(unittest.TestCase):
    def setUp(self):
        self.dl = DesktopLedger()
        self.original = {"transaction_id": "tx-A", "date": "2026-08-08", "description": "Coffee", "amount": "5.00", "debit_account": "Groceries", "credit_account": "Checking"}

    def test_genuine_retry_with_identical_content_is_accepted(self):
        r1 = self.dl.accept_batch([self.original])
        r2 = self.dl.accept_batch([dict(self.original)])
        self.assertEqual(r1["accepted"], ["tx-A"])
        self.assertEqual(r2["accepted"], ["tx-A"])
        self.assertEqual(r2["conflicted"], [])

    def test_retry_with_equivalent_but_differently_formatted_amount_is_accepted(self):
        # "5.0" and "5.00" are the same amount, just formatted differently
        reformatted = dict(self.original, amount="5.0")
        self.dl.accept_batch([self.original])
        r2 = self.dl.accept_batch([reformatted])
        self.assertEqual(r2["accepted"], ["tx-A"])
        self.assertEqual(r2["conflicted"], [])

    def test_retry_with_different_amount_is_flagged_as_conflict_not_silently_accepted(self):
        tampered = dict(self.original, amount="50000.00")
        self.dl.accept_batch([self.original])
        r2 = self.dl.accept_batch([tampered])
        self.assertEqual(r2["accepted"], [])
        self.assertEqual(len(r2["conflicted"]), 1)
        self.assertEqual(r2["conflicted"][0]["transaction_id"], "tx-A")

    def test_original_content_is_preserved_on_conflict(self):
        tampered = dict(self.original, amount="50000.00")
        self.dl.accept_batch([self.original])
        self.dl.accept_batch([tampered])
        self.assertEqual(self.dl.accepted["tx-A"]["amount"], "5.00")

    def test_retry_with_different_description_is_a_conflict(self):
        different = dict(self.original, description="Something else entirely")
        self.dl.accept_batch([self.original])
        r2 = self.dl.accept_batch([different])
        self.assertEqual(len(r2["conflicted"]), 1)


class TestHandshakeMalformedInput(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cognibudget_malformed_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pairing_garbage_input_raises_clean_pairing_error(self):
        limiter = PairingAttemptLimiter(state_path=os.path.join(self.tmpdir, "lockout.json"), base_lock_seconds=5)
        h = PairingHandshake("123456", str(uuid.uuid4()), role="phone", limiter=limiter)
        h.start()
        with self.assertRaises(PairingError):
            h.derive(b"not a real spake2 message at all")

    def test_pairing_garbage_input_counts_against_limiter(self):
        state_path = os.path.join(self.tmpdir, "lockout.json")
        limiter = PairingAttemptLimiter(state_path=state_path, base_lock_seconds=5)
        h = PairingHandshake("123456", str(uuid.uuid4()), role="phone", limiter=limiter)
        h.start()
        try:
            h.derive(b"garbage")
        except PairingError:
            pass
        self.assertTrue(os.path.exists(state_path))
        h2 = PairingHandshake("123456", str(uuid.uuid4()), role="phone", limiter=limiter)
        with self.assertRaises(PairingError):
            h2.start()

    def test_reconnect_garbage_input_raises_clean_pairing_error(self):
        trust = TrustStore(path=os.path.join(self.tmpdir, "trust.json"))
        identity = Identity()
        fp = identity_fingerprint(identity.public_bytes)
        trust.pin(fp, b64(identity.public_bytes))

        r = ReconnectHandshake(Identity(), fp, str(uuid.uuid4()), role="phone", trust_store=trust)
        r.start()
        with self.assertRaises(PairingError):
            r.derive({"ephemeral_public": "not valid base64!!!", "signature": "also garbage"})

    def test_reconnect_missing_keys_raises_clean_pairing_error(self):
        trust = TrustStore(path=os.path.join(self.tmpdir, "trust.json"))
        identity = Identity()
        fp = identity_fingerprint(identity.public_bytes)
        trust.pin(fp, b64(identity.public_bytes))

        r = ReconnectHandshake(Identity(), fp, str(uuid.uuid4()), role="phone", trust_store=trust)
        r.start()
        with self.assertRaises(PairingError):
            r.derive({})

    def test_reconnect_garbage_input_counts_against_limiter(self):
        state_path = os.path.join(self.tmpdir, "lockout.json")
        trust = TrustStore(path=os.path.join(self.tmpdir, "trust.json"))
        identity = Identity()
        fp = identity_fingerprint(identity.public_bytes)
        trust.pin(fp, b64(identity.public_bytes))
        limiter = PairingAttemptLimiter(state_path=state_path, base_lock_seconds=5)

        r = ReconnectHandshake(Identity(), fp, str(uuid.uuid4()), role="phone", trust_store=trust, limiter=limiter)
        r.start()
        try:
            r.derive({"ephemeral_public": "garbage!!!", "signature": "garbage!!!"})
        except PairingError:
            pass
        self.assertTrue(os.path.exists(state_path))
        r2 = ReconnectHandshake(identity, fp, str(uuid.uuid4()), role="phone", trust_store=trust, limiter=limiter)
        with self.assertRaises(PairingError):
            r2.start()


class TestMessageValidation(unittest.TestCase):
    def test_wrong_message_type_rejected(self):
        key = os.urandom(32)
        session_id = str(uuid.uuid4())
        token = encrypt_message(key, envelope("ack", session_id, "desktop", {}))
        with self.assertRaises(PairingError):
            decrypt_and_validate_message(key, token, "transaction_batch", session_id)

    def test_wrong_session_id_rejected(self):
        key = os.urandom(32)
        token = encrypt_message(key, envelope("ack", str(uuid.uuid4()), "desktop", {}))
        with self.assertRaises(PairingError):
            decrypt_and_validate_message(key, token, "ack", "a-different-session-id")

    def test_matching_type_and_session_accepted(self):
        key = os.urandom(32)
        session_id = str(uuid.uuid4())
        token = encrypt_message(key, envelope("ack", session_id, "desktop", {"x": 1}))
        msg = decrypt_and_validate_message(key, token, "ack", session_id)
        self.assertEqual(msg["payload"]["x"], 1)

    def test_identity_announcement_rejects_wrong_message_type(self):
        session_key = os.urandom(32)
        session_id = str(uuid.uuid4())
        fake_identity = Identity()
        sneaky_payload = {
            "public_key": b64(fake_identity.public_bytes),
            "fingerprint": identity_fingerprint(fake_identity.public_bytes),
            "label": "sneaky",
        }
        token = encrypt_message(session_key, envelope("transaction_batch", session_id, "phone", sneaky_payload))

        store = TrustStore(path=tempfile.mktemp())
        with self.assertRaises(PairingError):
            process_identity_announcement(session_key, token, store, session_id=session_id, replay_guard=ReplayGuard())


class TestReplayGuard(unittest.TestCase):
    def test_first_delivery_accepted(self):
        guard = ReplayGuard()
        key = os.urandom(32)
        session_id = str(uuid.uuid4())
        token = encrypt_message(key, envelope("ack", session_id, "desktop", {}))
        decrypt_and_validate_message(key, token, "ack", session_id, replay_guard=guard)  # should not raise

    def test_replay_rejected(self):
        guard = ReplayGuard()
        key = os.urandom(32)
        session_id = str(uuid.uuid4())
        token = encrypt_message(key, envelope("ack", session_id, "desktop", {}))
        decrypt_and_validate_message(key, token, "ack", session_id, replay_guard=guard)
        with self.assertRaises(PairingError):
            decrypt_and_validate_message(key, token, "ack", session_id, replay_guard=guard)

    def test_stale_timestamp_rejected(self):
        guard = ReplayGuard(max_clock_skew_seconds=1)
        message = {"message_id": str(uuid.uuid4()), "timestamp": "2020-01-01T00:00:00+00:00"}
        with self.assertRaises(PairingError):
            guard.check_and_record(message)

    def test_missing_message_id_rejected(self):
        guard = ReplayGuard()
        with self.assertRaises(PairingError):
            guard.check_and_record({"timestamp": "2026-01-01T00:00:00+00:00"})


class TestSessionKeyDerivation(unittest.TestCase):
    def test_deterministic_for_same_inputs(self):
        secret = os.urandom(32)
        session_id = str(uuid.uuid4())
        self.assertEqual(derive_session_key(secret, session_id), derive_session_key(secret, session_id))

    def test_different_session_id_yields_different_key(self):
        secret = os.urandom(32)
        key1 = derive_session_key(secret, str(uuid.uuid4()))
        key2 = derive_session_key(secret, str(uuid.uuid4()))
        self.assertNotEqual(key1, key2)

    def test_output_is_32_bytes(self):
        self.assertEqual(len(derive_session_key(os.urandom(32), str(uuid.uuid4()))), 32)

    def test_not_a_bare_sha256_concatenation(self):
        # Regression guard: confirms this isn't just hashlib.sha256(secret + session_id).digest()
        import hashlib
        secret = os.urandom(32)
        session_id = str(uuid.uuid4())
        naive = hashlib.sha256(secret + session_id.encode()).digest()
        self.assertNotEqual(derive_session_key(secret, session_id), naive)


if __name__ == "__main__":
    unittest.main()
