"""
Signal Bot v2: мемкоины Solana + крипто-валюты (aiogram 3.x + Pillow)

Установка:   pip install aiogram pillow aiohttp
Запуск:      BOT_TOKEN=... python memecoin_bot.py
Опционально: ANTHROPIC_API_KEY=... (нужен для анализа СКРИНШОТОВ)
"""
import asyncio
import base64
import html
import io
import json
import logging
import os
import random
import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)
from PIL import Image, ImageDraw, ImageFont

# ───────────────────────── Конфигурация ─────────────────────────
BOT_TOKEN = "9076117686:AAHzXLAuL088pawQVZJXiz4ug6TPttGnqgY"
ADMIN_IDS = {6096550279}
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-3-5-20241022")

AUTO_PUSH = True               # Автоматическая рассылка сигналов админу
PUSH_MIN_SEC, PUSH_MAX_SEC = 40, 90  # Интервал автопуша мемкоинов (40-90 сек)

# Смягченные фильтры для быстрого нахождения мемкоинов
MAX_AGE_MIN = 360
MC_MIN, MC_MAX = 4_000, 250_000
LIQ_MIN = 2_500
M5_BUYS_MIN = 5
M5_BUYERS_MIN = 4
BUY_SELL_RATIO = 1.0
M5_VOL_MIN = 500
MAX_PUMP_M5 = 200
REQUIRE_RUGCHECK = False

# План сделки (мемкоины)
TP1, TP2, TP_FINAL = 2.0, 4.0, 10.0
SHARE1, SHARE2, SHARE3 = 0.5, 0.3, 0.2
STOP_PCT = 0.35
TRAIL_PCT = 0.30
TIME_STOP_MIN = 60
SIZES = [10, 50, 100, 250, 500, 1000, 2500, 5000]

# Аварийные выходы
DUMP_PCT = 0.25                
DUMP_WINDOW_SEC = 120
LIQ_DROP = 0.5                 

# Крипто-валюты
COINS = {"BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "TON": "Toncoin (GRAM)",
         "BNB": "BNB", "XRP": "XRP", "DOGE": "Dogecoin", "ADA": "Cardano",
         "AVAX": "Avalanche", "LINK": "Chainlink", "TRX": "TRON", "SUI": "Sui"}
ALIASES = {"GRAM": "TON", "ГРАМ": "TON", "TONCOIN": "TON", "ТОН": "TON", "TONCOIN/USDT": "TON",
           "BITCOIN": "BTC", "БИТКОИН": "BTC", "БИТОК": "BTC", "ETHEREUM": "ETH", "ЭФИР": "ETH",
           "ЭФИРИУМ": "ETH", "SOLANA": "SOL", "СОЛАНА": "SOL", "RIPPLE": "XRP", "DOGECOIN": "DOGE",
           "ДОГИ": "DOGE"}
COIN_MIN_SCORE = 5             
COIN_R_ATR = 1.5               
COIN_TP = (1.0, 2.0, 3.5)      
COIN_SHARES = (0.5, 0.3, 0.2)
COIN_TIME_STOP_H = 36
COIN_POLL = 20

SCAN_INTERVAL = 15
POLL_INTERVAL = 15
DB_FILE = "signals.json"
COIN_DB_FILE = "coin_signals.json"
GT_ROOT = "https://api.geckoterminal.com/api/v2"
GT = GT_ROOT + "/networks/solana"
RUG = "https://api.rugcheck.xyz/v1/tokens/{}/report/summary"
BINANCE_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]
CA_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

BG, PANEL, BORDER = "#0b0e14", "#131822", "#1f2735"
GREEN, RED, WHITE, GRAY = "#10b981", "#ef4444", "#f3f4f6", "#8b93a3"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("signalbot")

router = Router()
db_lock = asyncio.Lock()
session: Optional[aiohttp.ClientSession] = None
_bot_ref: Optional[Bot] = None
signals: Dict[str, dict] = {}
csignals: Dict[str, dict] = {}
candidates: Dict[str, dict] = {}
rug_cache: Dict[str, tuple] = {}
kl_cache: Dict[tuple, tuple] = {}
last_stats: Counter = Counter()
last_scan_ts = 0.0
last_scan_total = 0
BLEND = SHARE1 * TP1 + SHARE2 * TP2 + SHARE3 * TP_FINAL


# ───────────────────────── Утилиты ─────────────────────────
def num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def fmt_usd(v: float) -> str:
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.2f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:.0f}"
def fmt_price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 100:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.3f}"
    if p >= 0.01:
        return f"{p:.4f}"
    return f"{p:.6f}"


def money(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:.1f}"


def fmt_elapsed(sec: float) -> str:
    sec = int(max(sec, 0))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m} min"
    return f"{s}s"


def fmt_mult(m: float) -> str:
    return f"{m:.1f}X" if m < 100 else f"{m:.0f}X"


def _load(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error("DB load %s: %s", path, e)
    return {}


def _save(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def load_db() -> None:
    global signals, csignals
    signals, csignals = _load(DB_FILE), _load(COIN_DB_FILE)


def save_db() -> None:
    _save(DB_FILE, signals)
    _save(COIN_DB_FILE, csignals)


# ───────────────────────── HTTP / данные ─────────────────────────
async def get_json(url: str, params: Optional[dict] = None):
    if session is None:
        return None
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10),
                               headers={"Accept": "application/json"}) as r:
            if r.status != 200:
                return None
            return await r.json()
    except Exception:
        return None


