# bot.py — Whales Alpha Memecoin Bot (MEME-only) — Full Inline Config, no ENV

import os
import time
import random
import threading
import sqlite3
import base64
from contextlib import contextmanager
from typing import Optional, Dict, List, Tuple

import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

# =========================
# Inline-Konfiguration
# =========================
BOT_TOKEN = "8410607683:AAGEErNQv0EUTA6H5INeHXtaCrt_KnG3KI8"          # <- dein Bot-Token
ADMIN_IDS = ["8076025426"]                                            # Admin-Telegram-IDs
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
CENTRAL_SOL_PUBKEY = "CAYNcLuq8Jybk8GV1e7nCKGK6msAw8ktKSaXDqqBc3f4"  # Zentrale Einzahlungsadresse (nur Info-/UI-Zweck)

DB_PATH = "memebot_full.db"
LAMPORTS_PER_SOL = 1_000_000_000
MIN_SUB_SOL = 0.1

# Live Trading via Jupiter v6
LIVE_TRADE = True            # <- für Live True setzen
TRADER_PRIVATE_KEY = "[175,201,155,170,122,157,92,3,129,91,248,195,92,212,234,154,125,62,234,166,75,175,163,234,25,129,13,28,82,120,26,215,165,226,65,173,159,126,43,183,5,218,226,17,181,44,119,252,152,24,248,182,204,229,75,122,205,147,35,69,245,88,140,255]"       # <- hier deinen Private Key eintragen (base58 ODER JSON-Array string)
TRADER_PUBLIC_KEY  = "CAYNcLuq8Jybk8GV1e7nCKGK6msAw8ktKSaXDqqBc3f4"       # <- zugehörige Public Key (wallet address)
MAX_SLIPPAGE_BPS = 300        # 3.00%
WSOL_MINT = "So11111111111111111111111111111111111111112"
JUP_QUOTE = "https://quote-api.jup.ag/v6/quote"
JUP_SWAP  = "https://quote-api.jup.ag/v6/swap"

# =========================
# Preise & Helfer
# =========================
_price_cache = {"t": 0.0, "usd": 0.0}

def get_sol_usd() -> float:
    now = time.time()
    if now - _price_cache["t"] < 60 and _price_cache["usd"] > 0:
        return _price_cache["usd"]
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": "solana", "vs_currencies": "usd"}, timeout=6)
        usd = float(r.json().get("solana", {}).get("usd", 0.0) or 0.0)
        if usd > 0:
            _price_cache.update({"t": now, "usd": usd})
            return usd
    except Exception:
        pass
    return _price_cache["usd"] or 0.0

def fmt_sol_usdc(lamports_or_int: int) -> str:
    lam = int(lamports_or_int or 0)
    sol = lam / LAMPORTS_PER_SOL
    usd = get_sol_usd()
    if usd > 0:
        return f"{sol:.6f} SOL (~{sol*usd:.2f} USDC)"
    return f"{sol:.6f} SOL"

