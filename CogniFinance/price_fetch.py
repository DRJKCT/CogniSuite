# price_fetch.py
# For CogniFinance -- tier 2 of the pricing approach: an OPT-IN,
# MANUAL-TRIGGER-ONLY price refresh using the USER'S OWN provider and
# API key.
#
# This is the entire network-touching surface of the investment/crypto
# modules, by design -- everything in investment_engine.py,
# crypto_engine.py, lot_matcher.py, and price_history.py is pure/local.
# Auditing what this feature sends over the network means reading this
# one small file, nothing else.
#
# Firm rules (from the planning brief, not negotiable):
#   - OFF BY DEFAULT on install. No exception.
#   - NEVER AUTOMATIC. No fetch on startup, no fetch on tab-open, no
#     background polling, ever. The only call path into refresh_prices()
#     should be a button's onClick handler. is_enabled() is an
#     additional guard refresh_prices() itself checks -- belt and braces,
#     not just a UI-level toggle.
#   - Fetches ONLY for symbols the user actually holds (callers pass
#     the exact symbol list; this module never fetches speculatively).
#   - Every call, success or failure, is logged via network_call_log.py
#     -- which knows nothing about *why* the call happened, only that
#     it did.
#   - Fully local use is always possible: leave this off and enter
#     prices manually / via CSV import. Nothing here is required.
#
# BRING YOUR OWN KEY: this app ships no provider credentials and
# redistributes no market data. The user chooses a provider, signs up
# with it, and pastes their own API key. Their use of that provider is
# governed by THEIR agreement with it (including whether their plan
# permits their use case) -- not by anything this app does. The key is
# stored in sql_price_fetch_settings, inside the DEK-protected ledger
# database. It is never logged, never shown in error text (see
# _redact), and must not be included in sync/export payloads.
#
# RESIDUAL LEAK, NAMED HONESTLY: even opt-in + manual-trigger-only,
# asking a price API "what is BTC worth" tells that API someone holds
# BTC, right now, from this IP -- and with a user-supplied key, it is
# tied to that user's provider account. Smaller than syncing balances
# or transactions, but not zero, and there is no way to fetch a live
# price from a third party while making that strictly zero. Disclosure
# (get_disclosure_text) and user choice are the mitigations; this
# comment is the acknowledgment, not a fix.
#
# Providers: see PROVIDERS. Adding one means adding a fetch function
# and one registry entry in this file only.

import json
import logging
import re
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, List, Optional

import network_call_log
import price_history

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 8

# Generic fallback copy for callers that import the constant. Prefer
# get_disclosure_text(), which names the provider actually selected.
DISCLOSURE_TEXT = (
    "When you click Refresh, this sends the tickers/symbols you hold (and the API key "
    "you provided, if the provider needs one) to the price provider you chose in Settings "
    "to look up current prices. Nothing else about your finances -- no balances, no "
    "account names, no transaction history -- is sent. This is off by default and only "
    "ever runs when you click the button."
)

# Ticker -> CoinGecko coin id. CoinGecko's free endpoint needs its own
# id, not the ticker, and has no reliable ticker->id search without an
# API key -- so this starts as a small manual mapping covering common
# assets and is meant to be extended, not treated as exhaustive.
COINGECKO_ID_MAP = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "XRP": "ripple",
    "LTC": "litecoin",
    "USDC": "usd-coin",
    "USDT": "tether",
}


class PriceFetchDisabledError(Exception):
    pass


class PriceFetchNotConfiguredError(PriceFetchDisabledError):
    """No provider chosen, or the chosen provider needs a key that isn't set.
    Subclasses PriceFetchDisabledError so existing UI handlers that catch the
    disabled case also catch this, and show its message instead of crashing."""


class RateLimitError(ValueError):
    """Provider says we're over quota; further calls this run would just burn more."""


# ---------------------------------------------------------------
# Settings (off by default)
# ---------------------------------------------------------------

