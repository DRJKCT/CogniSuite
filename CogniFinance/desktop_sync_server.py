"""
CogniSync desktop HTTP server.
Version 1.1
For CogniFinance ver 1.95

Ephemeral LAN server that receives encrypted transaction batches from the
Android companion app (MainActivity_dynamic_MN_timestamp_updated.kt) over the
cogni-transfer protocol and posts them into the desktop GeneralLedger.

Integrates:
  Phase 1 - request size cap, read timeout, token+session auth on every
            endpoint, module-level imports, LAN (0.0.0.0) binding.
  Phase 2 - per-transaction failure logging, robust LAN IP detection,
            atomic batch commit, account-type inference.
  Phase 3 - dispatch-table routing, SyncContext dependency injection,
            /api/status endpoint, delta-sync cursor scaffolding.
  Phase 4 - PairingAttemptLimiter, ReplayGuard + envelope validation,
            DesktopLedger idempotent acceptance, strict amount parsing.
  v0.3    - unknown-account hold/reject with UI hook (no auto-creation).
  v0.6    - transactional ReplayGuard rollback, bounded replay memory,
            persistent desktop identity + trust-pinned reconnect handshake.

v0.7 fixes:
  * held transactions stored in a list (no None-key collision when multiple
    transactions lack a transaction_id).
  * _state_lock guards shared state (unknown_accounts, held_transactions,
    desktop_ledger.accepted, handshake/reconnect transitions) against races
    between the HTTP handler thread and the desktop UI thread.
  * /api/reconnect rejects double-calls when a reconnect is already in
    progress for the session.

Fail-close:
  _handle_sync and reprocess_held require the ledger to support atomic
  transactions (begin/commit/rollback). If any is missing, the sync is
  REFUSED rather than proceeding without rollback capability.

Minimal disclosure (phone-safe messaging):
  The desktop process can read an encrypted ledger database holding sensitive
  financial data. Exception messages, DB/SQL errors, file paths, and schema
  details must NEVER be transmitted to the phone -- even over the
  authenticated, encrypted channel. Every string the phone may receive is a
  fixed constant defined below; full detail is written to the desktop log.

Protocol alignment notes:
  * Android's Spake2Ed25519 prepends a role byte ('A'/'B') to SPAKE2
    messages; Python's `spake2` library does not. This handler strips the
    inbound role byte and prepends 'B' on the outbound message.
  * PairingHandshake.derive() returns None and derives the key internally,
    so start() must be called first to obtain the desktop's pB.
"""

from __future__ import annotations

import hmac
import hashlib
import base64
import json
import logging
import logging.handlers
import os
import socket
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable, Optional

from cryptography.exceptions import InvalidTag

from protocol import (
    PROTOCOL,
    VERSION,
    DesktopLedger,
    PairingAttemptLimiter,
    PairingError,
    PairingHandshake,
    ReplayGuard,
    Identity,
    TrustStore,
    ReconnectHandshake,
    build_identity_announcement,
    process_identity_announcement,
    load_or_create_identity,
    identity_fingerprint,
    DEFAULT_TRUST_STORE_PATH,
    DEFAULT_IDENTITY_PATH,
    decrypt_and_validate_message,
    encrypt_message,
    envelope,
    parse_amount_strict,
)
from ledger import GeneralLedger, money

from config import (
    SYNC_LOG_PATH, PAIRED_PHONE_PATH,
    SYNC_IDENTITY_PATH as DEFAULT_IDENTITY_PATH,
    TRUST_STORE_PATH as DEFAULT_TRUST_STORE_PATH,
    atomic_secure_write,
)

# Optional dependency for LAN IP enumeration; the module still loads without it.
try:
    import netifaces
    _HAS_NETIFACES = True
except ImportError:
    netifaces = None
    _HAS_NETIFACES = False

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = str(SYNC_LOG_PATH)


def configure_file_logging(path: str = DEFAULT_LOG_PATH, level: int = logging.INFO) -> None:
    """
    Attach a rotating file handler so sync activity (including every
    "sync.rejected"/"sync.conflicted" reason, which is deliberately never
    sent to the phone -- see PHONE_REASON_REJECTED) survives after the
    terminal window that launched the server is closed.

    Idempotent: safe to call more than once (e.g. if the host app also
    configures logging) -- it won't attach a duplicate handler for the
    same path.

    Rotates at 5 MB with 3 backups kept, so this can't grow unbounded.
    The file is created with owner-only permissions (0600), matching the
    other .cognibudget_* state files this app keeps in the home directory,
    since sync logs can include account names and transaction metadata.
    """
    root = logging.getLogger()
    abs_path = os.path.abspath(path)
    for h in root.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler) and getattr(h, "_cognibudget_path", None) == abs_path:
            return  # already configured

    # Create the file up front (if missing) so permissions can be locked
    # down before any log content -- including account names -- is written.
    file_existed = os.path.exists(abs_path)
    handler = logging.handlers.RotatingFileHandler(
        abs_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    if not file_existed:
        try:
            os.chmod(abs_path, 0o600)
        except OSError:
            pass
    handler._cognibudget_path = abs_path  # tag for the idempotency check above
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    ))
    root.addHandler(handler)
    if root.level > level or root.level == logging.NOTSET:
        root.setLevel(level)