def md_escape(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return (text.replace('\\', '\\\\')
                .replace('_', '\\_')
                .replace('*', '\\*')
                .replace('`', '\\`')
                .replace('[', '\\['))

def is_admin(user_id: int) -> bool:
    return str(user_id) in ADMIN_IDS

def is_probably_solana_address(addr: str) -> bool:
    if not isinstance(addr, str): return False
    addr = addr.strip()
    if len(addr) < 32 or len(addr) > 44: return False
    allowed = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return all(ch in allowed for ch in addr)

# =========================
# DB & Schema (erweitert)
# =========================
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  username TEXT,
  is_admin INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  sub_active INTEGER DEFAULT 0,
  auto_mode TEXT DEFAULT 'OFF',
  auto_risk TEXT DEFAULT 'MEDIUM',
  sol_balance_lamports INTEGER DEFAULT 0,
  source_wallet TEXT,
  payout_wallet TEXT,
  sub_types TEXT DEFAULT '',
  referral_code TEXT DEFAULT '',
  referral_bonus_claimed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen_txs (
  sig TEXT PRIMARY KEY,
  user_id INTEGER,
  amount_lamports INTEGER,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_by INTEGER NOT NULL,
  market_type TEXT NOT NULL,
  base TEXT NOT NULL,
  token_address TEXT,
  notes TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  status TEXT DEFAULT 'OPEN'   -- OPEN/CLOSED
);
CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,        -- QUEUED/FILLED/OPEN/CLOSED/ERROR
  txid TEXT,
  message TEXT,
  stake_lamports INTEGER DEFAULT 0,
  entry_spent_lamports INTEGER DEFAULT 0,  -- wie viel SOL ausgegeben
  token_mint TEXT,
  token_amount_raw TEXT,                   -- outAmount (string int, smallest units)
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  closed_at TIMESTAMP,
  close_txid TEXT,
  close_received_lamports INTEGER DEFAULT 0,
  pnl_lamports INTEGER DEFAULT 0,
  pnl_percent REAL DEFAULT 0.0
);
"""

@contextmanager
def get_db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()

def init_db():
    with get_db() as con:
        con.executescript(SCHEMA)
        # idempotente Migrations
        for stmt in [
            "ALTER TABLE calls ADD COLUMN status TEXT DEFAULT 'OPEN'",
            "ALTER TABLE executions ADD COLUMN entry_spent_lamports INTEGER DEFAULT 0",
            "ALTER TABLE executions ADD COLUMN token_mint TEXT",
            "ALTER TABLE executions ADD COLUMN token_amount_raw TEXT",
            "ALTER TABLE executions ADD COLUMN closed_at TIMESTAMP",
            "ALTER TABLE executions ADD COLUMN close_txid TEXT",
            "ALTER TABLE executions ADD COLUMN close_received_lamports INTEGER DEFAULT 0",
            "ALTER TABLE executions ADD COLUMN pnl_lamports INTEGER DEFAULT 0",
            "ALTER TABLE executions ADD COLUMN pnl_percent REAL DEFAULT 0.0"
        ]:
            try: con.execute(stmt)
            except Exception: pass

init_db()

# =========================
# User/Call CRUD
# =========================
def row_get(row, key, default=None):
    if row is None: return default
    try:
        return row[key] if key in row.keys() else default
    except Exception:
        try:
            return row.get(key, default)
        except Exception:
            return default

def upsert_user(user_id: int, username: str, is_admin_flag: int):
    with get_db() as con:
        con.execute("""
            INSERT INTO users(user_id, username, is_admin)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
        """, (user_id, username or "", is_admin_flag))

def get_user(user_id: int):
    with get_db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def add_balance(user_id: int, lamports: int):
    with get_db() as con:
        con.execute("UPDATE users SET sol_balance_lamports = sol_balance_lamports + ? WHERE user_id=?", (lamports, user_id))

def subtract_balance(user_id: int, lamports: int) -> bool:
    with get_db() as con:
        row = con.execute("SELECT sol_balance_lamports FROM users WHERE user_id=?", (user_id,)).fetchone()
        cur = row_get(row, "sol_balance_lamports", 0)
        if cur < lamports:
            return False
        con.execute("UPDATE users SET sol_balance_lamports = sol_balance_lamports - ? WHERE user_id=?", (lamports, user_id))
        return True

def get_balance_lamports(user_id: int) -> int:
    with get_db() as con:
        row = con.execute("SELECT sol_balance_lamports FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row_get(row, "sol_balance_lamports", 0)

def set_subscription(user_id: int, active: bool):
    with get_db() as con:
        con.execute("UPDATE users SET sub_active=? WHERE user_id=?", (1 if active else 0, user_id))

def set_subscription_types(user_id: int, types: List[str]):
    st = ",".join(sorted(set([t.upper() for t in types if t])))
    with get_db() as con:
        con.execute("UPDATE users SET sub_types=? WHERE user_id=?", (st, user_id))

def set_auto_mode(user_id: int, mode: str):
    with get_db() as con:
        con.execute("UPDATE users SET auto_mode=? WHERE user_id=?", (mode, user_id))

def set_auto_risk(user_id: int, risk: str):
    with get_db() as con:
        con.execute("UPDATE users SET auto_risk=? WHERE user_id=?", (risk, user_id))

def get_subscription_types(user_id: int) -> List[str]:
    u = get_user(user_id)
    if not u: return []
    st = row_get(u, "sub_types", "")
    return [s for s in st.split(",") if s]

def _risk_fraction(risk: str) -> float:
    return {"LOW": 0.20, "MEDIUM": 0.35, "HIGH": 0.65}.get((risk or "").upper(), 0.35)

def _compute_stake_for_user(user_id: int) -> int:
    u = get_user(user_id)
    if not u: return 0
    frac = _risk_fraction(row_get(u, "auto_risk", "MEDIUM"))
    bal = row_get(u, "sol_balance_lamports", 0)
    return max(int(bal * frac), int(0.01 * LAMPORTS_PER_SOL))

def create_call(created_by: int, base: str, token_addr: str, notes: str) -> int:
    with get_db() as con:
        cur = con.execute("""
            INSERT INTO calls(created_by, market_type, base, token_address, notes)
            VALUES (?,?,?,?,?)
        """, (created_by, "MEME", base, token_addr, notes))
        return cur.lastrowid

def get_call(cid: int):
    with get_db() as con:
        return con.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()

def set_call_status(cid: int, status: str):
    with get_db() as con:
        con.execute("UPDATE calls SET status=? WHERE id=?", (status, cid))

def queue_execution(call_id: int, user_id: int, status: str = "QUEUED", message: str = "", stake_lamports: Optional[int] = None) -> int:
    if stake_lamports is None:
        stake_lamports = _compute_stake_for_user(user_id)
    with get_db() as con:
        cur = con.execute("""
            INSERT INTO executions(call_id, user_id, mode, status, message, stake_lamports)
            VALUES(?,?,'ON',?,?,?)
        """, (call_id, user_id, status, message, stake_lamports))
        return cur.lastrowid

def fmt_call(c) -> str:
    core = f"MEME • {row_get(c,'base','?')}"
    extra = f"\nToken: `{md_escape(row_get(c,'token_address',''))}`" if row_get(c,"token_address") else ""
    note = f"\nNotes: {md_escape(row_get(c,'notes',''))}" if row_get(c,"notes") else ""
    return f"🧩 *{core}*{extra}{note}"

def all_subscribers():
    with get_db() as con:
        return [r["user_id"] for r in con.execute("SELECT user_id FROM users WHERE sub_active=1").fetchall()]

# =========================
# Telebot init mit Safe-Send (FIX)
# =========================
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# Speichere Original-Methoden der *Instanz*
_original_send_message = bot.send_message
_original_edit_message_text = bot.edit_message_text

def _safe_send_message(chat_id, text, **kwargs):
    try:
        return _original_send_message(chat_id, text, **kwargs)
    except Exception:
        # Fallback: ohne Markdown/ParseMode erneut versuchen
        kwargs2 = dict(kwargs)
        kwargs2.pop("parse_mode", None)
        try:
            return _original_send_message(chat_id, str(text), **kwargs2)
        except Exception:
            # Letzter Versuch: minimal
            return _original_send_message(chat_id, str(text))

def _safe_edit_message_text(text, chat_id, message_id, **kwargs):
    try:
        return _original_edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception:
        kwargs2 = dict(kwargs)
        kwargs2.pop("parse_mode", None)
        return _original_edit_message_text(str(text), chat_id, message_id, **kwargs2)

# Monkeypatch
bot.send_message = _safe_send_message
bot.edit_message_text = _safe_edit_message_text

# =========================
# Jupiter Live Trading (optional)
# =========================
try:
    from solana.rpc.api import Client as SolClient
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction
    from solders.message import to_bytes_versioned
    import json, base58
    _sol_client = SolClient(SOLANA_RPC)
except Exception:
    SolClient = None
    Keypair = None
    VersionedTransaction = None
    to_bytes_versioned = None
    base58 = None
    _sol_client = None

def _load_keypair_inline() -> Optional["Keypair"]:
    if not TRADER_PRIVATE_KEY:
        return None
    try:
        txt = TRADER_PRIVATE_KEY.strip()
        if txt.startswith("["):
            arr = json.loads(txt)
            return Keypair.from_bytes(bytes(arr))
        else:
            secret = base58.b58decode(txt)
            return Keypair.from_bytes(secret)
    except Exception as e:
        print("Keypair parse error:", e)
        return None

_TRADER_KP = _load_keypair_inline()

def jup_quote(input_mint: str, output_mint: str, amount_raw: int) -> Optional[dict]:
    try:
        r = requests.get(JUP_QUOTE, params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount_raw)),
            "slippageBps": str(MAX_SLIPPAGE_BPS),
            "swapMode": "ExactIn",
            "onlyDirectRoutes": "false"
        }, timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("quote err:", e)
        return None

def jup_swap_tx(quote: dict, user_pubkey: str) -> Optional[str]:
    try:
        sr = requests.post(JUP_SWAP, json={
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "useSharedAccounts": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto",
        }, timeout=20)
        sr.raise_for_status()
        payload = sr.json()
        return payload.get("swapTransaction")
    except Exception as e:
        print("swap build err:", e)
        return None

def jup_send_b64_tx(b64tx: str) -> Optional[str]:
    try:
        raw = base64.b64decode(b64tx)
        tx = VersionedTransaction.deserialize(raw)
        tx = VersionedTransaction(tx.message, [_TRADER_KP.sign_message(to_bytes_versioned(tx.message))])
        sig = _sol_client.send_raw_transaction(tx.serialize()).value
        return str(sig)
    except Exception as e:
        print("send err:", e)
        return None

def dex_market_buy_live(token_mint: str, amount_lamports: int) -> dict:
    if not (LIVE_TRADE and _TRADER_KP and TRADER_PUBLIC_KEY and _sol_client):
        return {"status": "ERROR", "message": "live_disabled_or_missing_key"}
    q = jup_quote(WSOL_MINT, token_mint, amount_lamports)
    if not q or "routePlan" not in q:
        return {"status": "ERROR", "message": "no_route"}
    b64tx = jup_swap_tx(q, TRADER_PUBLIC_KEY)
    if not b64tx:
        return {"status": "ERROR", "message": "no_swap_tx"}
    sig = jup_send_b64_tx(b64tx)
    if not sig:
        return {"status": "ERROR", "message": "send_fail"}
    # Merke, wie viel Token wir erwarten (outAmount)
    out_amount = str(q.get("outAmount", "0"))
    return {"status": "FILLED", "txid": sig, "spent_lamports": int(amount_lamports),
            "token_amount_raw": out_amount, "token_mint": token_mint}

def dex_market_sell_live(token_mint: str, token_amount_raw: int) -> dict:
    if not (LIVE_TRADE and _TRADER_KP and TRADER_PUBLIC_KEY and _sol_client):
        return {"status": "ERROR", "message": "live_disabled_or_missing_key"}
    q = jup_quote(token_mint, WSOL_MINT, token_amount_raw)
    if not q or "routePlan" not in q:
        return {"status": "ERROR", "message": "no_route"}
    b64tx = jup_swap_tx(q, TRADER_PUBLIC_KEY)
    if not b64tx:
        return {"status": "ERROR", "message": "no_swap_tx"}
    sig = jup_send_b64_tx(b64tx)
    if not sig:
        return {"status": "ERROR", "message": "send_fail"}
    in_sol = int(q.get("outAmount", "0"))  # bei Sell ist outAmount in WSOL Lamports
    return {"status": "FILLED", "txid": sig, "received_lamports": in_sol}

def dex_market_buy_sim(user_id: int, base_label: str, amount_lamports: int, token_mint: str) -> dict:
    # Sim: wir tun so, als hätten wir 1:1 SOL->Token, outAmount = amount_lamports * 1e3 an "Token-Units"
    fake_out = amount_lamports * 1000
    return {"status": "FILLED", "txid": f"SIM-BUY-{base_label}-{int(time.time())}",
            "spent_lamports": amount_lamports, "token_amount_raw": str(fake_out), "token_mint": token_mint}

def dex_market_sell_sim(token_mint: str, token_amount_raw: int, entry_spent: int, factor: float) -> dict:
    # Sim: mit factor * entry_spent in SOL aussteigen
    recv = int(entry_spent * factor)
    return {"status": "FILLED", "txid": f"SIM-SELL-{token_mint[:4]}-{int(time.time())}",
            "received_lamports": recv}

def dex_market_buy(user_id: int, token_mint: str, amount_lamports: int, base_label: str = "") -> dict:
    if LIVE_TRADE:
        return dex_market_buy_live(token_mint, amount_lamports)
    return dex_market_buy_sim(user_id, base_label or token_mint[:6], amount_lamports, token_mint)

def dex_market_sell(token_mint: str, token_amount_raw: int, entry_spent: int) -> dict:
    if LIVE_TRADE:
        return dex_market_sell_live(token_mint, int(token_amount_raw))
    # Für Autoclose=100% nehmen wir 2x als Beispiel (wird im Monitor gesteuert)
    return dex_market_sell_sim(token_mint, int(token_amount_raw), entry_spent, factor=2.0)

def dex_estimate_sell_value_lamports(token_mint: str, token_amount_raw: int) -> Optional[int]:
    """Für Monitor: wie viel SOL würde ein Verkauf aktuell bringen? (Quote-only)"""
    if LIVE_TRADE:
        q = jup_quote(token_mint, WSOL_MINT, int(token_amount_raw))
        if not q or "outAmount" not in q:
            return None
        return int(q["outAmount"])
    else:
        # Simu: Wir lassen es langsam steigen: 1.2x + kleines Rauschen
        base = 1.2
        jitter = 0.0
        return None  # Im Simu lassen wir AutoClose trotzdem über SELL (2x) passieren.
                    # (Der Monitor löst Close selbst aus, ohne Valuation)
# =========================
# Keyboards
# =========================
def kb_main(u):
    bal = fmt_sol_usdc(row_get(u, "sol_balance_lamports", 0))
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💸 Einzahlen", callback_data="deposit"),
           InlineKeyboardButton("💳 Auszahlung", callback_data="withdraw"))
    kb.add(InlineKeyboardButton("🔔 Meme-Signale", callback_data="sub_menu"))
    kb.add(InlineKeyboardButton("⚙️ Auto-Entry", callback_data="auto_menu"),
           InlineKeyboardButton("🧩 Call erstellen", callback_data="admin_new_call"))
    kb.add(InlineKeyboardButton("🗂 Offene Calls", callback_data="admin_list_calls"),
           InlineKeyboardButton("❓ Hilfe", callback_data="help"))
    kb.add(InlineKeyboardButton(f"🏦 Guthaben: {bal}", callback_data="noop"))
    return kb

def kb_auto(u):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Auto: OFF", callback_data="auto_OFF"),
           InlineKeyboardButton("Auto: ON", callback_data="auto_ON"))
    kb.add(InlineKeyboardButton("Risk: LOW", callback_data="risk_LOW"),
           InlineKeyboardButton("Risk: MEDIUM", callback_data="risk_MEDIUM"),
           InlineKeyboardButton("Risk: HIGH", callback_data="risk_HIGH"))
    kb.add(InlineKeyboardButton("⬅️ Zurück", callback_data="back_home"))
    return kb

def kb_call_admin_controls(call_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⏹ Call schließen (Force)", callback_data=f"admin_close_call_{call_id}"))
    kb.add(InlineKeyboardButton("⬅️ Zurück", callback_data="back_home"))
    return kb

# =========================
# Transient States
# =========================
ADMIN_AWAIT_TOKEN: Dict[int, bool] = {}
WAITING_SOURCE_WALLET: Dict[int, bool] = {}
WAITING_PAYOUT_WALLET: Dict[int, bool] = {}
WAITING_WITHDRAW_AMOUNT: Dict[int, Optional[int]] = {}

# =========================
# Home & Start
# =========================
def get_bot_username():
    try:
        me = bot.get_me()
        return me.username or "<YourBotUsername>"
    except Exception:
        return "<YourBotUsername>"

BRAND = "🐋 Whales Alpha Memecoin Bot"

def home_text(u) -> str:
    raw_uname = ("@" + row_get(u, "username", "")) if row_get(u, "username") else f"ID {row_get(u, 'user_id','?')}"
    uname = md_escape(raw_uname)
    bal = fmt_sol_usdc(row_get(u, "sol_balance_lamports", 0))
    bot_username = get_bot_username()
    return (
        f"{BRAND}\n\n"
        f"👋 Hallo {uname}\n\n"
        "• Einzahlungen & Auto-Gutschrift\n"
        "• 🔔 Meme-Signale & Auto-Entry (LOW/MEDIUM/HIGH)\n"
        "• Auto-TP bei +100% & Force-Close durch Admin\n\n"
        f"🏦 Guthaben: *{bal}*\n"
        f"🤖 Live Trading: {'ON' if LIVE_TRADE else 'OFF'}"
    )

@bot.message_handler(commands=["start"])
def cmd_start(m: Message):
    uid = m.from_user.id
    uname = m.from_user.username or ""
    upsert_user(uid, uname, 1 if is_admin(uid) else 0)
    u = get_user(uid)
    bot.reply_to(m, home_text(u), reply_markup=kb_main(u))

# =========================
# Auto-Entry: Call Broadcast & Execution
# =========================
def broadcast_call_and_queue(call_row):
    """Sendet Call an Subcriber und queued Execution für Auto-ON Nutzer."""
    msg = "📣 Neuer Meme-Call:\n" + fmt_call(call_row)
    subs = all_subscribers()
    sent = 0
    for su in subs:
        try:
            bot.send_message(su, msg, parse_mode="Markdown")
            # Auto-ON ? -> QUEUE
            u = get_user(su)
            if row_get(u, "auto_mode", "OFF") == "ON":
                queue_execution(row_get(call_row,"id"), su, status="QUEUED", message="Queued by call broadcast")
            sent += 1
        except Exception:
            pass
    return sent

# =========================
# Executor & Monitor
# =========================
def execute_buy_for_queue():
    """Abarbeiten von QUEUED -> BUY -> OPEN"""
    while True:
        try:
            with get_db() as con:
                rows = con.execute("""
                    SELECT e.id as eid, e.user_id, e.call_id, e.status, e.stake_lamports,
                           c.token_address, c.base
                    FROM executions e
                    JOIN calls c ON c.id = e.call_id
                    WHERE e.status='QUEUED' AND c.status='OPEN'
                    LIMIT 100
                """).fetchall()
            for r in rows:
                eid = row_get(r,"eid"); uid = row_get(r,"user_id")
                stake = int(row_get(r,"stake_lamports",0))
                bal = get_balance_lamports(uid)
                if bal < stake:
                    with get_db() as con:
                        con.execute("UPDATE executions SET status='ERROR', message='Insufficient balance' WHERE id=?", (eid,))
                    try: bot.send_message(uid, f"❌ Auto-Entry fehlgeschlagen: Unzureichendes Guthaben ({fmt_sol_usdc(bal)} < {fmt_sol_usdc(stake)})")
                    except: pass
                    continue

                token_mint = row_get(r,"token_address")
                base_label = row_get(r,"base","?")

                # Balance abziehen (Einsatz reservieren)
                if not subtract_balance(uid, stake):
                    with get_db() as con:
                        con.execute("UPDATE executions SET status='ERROR', message='Balance subtract fail' WHERE id=?", (eid,))
                    continue

                res = dex_market_buy(uid, token_mint, stake, base_label=base_label)
                status = res.get("status","ERROR")
                txid = res.get("txid","")
                out_raw = res.get("token_amount_raw","0")
                with get_db() as con:
                    if status == "FILLED":
                        con.execute("""
                            UPDATE executions
                            SET status='OPEN', txid=?, message='JOINED',
                                entry_spent_lamports=?, token_mint=?, token_amount_raw=?
                            WHERE id=?
                        """, (txid, int(res.get("spent_lamports",0)), token_mint, str(out_raw), eid))
                    else:
                        # Einsatz zurück
                        add_balance(uid, stake)
                        con.execute("UPDATE executions SET status='ERROR', message=? WHERE id=?", (res.get("message","error"), eid))

                try:
                    if status == "FILLED":
                        bot.send_message(uid,
                            (f"🤖 Gekauft • {base_label}\n"
                             f"Einsatz: {fmt_sol_usdc(stake)}\n"
                             f"Tx: `{md_escape(txid)}`\n"
                             f"Position: OPEN"),
                            parse_mode="Markdown")
                    else:
                        bot.send_message(uid, f"❌ Kauf fehlgeschlagen: {md_escape(res.get('message','error'))}", parse_mode="Markdown")
                except: pass
        except Exception as e:
            print("execute_buy_for_queue err:", e)
        time.sleep(4)

def try_close_position(eid: int, force: bool=False, target_mult: float=2.0):
    """Schließt eine offene Execution (SELL) — force=True ignoriert Ziel."""
    with get_db() as con:
        e = con.execute("SELECT * FROM executions WHERE id=?", (eid,)).fetchone()
    if not e or row_get(e,"status") != "OPEN":
        return False, "not_open"

    token_mint = row_get(e,"token_mint")
    token_amount_raw = int(row_get(e,"token_amount_raw","0") or 0)
    entry_spent = int(row_get(e,"entry_spent_lamports",0))
    uid = int(row_get(e,"user_id",0))

    # Falls nicht force: prüfen, ob 2x erreicht (nur bei Live sinnvoll)
    if not force and LIVE_TRADE:
        est = dex_estimate_sell_value_lamports(token_mint, token_amount_raw)
        if est is None or est < entry_spent * target_mult:
            return False, "target_not_reached"

    res = dex_market_sell(token_mint, token_amount_raw, entry_spent)
    if res.get("status") != "FILLED":
        return False, res.get("message","sell_error")

    recv = int(res.get("received_lamports",0))
    pnl_lam = recv - entry_spent
    pnl_pct = (pnl_lam / entry_spent * 100.0) if entry_spent > 0 else 0.0

    # Guthaben gutschreiben (Erlös)
    add_balance(uid, recv)

    with get_db() as con:
        con.execute("""
            UPDATE executions
            SET status='CLOSED', closed_at=CURRENT_TIMESTAMP, close_txid=?, close_received_lamports=?,
                pnl_lamports=?, pnl_percent=?
            WHERE id=?
        """, (res.get("txid",""), recv, pnl_lam, pnl_pct, row_get(e,"id")))
    return True, {"recv": recv, "pnl_lam": pnl_lam, "pnl_pct": pnl_pct, "txid": res.get("txid","")}

def tp_monitor_loop():
    """Auto-TP: schließt OPEN Positionen bei +100% (2x)."""
    while True:
        try:
            with get_db() as con:
                rows = con.execute("""
                    SELECT id FROM executions
                    WHERE status='OPEN'
                    LIMIT 100
                """).fetchall()
            for r in rows:
                eid = int(row_get(r,"id",0))
                ok, info = try_close_position(eid, force=not LIVE_TRADE, target_mult=2.0)
                if ok and isinstance(info, dict):
                    with get_db() as con:
                        e = con.execute("SELECT * FROM executions WHERE id=?", (eid,)).fetchone()
                        uid = int(row_get(e,"user_id",0))
                        base = row_get(get_call(row_get(e,"call_id")),"base","?")
                        try:
                            bot.send_message(uid,
                                (f"✅ Auto-TP (+100%) • {base}\n"
                                 f"Erlös: {fmt_sol_usdc(info['recv'])}\n"
                                 f"PnL: {fmt_sol_usdc(info['pnl_lam'])} ({info['pnl_pct']:.2f}%)\n"
                                 f"Tx: `{md_escape(info['txid'])}`"),
                                parse_mode="Markdown")
                        except: pass
        except Exception as e:
            print("tp monitor err:", e)
        time.sleep(30)

# =========================
# UI Handler
# =========================
@bot.callback_query_handler(func=lambda c: True)
def on_cb(c: CallbackQuery):
    uid = c.from_user.id
    data = c.data or ""

    u = get_user(uid)
    if data == "back_home":
        bot.edit_message_text(home_text(u), c.message.chat.id, c.message.message_id, parse_mode="Markdown", reply_markup=kb_main(u))
        return

    if data == "noop":
        bot.answer_callback_query(c.id, "—")
        return

    if data == "help":
        bot.answer_callback_query(c.id)
        bot.send_message(uid,
                         ("ℹ️ Hilfe:\n"
                          "1) Auto-Entry einschalten & Risk wählen.\n"
                          "2) Admin erstellt Call → Auto-Entry buy.\n"
                          "3) Auto-TP bei +100% oder Admin Force-Close.\n"
                          "4) Guthaben = intern (Ein-/Auszahlung separat)."),
                         parse_mode="Markdown")
        return

    if data == "sub_menu":
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔔 Meme-News an", callback_data="sub_on"),
               InlineKeyboardButton("🔕 Meme-News aus", callback_data="sub_off"))
        kb.add(InlineKeyboardButton("⬅️ Zurück", callback_data="back_home"))
        bot.edit_message_text("Meme-News:", c.message.chat.id, c.message.message_id, reply_markup=kb)
        return

    if data == "sub_on":
        bal_sol = get_balance_lamports(uid) / LAMPORTS_PER_SOL
        if bal_sol < MIN_SUB_SOL:
            bot.answer_callback_query(c.id, f"Mindestens {MIN_SUB_SOL} SOL nötig.")
            return
        set_subscription(uid, True)
        set_subscription_types(uid, ["MEME"])
        bot.answer_callback_query(c.id, "Meme-News aktiviert")
        bot.send_message(uid, "🔔 Meme-News abonniert.", reply_markup=kb_main(u))
        return

    if data == "sub_off":
        set_subscription(uid, False)
        set_subscription_types(uid, [])
        bot.answer_callback_query(c.id, "Meme-News aus")
        bot.send_message(uid, "🔕 Meme-News abbestellt.", reply_markup=kb_main(u))
        return

    if data == "auto_menu":
        bot.edit_message_text("Auto-Entry Einstellungen:", c.message.chat.id, c.message.message_id, reply_markup=kb_auto(u))
        return

    if data.startswith("auto_"):
        mode = data.split("_", 1)[1]
        if mode not in ("OFF", "ON"): mode = "OFF"
        set_auto_mode(uid, mode)
        bot.answer_callback_query(c.id, f"Auto-Entry: {mode}")
        bot.edit_message_text(f"Auto-Entry gesetzt: *{mode}*", c.message.chat.id, c.message.message_id, parse_mode="Markdown", reply_markup=kb_auto(get_user(uid)))
        return

    if data.startswith("risk_"):
        risk = data.split("_", 1)[1]
        set_auto_risk(uid, risk)
        bot.answer_callback_query(c.id, f"Risk: {risk}")
        bot.edit_message_text(f"Risiko gesetzt: *{risk}*", c.message.chat.id, c.message.message_id, parse_mode="Markdown", reply_markup=kb_auto(get_user(uid)))
        return

    if data == "deposit":
        bot.answer_callback_query(c.id)
        WAITING_SOURCE_WALLET[uid] = True
        bot.send_message(uid, "🔑 Sende jetzt deine **Absender-Wallet (SOL)**:")
        return

    if data == "withdraw":
        bot.answer_callback_query(c.id)
        WAITING_PAYOUT_WALLET[uid] = True
        bot.send_message(uid, "🔑 Sende jetzt deine **Auszahlungsadresse (SOL)**:\n(danach Betrag in SOL schicken)")
        WAITING_WITHDRAW_AMOUNT[uid] = None
        return

    # === Admin: Call erstellen (FIXED) ===
    if data == "admin_new_call":
        if not is_admin(uid):
            bot.answer_callback_query(c.id, "Nicht erlaubt."); return
        ADMIN_AWAIT_TOKEN[uid] = True
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "🧩 Sende jetzt die **Token-Mint-Adresse** (SPL) für den Call.\nSobald du sie sendest, wird der Call erstellt und Auto-Entry ausgeführt.")
        return

    if data == "admin_list_calls":
        if not is_admin(uid):
            bot.answer_callback_query(c.id, "Nicht erlaubt."); return
        with get_db() as con:
            rows = con.execute("SELECT * FROM calls ORDER BY id DESC LIMIT 10").fetchall()
        if not rows:
            bot.answer_callback_query(c.id, "Keine Calls")
            return
        bot.answer_callback_query(c.id)
        for r in rows:
            txt = fmt_call(r) + f"\nStatus: *{row_get(r,'status','OPEN')}*"
            bot.send_message(uid, txt, parse_mode="Markdown", reply_markup=kb_call_admin_controls(row_get(r,"id")))
        return

    if data.startswith("admin_close_call_"):
        if not is_admin(uid):
            bot.answer_callback_query(c.id, "Nicht erlaubt."); return
        call_id = int(data.split("_")[-1])
        # Alle OPEN Executions dieses Calls schließen (Force)
        closed = 0
        with get_db() as con:
            exs = con.execute("SELECT id FROM executions WHERE call_id=? AND status='OPEN'", (call_id,)).fetchall()
        for ex in exs:
            ok, info = try_close_position(int(row_get(ex,"id")), force=True)
            if ok:
                closed += 1
        set_call_status(call_id, "CLOSED")
        bot.answer_callback_query(c.id, f"Call geschlossen. Positionen verkauft: {closed}")
        return

    bot.answer_callback_query(c.id, "")

# =========================
# Messages
# =========================
@bot.message_handler(func=lambda m: True)
def on_message(m: Message):
    uid = m.from_user.id
    text = (m.text or "").strip() if m.text else ""
    u = get_user(uid)
    if not u:
        upsert_user(uid, m.from_user.username or "", 1 if is_admin(uid) else 0)

    # Admin wartet auf Token für neuen Call
    if ADMIN_AWAIT_TOKEN.get(uid, False) and is_probably_solana_address(text):
        ADMIN_AWAIT_TOKEN[uid] = False
        token_mint = text.strip()
        base_label = token_mint[:4] + "…" + token_mint[-4:]
        cid = create_call(uid, base_label, token_mint, notes=f"by admin {uid}")
        c = get_call(cid)
        bot.reply_to(m, "✅ Call erstellt:\n" + fmt_call(c), parse_mode="Markdown", reply_markup=kb_call_admin_controls(cid))
        # Broadcast + Queue
        sent = broadcast_call_and_queue(c)
        bot.send_message(uid, f"📣 An {sent} Nutzer gesendet. Auto-Entry wird ausgeführt…")
        return

    # Nutzer sendet Token-Adresse → Auto-Entry (sofern ON)
    if is_probably_solana_address(text):
        u = get_user(uid)
        if row_get(u, "auto_mode", "OFF") != "ON":
            bot.reply_to(m, "🔔 Auto-Entry ist *OFF*. Schalte es unter *⚙️ Auto-Entry* ein, um automatisch zu kaufen.", parse_mode="Markdown")
            return
        token_mint = text.strip()
        base_label = token_mint[:4] + "…" + token_mint[-4:]
        cid = create_call(uid, base_label, token_mint, f"Auto from user {uid}")
        c = get_call(cid)
        # Für den Sender selbst zumindest queue'n (auch wenn kein Abo)
        qid = queue_execution(cid, uid, status="QUEUED", message="User token auto")
        bot.reply_to(m, f"🤖 Auto-Entry gestartet\nToken: `{md_escape(token_mint)}`\nEinsatz: {fmt_sol_usdc(_compute_stake_for_user(uid))}", parse_mode="Markdown")
        return

    # Ein-/Auszahlung vereinfachte Prompts (Dummy)
    if WAITING_SOURCE_WALLET.get(uid, False) and is_probably_solana_address(text):
        WAITING_SOURCE_WALLET[uid] = False
        bot.reply_to(m, f"✅ Absender-Wallet gespeichert.\nSende SOL an `{md_escape(CENTRAL_SOL_PUBKEY)}`", parse_mode="Markdown")
        return

    if WAITING_PAYOUT_WALLET.get(uid, False) and is_probably_solana_address(text):
        WAITING_PAYOUT_WALLET[uid] = False
        bot.reply_to(m, f"✅ Auszahlungsadresse gespeichert: `{md_escape(text)}`\nGib nun den Betrag in SOL ein (z. B. 0.25).", parse_mode="Markdown")
        WAITING_WITHDRAW_AMOUNT[uid] = None
        return

    if WAITING_WITHDRAW_AMOUNT.get(uid) is None:
        try:
            sol = float(text.replace(",", "."))
            if sol > 0:
                lam = int(sol * LAMPORTS_PER_SOL)
                if get_balance_lamports(uid) < lam:
                    bot.reply_to(m, f"Unzureichendes Guthaben. Verfügbar: {fmt_sol_usdc(get_balance_lamports(uid))}")
                else:
                    subtract_balance(uid, lam)
                    bot.reply_to(m, f"✅ Auszahlung erfasst (Dummy): {fmt_sol_usdc(lam)}")
                WAITING_WITHDRAW_AMOUNT.pop(uid, None)
                return
        except Exception:
            pass

    # Default
    bot.reply_to(m, "Ich habe das nicht verstanden. Nutze das Menü unten.", reply_markup=kb_main(get_user(uid)))

# =========================
# Executor/Monitor Threads
# =========================
threading.Thread(target=execute_buy_for_queue, daemon=True).start()
threading.Thread(target=tp_monitor_loop, daemon=True).start()

print("Bot läuft — MEME-only. LIVE_TRADE =", LIVE_TRADE)
bot.infinity_polling(timeout=60, long_polling_timeout=60)