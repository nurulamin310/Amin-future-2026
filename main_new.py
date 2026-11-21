#!/usr/bin/env python3
# Amin Future Signal - Advanced Edition
# Python 3.10+ | python-telegram-bot 22.x
import os
import csv
import io
import math
import logging
import asyncio
import base64
import json
import urllib.request
import urllib.error
import threading
from flask import Flask, jsonify
from dataclasses import dataclass
from datetime import datetime, timezone,
from collections import defaultdict
from typing import Optional
from telegram import Update
from telegram.request import HTTPXRequest
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_NAME = "AMIN FUTURE SIGNAL – ADVANCED EDITION"
# Security: tokens are loaded from hosting environment variables.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
TIMEZONE = timezone(timedelta(hours=6), name="BDT")
MAX_ROWS = 20000
MIN_CANDLES = 35
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("amin_future_signal")
# Uploaded CSV data stays in memory until restart.
DATASETS = {}
LAST_UPLOAD_SUMMARY = "এখনো কোনো CSV ফাইল আপলোড করা হয়নি।"
@dataclass
class Candle:
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float] = None
def safe_float(value, default=None):
    try:
        if value is None or str(value).strip() == "":
            return default
        result = float(str(value).strip())
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default
def normalize_header(name):
    return (
        str(name)
        .strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
    )
def parse_csv_bytes(raw: bytes):
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV ফাইলে Header নেই।")
    aliases = {
        "date": "timestamp",
        "time": "timestamp",
        "datetime": "timestamp",
        "pair": "symbol",
        "market": "symbol",
        "asset": "symbol",
        "tf": "timeframe",
        "interval": "timeframe",
        "o": "open",
        "h": "high",
        "l": "low",
        "c": "close",
        "v": "volume",
        "type": "market_type"
    }
    headers = {}
    for original in reader.fieldnames:
        h = normalize_header(original)
        headers[aliases.get(h, h)] = original
    required = [
        "timestamp",
        "open",
        "high",
        "low",
        "close"
    ]
    missing = [x for x in required if x not in headers]
    if missing:
        raise ValueError(
            "প্রয়োজনীয় কলাম নেই: " + ", ".join(missing)
        )
    parsed = defaultdict(list)
    bad = 0
    count = 0
    for row in reader:
        count += 1
        if count > MAX_ROWS:
            raise ValueError(
                f"একটি CSV-তে সর্বোচ্চ {MAX_ROWS}টি row নেওয়া হবে।"
            )
        def get(field, default=""):
            col = headers.get(field)
            return row.get(col, default) if col else default
        symbol = (
            str(get("symbol", "UNKNOWN"))
            .strip()
            .upper()
            or "UNKNOWN"
        )
        timeframe = (
            str(get("timeframe", "M1"))
            .strip()
            .upper()
            or "M1"
        )
        market_type = (
            str(get("market_type", "REAL"))
            .strip()
            .upper()
            or "REAL"
        )
        if timeframe not in ("M1", "M5", "M15"):
            bad += 1
            continue
        if market_type not in ("REAL", "OTC"):
            market_type = "UNVERIFIED"
        o, h, l, c = [
            safe_float(get(k))
            for k in ("open", "high", "low", "close")
        ]
        if (
            None in (o, h, l, c)
            or h < max(o, c)
            or l > min(o, c)
            or h < l
        ):
            bad += 1
            continue
        vol = safe_float(get("volume"))
        ts = str(get("timestamp")).strip()
        parsed[(symbol, timeframe, market_type)].append(
            Candle(ts, o, h, l, c, vol)
        )
    if not parsed:
        raise ValueError(
            "বিশ্লেষণের উপযোগী কোনো candle পাওয়া যায়নি।"
        )
    for key in parsed:
        try:
            parsed[key].sort(
                key=lambda x: x.timestamp
            )
        except Exception:
            pass
    return parsed, bad