# --- Tunables ----------------------------------------------------------------
MAX_REQUEST_SIZE = 10 * 1024 * 1024      # 10 MB request cap
READ_TIMEOUT_SECONDS = 30                # per-request socket read timeout
DEFAULT_PAIRED_PHONE_PATH = str(PAIRED_PHONE_PATH)

# --- Phone-safe response strings ---------------------------------------------
# SECURITY: every string the phone may receive is defined here. Exception
# text, database/SQL errors, file paths, and schema details are logged on the
# desktop only and never embedded in these. Audit this block to review the
# entire phone-visible surface.
PHONE_ERR_HANDSHAKE_REJECTED  = "Handshake rejected. Check the pairing code and try again."
PHONE_ERR_HANDSHAKE_FAILED    = "Handshake failed. See desktop logs for details."
PHONE_ERR_CONFIRM_REJECTED    = "Confirmation rejected. Check the pairing code and try again."
PHONE_ERR_CONFIRM_FAILED      = "Confirmation failed. See desktop logs for details."
PHONE_ERR_RECONNECT_REJECTED  = "Reconnect rejected. Pair the device again."
PHONE_ERR_RECONNECT_FAILED    = "Reconnect failed. See desktop logs for details."
PHONE_ERR_IDENTITY_REJECTED   = "Identity announcement rejected."
PHONE_ERR_SYNC_REJECTED       = "Sync rejected. See desktop logs for details."
PHONE_ERR_SYNC_FAILED         = "Sync failed. See desktop logs for details."
# Generic reason used for every rejected/conflicted item in the ACK.
PHONE_REASON_REJECTED         = "Rejected. See desktop logs for details."

# Account-type inference from GnuCash-style naming (matches the Android
# defaults "Expenses:Groceries" / "Liabilities:CreditCard"). Repurposed in v0.3
# as a *suggested default* for the desktop account-definition dropdown.
ACCOUNT_TYPE_PATTERNS = [
    ("Assets:", "Asset"),
    ("Liabilities:", "Liability"),
    ("Equity:", "Equity"),
    ("Income:", "Income"),
    ("Revenue:", "Income"),
    ("Expenses:", "Expense"),
    ("Expense:", "Expense"),
]

# Account types offered in the desktop "define account" dropdown.
ACCOUNT_TYPES = ["Asset", "Liability", "Equity", "Revenue", "Expense"]


# --- Helpers ------------------------------------------------------------------
def _infer_account_type(account_name: str) -> str:
    for prefix, acct_type in ACCOUNT_TYPE_PATTERNS:
        if account_name.startswith(prefix):
            return acct_type
    return "Expense"


def _b64url_decode(token: str) -> bytes:
    """Decode URL-safe base64, tolerating missing padding (Android uses NO_WRAP)."""
    token = token.strip()
    token += "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(token)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _require_txn_api(gl) -> None:
    """Fail-close guard: the ledger MUST support atomic batch transactions.

    Raises RuntimeError naming any missing methods instead of silently
    proceeding without atomicity (which could leave a partially-committed
    batch with no way to roll it back if a later transaction fails).
    Uses callable(), not hasattr(), so a non-callable attribute also fails.
    """
    required = ("begin_transaction", "commit_transaction", "rollback_transaction")
    missing = [name for name in required if not callable(getattr(gl, name, None))]
    if missing:
        raise RuntimeError(
            "GeneralLedger cannot post batches atomically — missing method(s): "
            f"{', '.join(missing)}. Refusing to sync rather than risk a partial commit."
        )


def _sanitize_ack_items(items: list, log_context: str) -> list:
    """Phone-safe copies of ACK rejected/conflicted items.

    Each detailed reason is logged on the desktop (keyed by transaction_id) so
    nothing is lost for debugging, but the phone receives only a fixed generic
    string. Ledger/DB internals therefore never reach the phone.
    """
    safe = []
    for item in items:
        tx_id = item.get("transaction_id")
        logger.info("%s: tx=%s reason=%r", log_context, tx_id, item.get("reason", ""))
        safe.append({"transaction_id": tx_id, "reason": PHONE_REASON_REJECTED})
    return safe


def _load_paired_phone_fingerprint(path: str = DEFAULT_PAIRED_PHONE_PATH) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f).get("fingerprint")
        return value if isinstance(value, str) and value else None
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return None


def _save_paired_phone_fingerprint(fingerprint: str, path: str = DEFAULT_PAIRED_PHONE_PATH) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"fingerprint": fingerprint}, f)
    os.replace(tmp, path)
    try:
        atomic_secure_write(
            path,
            json.dumps({"fingerprint": fingerprint}).encode("utf-8"),
        )
    except OSError:
        pass