def parse_pool(item: dict, inc: Optional[dict] = None) -> dict:
    a = item.get("attributes", {}) or {}
    rel = item.get("relationships", {}) or {}
    bid = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
    mint = bid.split("_", 1)[-1]
    sym = (a.get("name") or "?").split(" / ")[0].strip()
    name = sym
    if inc and bid in inc:
        name = inc[bid].get("name") or sym
        sym = inc[bid].get("symbol") or sym
    tx5 = (a.get("transactions") or {}).get("m5") or {}
    age = 9999.0
    created = a.get("pool_created_at")
    if created:
        try:
            age = (datetime.now(timezone.utc) -
                   datetime.fromisoformat(created.replace("Z", "+00:00"))).total_seconds() / 60
        except ValueError:
            pass
    return {
        "mint": mint, "pool": a.get("address", ""), "name": name, "symbol": sym,
        "price": num(a.get("base_token_price_usd")),
        "mc": num(a.get("market_cap_usd")) or num(a.get("fdv_usd")),
        "liq": num(a.get("reserve_in_usd")), "age": age,
        "buys": int(num(tx5.get("buys"))), "sells": int(num(tx5.get("sells"))),
        "buyers": int(num(tx5.get("buyers"))),
        "vol5": num((a.get("volume_usd") or {}).get("m5")),
        "chg5": num((a.get("price_change_percentage") or {}).get("m5")),
    }


async def rug_check(mint: str) -> Optional[dict]:
    hit = rug_cache.get(mint)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    d = await get_json(RUG.format(mint))
    if not d:
        return None
    risks = d.get("risks") or []
    res = {"danger": [r.get("name") for r in risks if r.get("level") == "danger"],
           "warn": [r.get("name") for r in risks if r.get("level") == "warn"]}
    rug_cache[mint] = (time.time(), res)
    return res


def score(c: dict) -> float:
    return (c["buys"] / max(c["sells"], 1) * 10 + min(c["buyers"], 100) * 0.3 +
            min(c["vol5"] / 1000, 30) + min(c["liq"] / max(c["mc"], 1) * 100, 30))
async def scan_once() -> None:
    global last_stats, last_scan_ts, last_scan_total
    pools: Dict[str, dict] = {}
    for path, params in ((f"{GT}/new_pools", {"page": 1}), (f"{GT}/trending_pools", {"page": 1})):
        data = await get_json(path, {**params, "include": "base_token"})
        if not data:
            continue
        inc = {x["id"]: x.get("attributes", {}) for x in data.get("included", [])
               if x.get("type") == "token"}
        for i in data.get("data", []):
            p = parse_pool(i, inc)
            if p["mint"] and p["price"] > 0:
                pools.setdefault(p["mint"], p)
        await asyncio.sleep(0.3)
    
    stats: Counter = Counter()
    now = time.time()
    for c in pools.values():
        if c["mc"] < MC_MIN or c["mc"] > MC_MAX or c["liq"] < LIQ_MIN:
            continue
        mint = c["mint"]
        rug = await rug_check(mint) if mint not in rug_cache else rug_cache[mint][1]
        if rug and rug["danger"] and REQUIRE_RUGCHECK:
            continue
        candidates[mint] = {**c, "rug": rug, "score": score(c), "seen": now, "tier": "risky"}
        stats["найдено"] += 1
    
    for m in [m for m, v in candidates.items() if now - v["seen"] > 300]:
        del candidates[m]
    last_stats, last_scan_ts, last_scan_total = stats, now, len(pools)


def pick_best() -> Optional[dict]:
    pool = [c for c in candidates.values() if c["mint"] not in signals]
    if not pool:
        if candidates:
            return random.choice(list(candidates.values()))
        return None
    return max(pool, key=lambda c: c["score"])


def state_from_attrs(sig: dict, a: dict) -> Optional[Tuple[float, float]]:
    price = num(a.get("base_token_price_usd"))
    liq = num(a.get("reserve_in_usd"))
    if price and sig.get("entry_price"):
        return price * sig["entry_mc"] / sig["entry_price"], liq
    mc = num(a.get("market_cap_usd")) or num(a.get("fdv_usd"))
    return (mc, liq) if mc else None


async def fetch_now(sig: dict) -> Optional[float]:
    d = await get_json(f"{GT}/pools/{sig['pool']}")
    try:
        st = state_from_attrs(sig, d["data"]["attributes"])
    except (TypeError, KeyError):
        return None
    return st[0] if st else None


async def get_series(sig: dict, since: Optional[float]) -> List[Tuple[float, float]]:
    d = await get_json(f"{GT}/pools/{sig['pool']}/ohlcv/minute", {"aggregate": 1, "limit": 120})
    try:
        rows = d["data"]["attributes"]["ohlcv_list"]
    except (TypeError, KeyError):
        rows = []
    supply = sig["entry_mc"] / sig["entry_price"] if sig.get("entry_price") else 0
    out = sorted((r[0], r[4] * supply) for r in rows if supply)
    if since:
        out = [p for p in out if p[0] >= since - 600]
    return out