def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period
def ema_series(values, period):
    if not values:
        return []
    alpha = 2 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out
def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(len(values) - period, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))
def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(len(candles) - period, len(candles)):
        cur = candles[i]
        prev = candles[i - 1]
        trs.append(max(
            cur.high - cur.low,
            abs(cur.high - prev.close),
            abs(cur.low - prev.close)
        ))
    return sum(trs) / period
def candle_pattern(c):
    body = abs(c.close - c.open)
    rng = max(c.high - c.low, 1e-12)
    upper = c.high - max(c.open, c.close)
    lower = min(c.open, c.close) - c.low
    if (
        body / rng <= 0.30
        and lower >= body * 2
        and upper <= max(body, rng * 0.15)
        and c.close > c.open
    ):
        return "Bullish Pin Bar", 1
    if (
        body / rng <= 0.30
        and upper >= body * 2
        and lower <= max(body, rng * 0.15)
        and c.close < c.open
    ):
        return "Bearish Pin Bar", -1
    return "No clear pin bar", 0
def engulfing(prev, cur):
    if (
        prev.close < prev.open
        and cur.close > cur.open
        and cur.open <= prev.close
        and cur.close >= prev.open
    ):
        return "Bullish Engulfing", 1
    if (
        prev.close > prev.open
        and cur.close < cur.open
        and cur.open >= prev.close
        and cur.close <= prev.open
    ):
        return "Bearish Engulfing", -1
    return "No engulfing", 0
def market_structure(candles, lookback=20):
    recent = candles[-lookback:]
    highs = [x.high for x in recent]
    lows = [x.low for x in recent]
    mid = len(recent) // 2
    first_high = max(highs[:mid])
    second_high = max(highs[mid:])
    first_low = min(lows[:mid])
    second_low = min(lows[mid:])
    if second_high > first_high and second_low > first_low:
        return "HH / HL (bullish structure)", 1
    if second_high < first_high and second_low < first_low:
        return "LH / LL (bearish structure)", -1
    return "Mixed / range structure", 0