# --- Dependency-injection context --------------------------------------------
@dataclass
class SyncContext:
    """Injected into every handler. Replaces the old `server.sync_server` hack."""
    gl: GeneralLedger
    handshake: PairingHandshake
    session_id: str
    http_token: str
    pairing_code: str
    server_port: int

    # Phase 4 security primitives (fresh per session).
    replay_guard: ReplayGuard = field(default_factory=ReplayGuard)
    desktop_ledger: DesktopLedger = field(default_factory=DesktopLedger)

    # Pairing handshake state. start() must run exactly once, so cache the desktop pB.
    cached_pb: Optional[bytes] = None
    handshake_started: bool = False

    # Persistent identity + pinned phone identity for fast reconnect.
    desktop_identity: Optional[Identity] = None
    trust_store: Optional[TrustStore] = None
    peer_fingerprint: Optional[str] = None
    reconnect_handshake: Optional[ReconnectHandshake] = None
    reconnect_started: bool = False

    # Phase 3 delta-sync cursor.
    last_sync_cursor: Optional[str] = None

    # v0.3 unknown-account handling.
    unknown_accounts: set = field(default_factory=set)      # names the user must define
    held_transactions: dict = field(default_factory=dict)   # tx_id -> tx dict, blocked on accounts
    on_unknown_accounts: Optional[Callable[[list], None]] = None  # UI notification hook

    # v0.7: guards shared mutable state against races between the HTTP handler
    # thread and the desktop UI thread (create_account / reprocess_held).
    _state_lock: threading.Lock = field(default_factory=threading.Lock)
    _shutdown_event: threading.Event = field(default_factory=threading.Event)

    on_unknown_accounts: Optional[Callable[[list], None]] = None
    on_sync_completed: Optional[Callable[[], None]] = None

    def get_session_key(self) -> bytes:
        """Return the key for the currently confirmed handshake."""
        if self.reconnect_handshake is not None:
            return self.reconnect_handshake.get_session_key()
        return self.handshake.get_session_key()

    @property
    def handshake_confirmed(self) -> bool:
        return bool(
            getattr(self.handshake, "_confirmed", False)
            or getattr(self.reconnect_handshake, "_confirmed", False)
        )

    @property
    def is_shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()

    def request_shutdown(self) -> None:
        self._shutdown_event.set()

    def create_account(self, name: str, account_type: str) -> tuple:
        """Create a ledger account (called from the UI) and clear it from the unknown set.

        Returns detail to the DESKTOP UI only -- never transmitted to the phone.
        """
        try:
            if name in self.gl.accounts:
                with self._state_lock:
                    self.unknown_accounts.discard(name)
                return (True, f"Account '{name}' already exists.")
            self.gl.create_account(name, account_type)
            with self._state_lock:
                self.unknown_accounts.discard(name)
            logger.info(f"User created account '{name}' ({account_type})")
            return (True, f"Created account '{name}' ({account_type}).")
        except Exception as e:
            logger.exception(f"Failed to create account '{name}'")
            return (False, str(e))

    def reprocess_held(self) -> dict:
        """Post held transactions whose accounts now all exist (no phone re-sync needed).

        Fail-close: requires atomic transaction support and posts the whole ready
        set in a single begin/commit/rollback. Raises RuntimeError if the ledger
        lacks the transaction API, or if the commit fails (after rollback). The
        caller (desktop UI) should wrap this in try/except.
        """
        _require_txn_api(self.gl)

        # Snapshot held state under the lock, then work outside it.
        with self._state_lock:
            held_items = list(self.held_transactions.items())

        ready = []
        still_missing = set()
        for tx_id, tx in held_items:
            missing = sorted(_extract_accounts_from_tx(tx) - set(self.gl.accounts.keys()))
            if missing:
                still_missing.update(missing)
            else:
                ready.append((tx_id, tx))

        if not ready:
            return {"posted": [], "still_missing": sorted(still_missing)}

        posted = []
        try:
            self.gl.begin_transaction()
            for tx_id, tx in ready:
                ok, msg = _post_transaction_to_ledger(self, tx)
                if not ok:
                    raise RuntimeError(f"Ledger rejected {tx_id}: {msg}")
            self.gl.commit_transaction()
            posted = [tx_id for tx_id, _ in ready]
            with self._state_lock:
                for tx_id, tx in ready:
                    self.desktop_ledger.accepted[tx_id] = tx  # idempotent if the phone resends
                    self.held_transactions.pop(tx_id, None)
        except Exception as e:
            logger.exception(f"Reprocess commit failed: {e}")
            try:
                self.gl.rollback_transaction()
            except Exception:
                logger.error("Reprocess rollback failed")
            raise

        return {"posted": posted, "still_missing": sorted(still_missing)}