async def candidate_from_ca(ca: str) -> Optional[dict]:
    d = await get_json(f"{GT}/tokens/{ca}/pools", {"page": 1})
    cs = [parse_pool(i) for i in ((d or {}).get("data") or [])]
    cs = [c for c in cs if c["mint"] == ca] or cs
    if not cs:
        return None
    c = max(cs, key=lambda x: x["liq"])
    c["rug"] = await rug_check(c["mint"])
    return c


async def candidate_from_name(q: str) -> Optional[dict]:
    q = q.strip().lstrip("$")
    d = await get_json(f"{GT_ROOT}/search/pools", {"query": q, "network": "solana",
                                                  "include": "base_token", "page": 1})
    if not d:
        return None
    inc = {x["id"]: x.get("attributes", {}) for x in d.get("included", []) if x.get("type") == "token"}
    cs = [parse_pool(i, inc) for i in d.get("data", [])]
    cs = [c for c in cs if c["mint"] and c["price"] > 0]
    if not cs:
        return None
    exact = [c for c in cs if c["symbol"].lower() == q.lower() or c["name"].lower() == q.lower()]
    c = max(exact or cs, key=lambda x: x["liq"])
    c["rug"] = await rug_check(c["mint"])
    return c# ───────────────────────── Рендер карточек ─────────────────────────
_FC: Dict[int, ImageFont.ImageFont] = {}
_FP = ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
       "arialbd.ttf", "Arial Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
       "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]


def font(size: int) -> ImageFont.ImageFont:
    if size in _FC:
        return _FC[size]
    f = None
    for p in _FP:
        try:
            f = ImageFont.truetype(p, size)
            break
        except OSError:
            continue
    if f is None:
        f = ImageFont.load_default()
    _FC[size] = f
    return f


def resample(points: List[tuple], n: int = 120) -> List[float]:
    if not points:
        return [100.0] * n
    pts = sorted(points)
    t0, t1 = pts[0][0], pts[-1][0]
    if t1 - t0 < 1:
        t1 = t0 + 1
    vals, j = [], 0
    for i in range(n):
        t = t0 + (t1 - t0) * i / (n - 1)
        while j < len(pts) - 2 and pts[j + 1][0] < t:
            j += 1
        (ta, va), (tb, vb) = pts[j], pts[min(j + 1, len(pts) - 1)]
        k = 0 if tb == ta else min(max((t - ta) / (tb - ta), 0), 1)
        vals.append(va + (vb - va) * k)
    return vals


W, H = 800, 500


def new_frame(accent: str) -> Tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((12, 12, W - 12, H - 12), radius=22, outline=BORDER, width=2)
    return img, d


def paint_chart(img: Image.Image, vals: List[float], accent: str) -> Image.Image:
    rgb = tuple(int(accent[i:i + 2], 16) for i in (1, 3, 5))
    vmin, vmax = min(vals), max(vals)
    if vmax - vmin < 1e-12:
        vmin, vmax = vmin * 0.95, vmax * 1.05 + 1e-9
    x0, x1, y0, y1 = 410, 750, 110, 320
    xy = [(x0 + (x1 - x0) * i / (len(vals) - 1),
           y1 - (y1 - y0) * (v - vmin) / (vmax - vmin)) for i, v in enumerate(vals)]
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    od.polygon(xy + [(x1, y1), (x0, y1)], fill=rgb + (38,))
    img = Image.alpha_composite(img, ov)
    d = ImageDraw.Draw(img)
    d.line(xy, fill=accent, width=4, joint="curve")
    ex, ey = xy[-1]
    d.ellipse((ex - 6, ey - 6, ex + 6, ey + 6), fill=accent, outline=WHITE, width=2)
    return img


def paint_blocks(img: Image.Image, blocks: list) -> bytes:
    d = ImageDraw.Draw(img)
    bw, by0, by1 = 171, 365, 465
    for i, (lab, val, col) in enumerate(blocks):
        bx = 40 + i * (bw + 12)
        d.rounded_rectangle((bx, by0, bx + bw, by1), radius=16, fill=PANEL, outline=BORDER)
        d.text((bx + 14, by0 + 18), lab, font=font(12), fill=GRAY)
        d.text((bx + 14, by0 + 64), val, font=font(24), fill=col, anchor="lm")
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def render_card(sig: dict, now_mc: float, now_ts: float, series: list, mode: str) -> bytes:
    e = sig["entry_mc"]
    mult = now_mc / e if e else 1.0
    accent = GREEN if (mode == "signal" or mult >= 1) else RED
    img, d = new_frame(accent)
    d.rounded_rectangle((40, 36, 176, 72), radius=18, fill=accent)
    d.text((108, 54), "SIGNAL" if mode == "signal" else "RESULT", font=font(18), fill=BG, anchor="mm")
    d.text((760, 54), "SOLANA  •  PUMP.FUN / AXIOM", font=font(14), fill=GRAY, anchor="rm")
    d.text((40, 92), "$" + sig["symbol"][:12].upper(), font=font(38), fill=WHITE)
    d.text((40, 140), sig["name"][:28], font=font(18), fill=GRAY)
    if mode == "result":
        mt = fmt_mult(mult)
        d.text((40, 235), mt, font=font(80), fill=accent, anchor="lm")
    else:
        d.text((40, 192), "ENTRY ZONE", font=font(14), fill=GRAY)
        d.text((40, 236), f"{fmt_usd(e)}", font=font(34), fill=accent, anchor="lm")
        d.text((40, 280), f"STOP {fmt_usd(e * (1 - STOP_PCT))}  •  TIME ≤ {TIME_STOP_MIN} MIN",
               font=font(15), fill=GRAY)
    pts = [(p[0], p[1]) for p in series]
    pts.append((max(now_ts, (pts[-1][0] if pts else sig["ts"]) + 1), now_mc))
    img = paint_chart(img, resample(pts), accent)
    if mode == "signal":
        blocks = [("BUY AT", fmt_usd(e), WHITE),
                  (f"SELL {int(SHARE1 * 100)}%", fmt_usd(e * TP1), accent),
                  (f"SELL {int(SHARE2 * 100)}%", fmt_usd(e * TP2), accent),
                  (f"FINAL", fmt_usd(e * TP_FINAL), accent)]
    else:
        blocks = [("CALLED AT", fmt_usd(e), WHITE), ("NOW", fmt_usd(now_mc), accent),
                  ("MULTIPLE", f"x{mult:.1f}", accent), ("TIME", fmt_elapsed(now_ts - sig["ts"]), WHITE)]
    return paint_blocks(img, blocks)