def analyze(candles):
    if len(candles) < MIN_CANDLES:
        return {
            "error": (
                f"কমপক্ষে {MIN_CANDLES}টি candle দরকার; "
                f"পাওয়া গেছে {len(candles)}টি।"
            )
        }
    closes = [x.close for x in candles]
    cur, prev = candles[-1], candles[-2]
    e9 = ema_series(closes, 9)[-1]
    e21 = ema_series(closes, 21)[-1]
    e50 = ema_series(closes, 50)[-1]
    current_rsi = rsi(closes)
    current_atr = atr(candles)
    recent = candles[-21:-1]
    resistance = max(x.high for x in recent)
    support = min(x.low for x in recent)
    pattern, pattern_dir = candle_pattern(cur)
    engulf, engulf_dir = engulfing(prev, cur)
    structure, structure_dir = market_structure(candles)
    bull = 0
    bear = 0
    reasons = []
    details = []
    # 1. Support / Resistance proximity
    candle_range = max(cur.high - cur.low, 1e-12)
    near_support = cur.close <= support + candle_range * 1.5
    near_resistance = cur.close >= resistance - candle_range * 1.5
    if near_support and cur.close > cur.open:
        bull += 2
        reasons.append("Support zone থেকে bullish response")
    if near_resistance and cur.close < cur.open:
        bear += 2
        reasons.append("Resistance zone থেকে bearish response")
    details.append(
        f"Support≈{support:.6f}, Resistance≈{resistance:.6f}"
    )
    # 2. Candle close position
    close_position = (cur.close - cur.low) / candle_range
    if cur.close > cur.open and close_position > 0.70:
        bull += 1
        details.append("Bullish close near candle high")
    elif cur.close < cur.open and close_position < 0.30:
        bear += 1
        details.append("Bearish close near candle low")
    # 3. Pin Bar
    if pattern_dir > 0:
        bull += 2
        reasons.append(pattern)
    elif pattern_dir < 0:
        bear += 2
        reasons.append(pattern)
    # 4. Engulfing
    if engulf_dir > 0:
        bull += 2
        reasons.append(engulf)
    elif engulf_dir < 0:
        bear += 2
        reasons.append(engulf)
    # 5. Breakout / Breakdown
    if cur.close > resistance and prev.close <= resistance:
        bull += 2
        reasons.append(
            "Recent resistance breakout (retest not yet proven)"
        )
    elif cur.close < support and prev.close >= support:
        bear += 2
        reasons.append(
            "Recent support breakdown (retest not yet proven)"
        )
    # 6. Candle Momentum
    avg_range = sum(
        x.high - x.low for x in candles[-15:-1]
    ) / 14
    if candle_range > avg_range * 1.25 and cur.close > cur.open:
        bull += 1
        details.append("Above-average bullish candle range")
    elif candle_range > avg_range * 1.25 and cur.close < cur.open:
        bear += 1
        details.append("Above-average bearish candle range")
    # 7. EMA Trend
    if e9 > e21 > e50:
        bull += 2
        details.append("EMA 9 > 21 > 50: bullish trend")
    elif e9 < e21 < e50:
        bear += 2
        details.append("EMA 9 < 21 < 50: bearish trend")
    else:
        details.append("EMA trend mixed")
    # 8. Buyer / Seller Pressure estimate
    if close_position >= 0.75 and cur.close > cur.open:
        bull += 1
        details.append("Buyer pressure estimate: strong")
    elif close_position <= 0.25 and cur.close < cur.open:
        bear += 1
        details.append("Seller pressure estimate: strong")
    else:
        details.append("Buyer/Seller pressure estimate: mixed")
    # 9. Signal direction scoring
    # 10. Multi-timeframe confirmation is calculated by caller
    # 11. Market Structure
    if structure_dir > 0:
        bull += 1
    elif structure_dir < 0:
        bear += 1
    details.append(structure)
    # 12. Liquidity Sweep proxy
    prior = candles[-11:-1]
    prior_high = max(x.high for x in prior)
    prior_low = min(x.low for x in prior)
    if cur.high > prior_high and cur.close < prior_high:
        bear += 1
        reasons.append("Possible buy-side liquidity sweep")
    if cur.low < prior_low and cur.close > prior_low:
        bull += 1
        reasons.append("Possible sell-side liquidity sweep")
    # 13. Volatility Filter
    if current_atr is None or current_atr <= 0:
        volatility = "Unknown"
    elif candle_range < current_atr * 0.35:
        volatility = "Very low"
    elif candle_range > current_atr * 2.5:
        volatility = "Spike / high"
    else:
        volatility = "Normal"
    details.append(f"Volatility: {volatility}")
    # 14. False Breakout Warning
    if cur.high > resistance and cur.close <= resistance:
        bear += 1
        reasons.append("Possible false upside breakout")
    if cur.low < support and cur.close >= support:
        bull += 1
        reasons.append("Possible false downside breakout")
    # 15. RSI Confirmation
    if current_rsi is not None:
        if 50 < current_rsi < 70:
            bull += 1
        elif 30 < current_rsi < 50:
            bear += 1
        details.append(f"RSI(14): {current_rsi:.1f}")
    else:
        details.append("RSI: insufficient data")
    # 16. Conservative CALL / PUT / WAIT decision
    diff = bull - bear
    total = bull + bear
    if diff >= 4 and bull >= 7 and volatility != "Spike / high":
        signal = "CALL"
    elif diff <= -4 and bear >= 7 and volatility != "Spike / high":
        signal = "PUT"
    else:
        signal = "WAIT"
    score = min(
        100,
        round(50 + abs(diff) * 5 + max(total - 8, 0) * 2)
    )
    if signal == "WAIT":
        score = min(score, 59)
    trend = (
        "UPTREND" if e9 > e21 > e50
        else "DOWNTREND" if e9 < e21 < e50
        else "MIXED"
    )
    return {
        "signal": signal,
        "score": score,
        "bull": bull,
        "bear": bear,
        "close": cur.close,
        "timestamp": cur.timestamp,
        "pattern": pattern,
        "engulfing": engulf,
        "trend": trend,
        "support": support,
        "resistance": resistance,
        "rsi": current_rsi,
        "atr": current_atr,
        "volatility": volatility,
        "reasons": reasons[:6],
        "details": details[:10],
        "candle_count": len(candles)
    }
