# protocol.py ver 0,3
# For CogniFinance ver 0.9.67

from __future__ import annotations
import base64, hashlib, hmac, json, os, re, secrets, time, uuid
from dataclasses import dataclass
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from spake2 import SPAKE2_A, SPAKE2_B
from config import (
    PAIRING_LOCKOUT_PATH as DEFAULT_PAIRING_LOCKOUT_PATH,
    TRUST_STORE_PATH as DEFAULT_TRUST_STORE_PATH,
    SYNC_IDENTITY_PATH as DEFAULT_IDENTITY_PATH,
    create_secure_file, atomic_secure_write,
)

PROTOCOL='cogni-transfer'; VERSION=1
def now_iso(): return datetime.now(timezone.utc).isoformat()
def b64(x): return base64.urlsafe_b64encode(x).decode()
def unb64(x): return base64.urlsafe_b64decode(x.encode())
def canonical_json(o): return json.dumps(o,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def payload_hash(o): return hashlib.sha256(canonical_json(o)).hexdigest()
def envelope(message_type,session_id,device_id,payload): return {'protocol':PROTOCOL,'version':VERSION,'message_type':message_type,'session_id':session_id,'message_id':str(uuid.uuid4()),'device_id':device_id,'timestamp':now_iso(),'payload':payload}

#Protocol ver. 0.4 Debug

class ReplayGuard:
    """
    Tracks seen message_ids within a session to reject replayed
    envelopes, and rejects envelopes whose timestamp is too far from
    "now" (clock-skew tolerance).

    The set is deliberately bounded. An accepted envelope is only
    replayable while it is inside the clock-skew window, so IDs older
    than that window can be evicted. A hard maximum is enforced as a
    defense-in-depth memory bound; when the limit is reached while all
    entries are still protected, new messages are rejected rather than
    evicting a still-replayable ID.

    The snapshot()/restore() pair lets the caller include replay state
    in an application-level transaction. In particular, if an encrypted
    batch is accepted by the replay/idempotency layers but the ledger
    commit later fails, the message must remain retryable.
    """

    def __init__(self, max_clock_skew_seconds: int = 300, max_seen_ids: int = 4096):
        if max_clock_skew_seconds <= 0:
            raise ValueError("max_clock_skew_seconds must be positive")
        if max_seen_ids <= 0:
            raise ValueError("max_seen_ids must be positive")
        self.max_clock_skew_seconds = max_clock_skew_seconds
        self.max_seen_ids = max_seen_ids
        # message_id -> monotonic receipt time. OrderedDict gives cheap
        # FIFO eviction while keeping membership O(1).
        self.seen_ids = OrderedDict()

    def _prune(self, now_mono: Optional[float] = None) -> None:
        now_mono = time.monotonic() if now_mono is None else now_mono
        cutoff = now_mono - self.max_clock_skew_seconds

        # Only evict entries whose replay-protection lifetime has expired.
        # Do NOT evict a still-fresh ID merely because max_seen_ids has been
        # reached: doing so would make a valid message replayable inside the
        # clock-skew window.
        while self.seen_ids:
            _, seen_at = next(iter(self.seen_ids.items()))
            if seen_at >= cutoff:
                break
            self.seen_ids.popitem(last=False)

    def check_and_record(self, message: dict) -> None:
        message_id = message.get("message_id")
        if not message_id:
            raise PairingError("Message is missing message_id.")

        # Replay-protected protocol messages must carry a valid timestamp.
        # Without this requirement, an attacker can replay a timestamp-less
        # message indefinitely once its message_id has been evicted.
        timestamp_str = message.get("timestamp")
        if not isinstance(timestamp_str, str) or not timestamp_str.strip():
            raise PairingError("Message is missing timestamp.")

        try:
            ts = datetime.fromisoformat(timestamp_str)
        except (TypeError, ValueError):
            raise PairingError("Message has an invalid timestamp.")

        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        skew = abs((datetime.now(timezone.utc) - ts).total_seconds())
        if skew > self.max_clock_skew_seconds:
            raise PairingError(
                f"Message timestamp is outside the allowed window ({skew:.0f}s skew)."
            )

        now_mono = time.monotonic()
        self._prune(now_mono)

        if message_id in self.seen_ids:
            raise PairingError(f"Replayed message detected (message_id={message_id}).")

        # max_seen_ids is now an admission limit rather than an eviction
        # mechanism. If all retained IDs are still inside the replay window,
        # accepting another message would require evicting a protected ID.
        # Rejecting instead preserves replay safety at the cost of temporary
        # back-pressure under extreme message rates.
        if len(self.seen_ids) >= self.max_seen_ids:
            raise PairingError(
                "ReplayGuard capacity reached; retry after older message IDs expire."
            )

        self.seen_ids[message_id] = now_mono

    def snapshot(self) -> OrderedDict:
        """Return an exact copy suitable for application-level rollback."""
        return OrderedDict(self.seen_ids)

    def restore(self, snapshot: OrderedDict) -> None:
        """Restore a snapshot previously returned by snapshot()."""
        self.seen_ids = OrderedDict(snapshot)
        self._prune()


def decrypt_and_validate_message(
    key: bytes,
    token: str,
    expected_message_type: str,
    expected_session_id: str,
    replay_guard: Optional["ReplayGuard"] = None,
) -> dict:
    """
    Decrypt a message and verify it's actually the envelope it claims to
    be, rather than just trusting whatever message_type/session_id the
    decrypted payload happens to say. Previously nothing checked these
    fields at all after decryption -- a decryptable envelope of any
    message_type could be processed as if it were a different one, as
    long as its payload happened to contain the right-shaped keys (see
    process_identity_announcement, which used to skip this check
    entirely). Also runs replay/staleness checks if a ReplayGuard is
    given. Returns the full decrypted message (not just the payload).
    """
    message = decrypt_message(key, token)
    if message.get("protocol") != PROTOCOL:
        raise PairingError(f"Unexpected protocol '{message.get('protocol')}'.")
    if message.get("version") != VERSION:
        raise PairingError(f"Unsupported protocol version {message.get('version')}.")
    if message.get("message_type") != expected_message_type:
        raise PairingError(f"Expected message_type '{expected_message_type}', got '{message.get('message_type')}'.")
    if message.get("session_id") != expected_session_id:
        raise PairingError("Message session_id does not match the current session.")
    if replay_guard is not None:
        replay_guard.check_and_record(message)
    return message
class Identity:
    def __init__(self, private=None):
        self.private = private or ed25519.Ed25519PrivateKey.generate()
        self.public = self.private.public_key()

    @classmethod
    def from_private_bytes(cls, private_bytes: bytes) -> "Identity":
        return cls(ed25519.Ed25519PrivateKey.from_private_bytes(private_bytes))

    @property
    def public_bytes(self):
        return self.public.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)

    @property
    def private_bytes(self):
        return self.private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )

    def sign(self,data): return self.private.sign(data)

    def verify(self,sig,data):
        try: self.public.verify(sig,data); return True
        except Exception: return False