# --- Route handlers (pure functions: (ctx, data) -> (status_code, body)) -----
def _handle_handshake(ctx: SyncContext, data: dict) -> tuple:
    """Android: {"session_id", "spake_message"} -> {"status","session_id","spake_message"}"""
    spake_b64 = data.get("spake_message", "")
    if not spake_b64:
        return (400, {"status": "error", "message": "Missing spake_message"})

    with ctx._state_lock:
        if ctx.handshake_started:
            return (400, {"status": "error", "message": "Handshake already started for this session"})

        try:
            raw_msg = _b64url_decode(spake_b64)
            # Strip Android's leading role byte ('A'); Python spake2 wants the bare point.
            if raw_msg[:1] != b"A":
                return (400, {"status": "error", "message": "Invalid SPAKE2 role"})
            # Keep the Android role byte. Python spake2 expects the peer
            # side identifier as part of the pairing message.
            peer_msg = raw_msg

            # Generate the desktop's pB exactly once. start() also enforces the
            # pairing lockout (PairingAttemptLimiter.check()).
            if ctx.cached_pb is None:
                ctx.cached_pb = ctx.handshake.start()
            pb_bytes = ctx.cached_pb

            # Process the phone's pA. derive() returns None and sets the session key.
            ctx.handshake.derive(peer_msg)

            # DEBUG ONLY: fingerprint the derived session key without printing
            # the key itself. The raw SPAKE2 shared secret is derived inside
            # protocol.py; this fingerprint lets us compare the final KDF output
            # against Android before touching the confirmation calculation.

            ctx.handshake_started = True

            # pb_bytes already has the 'B' side byte prepended by
            # PairingHandshake.start() (which forwards to the spake2
            # library's _SPAKE2_Base.start(), returning self.side +
            # self.outbound_message) -- do NOT prepend it again here.
            pb_b64 = _b64url_encode(pb_bytes)
            return (200, {
                "status": "handshake_ok",
                "session_id": ctx.session_id,
                "spake_message": pb_b64,
            })
        except PairingError as e:
            # Detail stays in the desktop log; the phone gets a fixed safe string.
            logger.warning("Handshake rejected: %s", e)
            return (403, {"status": "error", "message": PHONE_ERR_HANDSHAKE_REJECTED})
        except Exception:
            logger.exception("Handshake failed unexpectedly")
            return (500, {"status": "error", "message": PHONE_ERR_HANDSHAKE_FAILED})


def _handle_handshake_confirm(ctx: SyncContext, data: dict) -> tuple:
    """Android: {"session_id", "confirmation_tag"} -> {"status","session_id","confirmation_tag"}

    Tags are raw HMAC-SHA256 bytes (base64), NOT encrypted.
    """
    tag_b64 = data.get("confirmation_tag", "")
    if not tag_b64:
        return (400, {"status": "error", "message": "Missing confirmation_tag"})

    with ctx._state_lock:
        try:
            phone_tag = _b64url_decode(tag_b64)
            # Verifies the phone's tag against role "phone"; records limiter outcome.
            ctx.handshake.confirm_and_verify(phone_tag)
            desktop_tag = ctx.handshake.confirmation_tag()  # role "desktop"
            response = {
                "status": "confirmed",
                "session_id": ctx.session_id,
                "confirmation_tag": _b64url_encode(desktop_tag),
            }
            # Once the PAKE is confirmed, announce the desktop's stable identity
            # over the authenticated/encrypted session so the phone can pin it.
            if ctx.desktop_identity is not None:
                response["identity_announcement"] = build_identity_announcement(
                    ctx.handshake.get_session_key(),
                    ctx.session_id,
                    "desktop",
                    ctx.desktop_identity,
                    label="CogniSync desktop",
                )
                response["desktop_fingerprint"] = identity_fingerprint(ctx.desktop_identity.public_bytes)
            return (200, response)
        except PairingError as e:
            logger.warning("Confirmation rejected: %s", e)
            return (403, {"status": "error", "message": PHONE_ERR_CONFIRM_REJECTED})
        except Exception:
            logger.exception("Confirmation failed unexpectedly")
            return (500, {"status": "error", "message": PHONE_ERR_CONFIRM_FAILED})


def _handle_identity_announce(ctx: SyncContext, data: dict) -> tuple:
    """Phone -> desktop: encrypted long-term phone identity announcement."""
    token = data.get("identity_announcement", "")
    if not token:
        return (400, {"status": "error", "message": "Missing identity_announcement"})

    try:
        session_key = ctx.get_session_key()
        if ctx.trust_store is None:
            raise PairingError("Trust store is not configured.")
        # Read the session's already-pinned identity before processing the
        # announcement. process_identity_announcement() validates the identity
        # and only pins it after this expected-fingerprint check passes.
        with ctx._state_lock:
            old_fingerprint = ctx.peer_fingerprint
            expected_fingerprint = ctx.peer_fingerprint
            fingerprint = process_identity_announcement(
                session_key, token, ctx.trust_store,
                session_id=ctx.session_id,
                replay_guard=ctx.replay_guard,
                expected_fingerprint=None,
            )

            if old_fingerprint and old_fingerprint != fingerprint:
                ctx.trust_store.remove(old_fingerprint)
                logger.info(f"Updated pinned phone identity: {old_fingerprint} -> {fingerprint}")

            ctx.peer_fingerprint = fingerprint
        _save_paired_phone_fingerprint(fingerprint)
        logger.info("Pinned phone identity fingerprint=%s", fingerprint)
        return (200, {"status": "identity_pinned", "fingerprint": fingerprint})
    except PairingError as e:
        logger.warning("Identity announcement rejected: %s", e)
        return (403, {"status": "error", "message": PHONE_ERR_IDENTITY_REJECTED})
    except Exception:
        logger.exception("Identity announcement failed unexpectedly")
        return (500, {"status": "error", "message": PHONE_ERR_IDENTITY_REJECTED})


