"""Paper-trading engine: reads settings from Supabase, collects votes from the
five strategies, and simulates trades with fake money. No exchange account
or API key is used - only public price data."""

import logging
import os
import time
from datetime import datetime, timezone

import requests

from strategies import BUY, SELL, run_all

log = logging.getLogger("bot")

FEE = 0.001        # 0.1% simulated fee per trade
SLIPPAGE = 0.0005  # 0.05% simulated slippage
INTERVAL = os.getenv("CANDLE_INTERVAL", "1h")
MONITOR_SECONDS = 60    # how often stop-loss / take-profit are checked
EVAL_SECONDS = 300      # how often strategies vote
PRICE_HOSTS = ["https://data-api.binance.vision", "https://api.binance.com"]


# ---------------------------------------------------------------- database --
class DB:
    """Tiny Supabase REST client (uses plain HTTP, no extra libraries)."""

    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {"apikey": key, "Content-Type": "application/json"}
        if not key.startswith("sb_"):  # legacy JWT-style keys need this too
            self.headers["Authorization"] = f"Bearer {key}"

    @staticmethod
    def _eq(match):
        return {k: f"eq.{v}" for k, v in (match or {}).items()}

    def select(self, table, match=None):
        r = requests.get(f"{self.base}/{table}", headers=self.headers,
                         params={"select": "*", **self._eq(match)}, timeout=15)
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


# ------------------------------------------------------------- market data --
def get_candles(symbol, interval=INTERVAL, limit=200):
    last_err = None
    for host in PRICE_HOSTS:
        try:
            r = requests.get(f"{host}/api/v3/klines", timeout=15,
                             params={"symbol": symbol, "interval": interval, "limit": limit})
            r.raise_for_status()
            rows = r.json()[:-1]  # drop the still-forming candle
            return {
                "open": [float(x[1]) for x in rows],
                "high": [float(x[2]) for x in rows],
                "low": [float(x[3]) for x in rows],
                "close": [float(x[4]) for x in rows],
            }
        except Exception as e:  # try next host
            last_err = e
    raise RuntimeError(f"Could not fetch candles for {symbol}: {last_err}")


def get_price(symbol):
    last_err = None
    for host in PRICE_HOSTS:
        try:
            r = requests.get(f"{host}/api/v3/ticker/price", params={"symbol": symbol}, timeout=15)
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Could not fetch price for {symbol}: {last_err}")


def _now():
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------- bot --
class Bot:
    def __init__(self, db, candle_fn=get_candles, price_fn=get_price):
        self.db = db
        self.candles = candle_fn
        self.price = price_fn
        self.prev_status = None
        self.last_monitor = 0.0
        self.last_eval = 0.0

    # ---- helpers
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

    # ---- main loop step (called every few seconds)
    def tick(self):
        s = self.db.select("bot_settings", {"id": 1})[0]
        self.db.update("bot_settings", {"id": 1}, {"last_heartbeat": _now(), "last_error": None})

        if s["close_all_requested"]:
            self.close_all("panic button")
            self.db.update("bot_settings", {"id": 1},
                           {"close_all_requested": False, "status": "paused"})
            self.event("PANIC: all positions closed and bot paused", "warning")
            self.prev_status = "paused"
            return

        # Resuming after a pause = you acknowledge the limits; reset baselines.
        if self.prev_status == "paused" and s["status"] == "running":
            acct = self.account()
            eq = self.equity(acct)
            self.db.update("paper_account", {"id": 1}, {
                "peak_equity": eq, "day": datetime.now(timezone.utc).date().isoformat(),
                "day_start_equity": eq})
            self.event("Bot resumed; loss baselines reset")
        self.prev_status = s["status"]

        if s["status"] != "running":
            return

        now = time.time()
        if now - self.last_monitor >= MONITOR_SECONDS:
            self.last_monitor = now
            self.monitor(s)
        if now - self.last_eval >= EVAL_SECONDS:
            self.last_eval = now
            self.evaluate(s)

    # ---- stop-loss / take-profit / account limits
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

    def check_limits(self, s, prices):
        acct = self.account()
        eq = self.equity(acct, prices)
        today = datetime.now(timezone.utc).date().isoformat()
        day_start = acct["day_start_equity"]
        if acct["day"] != today or day_start is None:
            day_start = eq
        peak = max(float(acct["peak_equity"]), eq)
        self.db.update("paper_account", {"id": 1}, {
            "day": today, "day_start_equity": day_start, "peak_equity": peak,
            "last_equity": eq, "updated_at": _now()})

        if eq <= float(day_start) * (1 - float(s["max_daily_loss_pct"]) / 100):
            self.pause("Daily loss limit hit")
        elif eq <= peak * (1 - float(s["max_drawdown_pct"]) / 100):
            self.pause("Max drawdown limit hit")

    def pause(self, reason):
        self.db.update("bot_settings", {"id": 1}, {"status": "paused"})
        self.prev_status = "paused"
        self.event(f"Bot paused automatically: {reason}", "warning")

    # ---- voting
    def evaluate(self, s):
        enabled = s["strategies_enabled"]
        n_enabled = max(1, sum(1 for v in enabled.values() if v))
        threshold = min(max(1, int(s["vote_threshold"])), n_enabled)

        for symbol in s["symbols"]:
            try:
                candles = self.candles(symbol)
                votes = run_all(candles, enabled)
                buys = sum(1 for v in votes.values() if v == BUY)
                sells = sum(1 for v in votes.values() if v == SELL)
                price = self.price(symbol)
                held = [p for p in self.open_positions() if p["symbol"] == symbol]

                action, message = "none", ""
                if not held and buys >= threshold:
                    action, message = self.open_position(symbol, price, s, votes)
                elif held and sells >= threshold:
                    self.close_position(held[0], price, "sell_vote")
                    action, message = "sell", f"{sells} of {len(votes)} strategies voted sell"

                if buys or sells or action != "none":
                    self.db.insert("decision_logs", {
                        "symbol": symbol, "price": price, "votes": votes,
                        "buy_votes": buys, "sell_votes": sells,
                        "action": action, "message": message})
            except Exception as e:
                log.exception("evaluate failed for %s", symbol)
                self.db.update("bot_settings", {"id": 1}, {"last_error": f"{symbol}: {e}"[:300]})

    # ---- simulated orders
    def open_position(self, symbol, price, s, votes):
        acct = self.account()
        cash = float(acct["cash"])
        eq = self.equity(acct)
        size = min(eq * float(s["position_size_pct"]) / 100, cash / (1 + FEE))
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
            "status": "closed", "closed_at": _now(), "exit_price": fill,
            "pnl": pnl, "exit_reason": reason})
        self.event(f"Closed {p['symbol']} ({reason}) P&L {pnl:+.2f}")

    def close_all(self, reason):
        for p in self.open_positions():
            self.close_position(p, self.price(p["symbol"]), reason)