def format_report(key, candles):
    symbol, timeframe, market_type = key
    result = analyze(candles)
    if "error" in result:
        return (
            f"⚠️ {symbol} {timeframe} ({market_type})\n"
            f"{result['error']}"
        )
    lines = [
        f"📊 {BOT_NAME}",
        f"Market: {symbol} | Type: {market_type}",
        f"Timeframe: {timeframe}",
        (
            f"Signal: {result['signal']} | "
            f"Confluence score: {result['score']}/100 "
            "(not a win probability)"
        ),
        f"Trend: {result['trend']}",
        f"Last Close: {result['close']}",
        f"Support: {result['support']:.6f}",
        f"Resistance: {result['resistance']:.6f}",
        f"Pattern: {result['pattern']} | {result['engulfing']}",
        (
            f"RSI: {result['rsi']:.1f}"
            if result["rsi"] is not None else "RSI: N/A"
        ),
        f"Volatility: {result['volatility']}",
        "",
        "কারণ:"
    ]
    if result["reasons"]:
        lines.extend("• " + x for x in result["reasons"])
    else:
        lines.append("• যথেষ্ট শক্তিশালী confirmation নেই।")
    lines += ["", "অতিরিক্ত তথ্য:"]
    lines.extend("• " + x for x in result["details"])
    lines += [
        "",
        f"Data rows: {result['candle_count']}",
        f"CSV time: {result['timestamp'] or 'N/A'}",
        (
            "সময়: "
            + datetime.now(TIMEZONE).strftime("%Y-%m-%d %I:%M:%S %p")
            + " (Bangladesh GMT+6)"
        ),
        "",
        "নোট: এটি CSV-ভিত্তিক শিক্ষামূলক বিশ্লেষণ; "
        "Quotex-এর লাইভ ডেটা বা নিশ্চিত ট্রেডিং নির্দেশনা নয়।"
    ]
    return "\n".join(lines)
def backtest(candles, horizon=1):
    """Directional next-candle close test, not binary-options payout simulation."""
    if len(candles) < MIN_CANDLES + horizon + 1:
        return None
    wins = losses = skipped = 0
    calls = puts = 0
    for i in range(MIN_CANDLES, len(candles) - horizon):
        res = analyze(candles[:i + 1])
        if "error" in res or res["signal"] == "WAIT":
            skipped += 1
            continue
        entry = candles[i].close
        exit_price = candles[i + horizon].close
        if res["signal"] == "CALL":
            calls += 1
            correct = exit_price > entry
        else:
            puts += 1
            correct = exit_price < entry
        if correct:
            wins += 1
        else:
            losses += 1
    total = wins + losses
    if total == 0:
        return {
            "wins": 0,
            "losses": 0,
            "skipped": skipped,
            "rate": None,
            "calls": calls,
            "puts": puts
        }
    return {
        "wins": wins,
        "losses": losses,
        "skipped": skipped,
        "rate": wins / total * 100,
        "calls": calls,
        "puts": puts
    }