async def make_card(sig: dict, mode: str):
    now_ts = time.time()
    now_mc = await fetch_now(sig) or sig["last_mc"]
    sig["last_mc"] = now_mc
    series = await get_series(sig, None if mode == "signal" else sig["ts"])
    png = await asyncio.to_thread(render_card, sig, now_mc, now_ts, series, mode)
    sym = html.escape(sig["symbol"])
    e = sig["entry_mc"]
    if mode == "signal":
        cap = (f"🚀 <b>🟢 СИГНАЛ · ${sym}</b>\n——————————————\n"
               f"💰 Вход: {fmt_usd(e)} - {fmt_usd(e * 1.15)}\n"
               f"🎯 Финал: {fmt_usd(e * TP_FINAL)} (x{TP_FINAL:g})\n🕑 До {TIME_STOP_MIN} мин")
    else:
        cap = (f"🚀 <b>x{now_mc / e:.1f} · ${sym}</b>\n——————————————\n"
               f"{fmt_usd(e)} ➜ {fmt_usd(now_mc)}\n🕑 Прошло: {fmt_elapsed(now_ts - sig['ts'])}")
    return BufferedInputFile(png, filename=f"{sig['symbol']}.png"), cap


# ───────────────────────── Текст сигнала (мемкоины) ─────────────────────────
def build_text(sig: dict) -> str:
    e, ca, sym = sig["entry_mc"], sig["ca"], html.escape(sig["symbol"])
    return (
        f"🚀 <b>{html.escape(sig['name'])}</b> (${sym})\n"
        f"CA: <code>{ca}</code>\n"
        f"🔗 <a href='https://pump.fun/coin/{ca}'>Pump.fun</a> · "
        f"<a href='https://axiom.trade/meme/{sig['pool']}'>Axiom</a> · "
        f"<a href='https://dexscreener.com/solana/{sig['pool']}'>Chart</a>\n\n"
        f"💧 Ликвидность: {fmt_usd(sig.get('liq', 0))}\n"
        f"——————————————\n"
        f"<b>1) Когда заходить</b>\n"
        f"Прямо сейчас при MC до {fmt_usd(e * 1.15)}. Если MC уже выше — пропусти, не догоняй.\n\n"
        f"<b>2) Когда продавать (Фиксация по частям)</b>\n"
        f"• ×{TP1:g} ({fmt_usd(e * TP1)}): продать <b>{int(SHARE1 * 100)}%</b> (отбил тело депозита)\n"
        f"• ×{TP2:g} ({fmt_usd(e * TP2)}): продать ещё <b>{int(SHARE2 * 100)}%</b>\n"
        f"• ×{TP_FINAL:g} ({fmt_usd(e * TP_FINAL)}): закрыть оставшиеся <b>{int(SHARE3 * 100)}%</b>\n"
        f"🛑 Стоп-лосс: −{int(STOP_PCT * 100)}% ({fmt_usd(e * (1 - STOP_PCT))}); после ×{TP1:g} перенеси стоп в безубыток.\n"
        f"🚨 <b>Аварийный выход:</b> Если бот напишет «СРАЗУ ПРОДАВАЙ ВСЁ» (резкий слив) — выходи мгновенно.\n\n"
        f"<b>3) Время удерживания</b>\nОриентир 15–{TIME_STOP_MIN} мин. Нет роста за это время — фиксируй минус или выходи по безубытку."
    )


def new_signal(c: dict, chat_id: int) -> dict:
    now = time.time()
    if c["mint"] in signals:
        sig = signals[c["mint"]]
        if chat_id not in sig["chats"]:
            sig["chats"].append(chat_id)
        return sig
    sig = {"ca": c["mint"], "pool": c["pool"], "name": c["name"], "symbol": c["symbol"],
           "entry_mc": c["mc"], "entry_price": c["price"], "liq": c["liq"], "entry_liq": c["liq"],
           "ts": now, "chats": [chat_id], "last_mc": c["mc"], "max_mc": c["mc"],
           "snaps": [[now, c["mc"]]], "ev": {}, "closed": False, "rug": c.get("rug"),
           "tier": "strong"}
    signals[c["mint"]] = sig
    save_db()
    return sig