def _handle_reconnect(ctx: SyncContext, data: dict) -> tuple:
    """Phone -> desktop reconnect start; proves the pinned phone identity."""
    with ctx._state_lock:
        if ctx.handshake_started or ctx.handshake_confirmed:
            return (400, {"status": "error", "message": "A handshake is already active for this session"})
        # v0.7 fix: reject double-call when a reconnect is already in progress.
        if ctx.reconnect_handshake is not None:
            return (400, {"status": "error", "message": "Reconnect already in progress for this session"})

        if ctx.desktop_identity is None or ctx.trust_store is None or not ctx.peer_fingerprint:
            return (403, {"status": "error", "message": PHONE_ERR_RECONNECT_REJECTED})

    peer_msg = data.get("reconnect_message")
    if not isinstance(peer_msg, dict):
        return (400, {"status": "error", "message": "Missing reconnect_message"})

    try:
        reconnect = ReconnectHandshake(
            ctx.desktop_identity, ctx.peer_fingerprint, ctx.session_id,
            role="desktop", trust_store=ctx.trust_store,
            limiter=getattr(ctx.handshake, "limiter", None),
        )
        desktop_msg = reconnect.start()
        reconnect.derive(peer_msg)
        with ctx._state_lock:
            ctx.reconnect_handshake = reconnect
            ctx.reconnect_started = True
        return (200, {
            "status": "reconnect_ok",
            "session_id": ctx.session_id,
            "reconnect_message": desktop_msg,
        })
    except PairingError as e:
        logger.warning("Reconnect rejected: %s", e)
        with ctx._state_lock:
            ctx.reconnect_handshake = None
        return (403, {"status": "error", "message": PHONE_ERR_RECONNECT_REJECTED})
    except Exception:
        logger.exception("Reconnect failed unexpectedly")
        with ctx._state_lock:
            ctx.reconnect_handshake = None
        return (500, {"status": "error", "message": PHONE_ERR_RECONNECT_FAILED})


def _handle_reconnect_confirm(ctx: SyncContext, data: dict) -> tuple:
    """Phone -> desktop reconnect confirmation tag."""
    with ctx._state_lock:
        if ctx.reconnect_handshake is None or not ctx.reconnect_started:
            return (400, {"status": "error", "message": "Reconnect has not started"})

    tag_b64 = data.get("confirmation_tag", "")
    if not tag_b64:
        return (400, {"status": "error", "message": "Missing confirmation_tag"})

    try:
        phone_tag = _b64url_decode(tag_b64)
        ctx.reconnect_handshake.confirm_and_verify(phone_tag)
        desktop_tag = ctx.reconnect_handshake.confirmation_tag()
        return (200, {
            "status": "reconnected",
            "session_id": ctx.session_id,
            "confirmation_tag": _b64url_encode(desktop_tag),
        })
    except PairingError as e:
        logger.warning("Reconnect confirmation rejected: %s", e)
        with ctx._state_lock:
            ctx.reconnect_handshake = None
            ctx.reconnect_started = False
        return (403, {"status": "error", "message": PHONE_ERR_RECONNECT_REJECTED})
    except Exception:
        logger.exception("Reconnect confirmation failed unexpectedly")
        with ctx._state_lock:
            ctx.reconnect_handshake = None
            ctx.reconnect_started = False
        return (500, {"status": "error", "message": PHONE_ERR_RECONNECT_FAILED})

def _extract_accounts_from_tx(tx: dict) -> set:
    """Extract all account names referenced by a transaction.
    Supports both the new multi-line format (tx["lines"]) and the legacy
    flat format (tx["debit_account"] / tx["credit_account"]).
    """
    raw_lines = tx.get("lines")
    if raw_lines and isinstance(raw_lines, list):
        return {
            line.get("account")
            for line in raw_lines
            if isinstance(line, dict) and line.get("account")
        }
    return {
        a for a in (tx.get("debit_account"), tx.get("credit_account"))
        if a
    }

def _post_transaction_to_ledger(ctx: "SyncContext", tx: dict) -> tuple:
    """Post one validated transaction whose accounts already exist.
    Accepts two payload shapes:
      - New (v0.9+): tx["lines"] is a list of {account, type, amount, memo?}
      - Legacy (≤v0.8): flat tx["debit_account"], tx["credit_account"], tx["amount"]
    Returns (ok, msg). The msg may contain ledger internals and is used only
    for desktop logging — never transmitted to the phone.
    """
    try:
        raw_lines = tx.get("lines")
        if raw_lines and isinstance(raw_lines, list):
            # ---- New multi-line format ----
            lines_data = []
            for line in raw_lines:
                if not isinstance(line, dict):
                    return (False, "Transaction line is not an object.")
                account = line.get("account")
                entry_type = (line.get("type") or "").upper()
                amount_str = line.get("amount", "")
                if not account:
                    return (False, "Transaction line is missing 'account'.")
                try:
                    amount = parse_amount_strict(amount_str)
                except ValueError as e:
                    return (False, f"Invalid line amount: {e}")
                if entry_type == "DEBIT":
                    lines_data.append((account, amount, money(0)))
                elif entry_type == "CREDIT":
                    lines_data.append((account, money(0), amount))
                else:
                    return (False, f"Invalid line type: {entry_type!r}")
            if len(lines_data) < 2:
                return (False, "Transaction must have at least 2 lines.")
        else:
            # ---- Legacy 2-line format (backward compat) ----
            try:
                amt = parse_amount_strict(tx.get("amount", ""))
            except ValueError as e:
                return (False, f"Invalid amount: {e}")
            debit_account = tx.get("debit_account")
            credit_account = tx.get("credit_account")
            if not debit_account or not credit_account:
                return (False, "Legacy transaction missing debit_account or credit_account.")
            lines_data = [
                (debit_account, amt, money(0)),
                (credit_account, money(0), amt),
            ]

        return ctx.gl.record_transaction(
            date=tx["date"],
            description=tx.get("description", "Mobile Import"),
            lines_data=lines_data,
            imported_id=f"android_{tx['transaction_id']}",
            persist=True,
        )
    except Exception as e:
        return (False, str(e))


