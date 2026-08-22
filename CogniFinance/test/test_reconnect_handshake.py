"""
Tests for the trust-on-first-use identity pinning + signed-ephemeral-key
reconnect flow: TrustStore, build_identity_announcement /
process_identity_announcement, and ReconnectHandshake.

This is the fast-path alternative to PairingHandshake for devices that
have already paired once: instead of re-entering the pairing code,
each side proves it still holds the private key matching a previously-
pinned public key by signing a fresh ephemeral X25519 key.
"""
import os
import shutil
import tempfile
import unittest
import uuid

import protocol
from protocol import (
    Identity,
    TrustStore,
    identity_fingerprint,
    build_identity_announcement,
    process_identity_announcement,
    ReconnectHandshake,
    PairingHandshake,
    PairingAttemptLimiter,
    PairingError,
    make_pairing_code,
    b64,
)
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives import serialization


def _pair_and_pin(tmpdir):
    """Helper: run a full PAKE pairing, then exchange and pin identities
    on both sides. Returns (phone_identity, desktop_identity,
    phone_trust, desktop_trust, phone_fp_of_desktop, desktop_fp_of_phone)."""
    code = make_pairing_code()
    session_id = str(uuid.uuid4())
    phone_pairing = PairingHandshake(code, session_id, role="phone")
    desktop_pairing = PairingHandshake(code, session_id, role="desktop")
    msg_p, msg_d = phone_pairing.start(), desktop_pairing.start()
    phone_pairing.derive(msg_d)
    desktop_pairing.derive(msg_p)
    key_p = phone_pairing.confirm_and_verify(desktop_pairing.confirmation_tag())
    key_d = desktop_pairing.confirm_and_verify(phone_pairing.confirmation_tag())

    phone_identity = Identity()
    desktop_identity = Identity()
    phone_trust = TrustStore(path=os.path.join(tmpdir, "phone_trust.json"))
    desktop_trust = TrustStore(path=os.path.join(tmpdir, "desktop_trust.json"))

    phone_announce = build_identity_announcement(key_p, session_id, "phone", phone_identity)
    desktop_announce = build_identity_announcement(key_d, session_id, "desktop", desktop_identity)

    desktop_fp_of_phone = process_identity_announcement(key_d, phone_announce, desktop_trust)
    phone_fp_of_desktop = process_identity_announcement(key_p, desktop_announce, phone_trust)

    return phone_identity, desktop_identity, phone_trust, desktop_trust, phone_fp_of_desktop, desktop_fp_of_phone


class TestTrustStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cognibudget_truststore_test_")
        self.path = os.path.join(self.tmpdir, "trust.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_unknown_fingerprint_returns_none(self):
        store = TrustStore(path=self.path)
        self.assertIsNone(store.get_public_key("nonexistent"))
        self.assertFalse(store.is_known("nonexistent"))

    def test_pin_then_retrieve(self):
        store = TrustStore(path=self.path)
        identity = Identity()
        fp = identity_fingerprint(identity.public_bytes)
        store.pin(fp, b64(identity.public_bytes), label="Test Device")
        self.assertTrue(store.is_known(fp))
        self.assertEqual(store.get_public_key(fp), identity.public_bytes)

    def test_remove(self):
        store = TrustStore(path=self.path)
        identity = Identity()
        fp = identity_fingerprint(identity.public_bytes)
        store.pin(fp, b64(identity.public_bytes))
        store.remove(fp)
        self.assertFalse(store.is_known(fp))

    def test_persists_across_new_instances(self):
        store1 = TrustStore(path=self.path)
        identity = Identity()
        fp = identity_fingerprint(identity.public_bytes)
        store1.pin(fp, b64(identity.public_bytes))

        store2 = TrustStore(path=self.path)
        self.assertTrue(store2.is_known(fp))

    def test_file_is_permission_restricted(self):
        import stat
        store = TrustStore(path=self.path)
        identity = Identity()
        store.pin(identity_fingerprint(identity.public_bytes), b64(identity.public_bytes))
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)


class TestIdentityFingerprint(unittest.TestCase):
    def test_deterministic(self):
        identity = Identity()
        fp1 = identity_fingerprint(identity.public_bytes)
        fp2 = identity_fingerprint(identity.public_bytes)
        self.assertEqual(fp1, fp2)

    def test_different_identities_different_fingerprints(self):
        fp1 = identity_fingerprint(Identity().public_bytes)
        fp2 = identity_fingerprint(Identity().public_bytes)
        self.assertNotEqual(fp1, fp2)