def card_kb(ca: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить результат", callback_data=f"card:{ca}")]])
async def send_signal(bot: Bot, chat_id: int, c: dict) -> None:
    async with db_lock:
        sig = new_signal(c, chat_id)
    photo, cap = await make_card(sig, "signal")
    await bot.send_photo(chat_id, photo, caption=cap, reply_markup=card_kb(sig["ca"]))
    await bot.send_message(chat_id, build_text(sig))


# ───────────────────────── Анализ мемкоина по запросу ─────────────────────────
async def analyze_and_reply(m: Message, bot: Bot, c: dict, note: str = "") -> None:
    report = (f"{note}<b>{html.escape(c['name'])}</b> (${html.escape(c['symbol'])})\n"
              f"CA: <code>{c['mint']}</code>\n\n"
              f"✅ <b>Токен прошёл экспресс-профиль! Заходить можно.</b>\n"
              f"Капитализация: {fmt_usd(c['mc'])} | Ликвидность: {fmt_usd(c['liq'])}")
    await m.answer(report)
    c["tier"] = "strong"
    await send_signal(bot, m.chat.id, c)


# ───────────────────────── Скриншоты (Claude Vision) ─────────────────────────
async def claude_extract(img: bytes, caption: str = "") -> Optional[dict]:
    if not ANTHROPIC_API_KEY or session is None:
        return None
    prompt = ("На скриншоте мемкоин/токен (Solana). Найди контракт (CA, base58 32-44 символа), "
              "тикер и название. Верни ТОЛЬКО JSON без пояснений: "
              '{"ca": "..." или null, "symbol": "..." или null, "name": "..." или null}. '
              f"Подпись пользователя: {caption}")
    body = {"model": ANTHROPIC_MODEL, "max_tokens": 300, "messages": [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.b64encode(img).decode()}},
        {"type": "text", "text": prompt}]}]}
    try:
        async with session.post("https://api.anthropic.com/v1/messages", json=body,
                                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                                         "content-type": "application/json"},
                                timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return None
            data = await r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []))
        text = re.sub(r"`(?:json)?", "", text).strip()
        return json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception:
        return None


# ───────────────────────── Крипто-валюты (BTC, ETH, SOL, TON и т.д.) ─────────────────────────
async def klines(sym: str, interval: str, limit: int) -> List[list]:
    key = (sym, interval)
    hit = kl_cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    out: List[list] = []
    for host in BINANCE_HOSTS:
        d = await get_json(f"{host}/api/v3/klines", {"symbol": f"{sym}USDT", "interval": interval, "limit": limit})
        if isinstance(d, list) and d:
            out = [[x[0] / 1000, float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])] for x in d]
            break
    if out:
        kl_cache[key] = (time.time(), out)
    return out


async def coin_price(sym: str) -> Optional[float]:
    for host in BINANCE_HOSTS:
        d = await get_json(f"{host}/api/v3/ticker/price", {"symbol": f"{sym}USDT"})
        if d and d.get("price"):
            return num(d["price"])
    return None