def _init_db(gl):
    conn, cur = gl._connect()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sql_price_fetch_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def _get_setting(gl, key: str) -> Optional[str]:
    _init_db(gl)
    conn, cur = gl._connect()
    try:
        cur.execute("SELECT value FROM sql_price_fetch_settings WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()


def _set_setting(gl, key: str, value: str) -> None:
    _init_db(gl)
    conn, cur = gl._connect()
    try:
        cur.execute(
            "INSERT INTO sql_price_fetch_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def _delete_setting(gl, key: str) -> None:
    _init_db(gl)
    conn, cur = gl._connect()
    try:
        cur.execute("DELETE FROM sql_price_fetch_settings WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()


def is_enabled(gl) -> bool:
    return _get_setting(gl, "enabled") == "1"


def set_enabled(gl, enabled: bool) -> None:
    """The only way this flips to True is a user explicitly checking a
    box in Settings/the Investments tab -- this function doesn't get
    called from anywhere else in the codebase."""
    _set_setting(gl, "enabled", "1" if enabled else "0")


# ---------------------------------------------------------------
# Provider + key selection
# ---------------------------------------------------------------

_KEY_RE = re.compile(r"^[A-Za-z0-9_\-.]{8,128}$")


def get_provider_id(gl, asset_kind: str) -> Optional[str]:
    """The user's chosen provider for 'stock' or 'crypto'. Stocks have no
    default -- the user must choose. Crypto defaults to keyless CoinGecko."""
    chosen = _get_setting(gl, f"provider_{asset_kind}")
    if chosen in PROVIDERS and PROVIDERS[chosen].kind == asset_kind:
        return chosen
    return "coingecko" if asset_kind == "crypto" else None


def set_provider_id(gl, asset_kind: str, provider_id: str) -> None:
    p = PROVIDERS.get(provider_id)
    if p is None or p.kind != asset_kind:
        raise ValueError(f"'{provider_id}' is not a valid {asset_kind} provider.")
    _set_setting(gl, f"provider_{asset_kind}", provider_id)


def clear_provider_id(gl, asset_kind: str) -> None:
    """Un-select the provider (stocks go back to 'none selected'; crypto
    goes back to its keyless default)."""
    _delete_setting(gl, f"provider_{asset_kind}")


def set_api_key(gl, provider_id: str, key: str) -> None:
    if provider_id not in PROVIDERS:
        raise ValueError(f"Unknown provider '{provider_id}'.")
    key = (key or "").strip()
    if not _KEY_RE.match(key):
        raise ValueError(
            "That doesn't look like an API key (expected 8-128 letters, digits, '-', '_' or '.', "
            "with no spaces)."
        )
    _set_setting(gl, f"api_key_{provider_id}", key)


def get_api_key(gl, provider_id: str) -> Optional[str]:
    return _get_setting(gl, f"api_key_{provider_id}")


def has_api_key(gl, provider_id: str) -> bool:
    """For UI: show 'key saved' without ever revealing it."""
    return bool(get_api_key(gl, provider_id))


def clear_api_key(gl, provider_id: str) -> None:
    _delete_setting(gl, f"api_key_{provider_id}")


# ---------------------------------------------------------------
# HTTP helpers (keys never appear in errors or logs)
# ---------------------------------------------------------------

# Plain "Python-urllib" User-Agents are commonly rejected; send a browser-style one.
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}


def _redact(text: str, secret: Optional[str]) -> str:
    """Remove the API key (raw and URL-encoded) from any string before it can
    reach the UI, logger, or network_call_log."""
    if not secret:
        return text
    text = text.replace(secret, "***")
    return text.replace(urllib.parse.quote(secret, safe=""), "***")


def _http_get(url: str, secret: Optional[str] = None) -> str:
    """GET a URL and return the body. On HTTP/network errors, raises a
    ValueError whose message includes the status code and a redacted URL,
    so failures are diagnosable without leaking the API key."""
    req = urllib.request.Request(url, headers=_HEADERS)
    shown = _redact(url, secret)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # `from None`: don't chain the original error -- its URL contains the key.
        if e.code == 429:
            raise RateLimitError(f"HTTP 429 (rate limit) from {shown}") from None
        if e.code in (401, 403):
            raise ValueError(
                f"HTTP {e.code} from {shown} -- the provider rejected the request "
                f"(check that your API key is correct and active)."
            ) from None
        raise ValueError(f"HTTP {e.code} from {shown}") from None
    except urllib.error.URLError as e:
        raise ValueError(f"Network error for {shown}: {_redact(str(e.reason), secret)}") from None


def _get_json(url: str, secret: Optional[str], provider: str, symbol: str):
    try:
        return json.loads(_http_get(url, secret))
    except json.JSONDecodeError:
        raise ValueError(f"{provider} returned a non-JSON response for '{symbol}'.") from None


def _to_price(value, provider: str, symbol: str) -> Decimal:
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{provider} returned an unparseable price for '{symbol}': '{value}'.") from None
    if price <= 0:
        raise ValueError(f"{provider} returned a non-positive price for '{symbol}': '{value}'.")
    return price


# ---------------------------------------------------------------
# Providers
# ---------------------------------------------------------------

def _fetch_finnhub(symbol: str, key: str) -> Decimal:
    url = "https://finnhub.io/api/v1/quote?" + urllib.parse.urlencode({"symbol": symbol, "token": key})
    data = _get_json(url, key, "Finnhub", symbol)
    # Finnhub returns c=0 for symbols it doesn't know.
    if not isinstance(data, dict) or not data.get("c"):
        raise ValueError(f"Finnhub has no current price for '{symbol}'.")
    return _to_price(data["c"], "Finnhub", symbol)


def _fetch_twelvedata(symbol: str, key: str) -> Decimal:
    url = "https://api.twelvedata.com/price?" + urllib.parse.urlencode({"symbol": symbol, "apikey": key})
    data = _get_json(url, key, "Twelve Data", symbol)
    if isinstance(data, dict) and data.get("status") == "error":
        msg = _redact(str(data.get("message", "unknown error")), key)
        if data.get("code") == 429:
            raise RateLimitError(f"Twelve Data rate limit: {msg}")
        if data.get("code") in (401, 403):
            raise ValueError(f"Twelve Data rejected the request (check your API key): {msg}")
        raise ValueError(f"Twelve Data error for '{symbol}': {msg}")
    price = data.get("price") if isinstance(data, dict) else None
    if price is None:
        raise ValueError(f"Twelve Data has no current price for '{symbol}'.")
    return _to_price(price, "Twelve Data", symbol)


def _fetch_alphavantage(symbol: str, key: str) -> Decimal:
    url = "https://www.alphavantage.co/query?" + urllib.parse.urlencode(
        {"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": key}
    )
    data = _get_json(url, key, "Alpha Vantage", symbol)
    if not isinstance(data, dict):
        raise ValueError(f"Alpha Vantage returned an unexpected response for '{symbol}'.")
    if "Error Message" in data:
        raise ValueError(f"Alpha Vantage error for '{symbol}': {_redact(str(data['Error Message']), key)}")
    note = data.get("Note") or data.get("Information")
    if note:
        note = _redact(str(note), key)
        low = note.lower()
        if "apikey" in low and ("invalid" in low or "missing" in low):
            raise ValueError(f"Alpha Vantage rejected the request (check your API key): {note}")
        raise RateLimitError(f"Alpha Vantage limit reached: {note}")
    price = (data.get("Global Quote") or {}).get("05. price")
    if not price:
        raise ValueError(f"Alpha Vantage has no current price for '{symbol}'.")
    return _to_price(price, "Alpha Vantage", symbol)


def _fetch_yahoo(symbol: str, key: Optional[str] = None) -> Decimal:
    """UNOFFICIAL, no key. Personal use only per Yahoo's terms; can break
    without notice. Kept as an optional convenience, never a default."""
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(symbol, safe='')}?range=1d&interval=1d"
    )
    data = _get_json(url, None, "Yahoo", symbol)
    chart = data.get("chart") or {}
    err = chart.get("error")
    if err:
        raise ValueError(f"Yahoo error for '{symbol}': {err.get('description') or err.get('code')}")
    results = chart.get("result") or []
    if not results:
        raise ValueError(f"Yahoo returned no data for '{symbol}'.")
    price = (results[0].get("meta") or {}).get("regularMarketPrice")
    if price is None:
        raise ValueError(f"Yahoo has no current price for '{symbol}'.")
    return _to_price(price, "Yahoo", symbol)


