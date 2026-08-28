"""
Tests for protocol.PairingHandshake -- the SPAKE2-based authenticated
key exchange that replaced the original unauthenticated raw
Diffie-Hellman Handshake.

test_mitm_attack_is_rejected reproduces the exact attack strategy that
fully succeeded against the old Handshake class (attacker completes two
independent key exchanges, one with each real device, ending up holding
both session keys with neither side able to detect it). Against this
design, the same strategy is caught at the confirmation step because the
attacker doesn't know the pairing code.
"""
import os
import sys
import time
import shutil
import tempfile
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from protocol import (
    PairingHandshake,
    PairingAttemptLimiter,
    PairingError,
    make_pairing_code,
)


def _run_handshake(code_a: str, code_b: str, session_id: str, limiter_a=None, limiter_b=None):
    """Run a full two-round-trip handshake between a 'phone' and a
    'desktop' side using the given (possibly mismatched) codes. Returns
    (key_from_phone_side_or_None, key_from_desktop_side_or_None,
    error_from_phone_or_None, error_from_desktop_or_None)."""
    phone = PairingHandshake(code_a, session_id, role="phone", limiter=limiter_a)
    desktop = PairingHandshake(code_b, session_id, role="desktop", limiter=limiter_b)

    msg_p = phone.start()
    msg_d = desktop.start()
    phone.derive(msg_d)
    desktop.derive(msg_p)

    tag_p = phone.confirmation_tag()
    tag_d = desktop.confirmation_tag()

    key_p = err_p = key_d = err_d = None
    try:
        key_p = phone.confirm_and_verify(tag_d)
    except PairingError as e:
        err_p = e
    try:
        key_d = desktop.confirm_and_verify(tag_p)
    except PairingError as e:
        err_d = e

    return key_p, key_d, err_p, err_d


class TestPairingHandshakeHappyPath(unittest.TestCase):
    def test_matching_codes_produce_matching_keys(self):
        code = make_pairing_code()
        key_p, key_d, err_p, err_d = _run_handshake(code, code, str(uuid.uuid4()))
        self.assertIsNone(err_p)
        self.assertIsNone(err_d)
        self.assertEqual(key_p, key_d)
        self.assertEqual(len(key_p), 32)

    def test_different_sessions_produce_different_keys_even_with_same_code(self):
        code = make_pairing_code()
        key_1, _, _, _ = _run_handshake(code, code, str(uuid.uuid4()))
        key_2, _, _, _ = _run_handshake(code, code, str(uuid.uuid4()))
        self.assertNotEqual(key_1, key_2)

    def test_get_session_key_raises_before_confirmation(self):
        h = PairingHandshake(make_pairing_code(), str(uuid.uuid4()), role="phone")
        with self.assertRaises(PairingError):
            h.get_session_key()

    def test_get_session_key_works_after_confirmation(self):
        code = make_pairing_code()
        session_id = str(uuid.uuid4())
        phone = PairingHandshake(code, session_id, role="phone")
        desktop = PairingHandshake(code, session_id, role="desktop")
        msg_p, msg_d = phone.start(), desktop.start()
        phone.derive(msg_d)
        desktop.derive(msg_p)
        key = phone.confirm_and_verify(desktop.confirmation_tag())
        self.assertEqual(phone.get_session_key(), key)

    def test_invalid_role_rejected(self):
        with self.assertRaises(ValueError):
            PairingHandshake(make_pairing_code(), str(uuid.uuid4()), role="tablet")


class TestPairingHandshakeMismatch(unittest.TestCase):
    def test_mismatched_codes_are_rejected_not_silently_divergent(self):
        # This is the core property a PAKE gives you that plain DH
        # doesn't: a wrong code doesn't quietly produce a useless key
        # that fails later in some confusing way -- it's caught right
        # here, with a clear error.
        session_id = str(uuid.uuid4())
        key_p, key_d, err_p, err_d = _run_handshake("111111", "222222", session_id)
        self.assertIsNone(key_p)
        self.assertIsNone(key_d)
        self.assertIsInstance(err_p, PairingError)
        self.assertIsInstance(err_d, PairingError)

    def test_confirm_and_verify_before_derive_raises(self):
        h = PairingHandshake(make_pairing_code(), str(uuid.uuid4()), role="phone")
        with self.assertRaises(PairingError):
            h.confirm_and_verify(b"garbage")

    def test_confirmation_tag_before_derive_raises(self):
        h = PairingHandshake(make_pairing_code(), str(uuid.uuid4()), role="phone")
        with self.assertRaises(PairingError):
            h.confirmation_tag()


