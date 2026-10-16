#!/usr/bin/env python3
"""Free-tier Gemini commentary on top of the existing rule-based Gold bot.

Preserves the original bot's signals and levels. Gemini is advisory only.
Input: delayed GC=F COMEX futures candles; not XAUUSD/Alpari prices.
AI is called ONLY for manual status and fresh, unique BUY/SELL alerts.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import sys
import time
from urllib import request, error

import bot

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
API_KEY = os.environ.get("GEMINI_API_KEY", "")
MAX_LEN = 220


def safe_line(value: object) -> str:
    """Treat model text as untrusted output, not as orders or commands."""
    if not isinstance(value, str):
        return "مشخص نشده"
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:MAX_LEN] if value else "مشخص نشده"


def summary_payload(d: bot.Decision, bars: list[bot.Bar], now: int) -> dict:
    recent = bot.closed_15m(bars, now)[-14:]
    return {
        "instrument": bot.SYMBOL,
        "feed": "Yahoo GC=F delayed COMEX futures (NOT spot XAUUSD)",
        "as_of_utc": bot.iso_utc(now),
        "latest_completed_candle_utc": bot.iso_utc(d.candle),
        "algorithm_signal": d.side,
        "algorithm_reason": d.reason,
        "h1_trend": d.h1,
        "h4_trend": d.h4,
        "rsi14": round(d.rsi, 2) if d.rsi is not None else None,
        "adx14": round(d.adx, 2) if d.adx is not None else None,
        "atr14": round(d.atr, 3) if d.atr is not None else None,
        "entry": round(d.entry, 2) if d.entry is not None else None,
        "sl": round(d.sl, 2) if d.sl is not None else None,
        "tp1": round(d.tp1, 2) if d.tp1 is not None else None,
        "tp2": round(d.tp2, 2) if d.tp2 is not None else None,
        "latest_m15_candles": [
            {"utc": bot.iso_utc(b.start),
             "o": round(b.open, 2), "h": round(b.high, 2),
             "l": round(b.low, 2), "c": round(b.close, 2)}
            for b in recent
        ],
    }


SYSTEM_TEXT = (
    "تو تحلیلگر محتاط بازار طلا هستی. فقط با JSON ورودی پاسخ بده. "
    "این داده GC=F آتی طلا با احتمال تأخیر است، نه XAUUSD بروکر. "
    "دسترسی به اخبار امروز، تقویم اقتصادی زنده، نمودار کامل و اینترنت نداری. "
    "درباره اخبار و فاندامنتال یا بازده سرمایه و درصد موفقیت حدس نزن. "
    "جهت و سطوح خروجی موتور قانون‌محور را تغییر نده، قیمت جدید نساز. "
    "اگر signal=WAIT است آن را خرید یا فروش اعلام نکن. "
    "سه فیلد JSON فارسی تولید کن: verdict (SUPPORT یا CAUTION یا REJECT)، "
    "technical (حداکثر دو جمله روشن)، risk (یک جمله خطر اصلی). "
    "این فقط بازبینی مشورتی است؛ حکم معاملاتی قطعی نیست."
)


def gemini_review(d: bot.Decision, bars: list[bot.Bar], now: int) -> dict:
    """API call. Raises on errors; caller will safely fall back to rule engine."""
    if not API_KEY:
        raise RuntimeError("GEMINI_API_KEY secret not configured")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", MODEL):
        raise ValueError("Invalid model name")
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_TEXT}]},
        "contents": [{"role": "user", "parts": [{
            "text": json.dumps(summary_payload(d, bars, now), ensure_ascii=False)
        }]}],
        "generationConfig": {
            "temperature": 0.15,
            "maxOutputTokens": 650,
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "verdict": {"type": "STRING", "enum": ["SUPPORT", "CAUTION", "REJECT"]},
                    "technical": {"type": "STRING"},
                    "risk": {"type": "STRING"},
                },
                "required": ["verdict", "technical", "risk"],
            },
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    req = request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": API_KEY,  # never put the secret in the URL or log
        },
        method="POST",
    )
    with request.urlopen(req, timeout=35) as res:
        response = json.loads(res.read(100_000).decode("utf-8"))
    candidates = response.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini returned no candidates")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text_parts = [part.get("text", "") for part in parts if isinstance(part, dict)]
    content = "".join(text_parts)
    obj = json.loads(content)
    if not isinstance(obj, dict) or obj.get("verdict") not in {"SUPPORT", "CAUTION", "REJECT"}:
        raise ValueError("Unexpected Gemini response")
    return obj


def ai_comment(d: bot.Decision, bars: list[bot.Bar], now: int) -> str:
    age_min = (now - (d.candle + bot.BAR_SECONDS)) / 60
    if age_min > bot.MAX_AGE_MINUTES:
        return "🤖 AI: داده به‌روز نیست؛ برای جلوگیری از تحلیل گمراه‌کننده، بازبینی انجام نشد."
    if not API_KEY:
        return "🤖 AI: غیرفعال؛ کلید GEMINI_API_KEY هنوز در Secrets تنظیم نشده است."
    try:
        result = gemini_review(d, bars, now)
    except error.HTTPError as exc:
        # Do not print response bodies or URLs: may include sensitive content.
        print(f"AI API temporarily unavailable: HTTP {exc.code}. Rule-based alerts unchanged.")
        return "🤖 AI: فعلاً در دسترس نیست؛ این پیام فقط از موتور الگوریتمی است."
    except (ValueError, RuntimeError, error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        print(f"AI unavailable: {type(exc).__name__}. Rule-based alerts unchanged.")
        return "🤖 AI: فعلاً در دسترس نیست؛ این پیام فقط از موتور الگوریتمی است."
    verdict_names = {
        "SUPPORT": "تأیید مشروطِ تحلیل",
        "CAUTION": "احتیاط",
        "REJECT": "هشدار ریسک جدی",
    }
    verdict = verdict_names[result["verdict"]]
    return (f"🤖 نظر Gemini AI (مشورتی): {verdict}\n"
            f"تحلیل: {safe_line(result.get('technical'))}\n"
            f"ریسک: {safe_line(result.get('risk'))}\n"
            "⚠️ AI نه خبر زنده را می‌بیند و نه قیمت دقیق XAUUSD بروکر را.")


def run(mode: str, now: int | None = None) -> None:
    if mode in {"setup", "test"}:
        bot.run(mode, now=now)
        return
    now = int(time.time()) if now is None else now
    bars = bot.fetch_yahoo(bot.SYMBOL)
    d = bot.evaluate(bars, now, bot.SYMBOL)
    print(f"Data: {bot.SYMBOL}, last closed M15={bot.iso_utc(d.candle)}")
    print(f"Rule engine: {d.side} / {d.reason}")
    if mode == "status":
        bot.send(bot.message(d, bot.SYMBOL, now) + "\n\n" + ai_comment(d, bars, now))
        print("Status sent (AI advisory only)")
    elif mode == "scan":
        if d.side == "WAIT":
            print("WAIT; no new eligible setup and no AI request")
            return
        alert_id = f"{bot.SYMBOL}:{d.candle}:{d.side}"
        if bot.read_state().get("last_alert_id") == alert_id:
            print("Duplicate alert suppressed; no AI request")
            return
        msg = bot.message(d, bot.SYMBOL, now) + "\n\n" + ai_comment(d, bars, now)
        bot.send(msg)
        bot.save_state(alert_id, now)
        print("New rule signal delivered with advisory AI status")
    elif mode == "dry-run":
        print(bot.message(d, bot.SYMBOL, now) + "\n\n" + ai_comment(d, bars, now))
    else:
        raise ValueError(f"Unknown mode: {mode}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["setup", "test", "status", "scan", "dry-run"], default="scan")
    args = parser.parse_args()
    try:
        run(args.mode)
    except Exception as exc:
        # No key or bot token may appear in GitHub logs.
        detail = str(exc).replace(bot.TG_TOKEN, "[REDACTED]") if bot.TG_TOKEN else str(exc)
        detail = detail.replace(API_KEY, "[REDACTED]") if API_KEY else detail
        print(f"ERROR: {type(exc).__name__}: {detail}", file=sys.stderr)
        sys.exit(1)
