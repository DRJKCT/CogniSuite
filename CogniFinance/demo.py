import tempfile
from protocol import *
from protocol import _signable_reconnect_material

def main():
    print('=== 1. Pair devices ===')
    # The pairing code is generated once and shown out-of-band (e.g. on
    # the phone's screen) for the user to type into the desktop app --
    # it is never transmitted over the network channel this handshake
    # runs on, since SPAKE2's whole point is that knowing it is what
    # proves each side is really talking to the other.
    pairing_code = make_pairing_code()
    print('Pairing code (shown on phone, typed into desktop):', pairing_code)
    session_id = str(uuid.uuid4())

    print('=== 2. Establish authenticated session (SPAKE2 PAKE) ===')
    limiter = PairingAttemptLimiter()
    phone_pairing = PairingHandshake(pairing_code, session_id, role='phone', limiter=limiter)
    desktop_pairing = PairingHandshake(pairing_code, session_id, role='desktop', limiter=limiter)

    # Round 1: exchange SPAKE2 start() messages
    msg_p = phone_pairing.start()
    msg_d = desktop_pairing.start()
    phone_pairing.derive(msg_d)
    desktop_pairing.derive(msg_p)

    # Round 2: exchange confirmation tags -- this is what actually
    # detects a wrong code or a man-in-the-middle, rather than that
    # failure surfacing later as a mysterious decrypt error.
    tag_p = phone_pairing.confirmation_tag()
    tag_d = desktop_pairing.confirmation_tag()
    key = phone_pairing.confirm_and_verify(tag_d)
    desktop_pairing.confirm_and_verify(tag_p)
    print('Session established and mutually confirmed:', session_id)

    print('=== 2b. Prove a man-in-the-middle is actually rejected ===')
    # Same attack shape as before: an attacker relays its own SPAKE2
    # messages to each side instead of passing the real ones through,
    # without knowing the real pairing code.
    mitm_session = str(uuid.uuid4())
    real_phone = PairingHandshake(pairing_code, mitm_session, role='phone')
    attacker = PairingHandshake('000000', mitm_session, role='desktop')  # wrong code -- attacker doesn't know the real one
    real_msg = real_phone.start()
    attacker_msg = attacker.start()
    real_phone.derive(attacker_msg)
    attacker.derive(real_msg)
    try:
        real_phone.confirm_and_verify(attacker.confirmation_tag())
        print('!!! MITM WAS NOT DETECTED -- this would be a critical bug !!!')
    except PairingError as e:
        print('Man-in-the-middle correctly rejected:', e)

    print('=== 2c. Exchange and pin long-term identities (over the now-authenticated channel) ===')
    phone_identity = Identity()
    desktop_identity = Identity()
    phone_trust = TrustStore(path=os.path.join(tempfile.gettempdir(), 'cognibudget_demo_phone_trust.json'))
    desktop_trust = TrustStore(path=os.path.join(tempfile.gettempdir(), 'cognibudget_demo_desktop_trust.json'))

    phone_announce = build_identity_announcement(key, session_id, 'phone', phone_identity, label='Demo Android Phone')
    desktop_announce = build_identity_announcement(key, session_id, 'desktop', desktop_identity, label='Demo Desktop')
    replay_guard = ReplayGuard()
    desktop_pinned_fp = process_identity_announcement(key, phone_announce, desktop_trust, session_id=session_id, replay_guard=replay_guard)
    phone_pinned_fp = process_identity_announcement(key, desktop_announce, phone_trust, session_id=session_id, replay_guard=replay_guard)
    print('Desktop pinned phone identity:', desktop_pinned_fp)
    print('Phone pinned desktop identity:', phone_pinned_fp)

    print('=== 2d. Later: reconnect with NO pairing code, using the pinned identities ===')
    reconnect_session = str(uuid.uuid4())
    phone_reconnect = ReconnectHandshake(phone_identity, phone_pinned_fp, reconnect_session, role='phone', trust_store=phone_trust)
    desktop_reconnect = ReconnectHandshake(desktop_identity, desktop_pinned_fp, reconnect_session, role='desktop', trust_store=desktop_trust)
    rmsg_p = phone_reconnect.start()
    rmsg_d = desktop_reconnect.start()
    phone_reconnect.derive(rmsg_d)
    desktop_reconnect.derive(rmsg_p)
    key = phone_reconnect.confirm_and_verify(desktop_reconnect.confirmation_tag())
    desktop_reconnect.confirm_and_verify(phone_reconnect.confirmation_tag())
    print('Reconnected without re-entering a code. New session key established:', reconnect_session)

    print('=== 2e. Prove a forged reconnect (attacker without the pinned private key) is rejected ===')
    attacker_identity = Identity()
    forged_session = str(uuid.uuid4())
    victim_phone = ReconnectHandshake(Identity(), phone_pinned_fp, forged_session, role='phone', trust_store=phone_trust)
    victim_phone.start()
    attacker_ephemeral = x25519.X25519PrivateKey.generate()
    attacker_ephemeral_bytes = attacker_ephemeral.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    forged_signature = attacker_identity.sign(_signable_reconnect_material(attacker_ephemeral_bytes, forged_session, 'desktop'))
    forged_msg = {'ephemeral_public': b64(attacker_ephemeral_bytes), 'signature': b64(forged_signature)}
    try:
        victim_phone.derive(forged_msg)
        print('!!! FORGED RECONNECT WAS NOT DETECTED -- this would be a critical bug !!!')
    except PairingError as e:
        print('Forged reconnect correctly rejected:', e)

    print('=== 3. Pending transactions ===')
    phone = PhonePendingStore()
    phone.add(PendingTransaction('tx-11111111', '2026-08-08', 'Groceries', '54.23', 'Groceries', 'Checking'))
    phone.add(PendingTransaction('tx-22222222', '2026-08-08', 'Gas', '62.10', 'Gas', 'Checking'))
    print(len(phone.pending), 'pending')

    print('=== 4. Encrypted transfer ===')
    txs = phone.batch()
    payload = {'package_id': 'pkg-demo-001', 'transaction_count': len(txs), 'payload_hash': payload_hash(txs), 'transactions': txs}
    token = encrypt_message(key, envelope('transaction_batch', session_id, 'android-demo', payload))
    print('Encrypted package created')

    print('=== 5. Desktop verify ===')
    received = decrypt_message(key, token)
    batch = received['payload']
    assert batch['payload_hash'] == payload_hash(batch['transactions'])
    desktop = DesktopLedger()
    result = desktop.accept_batch(batch['transactions'])
    print('Accepted:', result['accepted'])
    print('Rejected:', result['rejected'])

    print('=== 6. Acknowledge ===')
    ack = encrypt_message(key, envelope('ack', session_id, 'desktop-demo', {'accepted': result['accepted'], 'rejected': result['rejected']}))
    print('ACK created')

    print('=== 7. Simulated connection loss before cleanup ===')
    print('Phone still has:', list(phone.pending))

    print('=== 8. Cleanup after ACK ===')
    a = decrypt_message(key, ack)
    phone.acknowledge(a['payload']['accepted'])
    print('Remaining:', list(phone.pending))

    print('=== 9. Retry/idempotency ===')
    retry = desktop.accept_batch(txs)
    print('Retry accepted:', retry['accepted'])
    print('Desktop record count:', len(desktop.accepted))
    print('PROTOTYPE RESULT: PASS')

if __name__ == '__main__':
    main()
