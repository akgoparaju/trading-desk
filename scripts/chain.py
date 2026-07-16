"""Options-chain file parser for the trade-decision plugin.

WHY THIS MODULE EXISTS: Alpha Vantage HISTORICAL_OPTIONS full chains are
~2M tokens / ~13k contracts. The Claude harness offloads such tool results
to a file whose JSON envelope varies. The chain must NEVER be loaded into LLM
context -- this module is the ONLY reader. It parses the file on disk, normalizes
contracts, and returns compact derived metrics (ATM IV, expected move, max pain,
OI walls, put/call ratio, skew). Functions never print or return whole chains.

stdlib-only. All derived metrics are pure functions over normalized contracts.
A normalized contract is a dict with (subset of) keys:
    expiration: "YYYY-MM-DD", strike: float, type: "call"|"put",
    mark/bid/ask/iv/delta: float, oi/volume: int.
Contracts missing strike/expiration/type are skipped during load.
"""

import json

from scripts import indicators

# Alpha Vantage field aliases -> normalized numeric key names.
_NUMERIC_ALIASES = {
    "strike": "strike",
    "mark": "mark",
    "bid": "bid",
    "ask": "ask",
    "iv": "iv",
    "implied_volatility": "iv",
    "delta": "delta",
    "oi": "oi",
    "open_interest": "oi",
    "volume": "volume",
}
_INT_KEYS = {"oi", "volume"}


def unwrap(obj):
    """Extract the payload from a possibly-wrapped JSON object.

    If ``obj`` is a dict with a "content" list whose first item has a "text"
    field, parse that text as JSON and recurse (MCP tool-result envelope).
    Otherwise return ``obj`` unchanged.
    """
    if isinstance(obj, dict):
        content = obj.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict) and "text" in first:
                try:
                    inner = json.loads(first["text"])
                except (ValueError, TypeError):
                    return obj
                return unwrap(inner)
    return obj


def _coerce_num(value, as_int):
    """float()/int(float()) coercion; return None on any failure."""
    try:
        num = float(value)
    except (ValueError, TypeError):
        return None
    return int(num) if as_int else num


def _normalize(raw):
    """Normalize one raw contract dict -> normalized dict, or None if invalid.

    Contracts missing strike/expiration/type are skipped (return None).
    Numeric coercion failures simply drop that key. mark falls back to the
    bid/ask midpoint, then to "last".
    """
    if not isinstance(raw, dict):
        return None

    out = {}

    expiration = raw.get("expiration")
    if expiration is None:
        return None
    out["expiration"] = expiration

    opt_type = raw.get("type")
    if opt_type is None:
        return None
    out["type"] = str(opt_type).lower()

    # strike must coerce to a number; otherwise skip the contract.
    strike = _coerce_num(raw.get("strike"), as_int=False)
    if strike is None:
        return None
    out["strike"] = strike

    for src_key, dst_key in _NUMERIC_ALIASES.items():
        if src_key in ("strike",):
            continue
        if dst_key in out or src_key not in raw:
            continue
        num = _coerce_num(raw[src_key], as_int=dst_key in _INT_KEYS)
        if num is not None:
            out[dst_key] = num

    # mark fallback: (bid + ask) / 2, then "last".
    if "mark" not in out:
        if "bid" in out and "ask" in out:
            out["mark"] = (out["bid"] + out["ask"]) / 2
        else:
            last = _coerce_num(raw.get("last"), as_int=False)
            if last is not None:
                out["mark"] = last

    return out