def user_allowed(update):
    allowed = os.getenv("ALLOWED_USER_IDS", "").strip()
    if not allowed:
        return True
    ids = {
        x.strip()
        for x in allowed.split(",")
        if x.strip()
    }
    return (
        update.effective_user is not None
        and str(update.effective_user.id) in ids
    )
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_allowed(update):
        return
    await update.message.reply_text(
        f"স্বাগতম!\n{BOT_NAME}\n\n"
        "CSV candle data পাঠালে স্বয়ংক্রিয় Analysis হবে।\n"
        "স্ক্রিনশট পাঠালে AI visual analysis হবে।\n\n"
        "Commands:\n"
        "/start - শুরু\n"
        "/help - CSV format ও নির্দেশনা\n"
        "/analyze - আপলোড করা ডেটা বিশ্লেষণ\n"
        "/stats - Backtest summary\n"
        "/markets - সংরক্ষিত market list\n\n"
        "কোনো live market feed/API এই bot-এ নেই।"
    )
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_allowed(update):
        return
    await update.message.reply_text(
        "CSV Header এভাবে রাখুন:\n"
        "symbol,timeframe,timestamp,open,high,low,close,volume,market_type\n\n"
        "উদাহরণ:\n"
        "EURUSD,M1,2026-11-17 10:00:00,"
        "1.0800,1.0805,1.0798,1.0803,100,REAL\n\n"
        "আবশ্যক: timestamp,open,high,low,close\n"
        "symbol না থাকলে UNKNOWN; timeframe না থাকলে M1; "
        "market_type না থাকলে REAL ধরা হবে।\n"
        "সমর্থিত timeframe: M1, M5, M15.\n"
        "প্রতি symbol/timeframe/type-এ অন্তত 35টি candle দিন; "
        "ভালো পরীক্ষার জন্য 200+ দিন।\n"
        "CSV-তে ক্যান্ডেলগুলো একই মার্কেট ও একই timeframe অনুযায়ী দিন।\n"
        "OTC লিখলেই ডেটা সত্যিকারের Quotex OTC হয়ে যায় না।\n\n"
        "স্ক্রিনশট পাঠালে Gemini AI দিয়ে ছবির দৃশ্যমান তথ্য "
        "বিশ্লেষণের চেষ্টা করবে।\n"
        "এই bot-এ কোনো live market-data API নেই।"
    )
async def markets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_allowed(update):
        return
    if not DATASETS:
        await update.message.reply_text("এখনো কোনো CSV ডেটা নেই।")
        return
    lines = ["সংরক্ষিত ডেটাসেট:"]
    for (symbol, tf, kind), candles in DATASETS.items():
        lines.append(
            f"• {symbol} | {tf} | {kind} | {len(candles)} candles"
        )
    await update.message.reply_text("\n".join(lines))
async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_allowed(update):
        return
    if not DATASETS:
        await update.message.reply_text(
            "আগে CSV ফাইল পাঠান। /help লিখে CSV format দেখুন।"
        )
        return
    for key, candles in DATASETS.items():
        await update.message.reply_text(format_report(key, candles))
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_allowed(update):
        return
    if not DATASETS:
        await update.message.reply_text(
            "Backtesting-এর জন্য আগে CSV আপলোড করুন।"
        )
        return
    messages = [
        "📈 Historical Backtest (পরের ১ candle-এর direction)"
    ]
    for key, candles in DATASETS.items():
        bt = backtest(candles, horizon=1)
        label = " | ".join(key)
        if bt is None:
            messages.append(f"{label}: পর্যাপ্ত ডেটা নেই।")
        elif bt["rate"] is None:
            messages.append(
                f"{label}: কোনো CALL/PUT signal পাওয়া যায়নি; "
                f"WAIT={bt['skipped']}"
            )
        else:
            messages.append(
                f"{label}\n"
                f"Wins={bt['wins']} | Losses={bt['losses']} | "
                f"Directional hit rate={bt['rate']:.1f}% | "
                f"WAIT={bt['skipped']}\n"
                f"CALL={bt['calls']} | PUT={bt['puts']}"
            )
    messages.append(
        "\nএটি কেবল পরের candle-এর দিক সঠিক ছিল কি না তার পরীক্ষা। "
        "এটি Quotex payout, expiry execution, spread বা slippage "
        "হিসাব করে না; অতএব এটি লাভের নিশ্চয়তা বা বাস্তব win rate নয়।"
    )
    await update.message.reply_text("\n\n".join(messages))