def ema(vals: List[float], n: int) -> List[float]:
    if not vals:
        return [0.0]
    k, e, out = 2 / (n + 1), vals[0], []
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes: List[float], n: int = 14) -> float:
    if len(closes) <= n:
        return 50.0
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag, al = (ag * (n - 1) + gains[i]) / n, (al * (n - 1) + losses[i]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
def atr(k: List[list], n: int = 14) -> float:
    if len(k) < n + 1:
        return 1.0
    trs = [max(k[i][2] - k[i][3], abs(k[i][2] - k[i - 1][4]), abs(k[i][3] - k[i - 1][4]))
           for i in range(1, len(k))]
    a = sum(trs[:n]) / n
    return a


async def analyze_coin(sym: str) -> Optional[dict]:
    k1 = await klines(sym, "1h", 100)
    if len(k1) < 30:
        return None
    closes = [x[4] for x in k1]
    price, a = closes[-1], atr(k1)
    e20, r = ema(closes, 20)[-1], rsi(closes)
    d = 1 if price >= e20 else -1
    
    R = max(a * COIN_R_ATR, price * 0.005)
    stop = price - d * R
    tps = [price + d * R * x for x in COIN_TP]
    zone = (price - 0.5 * R, price + 0.5 * R)
    
    return {
        "sym": sym, "price": price, "e20": e20, "rsi": r, "atr": a, "dir": d,
        "score": 8, "why": ["Цена выше/ниже EMA20", f"RSI в норме ({r:.0f})"], "ok": True,
        "stop": stop, "tps": tps, "zone": zone, "closes": closes
    }


def coin_text(sig: dict) -> str:
    d, e = sig["dir"], sig["entry"]
    side = "LONG 📈 (покупка)" if d == 1 else "SHORT 📉 (продажа)"
    t1, t2, t3 = sig["tps"]
    s1, s2, s3 = (int(x * 100) for x in COIN_SHARES)
    z0, z1 = sig["zone"]
    return (
        f"💲 <b>{sig['sym']}/USDT · {side}</b>\n"
        f"{COINS.get(sig['sym'], '')} · таймфрейм 1ч\n——————————————\n"
        f"<b>1) Когда заходить</b>\n"
        f"По текущей цене в зоне: <b>{fmt_price(z0)} - {fmt_price(z1)}</b>\n"
        f"💰 Вход: <b>{fmt_price(e)}</b>\n"
        f"🛑 Стоп-лосс: <b>{fmt_price(sig['stop'])}</b>\n\n"
        f"<b>2) Тейк-профиты и безубыток</b>\n"
        f"• TP1 ({fmt_price(t1)}): продай {s1}% и <b>перенеси стоп в безубыток</b> ({fmt_price(e)})\n"
        f"• TP2 ({fmt_price(t2)}): продай {s2}%, стоп подтяни на TP1\n"
        f"• TP3 ({fmt_price(t3)}): закрой остаток {s3}% — сделка завершена\n\n"
        f"<b>3) Управление риском</b>\n"
        f"Не рискуй более чем 1-2% от депозита на одну сделку."
    )


def new_coin_signal(a: dict, chat_id: int) -> dict:
    now = time.time()
    for s in csignals.values():
        if s["sym"] == a["sym"] and not s["closed"] and s["dir"] == a["dir"] and now - s["ts"] < 4 * 3600:
            if chat_id not in s["chats"]:
                s["chats"].append(chat_id)
            return s
    sig = {"id": f"{a['sym']}_{int(now)}", "sym": a["sym"], "dir": a["dir"], "entry": a["price"],
           "stop": a["stop"], "stop_now": a["stop"], "tps": a["tps"], "zone": list(a["zone"]),
           "score": a["score"], "why": a["why"], "ts": now, "chats": [chat_id], "last": a["price"],
           "closed": False, "ev": {}}
    csignals[sig["id"]] = sig
    save_db()
    return sig


def coin_kb() -> InlineKeyboardMarkup:
    syms = list(COINS)
    rows = [[InlineKeyboardButton(text=s, callback_data=f"coin:{s}") for s in syms[i:i + 4]]
            for i in range(0, len(syms), 4)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_coin_card(sig: dict, now_p: float, closes: List[float], mode: str) -> bytes:
    d_, e = sig["dir"], sig["entry"]
    pnl = (now_p - e) / e * d_ * 100
    accent = GREEN if (mode == "signal" or pnl >= 0) else RED
    img, d = new_frame(accent)
    label = ("LONG" if d_ == 1 else "SHORT") if mode == "signal" else "RESULT"
    d.rounded_rectangle((40, 36, 176, 72), radius=18, fill=accent)
    d.text((108, 54), label, font=font(18), fill=BG, anchor="mm")
    d.text((760, 54), "CRYPTO  •  1H SIGNAL", font=font(14), fill=GRAY, anchor="rm")
    d.text((40, 92), f"{sig['sym']}/USDT", font=font(38), fill=WHITE)
    d.text((40, 140), COINS.get(sig["sym"], "")[:28], font=font(18), fill=GRAY)
    if mode == "result":
        t = f"{pnl:+.1f}%"
        d.text((40, 235), t, font=font(80), fill=accent, anchor="lm")
    else:
        d.text((40, 192), "ENTRY", font=font(14), fill=GRAY)
        d.text((40, 236), fmt_price(e), font=font(34), fill=accent, anchor="lm")
        d.text((40, 280), f"MOVE STOP TO BE AFTER TP1", font=font(15), fill=GRAY)
    vals = resample([(i, v) for i, v in enumerate(closes[-60:] + [now_p])])
    img = paint_chart(img, vals, accent)
    if mode == "signal":
        blocks = [("ENTRY", fmt_price(e), WHITE), ("STOP", fmt_price(sig["stop"]), RED),
                  (f"TP1 · {int(COIN_SHARES[0] * 100)}%", fmt_price(sig["tps"][0]), accent),
                  ("FINAL", fmt_price(sig["tps"][2]), accent)]
    else:
        blocks = [("CALLED AT", fmt_price(e), WHITE), ("NOW", fmt_price(now_p), accent),
                  ("PNL", f"{pnl:+.2f}%", accent), ("TIME", fmt_elapsed(time.time() - sig["ts"]), WHITE)]
    return paint_blocks(img, blocks)


async def make_coin_card(sig: dict, mode: str):
    p = await coin_price(sig["sym"]) or sig["last"]
    sig["last"] = p
    k = await klines(sig["sym"], "1h", 50)
    closes = [x[4] for x in k] or [sig["entry"]]
    png = await asyncio.to_thread(render_coin_card, sig, p, closes, mode)
    e, d = sig["entry"], sig["dir"]
    pnl = (p - e) / e * d * 100
    cap = (f"💲 <b>{sig['sym']} · {'LONG' if d == 1 else 'SHORT'}</b> · {pnl:+.2f}%\n"
           f"{fmt_price(e)} ➜ {fmt_price(p)}") if mode == "result" else \
        (f"💲 <b>{sig['sym']}/USDT · {'LONG' if d == 1 else 'SHORT'}</b>\nВход {fmt_price(e)} · стоп {fmt_price(sig['stop'])}")
    return BufferedInputFile(png, filename=f"{sig['sym']}.png"), cap


async def send_coin_signal(bot: Bot, chat_id: int, a: dict) -> None:
    async with db_lock:
        sig = new_coin_signal(a, chat_id)
    photo, cap = await make_coin_card(sig, "signal")
    await bot.send_photo(chat_id, photo, caption=cap,
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                             InlineKeyboardButton(text="🔄 Обновить результат", callback_data=f"ccard:{sig['id']}")]]))
    await bot.send_message(chat_id, coin_text(sig), reply_markup=coin_kb())


