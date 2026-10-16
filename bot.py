#!/usr/bin/env python3
"""Gold Telegram Alert v1.0 — rule-based, free, single-user signal notifier.

Input: Yahoo Finance COMEX gold futures GC=F (delayed, NOT broker XAUUSD).
Output: Telegram alerts; no AI API, no broker trading, no news feed.
Python 3.10+ standard library only.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Optional
from urllib import error, parse, request

UTC = timezone.utc
BAR_SECONDS = 15 * 60
SYMBOL = os.getenv("GOLD_SYMBOL", "GC=F")
STATE_PATH = Path(os.getenv("BOT_STATE_PATH", "state.json"))
MAX_AGE_MINUTES = int(os.getenv("MAX_DATA_AGE_MINUTES", "75"))
PAUSE_SIGNALS = os.getenv("PAUSE_SIGNALS", "false").lower() in {"1", "true", "yes"}
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


@dataclass(frozen=True)
class Bar:
    start: int  # Unix epoch UTC, start of candle
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Decision:
    side: str  # BUY, SELL, WAIT
    candle: int
    close: float
    reason: str
    atr: Optional[float] = None
    rsi: Optional[float] = None
    adx: Optional[float] = None
    h1: str = "UNKNOWN"
    h4: str = "UNKNOWN"
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None


def http_json(url: str, *, post_data: Optional[dict] = None, timeout=16) -> dict:
    data = None if post_data is None else json.dumps(post_data).encode("utf-8")
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; GoldAlertResearch/1.0)",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = request.Request(url, headers=headers, data=data, method="POST" if data else "GET")
    with request.urlopen(req, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result


def parse_yahoo_chart(payload: dict) -> list[Bar]:
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise ValueError(f"Yahoo chart error: {chart['error']}")
    items = chart.get("result") or []
    if not items:
        raise ValueError("Yahoo Finance returned no chart result")
    item = items[0]
    stamps = item.get("timestamp") or []
    quotes = ((item.get("indicators") or {}).get("quote") or [{}])[0]
    bars = []
    for i, t in enumerate(stamps):
        vals = [((quotes.get(key) or [])[i] if i < len(quotes.get(key) or []) else None)
                for key in ("open", "high", "low", "close")]
        if any(v is None or not math.isfinite(float(v)) for v in vals):
            continue
        o, h, l, c = map(float, vals)
        if l <= 0 or h < l or max(o, c) > h + 1e-6 or min(o, c) < l - 1e-6:
            continue
        vols = quotes.get("volume") or []
        vol = vols[i] if i < len(vols) else 0
        bars.append(Bar(int(t), o, h, l, c, float(vol or 0)))
    return list(sorted({b.start: b for b in bars}.values(), key=lambda b: b.start))


def fetch_yahoo(symbol: str = SYMBOL) -> list[Bar]:
    encoded = parse.quote(symbol, safe="")
    query = "?range=30d&interval=15m&includePrePost=true"
    errs = []
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{encoded}{query}"
        try:
            bars = parse_yahoo_chart(http_json(url))
            if len(bars) < 400:
                raise RuntimeError("Too few 15m historical candles")
            return bars
        except (ValueError, KeyError, error.HTTPError, error.URLError, TimeoutError, RuntimeError) as exc:
            errs.append(f"{host}: {type(exc).__name__}: {exc}")
            time.sleep(1)
    raise RuntimeError("Data source not available. " + " | ".join(errs))


def closed_15m(bars: list[Bar], now: int) -> list[Bar]:
    """Exclude the still-open candle and allow a 60-second closure buffer."""
    return [b for b in bars if b.start + BAR_SECONDS <= now - 60]


def resample(bars: list[Bar], seconds: int, now: int) -> list[Bar]:
    """Build H1/H4 bars; only completed groups with mostly complete 15m coverage."""
    groups: dict[int, list[Bar]] = {}
    for b in bars:
        k = b.start // seconds * seconds
        groups.setdefault(k, []).append(b)
    minimum = max(2, int(seconds / BAR_SECONDS * 0.75))
    result = []
    for k in sorted(groups):
        subset = sorted(groups[k], key=lambda x: x.start)
        if k + seconds > now - 60 or len(subset) < minimum:
            continue
        # Do not interpolate exchange breaks; reject large gaps inside a group.
        if any(b.start - a.start > BAR_SECONDS * 2 for a, b in zip(subset, subset[1:])):
            continue
        result.append(Bar(k, subset[0].open, max(x.high for x in subset),
                          min(x.low for x in subset), subset[-1].close,
                          sum(x.volume for x in subset)))
    return result


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for x in values[1:]:
        out.append(alpha * x + (1.0 - alpha) * out[-1])
    return out


def rsi(bars: list[Bar], n=14) -> list[Optional[float]]:
    close = [b.close for b in bars]
    out: list[Optional[float]] = [None] * len(close)
    if len(close) <= n:
        return out
    changes = [close[i] - close[i - 1] for i in range(1, len(close))]
    gain = sum(max(x, 0.0) for x in changes[:n]) / n
    loss = sum(max(-x, 0.0) for x in changes[:n]) / n
    def calc(g, l):
        return 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    out[n] = calc(gain, loss)
    for i in range(n + 1, len(close)):
        delta = changes[i - 1]
        gain = (gain * (n - 1) + max(delta, 0.0)) / n
        loss = (loss * (n - 1) + max(-delta, 0.0)) / n
        out[i] = calc(gain, loss)
    return out


def atr(bars: list[Bar], n=14) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(bars)
    if len(bars) < n:
        return out
    tr = [bars[0].high - bars[0].low]
    for i in range(1, len(bars)):
        b, previous = bars[i], bars[i - 1]
        tr.append(max(b.high - b.low, abs(b.high - previous.close),
                      abs(b.low - previous.close)))
    value = sum(tr[:n]) / n
    out[n - 1] = value
    for i in range(n, len(bars)):
        value = (value * (n - 1) + tr[i]) / n
        out[i] = value
    return out


def adx(bars: list[Bar], n=14) -> list[Optional[float]]:
    """Wilder directional movement; first valid ADX after 2n-1 periods."""
    out: list[Optional[float]] = [None] * len(bars)
    if len(bars) < 2 * n + 1:
        return out
    trs, plus, minus = [], [], []
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        up, down = b.high - p.high, p.low - b.low
        trs.append(max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close)))
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
    sm_tr, sm_p, sm_m = sum(trs[:n]), sum(plus[:n]), sum(minus[:n])
    dx_values: list[float] = []
    for i in range(n, len(bars)):
        if i > n:
            sm_tr = sm_tr - sm_tr / n + trs[i - 1]
            sm_p = sm_p - sm_p / n + plus[i - 1]
            sm_m = sm_m - sm_m / n + minus[i - 1]
        pdi = 100 * sm_p / sm_tr if sm_tr else 0
        mdi = 100 * sm_m / sm_tr if sm_tr else 0
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0
        dx_values.append(dx)
        if len(dx_values) == n:
            out[i] = sum(dx_values) / n
        elif len(dx_values) > n:
            out[i] = (out[i - 1] * (n - 1) + dx) / n  # type: ignore[operator]
    return out


def trend(bars: list[Bar]) -> str:
    if len(bars) < 65:
        return "UNKNOWN"
    closes = [b.close for b in bars]
    fast, slow = ema(closes, 21), ema(closes, 55)
    if fast[-1] > slow[-1] and fast[-1] > fast[-3]:
        return "UP"
    if fast[-1] < slow[-1] and fast[-1] < fast[-3]:
        return "DOWN"
    return "SIDEWAYS"


def evaluate(bars: list[Bar], now: int, symbol: str = SYMBOL) -> Decision:
    m15 = closed_15m(bars, now)
    if len(m15) < 150:
        raise ValueError("Insufficient completed 15-minute candles")
    last = m15[-1]
    age = (now - (last.start + BAR_SECONDS)) / 60
    if age < 0 or age > MAX_AGE_MINUTES:
        return Decision("WAIT", last.start, last.close, f"Market data stale ({age:.0f} minutes)")
    h1, h4 = resample(m15, 3600, now), resample(m15, 14400, now)
    t1, t4 = trend(h1), trend(h4)
    av, rv, dv = atr(m15)[-1], rsi(m15)[-1], adx(m15)[-1]
    if not av or rv is None or dv is None:
        raise ValueError("Indicators unavailable")
    close = [b.close for b in m15]
    fast, slow = ema(close, 21), ema(close, 55)
    candle_body = abs(last.close - last.open)
    prev = m15[-2]
    up = t1 == t4 == "UP"
    down = t1 == t4 == "DOWN"
    recent_low = min(b.low for b in m15[-9:-1])
    recent_high = max(b.high for b in m15[-9:-1])
    if PAUSE_SIGNALS:
        why = "Manual news-risk pause is ON"
    elif not (up or down):
        why = "H1/H4 trends not aligned"
    elif dv < 18:
        why = "ADX below 18; trend insufficient"
    elif up and not (52 <= rv <= 72):
        why = "RSI outside BUY confirmation band"
    elif down and not (28 <= rv <= 48):
        why = "RSI outside SELL confirmation band"
    else:
        why = "Awaiting closed-candle pullback + break trigger"
    if not PAUSE_SIGNALS and dv >= 18 and av > 0:
        # Pullback in the previous 4 completed candles; current candle confirms.
        pullback_buy = min(b.low for b in m15[-5:-1]) <= fast[-2] + 0.4 * av
        pullback_sell = max(b.high for b in m15[-5:-1]) >= fast[-2] - 0.4 * av
        bullish = (up and 52 <= rv <= 72 and fast[-1] > slow[-1]
                   and last.close > fast[-1] and last.close > prev.high
                   and last.close > last.open and candle_body >= 0.2 * av
                   and pullback_buy)
        bearish = (down and 28 <= rv <= 48 and fast[-1] < slow[-1]
                   and last.close < fast[-1] and last.close < prev.low
                   and last.close < last.open and candle_body >= 0.2 * av
                   and pullback_sell)
        if bullish or bearish:
            side = "BUY" if bullish else "SELL"
            entry = last.close
            if bullish:
                sl = min(recent_low - 0.15 * av, entry - 1.5 * av)
                distance = entry - sl
                tp1, tp2 = entry + 1.5 * distance, entry + 2.5 * distance
            else:
                sl = max(recent_high + 0.15 * av, entry + 1.5 * av)
                distance = sl - entry
                tp1, tp2 = entry - 1.5 * distance, entry - 2.5 * distance
            if 0 < distance <= 4.5 * av:
                return Decision(side, last.start, last.close,
                                "H1/H4 aligned; M15 pullback and candle close confirmed",
                                av, rv, dv, t1, t4, entry, sl, tp1, tp2)
            why = "Stop loss too far from entry; trade rejected"
    return Decision("WAIT", last.start, last.close, why, av, rv, dv, t1, t4)


def iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def message(decision: Decision, symbol: str, now: int) -> str:
    stamp = iso_utc(decision.candle)
    age = max(0, (now - (decision.candle + BAR_SECONDS)) // 60)
    # Important: NEVER label futures levels as XAUUSD/Alpari entry prices.
    header = f"🟡 GOLD ALERT v1 | {symbol} (COMEX futures proxy)\n"
    header += f"M15 candle: {stamp}\nFeed age after close: {age} min (may be delayed)\n"
    header += f"H1: {decision.h1} | H4: {decision.h4}\n"
    metrics = (f"RSI: {decision.rsi:.1f} | ADX: {decision.adx:.1f} | "
               f"ATR: {decision.atr:.2f}\n" if decision.atr is not None and
               decision.rsi is not None and decision.adx is not None else "")
    if decision.side != "WAIT":
        return (header + f"\n{'🟢' if decision.side == 'BUY' else '🔴'} {decision.side} (research setup)\n" +
                f"Proxy entry: {decision.entry:.2f}\nProxy SL: {decision.sl:.2f}\n"
                f"Proxy TP1: {decision.tp1:.2f} (1.5R)\n"
                f"Proxy TP2: {decision.tp2:.2f} (2.5R)\n" + metrics +
                f"Why: {decision.reason}\n\n"
                "⚠️ Prices are GC=F gold futures, NOT Alpari XAUUSD. "
                "Do not place these numeric prices as broker orders without "
                "independently checking the broker chart, spread, news and risk. "
                "Not an automatic trade or guaranteed signal.")
    return (header + f"\n⏳ WAIT / NO TRADE\nClose: {decision.close:.2f}\n" +
            metrics + f"Why: {decision.reason}\n"
            "⚠️ GC=F futures are not XAUUSD spot. No order has been placed.")


def telegram(method: str, data: dict) -> dict:
    if not TG_TOKEN:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN secret")
    result = http_json(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", post_data=data)
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} did not succeed: {result.get('description')}")
    return result


def send(text: str) -> None:
    if not TG_CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_CHAT_ID secret")
    telegram("sendMessage", {"chat_id": TG_CHAT_ID, "text": text,
                             "disable_web_page_preview": True})


def setup_chat_id() -> None:
    result = telegram("getUpdates", {"limit": 100, "timeout": 0})
    updates = result.get("result", [])
    private_chats = [x.get("message", {}).get("chat", {}) for x in updates]
    ids = [c.get("id") for c in private_chats if c.get("type") == "private" and c.get("id")]
    if not ids:
        print("No private message found. Open your bot in Telegram, send /start, then run setup again.")
        return
    # GitHub public-repo run logs are public: deliver private ID only inside Telegram.
    telegram("sendMessage", {"chat_id": str(ids[-1]),
                             "text": ("🔐 Your private Telegram chat ID: "
                                      f"{ids[-1]}\n\nCopy this number into GitHub "
                                      "Settings → Secrets and variables → Actions "
                                      "as TELEGRAM_CHAT_ID. Do not post your bot token."),
                             "disable_web_page_preview": True})
    print("Chat ID sent privately to your Telegram bot conversation. No ID printed in public logs.")


def read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text("utf-8"))
    except FileNotFoundError:
        return {"last_alert_id": "", "sent_at_utc": ""}


def save_state(event_id: str, now: int) -> None:
    value = {"last_alert_id": event_id, "sent_at_utc": iso_utc(now)}
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(STATE_PATH)


def run(mode: str, now: Optional[int] = None) -> None:
    if mode == "setup":
        setup_chat_id()
        return
    if mode == "test":
        send("✅ Gold Telegram Alert v1 connected. This is a delivery test, NOT a trade signal.")
        print("Test message sent to Telegram")
        return
    now = int(time.time()) if now is None else now
    bars = fetch_yahoo(SYMBOL)
    decision = evaluate(bars, now, SYMBOL)
    print(f"Data: {SYMBOL}, last closed M15={iso_utc(decision.candle)}")
    print(f"Decision: {decision.side} / {decision.reason}")
    if mode == "status":
        send(message(decision, SYMBOL, now))
        print("Status sent")
    elif mode == "dry-run":
        print(message(decision, SYMBOL, now))
    elif mode == "scan":
        if decision.side == "WAIT":
            print("No new eligible setup; nothing sent")
            return
        alert_id = f"{SYMBOL}:{decision.candle}:{decision.side}"
        if read_state().get("last_alert_id") == alert_id:
            print("Duplicate alert suppressed")
            return
        # Freshness verified in evaluate(); send then store ID, never before send.
        send(message(decision, SYMBOL, now))
        save_state(alert_id, now)
        print(f"Alert delivered, saved id={alert_id}")
    else:
        raise ValueError(f"Unknown mode: {mode}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["setup", "test", "status", "scan", "dry-run"],
                        default="scan")
    args = parser.parse_args()
    try:
        run(args.mode)
    except Exception as exc:
        # Keep credentials out of exception logs (HTTP URL can contain bot token!).
        detail = str(exc)
        if TG_TOKEN:
            detail = detail.replace(TG_TOKEN, "[REDACTED_BOT_TOKEN]")
        print(f"ERROR: {type(exc).__name__}: {detail}", file=sys.stderr)
        sys.exit(1)