class TestMitmRejection(unittest.TestCase):
    def test_mitm_attack_is_rejected(self):
        """
        Reproduces the exact attack that fully succeeded against the old
        Handshake class: an attacker sits between the phone and the
        desktop, completing two independent handshakes (one facing each
        real device) and relaying its own messages instead of passing
        the real ones through. Against plain unauthenticated DH, both
        real devices ended up sharing a session key with the attacker
        with no way to detect it. Here, the attacker doesn't know the
        real pairing code, so confirmation must fail on both sides.
        """
        real_code = make_pairing_code()
        session_id = str(uuid.uuid4())

        real_phone = PairingHandshake(real_code, session_id, role="phone")
        real_desktop = PairingHandshake(real_code, session_id, role="desktop")
        attacker_facing_phone = PairingHandshake("000000", session_id, role="desktop")
        attacker_facing_desktop = PairingHandshake("000000", session_id, role="phone")

        msg_p = real_phone.start()
        msg_d = real_desktop.start()
        msg_atp = attacker_facing_phone.start()
        msg_atd = attacker_facing_desktop.start()

        # Attacker relays its own messages instead of the real ones
        real_phone.derive(msg_atp)
        attacker_facing_phone.derive(msg_p)

        real_desktop.derive(msg_atd)
        attacker_facing_desktop.derive(msg_d)

        with self.assertRaises(PairingError):
            real_phone.confirm_and_verify(attacker_facing_phone.confirmation_tag())

        with self.assertRaises(PairingError):
            real_desktop.confirm_and_verify(attacker_facing_desktop.confirmation_tag())

    def test_passive_eavesdropper_gains_nothing_usable(self):
        """
        A passive attacker who only observes the start()/confirmation
        messages (without participating as a relay) should not be able
        to derive the session key -- SPAKE2's whole point is that the
        exchanged messages don't reveal the password or the resulting
        key to someone who doesn't already know the password.
        """
        real_code = make_pairing_code()
        session_id = str(uuid.uuid4())
        phone = PairingHandshake(real_code, session_id, role="phone")
        desktop = PairingHandshake(real_code, session_id, role="desktop")

        msg_p = phone.start()
        msg_d = desktop.start()
        phone.derive(msg_d)
        desktop.derive(msg_p)
        real_key = phone.confirm_and_verify(desktop.confirmation_tag())

        # Eavesdropper captured msg_p and msg_d but doesn't know the code.
        # Trying every 6-digit code and checking against a captured
        # transcript is exactly what a PAKE is designed to make useless
        # offline -- simulate a handful of guesses to demonstrate none
        # of them produce the real key without also going through (and
        # failing) the online confirmation step.
        for guess in ["111111", "222222", "999999"]:
            attacker = PairingHandshake(guess, session_id, role="desktop")
            attacker.start()
            attacker.derive(msg_p)
            guessed_key = attacker._session_key  # internal, pre-confirmation
            self.assertNotEqual(guessed_key, real_key)


class TestPairingAttemptLimiter(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cognibudget_pairing_test_")
        self.state_path = os.path.join(self.tmpdir, "lockout.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_lockout_initially(self):
        limiter = PairingAttemptLimiter(state_path=self.state_path)
        limiter.check()  # should not raise

    def test_failure_triggers_lockout(self):
        limiter = PairingAttemptLimiter(state_path=self.state_path, base_lock_seconds=10)
        limiter.record_failure()
        with self.assertRaises(PairingError):
            limiter.check()

    def test_backoff_grows_with_repeated_failures(self):
        limiter = PairingAttemptLimiter(state_path=self.state_path, base_lock_seconds=5)
        limiter.record_failure()
        remaining_1 = limiter._load()["locked_until"] - time.time()
        limiter.record_failure()
        remaining_2 = limiter._load()["locked_until"] - time.time()
        limiter.record_failure()
        remaining_3 = limiter._load()["locked_until"] - time.time()
        self.assertLess(remaining_1, remaining_2)
        self.assertLess(remaining_2, remaining_3)

    def test_success_clears_lockout_state(self):
        limiter = PairingAttemptLimiter(state_path=self.state_path, base_lock_seconds=10)
        limiter.record_failure()
        limiter.record_success()
        limiter.check()  # should not raise -- state was cleared
        self.assertFalse(os.path.exists(self.state_path))

    def test_lockout_persists_across_new_limiter_instances(self):
        # Simulates surviving a process restart: a fresh Python object
        # pointed at the same state file should still see the lockout.
        limiter_1 = PairingAttemptLimiter(state_path=self.state_path, base_lock_seconds=10)
        limiter_1.record_failure()

        limiter_2 = PairingAttemptLimiter(state_path=self.state_path, base_lock_seconds=10)
        with self.assertRaises(PairingError):
            limiter_2.check()

    def test_full_handshake_honors_the_limiter(self):
        limiter = PairingAttemptLimiter(state_path=self.state_path, base_lock_seconds=10)
        session_id = str(uuid.uuid4())

        # A failed handshake should record against the limiter...
        _run_handshake("111111", "222222", session_id, limiter_a=limiter)

        # ...such that even a subsequent CORRECT attempt is blocked
        # until the backoff expires.
        code = make_pairing_code()
        h = PairingHandshake(code, str(uuid.uuid4()), role="phone", limiter=limiter)
        with self.assertRaises(PairingError):
            h.start()

    def test_state_file_is_permission_restricted(self):
        import stat
        limiter = PairingAttemptLimiter(state_path=self.state_path, base_lock_seconds=10)
        limiter.record_failure()
        mode = stat.S_IMODE(os.stat(self.state_path).st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