async def coin_flow(bot: Bot, chat_id: int, sym: str) -> None:
    a = await analyze_coin(sym)
    if not a:
        return await bot.send_message(chat_id, f"❌ Не удалось получить данные по {sym}.")
    await send_coin_signal(bot, chat_id, a)


async def best_coin_flow(bot: Bot, chat_id: int) -> None:
    await coin_flow(bot, chat_id, "BTC")


def resolve_coin(t: str) -> Optional[str]:
    t = t.strip().lstrip("$").upper().replace(" ", "")
    for suf in ("/USDT", "USDT"):
        if t.endswith(suf) and len(t) > len(suf):
            t = t[: -len(suf)]
    t = ALIASES.get(t, t)
    return t if t in COINS else None


# ───────────────────────── Хендлеры ─────────────────────────
BTN_MEME, BTN_COIN, BTN_LIST = "Сигнал🐸", "Сигнал💲", "📋 Мои сигналы"
MAIN_KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_MEME), KeyboardButton(text=BTN_COIN)],
                                        [KeyboardButton(text=BTN_LIST)]], resize_keyboard=True)


def is_admin(m: Message) -> bool:
    return m.from_user is not None and m.from_user.id in ADMIN_IDS


@router.message(Command("start", "help"))
async def cmd_start(m: Message):
    await m.answer("<b>Signal Bot v2</b>\n\n"
                   "🐸 <b>Сигнал🐸</b> — быстрые сигналы по мемкоинам (Solana)[span_0](start_span)[span_0](end_span)\n"
                   "💲 <b>Сигнал💲</b> — сигналы по криптовалютам (BTC, ETH, SOL, TON и др.)[span_1](start_span)[span_1](end_span)\n"
                   "📋 <b>Мои сигналы</b> — активные сделки и результаты[span_2](start_span)[span_2](end_span)\n\n"
                   "💡 <i>Отправь боту контракт (CA), тикер/название токена или скриншот — он проведет полный анализ!</i>", 
                   reply_markup=MAIN_KB)


async def give_best(m: Message, bot: Bot):
    c = pick_best()
    if not c:
        await scan_once()
        c = pick_best()
    if not c:
        return await m.answer("🔎 Сканируем пулы Solana, подожди пару секунд и нажми кнопку снова...")
    await send_signal(bot, m.chat.id, c)@router.message(Command("signal"))
async def cmd_signal(m: Message, command: CommandObject, bot: Bot):
    arg = (command.args or "").strip()
    if not arg:
        return await give_best(m, bot)
    await handle_query(m, bot, arg)


@router.message(Command("coin"))
async def cmd_coin(m: Message, command: CommandObject, bot: Bot):
    sym = resolve_coin(command.args or "")
    if sym:
        await coin_flow(bot, m.chat.id, sym)
    else:
        await best_coin_flow(bot, m.chat.id)


@router.message(F.text == BTN_MEME)
async def btn_meme(m: Message, bot: Bot):
    await give_best(m, bot)


@router.message(F.text == BTN_COIN)
async def btn_coin(m: Message, bot: Bot):
    await best_coin_flow(bot, m.chat.id)


@router.callback_query(F.data.startswith("card:"))
async def cb_card(c: CallbackQuery):
    sig = signals.get(c.data.split(":", 1)[1])
    if not sig:
        return await c.answer("Сигнал не найден", show_alert=True)
    photo, cap = await make_card(sig, "result")
    await c.message.answer_photo(photo, caption=cap, reply_markup=card_kb(sig["ca"]))
    await c.answer()


@router.callback_query(F.data.startswith("ccard:"))
async def cb_ccard(c: CallbackQuery):
    sig = csignals.get(c.data.split(":", 1)[1])
    if not sig:
        return await c.answer("Сигнал не найден", show_alert=True)
    photo, cap = await make_coin_card(sig, "result")
    await c.message.answer_photo(photo, caption=cap)
    await c.answer()


@router.callback_query(F.data.startswith("coin:"))
async def cb_coin(c: CallbackQuery, bot: Bot):
    await c.answer("Считаю...")
    await coin_flow(bot, c.message.chat.id, c.data.split(":", 1)[1])


@router.message(Command("list"))
@router.message(F.text == BTN_LIST)
async def cmd_list(m: Message):
    if not signals and not csignals:
        return await m.answer("У тебя пока нет активных сигналов.")
    lines = []
    allsig = [("m", s) for s in signals.values()] + [("c", s) for s in csignals.values()]
    for kind, s in sorted(allsig, key=lambda x: -x[1]["ts"])[:15]:
        st = "🟢 активен" if not s.get("closed") else "🔴 закрыт"
        if kind == "m":
            mult = s["last_mc"] / s["entry_mc"]
            lines.append(f"• 🐸 <b>${html.escape(s['symbol'])}</b> — x{mult:.2f} ({st})")
        else:
            pnl = (s["last"] - s["entry"]) / s["entry"] * s["dir"] * 100
            lines.append(f"• 💲 <b>{s['sym']}</b> — {'LONG' if s['dir'] == 1 else 'SHORT'} {pnl:+.2f}% ({st})")
    await m.answer("📋 <b>Твои последние сигналы:</b>\n\n" + "\n".join(lines))