async def csv_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LAST_UPLOAD_SUMMARY
    if not user_allowed(update):
        return
    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith(".csv"):
        await update.message.reply_text(
            "অনুগ্রহ করে .csv ফাইল পাঠান। /help লিখে format দেখুন।"
        )
        return
    if doc.file_size and doc.file_size > 8 * 1024 * 1024:
        await update.message.reply_text(
            "CSV ফাইল 8 MB-এর কম হতে হবে।"
        )
        return
    status = await update.message.reply_text(
        "CSV পেয়েছি। ডেটা যাচাই ও বিশ্লেষণ করছি…"
    )
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        raw = await tg_file.download_as_bytearray()
        parsed, bad = parse_csv_bytes(bytes(raw))
        DATASETS.clear()
        DATASETS.update(parsed)
        total = sum(len(v) for v in parsed.values())
        LAST_UPLOAD_SUMMARY = (
            f"{len(parsed)} dataset, "
            f"{total} valid candles, {bad} rejected rows"
        )
        await status.edit_text(
            f"✅ CSV গ্রহণ করা হয়েছে।\n"
            f"{LAST_UPLOAD_SUMMARY}\n\n"
            "এখন স্বয়ংক্রিয় Analysis রিপোর্ট দিচ্ছি।"
        )
        for key, candles in DATASETS.items():
            report = format_report(key, candles)
            if len(report) <= 4000:
                await update.message.reply_text(report)
            else:
                await update.message.reply_text(
                    report[:3900] + "\n…"
                )
    except Exception:
        log.exception("CSV processing failed")
        await status.edit_text(
            "CSV পড়তে সমস্যা হয়েছে। ফাইলের format যাচাই করে আবার পাঠান।"
        )
async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not user_allowed(update):
        return
    await update.message.reply_text(
        "আমি CSV candle data বিশ্লেষণ করতে পারি। "
        "CSV ফাইল বা মার্কেটের স্ক্রিনশট পাঠান, অথবা /help লিখুন।"
    )
# =========================================================
# PART 5/5 - GEMINI SCREENSHOT ANALYSIS + WEB + BOT START
# =========================================================
def gemini_image_analysis(image_bytes, mime_type="image/jpeg"):
    """Send a chart screenshot to Gemini for visual analysis."""
    if not GEMINI_API_KEY:
        raise ValueError(
            "GEMINI_API_KEY সেট করা নেই। Hosting environment-এ API key দিন।"
        )
    image_data = base64.b64encode(image_bytes).decode("utf-8")
    prompt = """
You are a careful technical chart analyst.
Analyze ONLY the visible information in this screenshot.
Please identify, when visible:
1. Market symbol and timeframe.
2. Current visible price and candle direction.
3. Overall trend: bullish, bearish, or sideways.
4. Important support and resistance zones.
5. Visible candlestick patterns.
6. EMA, RSI, MACD or other indicators, only if visible.
7. Possible buyer and seller pressure.
8. Possible breakout, rejection, or liquidity sweep.
9. Potential direction: CALL, PUT, or WAIT.
10. Confidence level: Low, Medium, or High.
Important rules:
- Never invent unreadable prices or indicators.
- Clearly mention anything that cannot be identified.
- Do not claim access to live market data.
- A screenshot is not enough to guarantee the next candle.
- Prefer WAIT when the chart is unclear or conflicting.
- This is educational technical analysis, not guaranteed financial advice.
Write the response in clear Bengali.
"""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": image_data
                    }
                }
            ]
        }]
    }
    request_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=request_data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Gemini API error ({exc.code}): {error_text[:500]}"
        )
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Gemini connection error: {exc.reason}"
        )
    candidates = result.get("candidates", [])
    if not candidates:
        raise RuntimeError(
            "Gemini কোনো বিশ্লেষণ ফেরত দেয়নি। ছবিটি আবার পাঠান।"
        )
    parts = candidates[0].get("content", {}).get("parts", [])
    answer = "\n".join(
        part.get("text", "")
        for part in parts
        if part.get("text")
    ).strip()
    if not answer:
        raise RuntimeError(
            "Gemini-এর উত্তর খালি এসেছে। আবার চেষ্টা করুন।"
        )
    return answer