def _handle_sync(ctx: SyncContext, data: dict) -> tuple:
    """Android: {"session_id", "encrypted_batch"} -> {"status","encrypted_ack"}

    v0.3: transactions referencing accounts that don't exist are NOT auto-created.
    They are held, rejected, and the missing account names are surfaced to the
    desktop UI (ctx.unknown_accounts / ctx.on_unknown_accounts).

    v0.7: held transactions are stored in a LIST so multiple transactions with a
    missing transaction_id are not collapsed into a single entry.

    Fail-close: the ledger MUST support atomic transactions; if it doesn't, the
    sync is refused rather than proceeding without rollback capability.

    Minimal disclosure: rejection reasons in the ACK are sanitized to a fixed
    generic string; full detail is logged on the desktop only.
    """
    encrypted_batch_b64 = data.get("encrypted_batch", "")
    if not encrypted_batch_b64:
        return (400, {"status": "error", "message": "Missing encrypted_batch"})

    try:
        # Fail-close: refuse to sync unless the ledger supports atomic
        # transactions. Checked FIRST so no session state (replay guard,
        # idempotency ledger) is mutated before we know posting is possible.
        _require_txn_api(ctx.gl)

        session_key = ctx.get_session_key()  # raises until confirmed

        replay_snapshot = ctx.replay_guard.snapshot()
        batch_envelope = decrypt_and_validate_message(
            key=session_key,
            token=encrypted_batch_b64,
            expected_message_type="transaction_batch",
            expected_session_id=ctx.session_id,
            replay_guard=ctx.replay_guard,
        )

        payload = batch_envelope.get("payload", {})
        transactions = payload.get("transactions", [])

        # v0.3 / v0.7: split by account existence. Unknown accounts are never
        # auto-created. held is a LIST so transactions with a missing/None
        # transaction_id are not collapsed into a single dict entry.
        postable = []
        held = []  # list of {"tx_id": ..., "tx": ..., "missing": [...]}
        for tx in transactions:
            tx_id = tx.get("transaction_id")
            missing = sorted(_extract_accounts_from_tx(tx) - set(ctx.gl.accounts.keys()))

            if missing:
                held.append({"tx_id": tx_id, "tx": tx, "missing": missing})
            else:
                postable.append(tx)

        # v0.7: mutate shared idempotency/hold state under the lock.
        with ctx._state_lock:
            previously_accepted = set(ctx.desktop_ledger.accepted.keys())
            ledger_snapshot = dict(ctx.desktop_ledger.accepted)

            result = ctx.desktop_ledger.accept_batch(postable)
            accepted_ids = result["accepted"]
            rejected = list(result["rejected"])
            conflicted = result["conflicted"]

            # Register held transactions and reject them with an actionable reason.
            # (The reason carries account names for the desktop log/UI; the ACK copy
            # sent to the phone is sanitized below.)
            for entry in held:
                tx_id = entry["tx_id"]
                ctx.unknown_accounts.update(entry["missing"])
                if tx_id:
                    ctx.held_transactions[tx_id] = entry["tx"]
                    rejected.append({
                        "transaction_id": tx_id,
                        "reason": (
                            f"Unknown account(s): {', '.join(entry['missing'])}. "
                            f"Define them on the desktop, then sync again."
                        ),
                    })
                else:
                    rejected.append({
                        "transaction_id": None,
                        "reason": "Unknown account(s) and missing transaction ID.",
                    })

            # Only post transactions that are NEW this batch (skip idempotent retries).
            newly_accepted_ids = set(accepted_ids) - previously_accepted

        # Notify the desktop UI that there are accounts to define.
        # (Lock released before the callback runs so the UI can take its time.)
        if held and ctx.on_unknown_accounts is not None:
            try:
                with ctx._state_lock:
                    snapshot = sorted(ctx.unknown_accounts)
                ctx.on_unknown_accounts(snapshot)
            except Exception:
                logger.exception("on_unknown_accounts callback failed")

        # _require_txn_api above guarantees begin/commit/rollback exist, so call
        # them unconditionally. There is deliberately NO non-atomic fallback.
        # Ledger I/O is deliberately OUTSIDE the lock (slow; no shared-state mutation).
        try:
            ctx.gl.begin_transaction()
            for tx in postable:
                tx_id = tx.get("transaction_id")
                if tx_id not in newly_accepted_ids:
                    continue
                ok, msg = _post_transaction_to_ledger(ctx, tx)
                if not ok:
                    raise RuntimeError(f"Ledger rejected {tx_id}: {msg}")
            ctx.gl.commit_transaction()
            if ctx.on_sync_completed:
                try:
                    ctx.on_sync_completed()
                except Exception:
                    logger.exception("on_sync_completed callback failed")
        except Exception as batch_err:
            logger.exception("Ledger commit failed; rolling back batch")
            try:
                ctx.gl.rollback_transaction()
            except Exception as rb_err:
                logger.error(f"Rollback failed: {rb_err}")
            # Restore both idempotency and replay state so the exact encrypted
            # batch remains retryable after a failed atomic ledger commit.
            with ctx._state_lock:
                ctx.desktop_ledger.accepted = ledger_snapshot
            ctx.replay_guard.restore(replay_snapshot)
            rejected = rejected + [
                {"transaction_id": tid, "reason": "Ledger commit failed"}
                for tid in accepted_ids
            ]
            accepted_ids = []
            conflicted = []

        # Phase 3 delta-sync cursor (scaffolding; backward-compatible).
        new_cursor = str(uuid.uuid4()) if accepted_ids else ctx.last_sync_cursor
        ctx.last_sync_cursor = new_cursor

        ack_payload = {
            "accepted": accepted_ids,
            # Minimal disclosure: sanitize reasons before they reach the phone.
            "rejected": _sanitize_ack_items(rejected, "sync.rejected"),
            "conflicted": _sanitize_ack_items(conflicted, "sync.conflicted"),
            "cursor": new_cursor,
        }
        ack_envelope = envelope("ack", ctx.session_id, "Desktop-CogniFinance", ack_payload)
        encrypted_ack_b64 = encrypt_message(session_key, ack_envelope)

        logger.info(
            f"Sync complete: {len(accepted_ids)} accepted, {len(rejected)} rejected, "
            f"{len(conflicted)} conflicted, {len(held)} held for account definition "
            f"(package_id={payload.get('package_id')})"
        )
        return (200, {"status": "success", "encrypted_ack": encrypted_ack_b64})

    except (PairingError, InvalidTag) as e:
        logger.warning("Sync rejected (auth/replay/envelope/decrypt): %s", e)
        return (403, {"status": "error", "message": PHONE_ERR_SYNC_REJECTED})
    except Exception:
        logger.exception("Batch processing failed")
        return (500, {"status": "error", "message": PHONE_ERR_SYNC_FAILED})