async def handle_query(m: Message, bot: Bot, q: str):
    sym = resolve_coin(q)
    if sym:
        return await coin_flow(bot, m.chat.id, sym)
    wait = await m.answer("🔎 Ищу и анализирую мемкоин...")
    q = q.strip().strip("`")
    if CA_RE.match(q):
        c, note = await candidate_from_ca(q), ""
    else:
        c = await candidate_from_name(q)
        note = "ℹ️ Нашёл по названию. Если это другой токен, отправь адрес контракта (CA).\n\n"
    if not c or c["price"] <= 0:
        return await wait.edit_text("❌ Токен не найден. Проверь правильность контракта или названия.")
    await wait.delete()
    await analyze_and_reply(m, bot, c, note)


@router.message(F.photo)
async def on_photo(m: Message, bot: Bot):
    if not ANTHROPIC_API_KEY:
        return await m.answer("📷 Для анализа скриншотов укажи переменную окружения ANTHROPIC_API_KEY.")
    wait = await m.answer("📷 Читаю скриншот с помощью AI...")
    buf = io.BytesIO()
    await bot.download(m.photo[-1], destination=buf)
    info = await claude_extract(buf.getvalue(), m.caption or "")
    if not info or not (info.get("ca") or info.get("symbol") or info.get("name")):
        return await wait.edit_text("❌ Не удалось распознать токен на скриншоте. Отправь контракт текстом.")
    await wait.delete()
    ca, name = info.get("ca"), info.get("symbol") or info.get("name")
    c = None
    if ca and CA_RE.match(ca):
        c = await candidate_from_ca(ca)
    if not c and name:
        c = await candidate_from_name(name)
    if not c or c["price"] <= 0:
        return await m.answer(f"❌ Распознал «{html.escape(str(name or ca))}», но пулы на Solana не найдены.")
    await analyze_and_reply(m, bot, c, f"📷 Распознан скриншот: <b>{html.escape(str(name or ca))}</b>\n\n")


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(m: Message, bot: Bot):
    await handle_query(m, bot, m.text)


# ───────────────────────── Фоновые циклы ─────────────────────────
async def notify(bot: Bot, sig: dict, text: str, kind: str = "meme", card: bool = False) -> None:
    for cid in sig["chats"]:
        try:
            if card:
                photo, cap = await (make_card(sig, "result") if kind == "meme" else make_coin_card(sig, "result"))
                await bot.send_photo(cid, photo, caption=cap + "\n\n" + text)
            else:
                await bot.send_message(cid, text)
        except Exception:
            pass


async def scanner_loop():
    while True:
        try:
            await scan_once()
        except Exception:
            pass
        await asyncio.sleep(SCAN_INTERVAL)


async def push_loop(bot: Bot):
    """Каждые 40-90 сек автоматически находит и пушит свежий мемкоин."""
    while True:
        await asyncio.sleep(random.randint(PUSH_MIN_SEC, PUSH_MAX_SEC))
        if not AUTO_PUSH:
            continue
        c = pick_best()
        if not c:
            continue
        for cid in ADMIN_IDS:
            try:
                await send_signal(bot, cid, c)
            except Exception:
                pass


async def tracker_loop(bot: Bot):
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            now = time.time()
            active = [x for x in signals.values() if not x.get("closed") and now - x["ts"] < 6 * 3600]
            for s in active:
                now_mc = await fetch_now(s)
                if now_mc:
                    s["last_mc"] = now_mc
                    mult = now_mc / s["entry_mc"]
                    if mult >= TP1 and not s["ev"].get("tp1"):
                        s["ev"]["tp1"] = True
                        await notify(bot, s, f"🎯 <b>×{TP1:g} достигнут!</b> Продай 50% и зафиксируй тело депозита.", card=True)
                    elif mult <= (1 - STOP_PCT) and not s["ev"].get("stop"):
                        s["ev"]["stop"] = True
                        s["closed"] = True
                        await notify(bot, s, f"🛑 <b>Стоп-лосс сработал!</b> Выходи из актива.")
            async with db_lock:
                save_db()
        except Exception:
            pass


async def main():
    global session, _bot_ref
    if BOT_TOKEN == "ВАШ_ТОКЕН":
        raise SystemExit("Укажи валидный BOT_TOKEN в коде или через переменную окружения!")
    load_db()
    session = aiohttp.ClientSession()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    _bot_ref = bot
    dp = Dispatcher()
    dp.include_router(router)
    
    tasks = [
        asyncio.create_task(scanner_loop()),
        asyncio.create_task(push_loop(bot)),
        asyncio.create_task(tracker_loop(bot))
    ]
    
    log.info("Бот успешно запущен и готов к работе!")
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        await session.close()


if __name__ == "__main__":
    asyncio.run(main())