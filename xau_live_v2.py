#!/usr/bin/env python3
"""Twelve Data XAU/USD bridge for the existing rule engine and Gemini overlay.

Uses the existing bot.py and ai_overlay.py unchanged. Research alerts only.
Gemini transient-failure retry and optional lower-cost fallback added in v2.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
import sys
import time
import random
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import bot
import ai_overlay as ai

SYMBOL = "XAU/USD"
SOURCE = "Twelve Data XAU/USD spot reference (not a broker execution quote)"
bot.SYMBOL = SYMBOL

# Correct the description passed to Gemini without editing the old AI file.
_previous_summary = ai.summary_payload

def xau_summary(decision, bars, now):
    payload = _previous_summary(decision, bars, now)
    payload["instrument"] = SYMBOL
    payload["feed"] = SOURCE
    return payload

ai.summary_payload = xau_summary
ai.SYSTEM_TEXT = ai.SYSTEM_TEXT.replace(
    "این داده GC=F آتی طلا با احتمال تأخیر است، نه XAUUSD بروکر.",
    "این داده XAU/USD اسپات از Twelve Data با احتمال تأخیر است؛ قیمت اجرایی بروکر نیست."
)


# Leave the existing Gemini payload, trading rules and Telegram delivery untouched.
# Only retry temporary backend errors. If the primary model remains unavailable,
# use a supported, less costly Gemini model as an advisory fallback.
_original_gemini_review = ai.gemini_review


def resilient_gemini_review(decision, bars, now):
    primary_model = ai.MODEL
    backup_model = "gemini-3.5-flash-lite"
    models = [primary_model]
    if primary_model != backup_model:
        models.append(backup_model)
    try:
        last_error = None
        for model_index, model in enumerate(models):
            ai.MODEL = model
            max_attempts = 3 if model_index == 0 else 2
            for attempt in range(max_attempts):
                try:
                    result = _original_gemini_review(decision, bars, now)
                    print(f"Gemini response received; model={model}; attempt={attempt + 1}")
                    return result
                except HTTPError as exc:
                    # Do not retry bad requests, invalid keys, missing model or billing errors.
                    # Avoid a fallback on 429 (rate limit / quota) to prevent excess usage.
                    if exc.code not in (502, 503, 504):
                        raise
                    last_error = exc
                    if attempt < max_attempts - 1:
                        pause = min(8.0, 2.0 * (2 ** attempt)) + random.uniform(0, 0.7)
                        print(f"Gemini {model}: HTTP {exc.code}, retrying in {pause:.1f}s")
                        time.sleep(pause)
                    else:
                        print(f"Gemini {model}: HTTP {exc.code} after {max_attempts} attempts")
            if model_index == 0 and len(models) > 1:
                print(f"Gemini: primary temporarily unavailable; trying {backup_model}")
        if last_error is not None:
            raise last_error
        raise RuntimeError("Gemini did not return a response")
    finally:
        ai.MODEL = primary_model


ai.gemini_review = resilient_gemini_review


def fetch_xau():
    key = os.getenv("TWELVE_DATA_API_KEY", "").strip()
    if not key:
        raise RuntimeError("TWELVE_DATA_API_KEY is missing in GitHub Secrets")

    query = urlencode({
        "symbol": SYMBOL,
        "interval": "15min",
        "outputsize": 1500,
        "timezone": "UTC",
        "apikey": key,
    })
    url = "https://api.twelvedata.com/time_series?" + query
    try:
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=25) as res:
            payload = json.load(res)
    except HTTPError as exc:
        raise RuntimeError(f"Twelve Data HTTP {exc.code}") from None
    except URLError:
        raise RuntimeError("Twelve Data network unavailable") from None

    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected Twelve Data response")
    if payload.get("status") == "error":
        code = payload.get("code", "unknown")
        msg = str(payload.get("message", "API error")).replace(key, "[hidden]")
        raise RuntimeError(f"Twelve Data error {code}: {msg[:160]}")

    result = []
    for item in payload.get("values", []):
        try:
            dt = datetime.strptime(item["datetime"], "%Y-%m-%d %H:%M:%S")
            stamp = int(dt.replace(tzinfo=timezone.utc).timestamp())
            o, h, l, c = (float(item[x]) for x in ("open", "high", "low", "close"))
            if not all(map(math.isfinite, (o, h, l, c))):
                continue
            if l <= 0 or not (l <= min(o, c) <= max(o, c) <= h):
                continue
            result.append(bot.Bar(stamp, o, h, l, c))
        except (TypeError, ValueError, KeyError):
            continue

    bars = sorted({b.start: b for b in result}.values(), key=lambda b: b.start)
    if len(bars) < 1100:
        raise RuntimeError(f"Not enough XAU/USD candles for H4: {len(bars)}")
    return bars


def format_alert(d, now):
    age = max(0, (now - d.candle - bot.BAR_SECONDS) // 60)
    parts = [
        "🟡 GOLD ALERT | XAU/USD (Twelve Data spot)",
        f"M15 candle: {bot.iso_utc(d.candle)}",
        f"Feed age after close: {age} min (may be delayed)",
        f"H1: {d.h1} | H4: {d.h4}",
        "",
    ]
    if d.side == "WAIT":
        parts += ["⏳ WAIT / NO TRADE", f"Close: {d.close:.2f}"]
    else:
        parts += [
            f"{'🟢' if d.side == 'BUY' else '🔴'} {d.side} (research setup)",
            f"Reference entry: {d.entry:.2f}",
            f"Reference SL: {d.sl:.2f}",
            f"Reference TP1: {d.tp1:.2f}",
            f"Reference TP2: {d.tp2:.2f}",
        ]
    if d.rsi is not None and d.adx is not None and d.atr is not None:
        parts.append(f"RSI: {d.rsi:.1f} | ADX: {d.adx:.1f} | ATR: {d.atr:.2f}")
    parts += [
        f"Why: {d.reason}",
        "⚠️ Indicative XAU/USD spot prices, not Alpari executable quotes. "
        "Confirm broker price, spread and news before trading. No order placed.",
    ]
    return "\n".join(parts)


def run(mode):
    if mode in ("setup", "test"):
        bot.run(mode)
        return

    now = int(time.time())
    bars = fetch_xau()
    d = bot.evaluate(bars, now, SYMBOL)
    print(f"Data: {SOURCE}; candles={len(bars)}; last completed={bot.iso_utc(d.candle)}")
    print(f"Rule engine: {d.side} / {d.reason}")

    if mode == "scan":
        if d.side == "WAIT":
            print("WAIT; no eligible signal; no Gemini request")
            return
        event_id = f"{SYMBOL}:{d.candle}:{d.side}"
        if bot.read_state().get("last_alert_id") == event_id:
            print("Duplicate signal suppressed")
            return
    elif mode not in ("status", "dry-run"):
        raise ValueError("Unknown mode")

    text = format_alert(d, now) + "\n\n" + ai.ai_comment(d, bars, now)
    if mode == "dry-run":
        print(text)
    else:
        bot.send(text)
        if mode == "scan":
            bot.save_state(event_id, now)
        print("Alert delivered (Gemini advisory only)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("setup", "test", "status", "scan", "dry-run"), default="scan")
    args = parser.parse_args()
    try:
        run(args.mode)
    except Exception as exc:
        detail = str(exc)
        for secret in (os.getenv("TWELVE_DATA_API_KEY"), bot.TG_TOKEN, ai.API_KEY):
            if secret:
                detail = detail.replace(secret, "[hidden]")
        print(f"ERROR: {type(exc).__name__}: {detail}", file=sys.stderr)
        sys.exit(1)