def load_or_create_identity(path: str) -> Identity:
    """Load a persistent desktop identity or create it on first use.

    The long-term Ed25519 private key is stored as raw 32-byte key material
    with owner-only permissions. A stable identity is required for reconnect:
    generating a new identity every server start would invalidate the pinned
    device relationship.
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if len(raw) != 32:
            raise ValueError("identity file has invalid length")
        return Identity.from_private_bytes(raw)
    except FileNotFoundError:
        identity = Identity()
        create_secure_file(path, identity.private_bytes)
        return identity
    except (OSError, ValueError) as e:
        raise PairingError(f"Unable to load desktop identity: {e}")
def make_pairing_code(): return f'{secrets.randbelow(1_000_000):06d}'
def derive_session_key(shared_secret, session_id):
    """
    HKDF-SHA256, not a bare SHA256(secret + session_id). A raw hash over
    concatenated bytes isn't a real KDF -- HKDF's extract-then-expand
    construction has provable security properties a homemade
    concatenation doesn't, and using it also means if this ever needs to
    derive multiple independent subkeys from the same shared secret
    later, that's already supported via `info`.
    """
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None,
        info=b"cogni-transfer-session-key-v1:" + session_id.encode(),
    ).derive(shared_secret)
def encrypt_message(key,message):
    nonce=os.urandom(12); return b64(nonce+AESGCM(key).encrypt(nonce,canonical_json(message),None))
def decrypt_message(key,token):
    raw=unb64(token); return json.loads(AESGCM(key).decrypt(raw[:12],raw[12:],None))
@dataclass
class PendingTransaction:
    """
    Documents the wire shape only -- never actually constructed anywhere
    in this codebase (verify_transaction and friends all operate on raw
    dicts from JSON). `lines` is the current format; the legacy flat
    debit_account/credit_account/amount shape is still accepted by
    verify_transaction for backward compatibility but is intentionally
    NOT reflected here, since new code should target `lines`.
    """
    transaction_id: str
    date: str
    description: str
    lines: list  # List[Dict[str, str]], each {"account", "type": "DEBIT"|"CREDIT", "amount"}
    note: str = ''
    def as_dict(self): return self.__dict__.copy()


_AMOUNT_RE = re.compile(r"-?\d{1,15}(\.\d{1,2})?")


def parse_amount_strict(value) -> Decimal:
    """
    Parse a transaction amount as Decimal, rejecting anything that isn't
    a plain, finite, positive decimal number with at most 2 fractional
    digits. `float(tx['amount'])` used to be used here instead, which
    silently accepted 'nan' and 'inf' as valid amounts (float NaN
    comparisons are always False in IEEE 754, so `nan <= 0` doesn't
    trigger the "must be positive" rejection at all) and 'inf'/'1e10'
    scientific notation as huge but "valid" transactions. Raises
    ValueError with a clear reason on anything invalid.
    """
    if not isinstance(value, str):
        raise ValueError("Amount must be provided as a string.")
    value = value.strip()
    if not _AMOUNT_RE.fullmatch(value):
        raise ValueError(f"Amount '{value}' is not a plain decimal number with at most 2 decimal places.")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"Amount '{value}' could not be parsed.")
    if amount <= 0:
        raise ValueError("Amount must be positive.")
    return amount


class DesktopLedger:
    def __init__(self): self.accepted={}
    def verify_transaction(self, tx):
        """Validate a transaction payload. Accepts two shapes:
          - New: {"lines": [{account, type, amount}, ...]}
          - Legacy: {"amount", "debit_account", "credit_account"}
        For multi-line transactions, verifies:
          - At least 2 lines
          - Each line has account/type/amount
          - Amounts are valid positive decimals (via parse_amount_strict)
          - Total debits == total credits
          - At least 2 distinct accounts are involved
        """
        if not tx.get("transaction_id"):
            return False, "Missing transaction ID."
        if "date" not in tx:
            return False, "Missing date."
        if "description" not in tx:
            return False, "Missing description."

        raw_lines = tx.get("lines")
        if raw_lines is not None:
            if not isinstance(raw_lines, list) or len(raw_lines) < 2:
                return False, "Transaction must have at least 2 lines."
            total_dr = Decimal("0")
            total_cr = Decimal("0")
            accounts_in_tx = set()
            for line in raw_lines:
                if not isinstance(line, dict):
                    return False, "Transaction line is not an object."
                account = line.get("account")
                entry_type = (line.get("type") or "").upper()
                amount_str = line.get("amount", "")
                if not account:
                    return False, "Transaction line missing 'account'."
                if entry_type not in ("DEBIT", "CREDIT"):
                    return False, f"Invalid line type: {entry_type!r}"
                try:
                    amount = parse_amount_strict(amount_str)
                except ValueError as e:
                    return False, f"Invalid line amount: {e}"
                if entry_type == "DEBIT":
                    total_dr += amount
                else:
                    total_cr += amount
                accounts_in_tx.add(account)
            if total_dr != total_cr:
                return False, (
                    f"Transaction out of balance "
                    f"(debits={total_dr}, credits={total_cr})."
                )
            if len(accounts_in_tx) < 2:
                return False, "Transaction must involve at least 2 distinct accounts."
            return True, "Verified."

        # Legacy 2-line format
        req = {"transaction_id", "date", "description", "amount", "debit_account", "credit_account"}
        if not req.issubset(tx):
            return False, "Missing required transaction fields."
        if tx["debit_account"] == tx["credit_account"]:
            return False, "Debit and credit accounts must differ."
        try:
            parse_amount_strict(tx["amount"])
        except ValueError as e:
            return False, str(e)
        return True, "Verified."

    @staticmethod
    def _transactions_equivalent(a, b) -> bool:
        """
        True if two transaction dicts represent the same real
        transaction. Handles both shapes:
          - New multi-line format: compares the `lines` list itself
            (account, type, amount per line, in order).
          - Legacy flat format: compares debit_account/credit_account/amount.
        Checking only the legacy fields regardless of shape was a real bug:
        for two `lines`-format transactions, debit_account/credit_account/
        amount are all simply absent (None) on BOTH sides, so those
        comparisons trivially passed no matter what the lines actually
        contained -- meaning two genuinely different transactions sharing
        a date and description would be treated as identical retries, and
        a real edit resent under the same transaction_id could be
        silently discarded instead of correctly flagged as a conflict.
        Amounts are compared as parsed Decimals (not raw strings) so
        '5.0' and '5.00' count as equal; falls back to string equality if
        either fails to parse, since an invalid amount can't be
        meaningfully compared as a number.
        """
        if a.get('date') != b.get('date') or a.get('description') != b.get('description'):
            return False

        a_lines, b_lines = a.get('lines'), b.get('lines')

        # If one has 'lines' and the other doesn't, they are fundamentally different shapes.
        if (a_lines is None) != (b_lines is None):
            return False

        if a_lines is not None:
            # --- NEW MULTI-LINE FORMAT ---
            if not isinstance(a_lines, list) or not isinstance(b_lines, list):
                return False
            if len(a_lines) != len(b_lines):
                return False

            def normalize_line(line):
                if not isinstance(line, dict): return None
                try:
                    acc = line.get('account')
                    typ = (line.get('type') or '').upper()
                    # parse_amount_strict ensures "5.0" and "5.00" are treated equally
                    amt = parse_amount_strict(line.get('amount', ''))
                    return (acc, typ, amt)
                except Exception:
                    return None # Invalid amount/format means it's not a valid retry

            a_norm = [normalize_line(l) for l in a_lines]
            b_norm = [normalize_line(l) for l in b_lines]

            if None in a_norm or None in b_norm:
                return False

            # CRITICAL FIX: Sort to make the comparison ORDER-INDEPENDENT.
            # (e.g., Phone sends [Groceries, Gas], Desktop receives [Gas, Groceries])
            return sorted(a_norm) == sorted(b_norm)


        # Legacy flat format
        if a.get('debit_account') != b.get('debit_account') or a.get('credit_account') != b.get('credit_account'):
            return False
        try:
            return parse_amount_strict(a.get('amount')) == parse_amount_strict(b.get('amount'))
        except ValueError:
            return a.get('amount') == b.get('amount')

    def accept_batch(self,transactions):
        accepted=[]; rejected=[]; conflicted=[]
        for tx in transactions:
            txid=tx.get('transaction_id')
            if not txid: rejected.append({'transaction_id':None,'reason':'Missing transaction ID'}); continue
            if txid in self.accepted:
                if self._transactions_equivalent(self.accepted[txid], tx):
                    accepted.append(txid)  # genuine idempotent retry, identical content
                else:
                    # Same ID, different content -- the original is kept
                    # (never silently overwritten), but the caller is
                    # told explicitly rather than this looking identical
                    # to a normal accepted retry.
                    conflicted.append({'transaction_id': txid, 'reason': 'Resent with different content than originally accepted; the original was kept.'})
                continue
            ok,reason=self.verify_transaction(tx)
            if ok: self.accepted[txid]=tx; accepted.append(txid)
            else: rejected.append({'transaction_id':txid,'reason':reason})
        return {'accepted':accepted,'rejected':rejected,'conflicted':conflicted}
class PhonePendingStore:
    def __init__(self): self.pending={}
    def add(self,tx): self.pending[tx.transaction_id]=tx
    def batch(self): return [tx.as_dict() for tx in self.pending.values()]
    def acknowledge(self,accepted_ids):
        for txid in accepted_ids: self.pending.pop(txid,None)
class PairingError(Exception):
    """Raised when a pairing attempt is rejected -- either the codes
    didn't match, someone tampered with the exchange, or the attempt
    limiter is currently locked out."""

class PairingAttemptLimiter:
    """
    Persisted exponential-backoff rate limiter for failed pairing
    attempts. A short numeric pairing code (~1,000,000 possibilities) is
    only safe against *online* guessing, not offline brute force -- that
    safety entirely depends on capping how many live attempts an
    attacker can make. An in-memory-only counter can be trivially reset
    by restarting the process, so state is persisted to disk, the same
    lesson already applied to this app's passphrase-unlock lockout.
    """

    def __init__(self, state_path: str = DEFAULT_PAIRING_LOCKOUT_PATH, base_lock_seconds: int = 5):
        self.state_path = state_path
        self.base_lock_seconds = base_lock_seconds

    def _load(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {"attempts": int(data.get("attempts", 0)), "locked_until": float(data.get("locked_until", 0))}
        except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError):
            return {"attempts": 0, "locked_until": 0}

    def _save(self, attempts: int, locked_until: float) -> None:
        try:
            atomic_secure_write(
                self.state_path,
                json.dumps({"attempts": attempts, "locked_until": locked_until}).encode(),
            )
        except OSError:
            pass  # best-effort; a failure to persist shouldn't crash pairing

    def check(self) -> None:
        """Raises PairingError if currently locked out from a prior run of failures."""
        state = self._load()
        remaining = state["locked_until"] - time.time()
        if remaining > 0:
            raise PairingError(f"Too many failed pairing attempts. Try again in {int(remaining) + 1} seconds.")

    def record_failure(self) -> None:
        state = self._load()
        attempts = state["attempts"] + 1
        locked_until = time.time() + self.base_lock_seconds * attempts
        self._save(attempts, locked_until)

    def record_success(self) -> None:
        try:
            os.remove(self.state_path)
        except OSError:
            pass


CONFIRM_CONTEXT = b"cogni-transfer-pairing-confirm-v1"


def _confirmation_tag(session_key: bytes, session_id: str, role: str) -> bytes:
    """
    Key-confirmation MAC. SPAKE2's finish() never raises on a wrong
    password -- a mismatched code silently produces a different, useless
    key on each side with no error at all (verified empirically in
    tests/test_pairing_handshake.py). Explicit confirmation is what
    actually detects a mismatch or a man-in-the-middle, rather than the
    failure surfacing later as a mysterious decrypt error on the first
    real message. Tags are bound to `role` (whichever side produced
    them) so an attacker can't just bounce a party's own tag back at it.
    """
    return hmac.new(session_key, CONFIRM_CONTEXT + session_id.encode() + role.encode(), hashlib.sha256).digest()

def _spake2_debug(label: str, data: bytes) -> None:
    print(
        f"SPAKE2 DEBUG - {label} SHA256: "
        f"{hashlib.sha256(data).hexdigest()}"
    )

class PairingHandshake:
    """
    Authenticated key exchange using SPAKE2, a Password-Authenticated
    Key Exchange (PAKE): both devices must be given the same pairing
    code (e.g. the phone displays it, the user types it into the
    desktop app) before starting. Unlike a plain Diffie-Hellman
    exchange, the derived key is cryptographically bound to the code --
    an attacker relaying/substituting messages without knowing the code
    ends up with a key that fails confirmation, rather than silently
    succeeding (see the MITM simulation in
    tests/test_pairing_handshake.py, which fails the way it should
    against this design).

    Usage (both sides run the same sequence, with role="phone" on one
    end and role="desktop" on the other):

        h = PairingHandshake(pairing_code, session_id, role="phone", limiter=...)
        my_msg = h.start()                            # send my_msg to the peer
        h.derive(peer_msg)                             # after receiving the peer's start() message
        my_tag = h.confirmation_tag()                   # send my_tag to the peer
        session_key = h.confirm_and_verify(peer_tag)     # after receiving the peer's tag

    `session_key` is only trustworthy after confirm_and_verify()
    succeeds -- get_session_key() raises if called before that, as a
    guard against a caller accidentally using an unconfirmed key.
    """

    def __init__(self, pairing_code: str, session_id: str, role: str, limiter: Optional[PairingAttemptLimiter] = None):
        if role not in ("phone", "desktop"):
            raise ValueError("role must be 'phone' or 'desktop'")
        self.role = role
        self.peer_role = "desktop" if role == "phone" else "phone"
        self.session_id = session_id
        self.limiter = limiter
        self._spake = SPAKE2_A(pairing_code.encode()) if role == "phone" else SPAKE2_B(pairing_code.encode())
        self._session_key: Optional[bytes] = None
        self._confirmed = False

    def start(self) -> bytes:
        if self.limiter:
            self.limiter.check()
        return self._spake.start()


    def derive(self, peer_msg: bytes) -> None:
        """
        Call after receiving the peer's start() message. Malformed input
        (not just a wrong-but-well-formed password) is converted to a
        clean PairingError and counted against the rate limiter --
        previously a garbage/corrupt peer_msg raised a raw, undocumented
        library exception (spake2.OffSides) here AND was never recorded
        as a failed attempt, meaning an attacker could send malformed
        messages indefinitely without ever tripping the persisted
        lockout that a well-formed wrong-password guess would trigger.
        """
        try:
            raw_key = self._spake.finish(peer_msg)
        except Exception as e:
            if self.limiter:
                self.limiter.record_failure()
            raise PairingError(f"Malformed or invalid pairing message: {e}")

        self._session_key = derive_session_key(raw_key, self.session_id)

    def confirmation_tag(self) -> bytes:
        if self._session_key is None:
            raise PairingError("derive() must be called before confirmation_tag().")
        return _confirmation_tag(self._session_key, self.session_id, self.role)

    def confirm_and_verify(self, peer_tag: bytes) -> bytes:
        """
        Verifies the peer's confirmation tag. Raises PairingError (and
        records a failed attempt against the limiter) if it doesn't
        match -- which happens if the codes didn't match, or if a
        man-in-the-middle interfered with the exchange. Returns the now-
        trusted session key on success.
        """
        if self._session_key is None:
            raise PairingError("derive() must be called before confirm_and_verify().")
        expected = _confirmation_tag(self._session_key, self.session_id, self.peer_role)
        if not hmac.compare_digest(expected, peer_tag):
            if self.limiter:
                self.limiter.record_failure()
            raise PairingError("Pairing confirmation failed -- the codes did not match, or the connection may have been tampered with.")
        if self.limiter:
            self.limiter.record_success()
        self._confirmed = True
        return self._session_key

    def get_session_key(self) -> bytes:
        if not self._confirmed:
            raise PairingError("Session key is not yet confirmed -- call confirm_and_verify() first.")
        return self._session_key


# ---------------- Identity pinning + signed-ephemeral-key reconnect ----------------
#
# PairingHandshake (above) requires a human to type in a pairing code
# every time -- fine for the *first* pairing, annoying for every
# subsequent sync. The pattern used here is the same one SSH uses for
# known_hosts: the first time two devices pair (over the
# PAKE-authenticated channel, so it's safe from MITM), they exchange and
# pin each other's long-term Ed25519 public key. On every later
# reconnect, each side proves it still holds the matching private key by
# signing a fresh ephemeral X25519 key with it, and the peer verifies
# that signature against the key pinned the first time -- never against
# a freshly-presented one, since trusting whatever key shows up would
# defeat the entire point of pinning.

def identity_fingerprint(public_key_bytes: bytes) -> str:
    """
    Stable identifier for an Identity, derived from its actual public
    key. Deliberately NOT the same as the free-text `device_id` field in
    envelope() -- that field is self-asserted by whoever sends the
    message and isn't cryptographically bound to anything, so it must
    never be used as a trust/lookup key.
    """
    return hashlib.sha256(public_key_bytes).hexdigest()[:16]

class TrustStore:
    """
    Persisted "known peer" identity pins, analogous to SSH's
    known_hosts. A peer's long-term Ed25519 public key is pinned here
    once, immediately after a successful PairingHandshake; reconnects
    thereafter verify against the pinned key rather than trusting
    whatever key is presented at connect time.
    """

    def __init__(self, path: str = DEFAULT_TRUST_STORE_PATH):
        self.path = path

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict) -> None:
        try:
            atomic_secure_write(
                self.path,
                json.dumps(data, indent=2).encode("utf-8"),
            )
        except OSError:
            pass

    def pin(self, fingerprint: str, public_key_b64: str, label: str = "") -> None:
        data = self._load()
        data[fingerprint] = {"public_key": public_key_b64, "label": label, "pinned_at": now_iso()}
        self._save(data)

    def get_public_key(self, fingerprint: str) -> Optional[bytes]:
        entry = self._load().get(fingerprint)
        if not entry:
            return None
        return unb64(entry["public_key"])

    def is_known(self, fingerprint: str) -> bool:
        return fingerprint in self._load()

    def remove(self, fingerprint: str) -> None:
        data = self._load()
        data.pop(fingerprint, None)
        self._save(data)

    def list_known(self) -> dict:
        return self._load()


def build_identity_announcement(session_key: bytes, session_id: str, role: str, identity: Identity, label: str = "") -> str:
    """
    Encrypt this side's long-term public key to announce over an
    already-authenticated session (i.e. only ever call this after a
    PairingHandshake has confirmed). Send the returned token to the peer.
    """
    payload = {
        "public_key": b64(identity.public_bytes),
        "fingerprint": identity_fingerprint(identity.public_bytes),
        "label": label,
    }
    return encrypt_message(session_key, envelope("identity_announce", session_id, role, payload))


def process_identity_announcement(
    session_key: bytes,
    token: str,
    trust_store: TrustStore,
    session_id: Optional[str] = None,
    replay_guard: Optional["ReplayGuard"] = None,
    expected_fingerprint: Optional[str] = None,
) -> str:
    """
    Decrypt a peer's identity announcement and pin their public key.
    Returns the peer's fingerprint. Raises PairingError if: the claimed
    fingerprint doesn't actually match the provided public key (a sender
    lying about its own fingerprint); the decrypted envelope isn't
    actually an 'identity_announce' message (previously unchecked --
    any decryptable envelope with the right-shaped payload used to be
    silently accepted regardless of its real message_type); the optional
    expected_fingerprint does not match the announced identity; or, if
    session_id/replay_guard are given, the envelope doesn't match the
    expected session or has already been seen.

    The expected-fingerprint check is deliberately performed BEFORE
    TrustStore.pin(). This makes identity validation atomic from the
    trust-store's perspective: a rejected identity can never be persisted.
    """
    if session_id is not None:
        msg = decrypt_and_validate_message(session_key, token, "identity_announce", session_id, replay_guard)
    else:
        msg = decrypt_message(session_key, token)
        if msg.get("message_type") != "identity_announce":
            raise PairingError(f"Expected message_type 'identity_announce', got '{msg.get('message_type')}'.")
    payload = msg["payload"]
    public_key_b64 = payload["public_key"]
    claimed_fingerprint = payload["fingerprint"]
    actual_fingerprint = identity_fingerprint(unb64(public_key_b64))
    if actual_fingerprint != claimed_fingerprint:
        raise PairingError("Identity announcement's fingerprint does not match its own public key.")

    # IMPORTANT: reject a conflicting identity BEFORE persisting anything.
    # The previous ordering called trust_store.pin() first, allowing a
    # rejected second identity to remain in the persisted trust store.
    if expected_fingerprint is not None and expected_fingerprint != actual_fingerprint:
        raise PairingError("A different peer identity is already pinned for this session.")

    trust_store.pin(actual_fingerprint, public_key_b64, label=payload.get("label", ""))
    return actual_fingerprint


RECONNECT_CONTEXT = b"cogni-transfer-reconnect-v1"


def _signable_reconnect_material(ephemeral_public_bytes: bytes, session_id: str, role: str) -> bytes:
    # Binding session_id and role into the signed material means a
    # captured signature can't be replayed for a different session (the
    # signed bytes wouldn't match), and can't be reflected back at its
    # own sender pretending to be the peer (role differs).
    return RECONNECT_CONTEXT + session_id.encode() + role.encode() + ephemeral_public_bytes


class ReconnectHandshake:
    """
    Fast reconnect for a previously-paired (trust-pinned) device pair --
    no pairing code needed. Each side generates a fresh X25519 ephemeral
    keypair for this session and signs it with its long-term Ed25519
    identity key; the peer verifies that signature against the ALREADY-
    PINNED public key from TrustStore. Only after signature verification
    succeeds does ECDH proceed to derive the session key, so an attacker
    can't substitute their own ephemeral key without forging a signature
    they can't produce without the pinned private key -- unlike the
    original unauthenticated Handshake this replaces.

    Raises PairingError immediately if the peer's fingerprint isn't in
    the trust store; the caller should fall back to a full
    PairingHandshake (re-pairing with a code) in that case.

    Usage mirrors PairingHandshake:

        r = ReconnectHandshake(my_identity, peer_fingerprint, session_id, role="phone", trust_store=store)
        my_msg = r.start()                       # send to peer
        r.derive(peer_msg)                        # after receiving the peer's start() message
        my_tag = r.confirmation_tag()               # send to peer
        session_key = r.confirm_and_verify(peer_tag)  # after receiving the peer's tag
    """

    def __init__(
        self,
        identity: Identity,
        peer_fingerprint: str,
        session_id: str,
        role: str,
        trust_store: TrustStore,
        limiter: Optional[PairingAttemptLimiter] = None,
    ):
        if role not in ("phone", "desktop"):
            raise ValueError("role must be 'phone' or 'desktop'")
        self.identity = identity
        self.peer_fingerprint = peer_fingerprint
        self.session_id = session_id
        self.role = role
        self.peer_role = "desktop" if role == "phone" else "phone"
        self.trust_store = trust_store
        self.limiter = limiter
        self._ephemeral = x25519.X25519PrivateKey.generate()
        self._session_key: Optional[bytes] = None
        self._confirmed = False

        peer_public_key_bytes = trust_store.get_public_key(peer_fingerprint)
        if peer_public_key_bytes is None:
            raise PairingError(f"No pinned identity for peer '{peer_fingerprint}' -- pair with a code first.")
        self._peer_verify_key = ed25519.Ed25519PublicKey.from_public_bytes(peer_public_key_bytes)

    def start(self) -> dict:
        if self.limiter:
            self.limiter.check()
        ephemeral_public_bytes = self._ephemeral.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        signature = self.identity.sign(_signable_reconnect_material(ephemeral_public_bytes, self.session_id, self.role))
        return {"ephemeral_public": b64(ephemeral_public_bytes), "signature": b64(signature)}

    def derive(self, peer_msg: dict) -> None:
        """
        Call after receiving the peer's start() message. Any failure --
        a bad signature, missing keys, invalid base64, or anything else
        malformed about peer_msg -- is converted to a clean PairingError
        and counted against the rate limiter. Previously, malformed
        input (as opposed to a well-formed-but-wrong signature) raised
        raw KeyError/binascii exceptions here and bypassed the limiter
        entirely, the same gap as in PairingHandshake.derive().
        """
        try:
            peer_ephemeral_bytes = unb64(peer_msg["ephemeral_public"])
            signature = unb64(peer_msg["signature"])
            signable = _signable_reconnect_material(peer_ephemeral_bytes, self.session_id, self.peer_role)
            self._peer_verify_key.verify(signature, signable)
        except Exception:
            if self.limiter:
                self.limiter.record_failure()
            raise PairingError("Signature verification failed on reconnect -- the peer's ephemeral key was not signed by the pinned identity.")
        peer_ephemeral_public = x25519.X25519PublicKey.from_public_bytes(peer_ephemeral_bytes)
        shared_secret = self._ephemeral.exchange(peer_ephemeral_public)
        self._session_key = derive_session_key(shared_secret, self.session_id)

    def confirmation_tag(self) -> bytes:
        if self._session_key is None:
            raise PairingError("derive() must be called before confirmation_tag().")
        return _confirmation_tag(self._session_key, self.session_id, self.role)

    def confirm_and_verify(self, peer_tag: bytes) -> bytes:
        if self._session_key is None:
            raise PairingError("derive() must be called before confirm_and_verify().")
        expected = _confirmation_tag(self._session_key, self.session_id, self.peer_role)
        if not hmac.compare_digest(expected, peer_tag):
            if self.limiter:
                self.limiter.record_failure()
            raise PairingError("Reconnect confirmation failed.")
        if self.limiter:
            self.limiter.record_success()
        self._confirmed = True
        return self._session_key

    def get_session_key(self) -> bytes:
        if not self._confirmed:
            raise PairingError("Session key is not yet confirmed -- call confirm_and_verify() first.")
        return self._session_key