async def photo_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not user_allowed(update):
        return
    if not GEMINI_API_KEY:
        await update.message.reply_text(
            "⚠️ Gemini API Key সেট করা নেই। "
            "Hosting-এর Environment Variables-এ GEMINI_API_KEY দিন।"
        )
        return
    status = await update.message.reply_text(
        "📸 স্ক্রিনশট পেয়েছি। Gemini AI দিয়ে চার্ট বিশ্লেষণ করছি..."
    )
    try:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        image_bytes = await tg_file.download_as_bytearray()
        result = await asyncio.to_thread(
            gemini_image_analysis,
            bytes(image_bytes),
            "image/jpeg"
        )
        result = (
            "📊 GEMINI SCREENSHOT ANALYSIS\n\n"
            + result
            + "\n\n⚠️ এটি ছবির দৃশ্যমান তথ্যভিত্তিক বিশ্লেষণ। "
            "লাইভ ডেটা বা নিশ্চিত ট্রেডিং সিগন্যাল নয়।"
        )
        if len(result) <= 4000:
            await status.edit_text(result)
        else:
            await status.edit_text(result[:3900] + "\n...")
    except Exception as exc:
        log.exception("Screenshot analysis failed")
        await status.edit_text(
            "❌ স্ক্রিনশট বিশ্লেষণ করা যায়নি।\n"
            f"সমস্যা: {str(exc)[:700]}"
        )
# =========================================================
# FLASK WEB SERVER
# =========================================================
web_app = Flask(__name__)
@web_app.route("/", methods=["GET"])
def home():
    return "AMIN FUTURE SIGNAL BOT IS RUNNING."
@web_app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "bot": BOT_NAME,
        "last_upload": LAST_UPLOAD_SUMMARY
    })
def run_web():
    port = int(os.getenv("PORT", "10000"))
    web_app.run(
        host="0.0.0.0",
        port=port,
        use_reloader=False
    )
# =========================================================
# MAIN TELEGRAM BOT
# =========================================================
def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN environment variable সেট করুন।"
        )
    direct_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=20.0,
        read_timeout=30.0,
        write_timeout=30.0,
        httpx_kwargs={"trust_env": False}
    )
    direct_updates_request = HTTPXRequest(
        connection_pool_size=4,
        connect_timeout=20.0,
        read_timeout=40.0,
        write_timeout=30.0,
        httpx_kwargs={"trust_env": False}
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(direct_request)
        .get_updates_request(direct_updates_request)
        .build()
    )
    # Telegram commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("analyze", analyze_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("markets", markets_cmd))
    # Screenshot analysis
    app.add_handler(
        MessageHandler(filters.PHOTO, photo_handler)
    )
    # CSV upload
    app.add_handler(
        MessageHandler(filters.Document.ALL, csv_handler)
    )
    # Other text messages
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            unknown_text
        )
    )
    # Start web server in background
    web_thread = threading.Thread(
        target=run_web,
        daemon=True
    )
    web_thread.start()
    log.info("%s starting", BOT_NAME)
    try:
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
            close_loop=True
        )
    except Exception:
        log.exception(
            "Bot stopped. Check Telegram network access and hosting logs."
        )
        raise
if __name__ == "__main__":
    main()