def _extract_list(obj):
    """Return the contract list from a parsed payload, or None if not found."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for key in ("data", "options"):
            value = obj.get(key)
            if isinstance(value, list):
                return value
    return None


def load_contracts(path: str) -> list[dict]:
    """Load and normalize an options chain from ``path``.

    Attempts, in order: (a) raw JSON list, (b) dict with "data"/"options" list,
    (c) MCP tool-result envelope (unwrap then a/b), (d) JSONL (one contract per
    line). Raises ValueError if none match. Malformed individual contracts are
    skipped, not fatal.
    """
    with open(path, "r") as fh:
        text = fh.read()

    raw_list = None
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None

    if parsed is not None:
        raw_list = _extract_list(parsed)
        if raw_list is None:
            # (c) MCP envelope: unwrap then retry (a)/(b).
            raw_list = _extract_list(unwrap(parsed))

    if raw_list is None:
        # (d) JSONL fallback: one contract JSON object per line.
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        if rows:
            raw_list = rows

    if raw_list is None:
        raise ValueError(f"no contracts found in {path}")

    contracts = [c for c in (_normalize(r) for r in raw_list) if c is not None]
    if not contracts:
        raise ValueError(f"no contracts found in {path}")
    return contracts


def _for_expiry(contracts, expiry):
    """Contracts matching ``expiry`` (all contracts if expiry is None)."""
    if expiry is None:
        return contracts
    return [c for c in contracts if c.get("expiration") == expiry]


def expiries(contracts) -> list[str]:
    """Sorted unique expirations across all contracts."""
    return sorted({c["expiration"] for c in contracts if c.get("expiration")})


def nearest_strike(contracts, spot, expiry, opt_type):
    """Contract of ``opt_type`` at ``expiry`` whose strike is closest to spot.

    Returns None if no such contract exists.
    """
    candidates = [
        c for c in contracts
        if c.get("expiration") == expiry and c.get("type") == opt_type and "strike" in c
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(c["strike"] - spot))


def atm_iv(contracts, spot, expiry):
    """Mean of call-IV and put-IV at the nearest strike to spot for ``expiry``.

    The nearest strike is resolved PER LEG (call and put independently), so on
    an asymmetric strike grid the legs may sit at different strikes — intentional:
    degrades gracefully when one side of the chain is sparse.
    Skips legs with missing or zero iv. Returns None if neither leg has iv.
    """
    ivs = []
    for opt_type in ("call", "put"):
        leg = nearest_strike(contracts, spot, expiry, opt_type)
        if leg is not None:
            iv = leg.get("iv")
            if iv:  # skips missing (None) and zero
                ivs.append(iv)
    if not ivs:
        return None
    return sum(ivs) / len(ivs)


def atm_iv_by_expiry(contracts, spot) -> list[dict]:
    """ATM IV for every expiry, skipping expiries where atm_iv is None."""
    out = []
    for expiry in expiries(contracts):
        value = atm_iv(contracts, spot, expiry)
        if value is not None:
            out.append({"expiry": expiry, "atm_iv": value})
    return out


def expected_move(contracts, spot, expiry):
    """Expected move from the ATM straddle at ``expiry``.

    Straddle = call.mark + put.mark at the nearest strike to spot. Returns None
    if either leg is missing or lacks a mark. one_sigma uses the 0.85 straddle
    heuristic.
    """
    call = nearest_strike(contracts, spot, expiry, "call")
    put = nearest_strike(contracts, spot, expiry, "put")
    if call is None or put is None:
        return None
    if "mark" not in call or "mark" not in put:
        return None
    straddle = call["mark"] + put["mark"]
    one_sigma = 0.85 * straddle
    return {
        "expiry": expiry,
        "straddle": straddle,
        "straddle_pct": straddle / spot,
        "one_sigma": one_sigma,
        "one_sigma_pct": one_sigma / spot,
        "range_low": spot - one_sigma,
        "range_high": spot + one_sigma,
    }


def max_pain(contracts, expiry):
    """Strike minimizing total option-writer payout at ``expiry`` settlement.

    Candidates are each listed strike at that expiry. Payout at settlement S:
        sum(call.oi * max(0, S - call.strike)) + sum(put.oi * max(0, put.strike - S)).
    Returns the candidate S minimizing payout (ties -> lowest strike). None if
    no open interest exists at the expiry at all.
    """
    legs = [c for c in _for_expiry(contracts, expiry) if "strike" in c]
    if not any(c.get("oi") for c in legs):
        return None

    strikes = sorted({c["strike"] for c in legs})
    best_strike = None
    best_payout = None
    for settlement in strikes:
        payout = 0.0
        for c in legs:
            oi = c.get("oi")
            if not oi:
                continue
            if c.get("type") == "call":
                payout += oi * max(0.0, settlement - c["strike"])
            elif c.get("type") == "put":
                payout += oi * max(0.0, c["strike"] - settlement)
        if best_payout is None or payout < best_payout:
            best_payout = payout
            best_strike = settlement
    return best_strike


def oi_walls(contracts, expiry, spot) -> dict:
    """Open-interest walls and near-money clusters at ``expiry``.

    call_wall: strike with max call OI STRICTLY ABOVE spot (None if none).
    put_wall:  strike with max put OI AT OR BELOW spot (None if none).
    near_money_clusters: top-3 by OI among all contracts within +/-10% of spot,
        each {"strike", "type", "oi"}, sorted descending by OI.
    """
    legs = _for_expiry(contracts, expiry)

    def wall(opt_type, predicate):
        # Aggregate OI per strike for this option type, filtered by predicate.
        by_strike = {}
        for c in legs:
            if c.get("type") != opt_type or "strike" not in c:
                continue
            strike = c["strike"]
            if not predicate(strike):
                continue
            by_strike[strike] = by_strike.get(strike, 0) + (c.get("oi") or 0)
        if not by_strike:
            return None
        best = max(by_strike, key=lambda s: by_strike[s])
        return {"strike": best, "oi": by_strike[best]}

    call_wall = wall("call", lambda s: s > spot)
    put_wall = wall("put", lambda s: s <= spot)

    low, high = spot * 0.9, spot * 1.1
    near = [
        {"strike": c["strike"], "type": c.get("type"), "oi": c.get("oi") or 0}
        for c in legs
        if "strike" in c and low <= c["strike"] <= high
    ]
    near.sort(key=lambda c: c["oi"], reverse=True)

    return {
        "call_wall": call_wall,
        "put_wall": put_wall,
        "near_money_clusters": near[:3],
    }


def put_call_ratio(contracts, expiry=None):
    """Put/call open-interest ratio for ``expiry`` (all expiries if None).

    Formula: sum(put oi) / sum(call oi). Returns None if call OI totals zero.
    """
    legs = _for_expiry(contracts, expiry)
    put_oi = sum(c.get("oi") or 0 for c in legs if c.get("type") == "put")
    call_oi = sum(c.get("oi") or 0 for c in legs if c.get("type") == "call")
    if call_oi == 0:
        return None
    return put_oi / call_oi


def skew_25d(contracts, spot, expiry):
    """25-delta skew = IV(25d put) - IV(25d call) at ``expiry``.

    Selects the put whose |delta| is closest to 0.25 and the call whose delta is
    closest to 0.25, among contracts that have BOTH delta and iv. Returns None if
    either side is unavailable.
    """
    legs = [
        c for c in _for_expiry(contracts, expiry)
        if c.get("delta") is not None and c.get("iv") is not None
    ]
    puts = [c for c in legs if c.get("type") == "put"]
    calls = [c for c in legs if c.get("type") == "call"]
    if not puts or not calls:
        return None
    put_25 = min(puts, key=lambda c: abs(abs(c["delta"]) - 0.25))
    call_25 = min(calls, key=lambda c: abs(abs(c["delta"]) - 0.25))
    return put_25["iv"] - call_25["iv"]


def realized_for_comparison(closes: list[float]) -> dict:
    """Realized volatility over 20 and 30 trading days for IV comparison."""
    return {
        "rv20": indicators.realized_vol(closes, 20),
        "rv30": indicators.realized_vol(closes, 30),
    }