def _handle_status(ctx: SyncContext, data: dict) -> tuple:
    """Health check + unknown-account state.

    Callable before the handshake completes (token+session auth only). The
    desktop UI polls this to discover which accounts still need to be defined.
    The exposed `unknown_accounts` are account names the phone itself submitted
    (not DB contents), so this is low-risk.
    """

    with ctx._state_lock:
        unknown = sorted(ctx.unknown_accounts)
        held_ids = sorted(ctx.held_transactions.keys())
        reconnect_handshake = ctx.reconnect_handshake

    return (200, {
        "status": "ok",
        "session_id": ctx.session_id,
        "protocol": PROTOCOL,
        "version": VERSION,
        "handshake_confirmed": ctx.handshake_confirmed,
        "reconnect_available": bool(ctx.peer_fingerprint and ctx.desktop_identity and ctx.trust_store),
        "desktop_fingerprint": (identity_fingerprint(ctx.desktop_identity.public_bytes)
                                if ctx.desktop_identity is not None else None),
        "peer_fingerprint": ctx.peer_fingerprint,
        "last_sync_cursor": ctx.last_sync_cursor,
        "unknown_accounts": unknown,
        "held_transaction_count": len(held_ids),
        "held_transaction_ids": held_ids,
        "server_time": datetime.now(timezone.utc).isoformat(),
    })


def _handle_shutdown(ctx: SyncContext, data: dict) -> tuple:
    logger.info("Shutdown signal received.")
    ctx.request_shutdown()
    return (200, {"status": "shutting_down"})


# --- Dispatch table (add a new endpoint with ONE line) -----------------------
ROUTE_TABLE = {
    "/api/handshake": _handle_handshake,
    "/api/handshake/confirm": _handle_handshake_confirm,
    "/api/identity/announce": _handle_identity_announce,
    "/api/reconnect": _handle_reconnect,
    "/api/reconnect/confirm": _handle_reconnect_confirm,
    "/api/sync": _handle_sync,
    "/api/status": _handle_status,
    "/api/shutdown": _handle_shutdown,
}