class TestIdentityAnnouncement(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cognibudget_announce_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_announcement_pins_correct_key(self):
        phone_identity, desktop_identity, phone_trust, desktop_trust, phone_fp, desktop_fp = _pair_and_pin(self.tmpdir)
        self.assertEqual(phone_trust.get_public_key(phone_fp), desktop_identity.public_bytes)
        self.assertEqual(desktop_trust.get_public_key(desktop_fp), phone_identity.public_bytes)

    def test_tampered_fingerprint_claim_is_rejected(self):
        # A malicious announcement claiming a fingerprint that doesn't
        # match its own embedded public key should be rejected outright
        # rather than pinned under the wrong identifier.
        session_id = str(uuid.uuid4())
        session_key = os.urandom(32)
        real_identity = Identity()
        attacker_identity = Identity()

        payload = {
            "public_key": b64(real_identity.public_bytes),
            "fingerprint": identity_fingerprint(attacker_identity.public_bytes),  # lies about the fingerprint
            "label": "",
        }
        token = protocol.encrypt_message(session_key, protocol.envelope("identity_announce", session_id, "phone", payload))

        store = TrustStore(path=os.path.join(self.tmpdir, "victim_trust.json"))
        with self.assertRaises(PairingError):
            process_identity_announcement(session_key, token, store)


class TestReconnectHandshakeHappyPath(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cognibudget_reconnect_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reconnect_after_pairing_produces_matching_keys(self):
        phone_identity, desktop_identity, phone_trust, desktop_trust, phone_fp, desktop_fp = _pair_and_pin(self.tmpdir)

        session_id = str(uuid.uuid4())
        phone_r = ReconnectHandshake(phone_identity, phone_fp, session_id, role="phone", trust_store=phone_trust)
        desktop_r = ReconnectHandshake(desktop_identity, desktop_fp, session_id, role="desktop", trust_store=desktop_trust)

        msg_p, msg_d = phone_r.start(), desktop_r.start()
        phone_r.derive(msg_d)
        desktop_r.derive(msg_p)
        key_p = phone_r.confirm_and_verify(desktop_r.confirmation_tag())
        key_d = desktop_r.confirm_and_verify(phone_r.confirmation_tag())

        self.assertEqual(key_p, key_d)
        self.assertEqual(len(key_p), 32)

    def test_reconnect_key_differs_each_session(self):
        phone_identity, desktop_identity, phone_trust, desktop_trust, phone_fp, desktop_fp = _pair_and_pin(self.tmpdir)

        def do_reconnect():
            session_id = str(uuid.uuid4())
            phone_r = ReconnectHandshake(phone_identity, phone_fp, session_id, role="phone", trust_store=phone_trust)
            desktop_r = ReconnectHandshake(desktop_identity, desktop_fp, session_id, role="desktop", trust_store=desktop_trust)
            msg_p, msg_d = phone_r.start(), desktop_r.start()
            phone_r.derive(msg_d)
            desktop_r.derive(msg_p)
            return phone_r.confirm_and_verify(desktop_r.confirmation_tag())

        self.assertNotEqual(do_reconnect(), do_reconnect())

    def test_no_pairing_code_needed(self):
        # Sanity check: ReconnectHandshake's constructor signature has no
        # pairing-code parameter at all -- it can only be driven by an
        # already-pinned identity.
        import inspect
        sig = inspect.signature(ReconnectHandshake.__init__)
        self.assertNotIn("pairing_code", sig.parameters)
        self.assertNotIn("code", sig.parameters)

    def test_get_session_key_raises_before_confirmation(self):
        phone_identity, desktop_identity, phone_trust, desktop_trust, phone_fp, desktop_fp = _pair_and_pin(self.tmpdir)
        r = ReconnectHandshake(phone_identity, phone_fp, str(uuid.uuid4()), role="phone", trust_store=phone_trust)
        with self.assertRaises(PairingError):
            r.get_session_key()


class TestReconnectHandshakeSecurity(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cognibudget_reconnect_sec_test_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_unknown_peer_fingerprint_rejected_immediately(self):
        trust = TrustStore(path=os.path.join(self.tmpdir, "trust.json"))
        with self.assertRaises(PairingError):
            ReconnectHandshake(Identity(), "never-pinned", str(uuid.uuid4()), role="phone", trust_store=trust)

    def test_forged_signature_from_wrong_private_key_rejected(self):
        """
        The core security property: an attacker who does NOT hold the
        pinned private key cannot produce a signature that verifies
        against the pinned public key, no matter what ephemeral key they
        present.
        """
        trust = TrustStore(path=os.path.join(self.tmpdir, "trust.json"))
        real_identity = Identity()
        fp = identity_fingerprint(real_identity.public_bytes)
        trust.pin(fp, b64(real_identity.public_bytes))

        attacker_identity = Identity()  # different private key entirely
        session_id = str(uuid.uuid4())

        victim = ReconnectHandshake(Identity(), fp, session_id, role="phone", trust_store=trust)
        victim.start()

        attacker_ephemeral = x25519.X25519PrivateKey.generate()
        attacker_ephemeral_bytes = attacker_ephemeral.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        forged_signature = attacker_identity.sign(
            protocol._signable_reconnect_material(attacker_ephemeral_bytes, session_id, "desktop")
        )
        forged_msg = {"ephemeral_public": b64(attacker_ephemeral_bytes), "signature": b64(forged_signature)}

        with self.assertRaises(PairingError):
            victim.derive(forged_msg)

    def test_cross_session_signature_replay_rejected(self):
        """A captured, validly-signed message from one session must not
        verify in a different session -- session_id is bound into the
        signed material."""
        trust = TrustStore(path=os.path.join(self.tmpdir, "trust.json"))
        dummy_trust = TrustStore(path=os.path.join(self.tmpdir, "dummy_trust.json"))
        desktop_identity = Identity()
        desktop_fp = identity_fingerprint(desktop_identity.public_bytes)
        trust.pin(desktop_fp, b64(desktop_identity.public_bytes))
        dummy_trust.pin("dummy", b64(Identity().public_bytes))

        old_session = str(uuid.uuid4())
        old_desktop_side = ReconnectHandshake(desktop_identity, "dummy", old_session, role="desktop", trust_store=dummy_trust)
        captured_msg = old_desktop_side.start()

        new_session = str(uuid.uuid4())
        phone_r = ReconnectHandshake(Identity(), desktop_fp, new_session, role="phone", trust_store=trust)
        phone_r.start()

        with self.assertRaises(PairingError):
            phone_r.derive(captured_msg)

    def test_role_reflection_rejected(self):
        """
        A message signed under role='phone' must not verify against a
        receiver that is *also* configured as role='phone' (which
        expects its peer to be role='desktop') -- this is what prevents
        an attacker from capturing a party's own outgoing message and
        bouncing it back at that same party, claiming to be its peer.
        """
        trust = TrustStore(path=os.path.join(self.tmpdir, "trust.json"))
        dummy_trust = TrustStore(path=os.path.join(self.tmpdir, "dummy_trust.json"))
        phone_identity = Identity()
        phone_fp = identity_fingerprint(phone_identity.public_bytes)
        trust.pin(phone_fp, b64(phone_identity.public_bytes))
        dummy_trust.pin("dummy", b64(Identity().public_bytes))

        session_id = str(uuid.uuid4())
        # phone_identity signs its own start() message with role='phone'
        phone_side = ReconnectHandshake(phone_identity, "dummy", session_id, role="phone", trust_store=dummy_trust)
        phone_msg = phone_side.start()

        # A receiver ALSO configured as role='phone' expects its peer to
        # be role='desktop' -- reflecting phone's own message back at
        # this receiver should fail, even though the signature itself is
        # cryptographically valid (just valid for a different role).
        reflecting_receiver = ReconnectHandshake(Identity(), phone_fp, session_id, role="phone", trust_store=trust)
        reflecting_receiver.start()
        with self.assertRaises(PairingError):
            reflecting_receiver.derive(phone_msg)

    def test_rate_limiter_engages_on_forged_signature(self):
        trust = TrustStore(path=os.path.join(self.tmpdir, "trust.json"))
        limiter = PairingAttemptLimiter(state_path=os.path.join(self.tmpdir, "lockout.json"), base_lock_seconds=10)
        real_identity = Identity()
        fp = identity_fingerprint(real_identity.public_bytes)
        trust.pin(fp, b64(real_identity.public_bytes))

        attacker_identity = Identity()
        session_id = str(uuid.uuid4())
        victim = ReconnectHandshake(Identity(), fp, session_id, role="phone", trust_store=trust, limiter=limiter)
        victim.start()

        attacker_ephemeral = x25519.X25519PrivateKey.generate()
        attacker_ephemeral_bytes = attacker_ephemeral.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        forged_signature = attacker_identity.sign(
            protocol._signable_reconnect_material(attacker_ephemeral_bytes, session_id, "desktop")
        )
        forged_msg = {"ephemeral_public": b64(attacker_ephemeral_bytes), "signature": b64(forged_signature)}
        try:
            victim.derive(forged_msg)
        except PairingError:
            pass

        # A subsequent attempt (even with valid setup) should now be blocked by the limiter
        victim2 = ReconnectHandshake(real_identity, fp, str(uuid.uuid4()), role="phone", trust_store=trust, limiter=limiter)
        with self.assertRaises(PairingError):
            victim2.start()


if __name__ == "__main__":
    unittest.main()
