"""Five strategies that each cast a vote: 'buy', 'sell' or 'hold'.

Each function receives candles as a dict of lists (oldest -> newest, closed
candles only): {"open": [...], "high": [...], "low": [...], "close": [...]}
"""

BUY, SELL, HOLD = "buy", "sell", "hold"


def _sma(values, n):
    return sum(values[-n:]) / n


def _rsi(closes, n=14):
    """Wilder's RSI."""
    if len(closes) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_gain, avg_loss = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(d, 0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-d, 0)) / n
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


# 1. Dip buyer ---------------------------------------------------------------
def dip_buyer(c, lookback=24, drop_pct=3.0, rise_pct=3.0):
    closes = c["close"][-lookback:]
    if len(closes) < lookback:
        return HOLD
    last, hi, lo = closes[-1], max(closes), min(closes)
    if (hi - last) / hi * 100 >= drop_pct:
        return BUY
    if (last - lo) / lo * 100 >= rise_pct:
        return SELL
    return HOLD


# 2. Moving average trend ------------------------------------------------------
def ma_cross(c, fast=10, slow=30, margin=0.001):
    closes = c["close"]
    if len(closes) < slow:
        return HOLD
    f, s = _sma(closes, fast), _sma(closes, slow)
    if f > s * (1 + margin):
        return BUY
    if f < s * (1 - margin):
        return SELL
    return HOLD


# 3. RSI -----------------------------------------------------------------------
def rsi_strategy(c, low=35, high=65):
    value = _rsi(c["close"])
    if value is None:
        return HOLD
    if value < low:
        return BUY
    if value > high:
        return SELL
    return HOLD


# 4. Breakout ------------------------------------------------------------------
def breakout(c, up_lookback=20, down_lookback=10):
    h, l, cl = c["high"], c["low"], c["close"]
    if len(cl) < up_lookback + 1:
        return HOLD
    if cl[-1] > max(h[-up_lookback - 1:-1]):
        return BUY
    if cl[-1] < min(l[-down_lookback - 1:-1]):
        return SELL
    return HOLD


# 5. Price action at key levels -------------------------------------------------
def _cluster(levels, tol):
    """Merge nearby price levels; keep zones touched at least twice."""
    groups = []
    for x in sorted(levels):
        if groups and abs(x - sum(groups[-1]) / len(groups[-1])) / x <= tol:
            groups[-1].append(x)
        else:
            groups.append([x])
    return [sum(g) / len(g) for g in groups if len(g) >= 2]


def key_levels(c, lookback=150, tol=0.004, pivot=3):
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    if len(cl) < 60:
        return HOLD
    # find swing lows/highs on history, excluding the newest candle
    hh, ll = h[:-1][-lookback:], l[:-1][-lookback:]
    supports, resistances = [], []
    for i in range(pivot, len(ll) - pivot):
        if ll[i] == min(ll[i - pivot:i + pivot + 1]):
            supports.append(ll[i])
        if hh[i] == max(hh[i - pivot:i + pivot + 1]):
            resistances.append(hh[i])
    supports, resistances = _cluster(supports, tol), _cluster(resistances, tol)

    op, hi, lo, close, prev_close = o[-1], h[-1], l[-1], cl[-1], cl[-2]
    body = abs(close - op)
    lower_wick = min(op, close) - lo
    upper_wick = hi - max(op, close)

    for s in supports:
        if lo <= s * (1 + tol) and close > s and lower_wick > 0 and lower_wick >= body and close >= op:
            return BUY  # touched support and bounced (rejection candle)
        if close < s * (1 - tol) and prev_close >= s:
            return SELL  # support broke
    for r in resistances:
        if hi >= r * (1 - tol) and close < r and upper_wick > 0 and upper_wick >= body and close <= op:
            return SELL  # touched resistance and got rejected
    return HOLD


STRATEGIES = {
    "dip_buyer": dip_buyer,
    "ma_cross": ma_cross,
    "rsi": rsi_strategy,
    "breakout": breakout,
    "key_levels": key_levels,
}


def run_all(candles, enabled=None):
    """Return {strategy_name: vote} for every enabled strategy."""
    enabled = enabled or {}
    votes = {}
    for name, fn in STRATEGIES.items():
        if not enabled.get(name, True):
            continue
        try:
            votes[name] = fn(candles)
        except Exception:
            votes[name] = HOLD
    return votes