# --- HTTP adapter -------------------------------------------------------------
class CogniSyncHandler(BaseHTTPRequestHandler):
    """Thin adapter between HTTPServer and the pure route handlers."""

    def setup(self):
        super().setup()
        self.request.settimeout(READ_TIMEOUT_SECONDS)

    def log_message(self, format, *args):
        logger.info(f"{self.address_string()} - {format % args}")

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._send_response(400, {
                "status": "error",
                "message": "Invalid Content-Length",
            })
            return

        if content_length > MAX_REQUEST_SIZE:
            self._send_response(413, {
                "status": "error",
                "message": f"Request too large. Max {MAX_REQUEST_SIZE} bytes",
            })
            return

        try:
            body = self.rfile.read(content_length)
        except socket.timeout:
            self._send_response(408, {"status": "error", "message": "Request read timeout"})
            return

        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_response(400, {"status": "error", "message": "Invalid JSON"})
            return

        if not isinstance(data, dict):
            self._send_response(400, {"status": "error", "message": "Request body must be a JSON object"})
            return

        handler_func = ROUTE_TABLE.get(self.path)
        if handler_func is None:
            self._send_response(404, {"status": "error", "message": "Unknown endpoint"})
            return

        ctx: SyncContext = self.server.sync_context

        # Auth on every endpoint: HTTP Authorization bearer token + session_id.
        # Use the standard Authorization header and constant-time comparison.
        authorization = self.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")

        if scheme.lower() != "bearer" or not separator:
            self._send_response(401, {"status": "error", "message": "Invalid HTTP token"})
            return
        if not hmac.compare_digest(token, ctx.http_token):
            self._send_response(401, {"status": "error", "message": "Invalid HTTP token"})
            return


        if data.get("session_id") != ctx.session_id:
            self._send_response(400, {"status": "error", "message": "Session ID mismatch"})
            return

        code, response_body = handler_func(ctx, data)
        self._send_response(code, response_body)

        # Stop serve_forever() from a separate thread after responding.
        if ctx.is_shutdown_requested:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _send_response(self, code: int, body: dict):
        payload = json.dumps(body).encode("utf-8")

        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)


# --- Server -------------------------------------------------------------------
class CogniSyncServer(HTTPServer):
    """Ephemeral LAN HTTP server for mobile sync."""

    def __init__(self, gl: GeneralLedger, pairing_code: str, port: int = 40317, bind_address: str = "0.0.0.0",
        on_unknown_accounts: Optional[Callable[[list], None]] = None,
        on_sync_completed: Optional[Callable[[], None]] = None,
        **kwargs,):
        # Ensure sync activity (including rejection reasons never sent to the
        # phone) lands somewhere durable, even if the host app never calls
        # configure_file_logging() itself. Idempotent, so this is harmless
        # if the host app *does* configure it independently.
        configure_file_logging()

        # 0.0.0.0 is required: the phone connects over the LAN to desktop_ip.
        super().__init__((bind_address, port), CogniSyncHandler)

        # Generate the session_id ONCE and reuse it for the handshake + context.
        session_id = str(uuid.uuid4())
        limiter = PairingAttemptLimiter()

        desktop_identity = load_or_create_identity(DEFAULT_IDENTITY_PATH)
        trust_store = TrustStore(DEFAULT_TRUST_STORE_PATH)
        paired_phone_fingerprint = _load_paired_phone_fingerprint()
        if paired_phone_fingerprint and not trust_store.is_known(paired_phone_fingerprint):
            paired_phone_fingerprint = None

        self.sync_context = SyncContext(
            gl=gl,
            handshake=PairingHandshake(pairing_code, session_id, role="desktop", limiter=limiter),
            session_id=session_id,
            http_token=uuid.uuid4().hex,
            pairing_code=pairing_code,
            server_port=self.server_port,
            desktop_identity=desktop_identity,
            trust_store=trust_store,
            peer_fingerprint=paired_phone_fingerprint,
            on_unknown_accounts=on_unknown_accounts,
            on_sync_completed=on_sync_completed,
        )

    @property
    def bootstrap_data(self) -> dict:
        """Data to encode in the Desktop Init QR code (matches DesktopBootstrapQr)."""
        return {
            "protocol": PROTOCOL,
            "version": VERSION,
            "desktop_ip": self._get_local_ip(),
            "port": self.server_port,
            "session_id": self.sync_context.session_id,
            "http_token": self.sync_context.http_token,
            "desktop_fingerprint": identity_fingerprint(self.sync_context.desktop_identity.public_bytes),
            "reconnect_available": bool(self.sync_context.peer_fingerprint),

        }

    def _get_local_ip(self) -> str:
        # Strategy 1: UDP connect trick (fastest, works on most networks).
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass

        # Strategy 2: enumerate interfaces (works on air-gapped/VPN) if available.
        if _HAS_NETIFACES:
            try:
                for iface in netifaces.interfaces():
                    addrs = netifaces.ifaddresses(iface)
                    for addr in addrs.get(netifaces.AF_INET, []):
                        ip = addr.get("addr", "")
                        if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                            return ip
            except Exception:
                pass

        # Strategy 3: hostname resolution last resort.
        try:
            ip = socket.gethostbyname(socket.gethostname())
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass

        logger.warning("Could not detect LAN IP; falling back to 127.0.0.1. "
                       "The phone will not be able to connect over LAN.")
        return "127.0.0.1"


# --- Example usage -------------------------------------------------------------
# from ledger import GeneralLedger
# from protocol import make_pairing_code
#
# gl = GeneralLedger(...)                     # your existing ledger
# pairing_code = make_pairing_code()          # 6-digit code shown to the user
# server = CogniSyncServer(gl, pairing_code)
#
# display_qr(json.dumps(server.bootstrap_data))   # phone scans this
# show_pairing_code(pairing_code)                 # user types this into the phone
#
# server.serve_forever()                      # blocks until /api/shutdown is received