def _fetch_coingecko(symbol: str, key: Optional[str] = None) -> Decimal:
    coin_id = COINGECKO_ID_MAP.get(symbol.upper())
    if coin_id is None:
        raise ValueError(
            f"No CoinGecko id mapping for '{symbol}'. Add one to COINGECKO_ID_MAP in price_fetch.py."
        )
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
    data = _get_json(url, None, "CoinGecko", symbol)
    price = data.get(coin_id, {}).get("usd") if isinstance(data, dict) else None
    if price is None:
        raise ValueError(f"CoinGecko returned no USD price for '{symbol}'.")
    return _to_price(price, "CoinGecko", symbol)


@dataclass(frozen=True)
class Provider:
    id: str            # stored in settings and recorded as the price source
    label: str         # shown in the UI
    kind: str          # 'stock' or 'crypto'
    needs_key: bool
    host: str          # shown in the disclosure
    signup_url: str    # where the user gets a key ('' if none needed)
    note: str          # one line of caution shown next to the provider choice
    fetch: Callable[[str, Optional[str]], Decimal]


_USE_CASE_NOTE = "Check that your plan's terms permit your use (many free tiers are personal-use only)."

PROVIDERS: Dict[str, Provider] = {p.id: p for p in (
    Provider("finnhub", "Finnhub", "stock", True, "finnhub.io",
             "https://finnhub.io/register", _USE_CASE_NOTE, _fetch_finnhub),
    Provider("twelvedata", "Twelve Data", "stock", True, "twelvedata.com",
             "https://twelvedata.com/pricing", _USE_CASE_NOTE, _fetch_twelvedata),
    Provider("alphavantage", "Alpha Vantage", "stock", True, "alphavantage.co",
             "https://www.alphavantage.co/support/#api-key",
             _USE_CASE_NOTE + " The free tier allows very few requests per day.", _fetch_alphavantage),
    Provider("yahoo", "Yahoo Finance (unofficial, no key)", "stock", False, "finance.yahoo.com",
             "", "Unofficial endpoint; personal use only per Yahoo's terms; may break at any time.",
             _fetch_yahoo),
    Provider("coingecko", "CoinGecko (public, no key)", "crypto", False, "coingecko.com",
             "", "Public endpoint with rate limits; review CoinGecko's terms for your use.",
             _fetch_coingecko),
)}


