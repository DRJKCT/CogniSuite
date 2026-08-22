#!/usr/bin/env python3
"""
MITM attack simulations against the cogni-transfer security protocol.

Each test simulates a specific attack vector and verifies the protocol
resists it.  The script is self-contained — it instantiates the protocol
classes directly (no network, no HTTP server) so the focus is purely on
the cryptographic properties.

Attack vectors simulated:
  1. Active MITM during SPAKE2 pairing (message substitution)
  2. Wrong pairing code (silent key divergence → confirmation failure)
  3. MITM during reconnect (ephemeral key substitution without pinned key)
  4. Replay of a previously-seen encrypted message
  5. Identity announcement fingerprint tampering
  6. Envelope type confusion (valid ciphertext used as wrong message_type)
  7. Reflected confirmation tag (phone's own tag bounced back at it)
  8. Tampered encrypted batch (bit-flip in ciphertext)
"""

import sys
import os
import copy

# Ensure we can import protocol.py from the attached directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
    derive_session_key,
    b64,
    unb64,
    parse_amount_strict,
)

# Use a temp directory for trust store / identity files so we don't clobber real state.
import tempfile
TMP = tempfile.mkdtemp(prefix="cogni_mitm_sim_")

RESULTS = []


def report(test_name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    RESULTS.append((test_name, passed, detail))
    print(f"[{status}] {test_name}")
    if detail:
        for line in detail.strip().split("\n"):
            print(f"         {line}")
    print()


# ---------------------------------------------------------------------------
# Test 1: Active MITM during SPAKE2 pairing
# ---------------------------------------------------------------------------
def test_mitm_spake2_substitution():
    """
    Mallory intercepts the SPAKE2 exchange between phone and desktop,
    substituting her own messages in both directions.  Without knowing
    the pairing code she ends up with two independent keys, neither of
    which matches the other side's confirmation tag.
    """
    code = "123456"
    session_id = "sess-mitm-1"

    # --- Legitimate parties ---
    phone = PairingHandshake(code, session_id, role="phone")
    desktop = PairingHandshake(code, session_id, role="desktop")

    # --- Mallory (doesn't know the code, guesses "000000") ---
    # She runs TWO handshakes: one as "desktop" (B) toward the phone (A),
    # one as "phone" (A) toward the real desktop (B).
    mallory_as_desktop = PairingHandshake("000000", session_id, role="desktop")
    mallory_as_phone = PairingHandshake("000000", session_id, role="phone")

    # Phone (A) and desktop (B) start
    phone_pA = phone.start()
    desktop_pB = desktop.start()

    # Mallory also starts both sides
    mallory_as_desktop_pB = mallory_as_desktop.start()  # B-side message
    mallory_as_phone_pA = mallory_as_phone.start()       # A-side message

    # Phone derives using Mallory's pB (B-side, correct role for A to receive)
    phone.derive(mallory_as_desktop_pB)
    # Desktop derives using Mallory's pA (A-side, correct role for B to receive)
    desktop.derive(mallory_as_phone_pA)

    # Mallory derives both sides using the real parties' messages
    mallory_as_desktop.derive(phone_pA)   # Mallory (B) receives phone's real pA (A)
    mallory_as_phone.derive(desktop_pB)    # Mallory (A) receives desktop's real pB (B)

    # Each side produces a confirmation tag
    phone_tag = phone.confirmation_tag()
    desktop_tag = desktop.confirmation_tag()

    # Phone tries to verify desktop's tag — but desktop computed its tag
    # with a key derived from Mallory's pA', not phone's real pA.
    # So phone and desktop have DIFFERENT session keys.
    phone_key = phone._session_key
    desktop_key = desktop._session_key

    keys_match = phone_key == desktop_key

    # Attempt cross-verification: phone verifies desktop's tag
    cross_verify_ok = True
    try:
        phone.confirm_and_verify(desktop_tag)
    except PairingError:
        cross_verify_ok = False

    # Desktop verifies phone's tag
    cross_verify_ok_2 = True
    try:
        desktop.confirm_and_verify(phone_tag)
    except PairingError:
        cross_verify_ok_2 = False

    passed = (not keys_match) and (not cross_verify_ok) and (not cross_verify_ok_2)
    detail = (
        f"phone_key == desktop_key: {keys_match} (should be False)\n"
        f"phone verifies desktop tag: {cross_verify_ok} (should be False)\n"
        f"desktop verifies phone tag: {cross_verify_ok_2} (should be False)"
    )
    report("MITM: SPAKE2 message substitution", passed, detail)


# ---------------------------------------------------------------------------
# Test 2: Wrong pairing code (silent divergence → confirmation catches it)
# ---------------------------------------------------------------------------
def test_wrong_pairing_code():
    """
    Phone and desktop use different pairing codes.  SPAKE2's finish()
    silently succeeds (no exception), producing different keys.
    The confirmation tag exchange is what actually detects the mismatch.
    """
    session_id = "sess-wrong-code"
    phone = PairingHandshake("111111", session_id, role="phone")
    desktop = PairingHandshake("999999", session_id, role="desktop")

    pA = phone.start()
    pB = desktop.start()

    # Both derive without error (SPAKE2 doesn't raise on wrong password)
    phone.derive(pB)
    desktop.derive(pA)

    phone_tag = phone.confirmation_tag()
    desktop_tag = desktop.confirmation_tag()

    # Tags should NOT match
    tags_match = phone_tag == desktop_tag

    # Cross-verification should fail
    phone_verify_failed = False
    try:
        phone.confirm_and_verify(desktop_tag)
    except PairingError:
        phone_verify_failed = True

    desktop_verify_failed = False
    try:
        desktop.confirm_and_verify(phone_tag)
    except PairingError:
        desktop_verify_failed = True

    passed = (not tags_match) and phone_verify_failed and desktop_verify_failed
    detail = (
        f"tags_match: {tags_match} (should be False)\n"
        f"phone confirm_and_verify raised: {phone_verify_failed} (should be True)\n"
        f"desktop confirm_and_verify raised: {desktop_verify_failed} (should be True)"
    )
    report("MITM: Wrong pairing code detected by confirmation", passed, detail)


# ---------------------------------------------------------------------------
# Test 3: MITM during reconnect (ephemeral key substitution)
# ---------------------------------------------------------------------------
def test_reconnect_mitm_ephemeral_substitution():
    """
    After a successful pairing, both sides have pinned each other's
    long-term Ed25519 public key.  On reconnect, each side signs a
    fresh X25519 ephemeral key with its long-term key.

    Mallory intercepts the reconnect and substitutes her own ephemeral
    key, but she can't forge the signature because she doesn't have the
    pinned private key.
    """
    identity_path = os.path.join(TMP, "reconnect_identity.key")
    trust_path = os.path.join(TMP, "reconnect_trust.json")

    # --- Legitimate pairing (set up pinned identity) ---
    phone_identity = Identity()
    desktop_identity = load_or_create_identity(identity_path)

    phone_trust = TrustStore(trust_path)
    desktop_trust = TrustStore(trust_path)

    # Pin each other's identity (simulating post-pairing state)
    phone_trust.pin(
        identity_fingerprint(desktop_identity.public_bytes),
        b64(desktop_identity.public_bytes),
        label="desktop",
    )
    desktop_trust.pin(
        identity_fingerprint(phone_identity.public_bytes),
        b64(phone_identity.public_bytes),
        label="phone",
    )

    phone_fp = identity_fingerprint(phone_identity.public_bytes)
    desktop_fp = identity_fingerprint(desktop_identity.public_bytes)

    session_id = "sess-reconnect-mitm"

    # --- Legitimate reconnect ---
    phone_reconnect = ReconnectHandshake(
        phone_identity, desktop_fp, session_id, role="phone",
        trust_store=phone_trust,
    )
    phone_msg = phone_reconnect.start()

    # --- Mallory intercepts phone_msg and substitutes her own ephemeral key ---
    # She generates her own X25519 keypair and signs it with HER OWN Ed25519 key
    # (not the phone's pinned key)
    mallory_identity = Identity()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import x25519
    from protocol import _signable_reconnect_material, _confirmation_tag

    mallory_ephemeral = x25519.X25519PrivateKey.generate()
    mallory_ephemeral_public = mallory_ephemeral.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    # Mallory signs with HER key, using the PHONE's role/session so the
    # format is correct — but the signature won't verify against the
    # DESKTOP's pinned copy of the phone's public key.
    mallory_signable = _signable_reconnect_material(
        mallory_ephemeral_public, session_id, "phone"
    )
    mallory_sig = mallory_identity.sign(mallory_signable)

    tampered_msg = {
        "ephemeral_public": b64(mallory_ephemeral_public),
        "signature": b64(mallory_sig),
    }

    # --- Desktop tries to derive with the tampered message ---
    desktop_reconnect = ReconnectHandshake(
        desktop_identity, phone_fp, session_id, role="desktop",
        trust_store=desktop_trust,
    )
    desktop_msg = desktop_reconnect.start()  # desktop's own start (not used in this test)

    derive_raised = False
    try:
        desktop_reconnect.derive(tampered_msg)
    except PairingError:
        derive_raised = True

    passed = derive_raised
    detail = (
        f"desktop.derive() with Mallory's substituted key raised PairingError: {derive_raised}\n"
        f"(Mallory cannot forge a signature under the phone's pinned Ed25519 key)"
    )
    report("MITM: Reconnect ephemeral key substitution", passed, detail)


# ---------------------------------------------------------------------------
# Test 4: Replay attack (replayed encrypted message)
# ---------------------------------------------------------------------------
def test_replay_attack():
    """
    Mallory captures a valid encrypted message and replays it.
    ReplayGuard should reject the duplicate message_id.
    """
    session_id = "sess-replay"
    # Set up a confirmed session key (simulated)
    code = "424242"
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

    # Phone sends a valid encrypted message
    msg = envelope("test_message", session_id, "phone", {"data": "secret"})
    encrypted = encrypt_message(session_key, msg)

    # Desktop receives it the first time — should succeed
    guard = ReplayGuard()
    first_ok = True
    try:
        decrypt_and_validate_message(
            session_key, encrypted, "test_message", session_id, guard
        )
    except PairingError:
        first_ok = False

    # Mallory replays the same encrypted message — should be rejected
    replay_rejected = False
    try:
        decrypt_and_validate_message(
            session_key, encrypted, "test_message", session_id, guard
        )
    except PairingError as e:
        replay_rejected = "Replayed message" in str(e)

    passed = first_ok and replay_rejected
    detail = (
        f"first delivery accepted: {first_ok} (should be True)\n"
        f"replay rejected with 'Replayed message': {replay_rejected} (should be True)"
    )
    report("MITM: Replay of captured encrypted message", passed, detail)


# ---------------------------------------------------------------------------
# Test 5: Identity announcement fingerprint tampering
# ---------------------------------------------------------------------------
def test_identity_tampering():
    """
    Mallory intercepts the encrypted identity announcement and modifies
    the claimed fingerprint to match her own key.  The protocol verifies
    that the claimed fingerprint matches the actual public key in the
    message — a mismatch is detected.
    """
    session_id = "sess-id-tamper"
    code = "555555"
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

    # Legitimate identity announcement
    phone_identity = Identity()
    trust_store = TrustStore(os.path.join(TMP, "id_tamper_trust.json"))
    token = build_identity_announcement(
        session_key, session_id, "phone", phone_identity, label="phone"
    )

    # Mallory tamper: decrypt the message, change the fingerprint, re-encrypt
    # (She can't do this without the key in practice, but let's test the
    # case where the fingerprint field is manipulated at the application layer)
    msg = decrypt_message(session_key, token)
    payload = msg["payload"]

    # Tamper: claim a different fingerprint
    mallory_identity = Identity()
    tampered_fingerprint = identity_fingerprint(mallory_identity.public_bytes)
    payload["fingerprint"] = tampered_fingerprint
    msg["payload"] = payload

    # Re-encrypt the tampered message
    from protocol import canonical_json
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import os as _os

    nonce = _os.urandom(12)
    tampered_token = b64(nonce + AESGCM(session_key).encrypt(
        nonce, canonical_json(msg), None
    ))

    tamper_detected = False
    try:
        process_identity_announcement(
            session_key, tampered_token, trust_store,
            session_id=session_id,
        )
    except PairingError as e:
        tamper_detected = "fingerprint does not match" in str(e).lower()

    # Also test the legitimate (untampered) announcement works
    legit_ok = True
    legit_trust = TrustStore(os.path.join(TMP, "id_tamper_trust_legit.json"))
    try:
        fp = process_identity_announcement(
            session_key, token, legit_trust,
            session_id=session_id,
        )
        # Verify the pinned fingerprint matches
        legit_ok = fp == identity_fingerprint(phone_identity.public_bytes)
    except PairingError:
        legit_ok = False

    passed = tamper_detected and legit_ok
    detail = (
        f"legitimate announcement accepted: {legit_ok} (should be True)\n"
        f"tampered fingerprint detected: {tamper_detected} (should be True)"
    )
    report("MITM: Identity announcement fingerprint tampering", passed, detail)


# ---------------------------------------------------------------------------
# Test 6: Envelope type confusion
# ---------------------------------------------------------------------------
def test_envelope_type_confusion():
    """
    Mallory captures a valid encrypted message of type "identity_announce"
    and tries to submit it as a "transaction_batch" (or vice versa).
    decrypt_and_validate_message checks the message_type field after
    decryption and rejects mismatches.
    """
    session_id = "sess-type-confusion"
    code = "777777"
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

    # Create a valid "identity_announce" message
    msg = envelope("identity_announce", session_id, "phone", {"public_key": "fake", "fingerprint": "fake"})
    encrypted = encrypt_message(session_key, msg)

    # Try to use it as a "transaction_batch"
    type_confusion_rejected = False
    try:
        decrypt_and_validate_message(
            session_key, encrypted, "transaction_batch", session_id, None
        )
    except PairingError as e:
        type_confusion_rejected = "Expected message_type" in str(e)

    # Verify the correct type DOES work
    correct_type_ok = True
    try:
        result = decrypt_and_validate_message(
            session_key, encrypted, "identity_announce", session_id, None
        )
        correct_type_ok = result.get("message_type") == "identity_announce"
    except PairingError:
        correct_type_ok = False

    passed = type_confusion_rejected and correct_type_ok
    detail = (
        f"correct type accepted: {correct_type_ok} (should be True)\n"
        f"type confusion rejected: {type_confusion_rejected} (should be True)"
    )
    report("MITM: Envelope type confusion", passed, detail)


# ---------------------------------------------------------------------------
# Test 7: Reflected confirmation tag (phone's own tag bounced back)
# ---------------------------------------------------------------------------
def test_reflected_confirmation_tag():
    """
    Mallory captures the phone's confirmation tag and reflects it back
    at the phone (or desktop's tag back at desktop).  The confirmation
    tag is bound to `role` — a party's own tag won't verify as its peer's.
    """
    session_id = "sess-reflect"
    code = "888888"
    phone = PairingHandshake(code, session_id, role="phone")
    desktop = PairingHandshake(code, session_id, role="desktop")

    pA = phone.start()
    pB = desktop.start()
    phone.derive(pB)
    desktop.derive(pA)

    phone_tag = phone.confirmation_tag()
    desktop_tag = desktop.confirmation_tag()

    # Phone tries to verify its OWN tag (reflected by Mallory)
    phone_self_verify_failed = False
    try:
        phone.confirm_and_verify(phone_tag)
    except PairingError:
        phone_self_verify_failed = True

    # Desktop tries to verify its OWN tag
    desktop_self_verify_failed = False
    try:
        desktop.confirm_and_verify(desktop_tag)
    except PairingError:
        desktop_self_verify_failed = True

    # Cross-verification (legitimate) should succeed
    phone_verify_desktop_ok = True
    try:
        phone.confirm_and_verify(desktop_tag)
    except PairingError:
        phone_verify_desktop_ok = False

    passed = phone_self_verify_failed and desktop_self_verify_failed and phone_verify_desktop_ok
    detail = (
        f"phone rejects own reflected tag: {phone_self_verify_failed} (should be True)\n"
        f"desktop rejects own reflected tag: {desktop_self_verify_failed} (should be True)\n"
        f"phone accepts desktop's real tag: {phone_verify_desktop_ok} (should be True)"
    )
    report("MITM: Reflected confirmation tag", passed, detail)


# ---------------------------------------------------------------------------
# Test 8: Tampered ciphertext (bit-flip attack)
# ---------------------------------------------------------------------------
def test_ciphertext_bitflip():
    """
    AES-GCM is authenticated encryption — flipping a bit in the ciphertext
    should cause decryption to fail with InvalidTag, not produce a
    silently corrupted plaintext.
    """
    session_id = "sess-bitflip"
    code = "101010"
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

    # Create a valid encrypted message
    msg = envelope("test_message", session_id, "phone", {"amount": "100.00"})
    encrypted = encrypt_message(session_key, msg)

    # Flip a bit in the ciphertext (not the nonce)
    raw = bytearray(unb64(encrypted))
    raw[len(raw) // 2] ^= 0x01  # flip one bit in the middle
    tampered_encrypted = b64(bytes(raw))

    # Decryption should fail
    from cryptography.exceptions import InvalidTag

    decrypt_failed = False
    try:
        decrypt_message(session_key, tampered_encrypted)
    except (InvalidTag, Exception):
        decrypt_failed = True

    # Original should still decrypt fine
    original_ok = True
    try:
        result = decrypt_message(session_key, encrypted)
        original_ok = result.get("payload", {}).get("amount") == "100.00"
    except Exception:
        original_ok = False

    passed = decrypt_failed and original_ok
    detail = (
        f"original decrypts fine: {original_ok} (should be True)\n"
        f"bit-flipped ciphertext rejected: {decrypt_failed} (should be True)"
    )
    report("MITM: Ciphertext bit-flip (AES-GCM integrity)", passed, detail)


# ---------------------------------------------------------------------------
# Test 9: Session ID mismatch
# ---------------------------------------------------------------------------
def test_session_id_mismatch():
    """
    A message encrypted under one session_id is submitted as if it
    belonged to a different session.  decrypt_and_validate_message
    checks the session_id field.
    """
    code = "303030"
    real_session = "sess-real-123"
    fake_session = "sess-fake-999"

    phone = PairingHandshake(code, real_session, role="phone")
    desktop = PairingHandshake(code, real_session, role="desktop")

    pA = phone.start()
    pB = desktop.start()
    phone.derive(pB)
    desktop.derive(pA)

    phone_tag = phone.confirmation_tag()
    desktop_tag = desktop.confirmation_tag()
    phone.confirm_and_verify(desktop_tag)
    desktop.confirm_and_verify(phone_tag)
    session_key = phone.get_session_key()

    # Create a message for the REAL session
    msg = envelope("test_message", real_session, "phone", {"data": "secret"})
    encrypted = encrypt_message(session_key, msg)

    # Try to validate it against a FAKE session
    mismatch_rejected = False
    try:
        decrypt_and_validate_message(
            session_key, encrypted, "test_message", fake_session, None
        )
    except PairingError as e:
        mismatch_rejected = "session_id" in str(e).lower()

    # Correct session should work
    correct_ok = True
    try:
        decrypt_and_validate_message(
            session_key, encrypted, "test_message", real_session, None
        )
    except PairingError:
        correct_ok = False

    passed = mismatch_rejected and correct_ok
    detail = (
        f"correct session_id accepted: {correct_ok} (should be True)\n"
        f"mismatched session_id rejected: {mismatch_rejected} (should be True)"
    )
    report("MITM: Session ID cross-use", passed, detail)


# ---------------------------------------------------------------------------
# Test 10: Protocol version downgrade
# ---------------------------------------------------------------------------
def test_protocol_version_downgrade():
    """
    A message with a different protocol version or protocol name is
    rejected by decrypt_and_validate_message.
    """
    code = "404040"
    session_id = "sess-version"

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

    # Create a message with wrong protocol name
    msg = envelope("test_message", session_id, "phone", {"data": "secret"})
    msg["protocol"] = "evil-protocol"
    from protocol import canonical_json
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import os as _os

    nonce = _os.urandom(12)
    wrong_proto_token = b64(nonce + AESGCM(session_key).encrypt(
        nonce, canonical_json(msg), None
    ))

    proto_rejected = False
    try:
        decrypt_and_validate_message(
            session_key, wrong_proto_token, "test_message", session_id, None
        )
    except PairingError as e:
        proto_rejected = "protocol" in str(e).lower()

    # Wrong version
    msg2 = envelope("test_message", session_id, "phone", {"data": "secret"})
    msg2["version"] = 999
    nonce2 = _os.urandom(12)
    wrong_ver_token = b64(nonce2 + AESGCM(session_key).encrypt(
        nonce2, canonical_json(msg2), None
    ))

    ver_rejected = False
    try:
        decrypt_and_validate_message(
            session_key, wrong_ver_token, "test_message", session_id, None
        )
    except PairingError as e:
        ver_rejected = "version" in str(e).lower()

    passed = proto_rejected and ver_rejected
    detail = (
        f"wrong protocol name rejected: {proto_rejected} (should be True)\n"
        f"wrong version rejected: {ver_rejected} (should be True)"
    )
    report("MITM: Protocol/version downgrade", passed, detail)


# ---------------------------------------------------------------------------
# Test 11: Amount injection (NaN / infinity / scientific notation)
# ---------------------------------------------------------------------------
def test_amount_injection():
    """
    Malicious transaction amounts that would bypass validation if float()
    were used instead of parse_amount_strict().
    """
    test_cases = [
        ("nan", False, "NaN bypasses float comparison"),
        ("inf", False, "Infinity as amount"),
        ("-inf", False, "Negative infinity"),
        ("1e10", False, "Scientific notation"),
        ("0", False, "Zero amount"),
        ("-5.00", False, "Negative amount"),
        ("", False, "Empty string"),
        ("  ", False, "Whitespace only"),
        ("5.00", True, "Valid amount"),
        ("100", True, "Integer amount"),
        ("0.01", True, "Minimum cent"),
        ("9999999999.99", True, "Large valid amount"),
    ]

    all_passed = True
    details = []
    for value, should_pass, description in test_cases:
        try:
            result = parse_amount_strict(value)
            actual_pass = result > 0
        except (ValueError, Exception):
            actual_pass = False

        ok = actual_pass == should_pass
        if not ok:
            all_passed = False
        details.append(f"  {value:>15s} → {'pass' if actual_pass else 'reject':6s}  (expected {'pass' if should_pass else 'reject':6s})  {'✓' if ok else '✗'} {description}")

    report("MITM: Amount injection (NaN/inf/scientific)", all_passed, "\n".join(details))


# ---------------------------------------------------------------------------
# Test 12: DesktopLedger idempotent re-acceptance with content change
# ---------------------------------------------------------------------------
def test_idempotent_conflict_detection():
    """
    If the phone resends a transaction with the SAME ID but DIFFERENT
    content, the DesktopLedger should flag it as a conflict rather than
    silently overwriting the original.
    """
    ledger = DesktopLedger()

    tx1 = {
        "transaction_id": "tx-001",
        "date": "2026-01-01",
        "description": "Coffee",
        "amount": "4.50",
        "debit_account": "Groceries",
        "credit_account": "Credit Card",
    }

    # First submission — accepted
    result1 = ledger.accept_batch([tx1])

    # Idempotent retry (identical content) — accepted as retry
    result2 = ledger.accept_batch([tx1])

    # Resent with DIFFERENT content — should be conflicted
    tx2 = copy.deepcopy(tx1)
    tx2["amount"] = "999.99"
    tx2["description"] = "LAPTOP PURCHASE"
    result3 = ledger.accept_batch([tx2])

    first_accepted = "tx-001" in result1["accepted"]
    retry_accepted = "tx-001" in result2["accepted"]
    conflict_detected = len(result3["conflicted"]) > 0 and result3["conflicted"][0]["transaction_id"] == "tx-001"
    original_preserved = ledger.accepted["tx-001"]["amount"] == "4.50"

    passed = first_accepted and retry_accepted and conflict_detected and original_preserved
    detail = (
        f"first submission accepted: {first_accepted} (should be True)\n"
        f"identical retry accepted: {retry_accepted} (should be True)\n"
        f"different content flagged as conflict: {conflict_detected} (should be True)\n"
        f"original transaction preserved (not overwritten): {original_preserved} (should be True)"
    )
    report("MITM: Idempotent conflict detection (same ID, different content)", passed, detail)


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
def main():
    print("=" * 72)
    print("  cogni-transfer Protocol — MITM Attack Simulation Suite")
    print("=" * 72)
    print()

    test_mitm_spake2_substitution()
    test_wrong_pairing_code()
    test_reconnect_mitm_ephemeral_substitution()
    test_replay_attack()
    test_identity_tampering()
    test_envelope_type_confusion()
    test_reflected_confirmation_tag()
    test_ciphertext_bitflip()
    test_session_id_mismatch()
    test_protocol_version_downgrade()
    test_amount_injection()
    test_idempotent_conflict_detection()

    # Summary
    print("=" * 72)
    total = len(RESULTS)
    passed = sum(1 for _, p, _ in RESULTS if p)
    failed = total - passed
    print(f"  Results: {passed}/{total} passed, {failed} failed")
    print("=" * 72)
    if failed:
        print("\n  FAILED TESTS:")
        for name, p, detail in RESULTS:
            if not p:
                print(f"    ✗ {name}")
                if detail:
                    for line in detail.strip().split("\n"):
                        print(f"      {line}")
        print()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
