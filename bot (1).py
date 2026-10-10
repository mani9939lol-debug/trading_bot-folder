"""Paper-trading engine (fake money). Reads settings from Supabase, collects
votes from the five strategies, and simulates trades.

Prices come from any exchange supported by the ccxt library (public data,
no account or API key needed). No real orders are ever placed by this file.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from strategies import BUY, SELL, run_all

log = logging.getLogger("bot")

FEE = 0.001        # 0.1% simulated fee per trade
SLIPPAGE = 0.0005  # 0.05% simulated slippage
INTERVAL = os.getenv("CANDLE_INTERVAL", "1h")
MONITOR_SECONDS = 60    # how often stop-loss / take-profit / limits are checked
EVAL_SECONDS = 300      # how often strategies vote
BINANCE_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]
QUOTES = ("USDT", "USDC", "BUSD", "USD", "EUR", "GBP", "BTC", "ETH")


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _parse(ts):
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


# ------------------------------------------------------------------ symbols --
def normalize(symbol):
    """'btcusdt', 'BTC/USDT' or 'BTC-USDT' -> ('BTC/USDT', 'BTCUSDT')."""
    s = str(symbol).strip().upper().replace("-", "/")
    if "/" in s:
        base, quote = s.split("/", 1)
    else:
        for q in QUOTES:
            if s.endswith(q) and len(s) > len(q):
                base, quote = s[:-len(q)], q
                break
        else:
            raise ValueError(f"Cannot read symbol '{symbol}'")
    return f"{base}/{quote}", f"{base}{quote}"


# ----------------------------------------------------------------- database --
class DB:
    """Tiny Supabase REST client (plain HTTP, no extra libraries)."""

    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": key, "Content-Type": "application/json"}
        if not key.startswith("sb_"):  # legacy JWT-style keys need this too
            self.headers["Authorization"] = f"Bearer {key}"

    @staticmethod
    def _eq(match):
        return {k: f"eq.{v}" for k, v in (match or {}).items()}

    def select(self, table, match=None, extra=None):
        params = {"select": "*", **self._eq(match), **(extra or {})}
        r = requests.get(f"{self.base}/{table}", headers=self.headers, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def insert(self, table, row):
        h = {**self.headers, "Prefer": "return=representation"}
        r = requests.post(f"{self.base}/{table}", headers=h, json=row, timeout=15)
        r.raise_for_status()
        return r.json()

    def update(self, table, match, values):
        r = requests.patch(f"{self.base}/{table}", headers=self.headers,
                           params=self._eq(match), json=values, timeout=15)
        r.raise_for_status()


# -------------------------------------------------------------- market data --
_exchanges = {}


def _exchange(name):
    if name not in _exchanges:
        import ccxt
        if name not in ccxt.exchanges:
            raise ValueError(f"Unknown exchange '{name}'")
        _exchanges[name] = getattr(ccxt, name)({"enableRateLimit": True, "timeout": 15000})
    return _exchanges[name]


def _binance_get(path, params):
    last_err = None
    for host in BINANCE_HOSTS:
        try:
            r = requests.get(f"{host}{path}", params=params, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Binance public data unavailable: {last_err}")


def get_candles(exchange, symbol, interval=INTERVAL, limit=200):
    pair, compact = normalize(symbol)
    try:
        rows = _exchange(exchange).fetch_ohlcv(pair, interval, limit=limit)[:-1]  # drop forming candle
        if len(rows) < 60:
            raise RuntimeError("too few candles")
        return {"open": [r[1] for r in rows], "high": [r[2] for r in rows],
                "low": [r[3] for r in rows], "close": [r[4] for r in rows]}
    except Exception as e:
        log.warning("%s candles for %s failed (%s); falling back to Binance public data",
                    exchange, pair, e)
    rows = _binance_get("/api/v3/klines", {"symbol": compact, "interval": interval, "limit": limit})[:-1]
    return {"open": [float(x[1]) for x in rows], "high": [float(x[2]) for x in rows],
            "low": [float(x[3]) for x in rows], "close": [float(x[4]) for x in rows]}


def get_price(exchange, symbol):
    pair, compact = normalize(symbol)
    try:
        return float(_exchange(exchange).fetch_ticker(pair)["last"])
    except Exception as e:
        log.warning("%s price for %s failed (%s); falling back to Binance public data",
                    exchange, pair, e)
    return float(_binance_get("/api/v3/ticker/price", {"symbol": compact})["price"])


# ---------------------------------------------------------------------- bot --
class Bot:
    def __init__(self, db, candle_fn=get_candles, price_fn=get_price):
        self.db = db
        self._candles = candle_fn
        self._price = price_fn
        self.exchange = "binance"
        self.prev_status = None
        self.last_monitor = 0.0
        self.last_eval = 0.0

    # ---- helpers
    def candles(self, symbol):
        return self._candles(self.exchange, symbol)

    def price(self, symbol):
        return self._price(self.exchange, symbol)

    def event(self, message, level="info"):
        log.info("%s: %s", level, message)
        self.db.insert("bot_events", {"level": level, "message": message})

    def account(self):
        return self.db.select("paper_account", {"id": 1})[0]

    def open_positions(self):
        return self.db.select("positions", {"status": "open"})

    def equity(self, acct, prices=None):
        prices = prices or {}
        total = float(acct["cash"])
        for p in self.open_positions():
            px = prices.get(p["symbol"]) or self.price(p["symbol"])
            total += float(p["qty"]) * px
        return total

    # ---- main loop step (called every ~15 seconds)
    def tick(self):
        s = self.db.select("bot_settings", {"id": 1})[0]
        self.exchange = (s.get("exchange") or "binance").lower()
        self.db.update("bot_settings", {"id": 1}, {"last_heartbeat": _iso(_now()), "last_error": None})

        if s["close_all_requested"]:
            self.close_all("panic button")
            self.db.update("pending_approvals", {"status": "pending"},
                           {"status": "rejected", "decided_at": _iso(_now())})
            self.db.update("bot_settings", {"id": 1},
                           {"close_all_requested": False, "status": "paused"})
            self.event("PANIC: all positions closed, pending approvals cancelled, bot paused", "warning")
            self.prev_status = "paused"
            return

        # Resuming after a pause = you acknowledge the limits; reset baselines.
        if self.prev_status == "paused" and s["status"] == "running":
            eq = self.equity(self.account())
            self.db.update("paper_account", {"id": 1}, {
                "peak_equity": eq, "day": _now().date().isoformat(), "day_start_equity": eq})
            self.event("Bot resumed; loss baselines reset")
        self.prev_status = s["status"]

        self.process_approvals(s)

        if s["status"] != "running":
            return

        now = time.time()
        if now - self.last_monitor >= MONITOR_SECONDS:
            self.last_monitor = now
            self.monitor(s)
        if now - self.last_eval >= EVAL_SECONDS:
            self.last_eval = now
            self.evaluate(s)

    # ---- stop-loss / take-profit / account limits / monthly goal
    def monitor(self, s):
        positions = self.open_positions()
        prices = {sym: self.price(sym) for sym in {p["symbol"] for p in positions}}
        for p in positions:
            px = prices[p["symbol"]]
            if px <= float(p["stop_price"]):
                self.close_position(p, px, "stop_loss")
            elif px >= float(p["take_profit_price"]):
                self.close_position(p, px, "take_profit")
        self.check_limits(s, prices)
        self.check_goal(s)

    def check_limits(self, s, prices):
        acct = self.account()
        eq = self.equity(acct, prices)
        today = _now().date().isoformat()
        day_start = acct["day_start_equity"]
        if acct["day"] != today or day_start is None:
            day_start = eq
        peak = max(float(acct["peak_equity"]), eq)
        self.db.update("paper_account", {"id": 1}, {
            "day": today, "day_start_equity": day_start, "peak_equity": peak,
            "last_equity": eq, "updated_at": _iso(_now())})

        if eq <= float(day_start) * (1 - float(s["max_daily_loss_pct"]) / 100):
            self.pause("Daily loss limit hit")
        elif eq <= peak * (1 - float(s["max_drawdown_pct"]) / 100):
            self.pause("Max drawdown limit hit")

    def check_goal(self, s):
        goal = s.get("monthly_goal_usd")
        if not s.get("pause_on_goal") or not goal:
            return
        month = _now().strftime("%Y-%m")
        if s.get("goal_hit_month") == month:
            return
        month_start = _now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        closed = self.db.select("positions", {"status": "closed"},
                                {"closed_at": f"gte.{_iso(month_start)}"})
        realized = sum(float(p["pnl"] or 0) for p in closed)
        if realized >= float(goal):
            self.db.update("bot_settings", {"id": 1}, {"goal_hit_month": month})
            self.pause(f"Monthly profit goal reached (+{realized:.2f})")

    def pause(self, reason):
        self.db.update("bot_settings", {"id": 1}, {"status": "paused"})
        self.prev_status = "paused"
        self.event(f"Bot paused automatically: {reason}", "warning")

    # ---- voting
    def evaluate(self, s):
        enabled = s["strategies_enabled"]
        n_enabled = max(1, sum(1 for v in enabled.values() if v))
        threshold = min(max(1, int(s["vote_threshold"])), n_enabled)

        configured = []
        for raw in s["symbols"]:
            try:
                configured.append(normalize(raw)[1])
            except ValueError as e:
                self.db.update("bot_settings", {"id": 1}, {"last_error": str(e)[:300]})
        # More important coins are looked at first, so they get the cash first.
        for symbol in sorted(dict.fromkeys(configured), key=lambda x: -self.weight(s, x)):
            try:
                votes = run_all(self.candles(symbol), enabled)
                buys = sum(1 for v in votes.values() if v == BUY)
                sells = sum(1 for v in votes.values() if v == SELL)
                price = self.price(symbol)
                held = [p for p in self.open_positions() if p["symbol"] == symbol]

                action, message = "none", ""
                if held and sells >= threshold:
                    for p in held:
                        self.close_position(p, price, "sell_vote")
                    action, message = "sell", f"{sells} of {len(votes)} strategies voted sell"
                elif buys >= threshold:
                    blocked = self.entry_blocked(symbol, s)
                    if blocked:
                        message = blocked
                    else:
                        action, message = self.open_position(
                            symbol, price, s, votes, weight=self.weight(s, symbol))

                if buys or sells or action != "none":
                    self.db.insert("decision_logs", {
                        "symbol": symbol, "price": price, "votes": votes,
                        "buy_votes": buys, "sell_votes": sells,
                        "action": action, "message": message})
            except Exception as e:
                log.exception("evaluate failed for %s", symbol)
                self.db.update("bot_settings", {"id": 1}, {"last_error": f"{symbol}: {e}"[:300]})

    # ---- importance and limits for running several trades at once
    @staticmethod
    def weight(s, symbol):
        """Coin importance 1-5 (default 3). Scales position size and decides who is served first."""
        weights = s.get("symbol_weights") or {}
        for key, val in weights.items():
            try:
                if normalize(key)[1] == symbol:
                    return min(5.0, max(0.5, float(val)))
            except (ValueError, TypeError):
                continue
        return 3.0

    def entry_blocked(self, symbol, s):
        """Return a reason string if a new trade on this coin is not allowed right now."""
        open_all = self.open_positions()
        mine = [p for p in open_all if p["symbol"] == symbol]
        max_open = int(s.get("max_open_positions") or 5)
        max_coin = int(s.get("max_positions_per_coin") or 1)
        cooldown = int(s.get("entry_cooldown_min") or 60)
        if len(open_all) >= max_open:
            return f"skipped: already holding {len(open_all)} trades (limit {max_open})"
        if len(mine) >= max_coin:
            return f"skipped: already {len(mine)} trade(s) in {symbol} (limit {max_coin})"
        if mine:
            newest = max(_parse(p["opened_at"]) for p in mine)
            if _now() - newest < timedelta(minutes=cooldown):
                return f"skipped: last {symbol} trade was under {cooldown} min ago"
        return None

    def exposure_room(self, s, equity):
        """Dollars still available before the total-exposure cap is reached."""
        cap = equity * float(s.get("max_total_exposure_pct") or 60) / 100
        used = sum(float(p["qty"]) * float(p["entry_price"]) for p in self.open_positions())
        return cap - used

    # ---- approvals for big trades
    def request_approval(self, symbol, price, s, votes, size):
        pending = [a for a in self.db.select("pending_approvals", {"status": "pending"})
                   if a["symbol"] == symbol]
        if pending:
            return "skipped", "approval already pending"
        minutes = int(s.get("approval_timeout_min") or 30)
        self.db.insert("pending_approvals", {
            "symbol": symbol, "side": "buy", "size_usd": round(size, 2), "price": price,
            "votes": votes, "expires_at": _iso(_now() + timedelta(minutes=minutes))})
        self.event(f"Approval needed: BUY {symbol} about ${size:,.0f} (expires in {minutes} min)", "warning")
        return "pending", f"waiting for your approval (${size:,.0f})"

    def process_approvals(self, s):
        for a in self.db.select("pending_approvals", {"status": "pending"}):
            if _parse(a["expires_at"]) <= _now():
                self.db.update("pending_approvals", {"id": a["id"]},
                               {"status": "expired", "decided_at": _iso(_now())})
                self.event(f"Approval for {a['symbol']} expired; no trade made")
        for a in self.db.select("pending_approvals", {"status": "approved"}):
            blocked = self.entry_blocked(a["symbol"], s)
            if s["status"] != "running" or blocked:
                self.db.update("pending_approvals", {"id": a["id"]}, {"status": "expired"})
                self.event(f"Approved {a['symbol']} trade skipped ({blocked or 'bot paused'})")
                continue
            price = self.price(a["symbol"])
            result, message = self.open_position(a["symbol"], price, s, a.get("votes") or {},
                                                 approved_size=float(a["size_usd"]),
                                                 weight=self.weight(s, a["symbol"]))
            self.db.update("pending_approvals", {"id": a["id"]}, {"status": "executed"})
            self.db.insert("decision_logs", {
                "symbol": a["symbol"], "price": price, "votes": a.get("votes"),
                "action": result, "message": f"approved by you: {message}"})

    # ---- simulated orders
    def open_position(self, symbol, price, s, votes, approved_size=None, weight=3.0):
        acct = self.account()
        cash = float(acct["cash"])
        eq = self.equity(acct)
        # importance 3 = normal size; 5 = about 1.7x; 1 = about 0.3x
        size = eq * float(s["position_size_pct"]) / 100 * (weight / 3.0)
        size = min(size, cash / (1 + FEE), self.exposure_room(s, eq))
        if approved_size is None:
            limit = s.get("big_trade_usd")
            if limit and size > float(limit):
                return self.request_approval(symbol, price, s, votes, size)
        else:
            size = min(approved_size, cash / (1 + FEE), self.exposure_room(s, eq))
        if size < 10:
            return "skipped", "not enough cash for a new position"
        fill = price * (1 + SLIPPAGE)
        qty, fee = size / fill, size * FEE
        self.db.insert("positions", {
            "symbol": symbol, "qty": qty, "entry_price": fill, "entry_fee": fee,
            "stop_price": fill * (1 - float(s["stop_loss_pct"]) / 100),
            "take_profit_price": fill * (1 + float(s["take_profit_pct"]) / 100),
            "entry_votes": votes})
        self.db.update("paper_account", {"id": 1}, {"cash": cash - size - fee})
        n_buy = sum(1 for v in votes.values() if v == BUY)
        return "buy", f"{n_buy} of {len(votes)} strategies voted buy"

    def close_position(self, p, price, reason):
        fill = price * (1 - SLIPPAGE)
        qty = float(p["qty"])
        proceeds = qty * fill
        fee = proceeds * FEE
        pnl = proceeds - fee - qty * float(p["entry_price"]) - float(p["entry_fee"])
        acct = self.account()
        self.db.update("paper_account", {"id": 1}, {"cash": float(acct["cash"]) + proceeds - fee})
        self.db.update("positions", {"id": p["id"]}, {
            "status": "closed", "closed_at": _iso(_now()), "exit_price": fill,
            "pnl": pnl, "exit_reason": reason})
        self.event(f"Closed {p['symbol']} ({reason}) P&L {pnl:+.2f}")

    def close_all(self, reason):
        for p in self.open_positions():
            self.close_position(p, self.price(p["symbol"]), reason)