def providers_for(asset_kind: str) -> List[Provider]:
    """For the UI's provider dropdown."""
    return [p for p in PROVIDERS.values() if p.kind == asset_kind]


def get_disclosure_text(gl, asset_kind: str) -> str:
    """Disclosure copy naming the provider actually selected."""
    pid = get_provider_id(gl, asset_kind)
    if pid is None:
        return ("No price provider is selected. Choose one in Settings to enable online "
                "refresh, or keep entering prices manually (fully local).")
    p = PROVIDERS[pid]
    sent = "the tickers/symbols you hold"
    if p.needs_key:
        sent += " and the API key you provided"
    return (
        f"When you click Refresh, this sends {sent} to {p.label} ({p.host}) to look up current "
        f"prices. Nothing else about your finances -- no balances, no account names, no "
        f"transaction history -- is sent. Your use of {p.label} is governed by your own "
        f"agreement with them. This is off by default and only ever runs when you click the button."
    )


# ---------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------

def refresh_prices(gl, symbols: List[str], asset_kind: str) -> Dict[str, Optional[str]]:
    """Manually-triggered refresh for a specific list of symbols the
    user actually holds. `asset_kind` is 'stock' or 'crypto' -- callers
    (the Investments tab, the Crypto tab) each know which kind their
    holdings are and call this once per kind rather than mixing both
    in one call, keeping the source choice unambiguous.

    Uses the provider (and API key, if needed) the user configured.
    Returns {symbol: error_message_or_None}. On success for a symbol,
    the price is written to price_history.py's store with source set to
    the provider id; on failure, nothing is written for that symbol and
    its entry in the returned dict holds the (key-redacted) error.

    Every symbol's outcome, success or failure, is logged via
    network_call_log.py individually, so a partial failure is visible
    per-symbol rather than only as a lump 'refresh failed'. If the
    provider reports a rate limit, remaining symbols are skipped (and
    marked with that error) instead of burning more quota.
    """
    if not is_enabled(gl):
        raise PriceFetchDisabledError(
            "Price fetching is disabled. The user must opt in before this can run."
        )
    if asset_kind not in ("stock", "crypto"):
        raise ValueError("asset_kind must be 'stock' or 'crypto'.")
    if not symbols:
        return {}

    provider_id = get_provider_id(gl, asset_kind)
    if provider_id is None:
        raise PriceFetchNotConfiguredError(
            f"No {asset_kind} price provider is selected. Choose one in Settings, "
            f"or enter prices manually."
        )
    provider = PROVIDERS[provider_id]
    key = get_api_key(gl, provider_id) if provider.needs_key else None
    if provider.needs_key and not key:
        raise PriceFetchNotConfiguredError(
            f"{provider.label} needs an API key. Add yours in Settings "
            f"(free keys: {provider.signup_url})."
        )

    today = _today_iso()
    results: Dict[str, Optional[str]] = {}
    limit_msg: Optional[str] = None

    for raw in symbols:
        symbol = raw.strip().upper()
        if limit_msg is not None:
            results[symbol] = f"Skipped: {limit_msg}"
            continue
        try:
            price = provider.fetch(symbol, key)
            price_history.record_price(gl, symbol, today, price, source=provider.id)
            network_call_log.log_call(gl, provider.id, [symbol], "success")
            results[symbol] = None
        except Exception as e:
            msg = _redact(str(e), key)  # belt and braces: fetchers already redact
            logger.warning(f"price_fetch: failed to fetch '{symbol}' from {provider.id}: {msg}")
            network_call_log.log_call(gl, provider.id, [symbol], "failure", detail=msg)
            results[symbol] = msg
            if isinstance(e, RateLimitError):
                limit_msg = msg
    return results


def _today_iso() -> str:
    from datetime import date
    return date.today().isoformat()
