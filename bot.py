# bot.py
# UTF-8 — Whales Alpha Memecoin Bot (MEME-only) — Live-Trade + Auto-Payout only

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

# -------- Solana libs (sign/send) ----------
from solana.rpc.api import Client as SolClient
from solana.rpc.types import TxOpts
from solana.keypair import Keypair
from solana.publickey import PublicKey
from solana.transaction import Transaction
from solana.rpc.commitment import Confirmed
import base58

# ---------------------------
# Configuration (ENV)
# ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable required")

ADMIN_IDS = [a.strip() for a in os.getenv("ADMIN_IDS", "").split(",") if a.strip()]
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com").strip()
CENTRAL_SOL_PUBKEY = os.getenv("CENTRAL_SOL_PUBKEY", "").strip()
CENTRAL_SOL_SECRET = os.getenv("CENTRAL_SOL_SECRET", "").strip()  # base58 secret
JUPITER_BASE = os.getenv("JUPITER_BASE", "https://quote-api.jup.ag").rstrip("/")

if not CENTRAL_SOL_PUBKEY or not CENTRAL_SOL_SECRET:
    raise RuntimeError("CENTRAL_SOL_PUBKEY and CENTRAL_SOL_SECRET are required")

# Withdraw fee tiers (lockup_days: fee_percent)
DEFAULT_FEE_TIERS = {0: 20.0, 5: 15.0, 7: 10.0, 10: 5.0}
_fee_tiers: Dict[int, float] = {}
raw_tiers = os.getenv("WITHDRAW_FEE_TIERS", "")
if raw_tiers:
    try:
        for part in raw_tiers.split(","):
            d, p = part.split(":")
            _fee_tiers[int(d)] = float(p)
    except Exception:
        _fee_tiers = DEFAULT_FEE_TIERS.copy()
else:
    _fee_tiers = DEFAULT_FEE_TIERS.copy()

DB_PATH = os.getenv("DB_PATH", "memebot_live.db")
LAMPORTS_PER_SOL = 1_000_000_000
MIN_SUB_SOL = float(os.getenv("MIN_SUB_SOL", "0.1"))

# ---------------------------
# Price helper (Coingecko)
# ---------------------------
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

def parse_fee_tiers() -> List[Tuple[int, float]]:
    return sorted([(int(d), float(p)) for d, p in _fee_tiers.items()], key=lambda x: x[0])

# ---------------------------
# DB schema & helpers
# ---------------------------
SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  username TEXT,
  is_admin INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  sol_balance_lamports INTEGER DEFAULT 0,
  payout_wallet TEXT
);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_by INTEGER NOT NULL,
  base TEXT NOT NULL,            -- Symbol/Name
  token_address TEXT,            -- Zieltoken (Mint)
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  status TEXT NOT NULL,          -- QUEUED/FILLED/ERROR/CLOSED
  txid TEXT,
  stake_lamports INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS payouts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount_lamports INTEGER NOT NULL,
  status TEXT DEFAULT 'SENT',    -- (auto) SENT only
  note TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  lockup_days INTEGER DEFAULT 0,
  fee_percent REAL DEFAULT 0.0,
  tx_sig TEXT
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
        # idempotente Spalten
        for stmt in [
            "ALTER TABLE executions ADD COLUMN stake_lamports INTEGER DEFAULT 0",
            "ALTER TABLE payouts ADD COLUMN tx_sig TEXT"
        ]:
            try: con.execute(stmt)
            except Exception: pass

def row_get(row, key, default=None):
    if row is None:
        return default
    try:
        return row[key] if key in row.keys() else default
    except Exception:
        try:
            return row.get(key, default)
        except Exception:
            return default

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

def set_balance(user_id: int, lamports: int):
    with get_db() as con:
        con.execute("UPDATE users SET sol_balance_lamports = ? WHERE user_id=?", (lamports, user_id))

def get_balance_lamports(user_id: int) -> int:
    with get_db() as con:
        row = con.execute("SELECT sol_balance_lamports FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row_get(row, "sol_balance_lamports", 0)

def set_payout_wallet(user_id: int, wallet: str):
    with get_db() as con:
        con.execute("UPDATE users SET payout_wallet=? WHERE user_id=?", (wallet, user_id))

def list_open_reserved(user_id: int) -> int:
    """Summe reservierter Einsätze in offenen/aktiven Trades (nicht CLOSED)."""
    with get_db() as con:
        rows = con.execute("""
            SELECT COALESCE(SUM(stake_lamports),0) AS resv
            FROM executions
            WHERE user_id=? AND status IN ('QUEUED','FILLED')
        """, (user_id,)).fetchone()
        return int(row_get(rows, "resv", 0))

def available_balance(user_id: int) -> int:
    bal = get_balance_lamports(user_id)
    resv = list_open_reserved(user_id)
    free = max(0, bal - resv)
    return free

def all_subscribers_ids() -> List[int]:
    with get_db() as con:
        return [r["user_id"] for r in con.execute("SELECT user_id FROM users").fetchall()]

# ---------------------------
# Jupiter + Solana client
# ---------------------------
sol = SolClient(SOLANA_RPC, commitment=Confirmed)
CENTRAL_PUB = PublicKey(CENTRAL_SOL_PUBKEY)
CENTRAL_KP = Keypair.from_secret_key(base58.b58decode(CENTRAL_SOL_SECRET))

def jup_quote(input_mint: str, output_mint: str, amount_in_lamports: int) -> dict:
    url = f"{JUPITER_BASE}/v6/quote"
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount_in_lamports,
        "slippageBps": 150,  # 1.5% slippage
        "platformFeeBps": 0
    }
    r = requests.get(url, params=params, timeout=12)
    r.raise_for_status()
    return r.json()

def jup_swap(quote_response: dict, user_pubkey: str) -> str:
    """Ruft /v6/swap auf, signiert die B64-Transaktion, sendet sie und gibt Signature zurück."""
    url = f"{JUPITER_BASE}/v6/swap"
    payload = {
        "quoteResponse": quote_response,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto"
    }
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    swap_tx_b64 = r.json()["swapTransaction"]
    raw = base64.b64decode(swap_tx_b64)
    tx = Transaction.deserialize(raw)
    tx.sign(CENTRAL_KP)
    tx_sig = sol.send_transaction(tx, CENTRAL_KP, opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"))
    return tx_sig.value  # signature

# ---------------------------
# Trade engine (LIVE)
# ---------------------------
WSOL_MINT = "So11111111111111111111111111111111111111112"

def dex_market_buy_live(token_mint: str, amount_lamports: int) -> Tuple[str, str]:
    """
    Kauft token_mint für 'amount_lamports' SOL vom zentralen Wallet.
    Gibt (status, tx_sig) zurück.
    """
    try:
        q = jup_quote(WSOL_MINT, token_mint, amount_lamports)
        routes = q.get("data") or []
        if not routes:
            return ("ERROR_NO_ROUTE", "")
        best = routes[0]
        sig = jup_swap(best, str(CENTRAL_PUB))
        return ("FILLED", sig)
    except Exception as e:
        print("dex_market_buy_live error:", e)
        return ("ERROR", "")

# ---------------------------
# Keyboards / UI
# ---------------------------
def kb_main(u):
    bal = fmt_sol_usdc(get_balance_lamports(row_get(u,"user_id",0)))
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💳 Auszahlung", callback_data="withdraw"))
    if is_admin(row_get(u,"user_id",0)):
        kb.add(InlineKeyboardButton("🛠️ Admin (Call)", callback_data="admin_menu"))
    kb.add(InlineKeyboardButton(f"🏦 Guthaben: {bal}", callback_data="noop"))
    return kb

def kb_withdraw_options():
    kb = InlineKeyboardMarkup()
    tiers = sorted(parse_fee_tiers(), key=lambda x: x[0])
    for days, pct in tiers:
        label = "Sofort • Fee 20%" if days == 0 else f"{days} Tage • Fee {pct}%"
        kb.add(InlineKeyboardButton(label, callback_data=f"payoutopt_{days}"))
    kb.add(InlineKeyboardButton("↩️ Abbrechen", callback_data="back_home"))
    return kb

def kb_admin():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("➕ MEME-Call erstellen", callback_data="admin_new_call"))
    kb.add(InlineKeyboardButton("📣 Broadcast & Auto-Join", callback_data="admin_broadcast_last"))
    kb.add(InlineKeyboardButton("⬅️ Zurück", callback_data="back_home"))
    return kb

# ---------------------------
# Bot init & safe send wrappers
# ---------------------------
init_db()
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

_original_send_message = bot.send_message
def _safe_send_message(chat_id, text, **kwargs):
    try:
        return _original_send_message(chat_id, text, **kwargs)
    except Exception:
        pm = kwargs.get("parse_mode")
        if pm and str(pm).upper().startswith("MARKDOWN"):
            kwargs2 = dict(kwargs); kwargs2["parse_mode"] = "Markdown"
            try:
                return _original_send_message(chat_id, md_escape(str(text)), **kwargs2)
            except Exception:
                kwargs3 = dict(kwargs2); kwargs3.pop("parse_mode", None)
                return _original_send_message(chat_id, str(text), **kwargs3)
        else:
            kwargs3 = dict(kwargs); kwargs3.pop("parse_mode", None)
            return _original_send_message(chat_id, str(text), **kwargs3)
bot.send_message = _safe_send_message

# ---------------------------
# Admin / Calls / Executions
# ---------------------------
def create_call(created_by: int, base: str, token_addr: str) -> int:
    with get_db() as con:
        cur = con.execute("""
            INSERT INTO calls(created_by, base, token_address)
            VALUES (?,?,?)
        """, (created_by, base, token_addr))
        return cur.lastrowid

def get_call(cid: int):
    with get_db() as con:
        return con.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()

def queue_exec(call_id: int, user_id: int, stake_lamports: int) -> int:
    with get_db() as con:
        cur = con.execute("""
            INSERT INTO executions(call_id, user_id, status, stake_lamports)
            VALUES(?,?,'QUEUED',?)
        """, (call_id, user_id, stake_lamports))
        return cur.lastrowid

def fmt_call(c) -> str:
    return f"🧩 *MEME • {md_escape(row_get(c,'base','?'))}*\nToken: `{md_escape(row_get(c,'token_address',''))}`"

def compute_stake_for_user(user_id: int) -> int:
    """Einfach: 35% des freien Guthabens, min 0.01 SOL."""
    free = available_balance(user_id)
    stake = max(int(free * 0.35), int(0.01 * LAMPORTS_PER_SOL))
    return min(stake, free)

def broadcast_and_queue(call_row):
    users = all_subscribers_ids()
    sent = 0
    for uid in users:
        free = available_balance(uid)
        if free <= 0:
            continue
        stake = compute_stake_for_user(uid)
        if stake <= 0:
            continue
        queue_exec(row_get(call_row,"id"), uid, stake)
        try:
            bot.send_message(uid, f"📣 Neuer Meme-Call\n{fmt_call(call_row)}\n\n🤖 Auto-Entry *aktiviert*\nReserviert: {fmt_sol_usdc(stake)}")
            sent += 1
        except Exception:
            pass
    return sent

def run_executor_once():
    with get_db() as con:
        rows = con.execute("""
            SELECT e.id as eid, e.user_id, e.call_id, e.status, e.stake_lamports
            FROM executions e
            WHERE e.status='QUEUED'
            LIMIT 200
        """).fetchall()
    for r in rows:
        eid = row_get(r,"eid")
        uid = row_get(r,"user_id")
        cid = row_get(r,"call_id")
        stake = int(row_get(r,"stake_lamports",0))
        call = get_call(cid)
        token_mint = row_get(call,"token_address","")
        if not token_mint:
            with get_db() as con:
                con.execute("UPDATE executions SET status='ERROR' WHERE id=?", (eid,))
            try: bot.send_message(uid, "❌ Trade fehlgeschlagen (kein Token).")
            except Exception: pass
            continue
        # Live Swap
        status, sig = dex_market_buy_live(token_mint, stake)
        if status != "FILLED":
            with get_db() as con:
                con.execute("UPDATE executions SET status='ERROR', txid=? WHERE id=?", (sig, eid))
            try: bot.send_message(uid, f"❌ Trade ERROR\n{fmt_call(call)}")
            except Exception: pass
            continue
        # Reserve bleibt blockiert, Guthaben bleibt unverändert (Custody ist zentral)
        with get_db() as con:
            con.execute("UPDATE executions SET status='FILLED', txid=? WHERE id=?", (sig, eid))
        try:
            bot.send_message(uid, f"✅ FILLED • {fmt_call(call)}\nEinsatz: {fmt_sol_usdc(stake)}\n`{md_escape(sig)}`")
        except Exception:
            pass

def executor_loop():
    while True:
        try:
            run_executor_once()
        except Exception as e:
            print("executor_loop error:", e)
        time.sleep(4)

# ---------------------------
# Payouts (AUTOMATISCH) – nur Restguthaben, berücksichtigt Reserven
# ---------------------------
WAITING_WITHDRAW_AMOUNT: Dict[int, Optional[int]] = {}
WAITING_PAYOUT_WALLET: Dict[int, bool] = {}

def send_sol(to_pubkey: str, lamports: int) -> str:
    """Sende SOL aus der zentralen Wallet – gibt Tx Signature zurück."""
    to = PublicKey(to_pubkey)
    # einfache SOL-Überweisung (SystemProgram transfer)
    from solana.system_program import TransferParams, transfer
    tx = Transaction()
    tx.add(transfer(TransferParams(from_pubkey=CENTRAL_PUB, to_pubkey=to, lamports=lamports)))
    resp = sol.send_transaction(tx, CENTRAL_KP, opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"))
    return resp.value

def kb_payout_done():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⬅️ Zurück", callback_data="back_home"))
    return kb

def handle_auto_payout(uid: int, amount_lam: int, lock_days: int, fee_percent: float):
    fee_lam = int(round(amount_lam * (fee_percent / 100.0)))
    net_lam = max(0, amount_lam - fee_lam)
    # Verfügbarkeit prüfen: nur freies Guthaben
    free = available_balance(uid)
    if amount_lam > free:
        raise ValueError(f"Unzureichendes freies Guthaben. Frei: {fmt_sol_usdc(free)}")

    # Abziehen (intern)
    set_balance(uid, get_balance_lamports(uid) - amount_lam)

    # Wallet holen
    u = get_user(uid)
    payout_wallet = row_get(u, "payout_wallet", "")
    if not payout_wallet:
        raise ValueError("Keine Auszahlungsadresse gesetzt.")

    # (Optional) Lockup-Handling: hier nur Fee-Modell, Auszahlung erfolgt sofort on-chain
    sig = send_sol(payout_wallet, net_lam)

    # DB loggen
    with get_db() as con:
        con.execute(
            "INSERT INTO payouts(user_id, amount_lamports, status, note, lockup_days, fee_percent, tx_sig) VALUES (?,?,?,?,?,?,?)",
            (uid, amount_lam, "SENT", f"Auto payout ({lock_days}d, fee {fee_percent}%)", lock_days, fee_percent, sig)
        )

    return fee_lam, net_lam, sig

# ---------------------------
# UI / Home
# ---------------------------
def get_bot_username():
    try:
        me = bot.get_me()
        return me.username or "<YourBotUsername>"
    except Exception:
        return "<YourBotUsername>"

BRAND = "🐋 Whales Alpha Memecoin Bot"

def home_text(u) -> str:
    bal = fmt_sol_usdc(get_balance_lamports(row_get(u,"user_id",0)))
    return (
        f"{BRAND}\n\n"
        f"👋 Hallo @{md_escape(row_get(u,'username','')) or ('ID '+str(row_get(u,'user_id','?')))}\n"
        f"🏦 Guthaben: *{bal}*\n\n"
        "• Admin Calls → echter DEX-Entry (Jupiter)\n"
        "• Auszahlungen: automatisch (mit Lockup/Fee), nur freies Restguthaben\n"
    )

# ---------------------------
# Handlers
# ---------------------------
@bot.message_handler(commands=["start"])
def cmd_start(m: Message):
    uid = m.from_user.id
    uname = m.from_user.username or ""
    upsert_user(uid, uname, 1 if is_admin(uid) else 0)
    u = get_user(uid)
    bot.reply_to(m, home_text(u), reply_markup=kb_main(u))

@bot.callback_query_handler(func=lambda c: True)
def on_cb(c: CallbackQuery):
    uid = c.from_user.id
    u = get_user(uid)
    data = c.data or ""

    if data == "noop":
        bot.answer_callback_query(c.id, "—"); return

    if data == "back_home":
        bot.edit_message_text(home_text(u), c.message.chat.id, c.message.message_id, reply_markup=kb_main(u))
        return

    # Withdraw
    if data == "withdraw":
        if not row_get(u, "payout_wallet"):
            WAITING_PAYOUT_WALLET[uid] = True
            bot.answer_callback_query(c.id, "Bitte zuerst Auszahlungsadresse senden.")
            bot.send_message(uid, "🔑 Sende jetzt deine **Auszahlungsadresse (SOL)**:")
            return
        WAITING_WITHDRAW_AMOUNT[uid] = None
        bot.answer_callback_query(c.id)
        bot.send_message(uid,
                         f"💳 Auszahlungsadresse: `{md_escape(row_get(u,'payout_wallet','-'))}`\n"
                         "🔄 Zum Ändern: einfach neue Adresse schicken.\n\n"
                         "Gib nun den Betrag in SOL ein (z. B. `0.25`).",
                         parse_mode="Markdown")
        return

    if data.startswith("payoutopt_"):
        try:
            days = int(data.split("_", 1)[1])
        except Exception:
            bot.answer_callback_query(c.id, "Ungültig."); return
        fee_percent = float(_fee_tiers.get(days, 0.0))
        pending = WAITING_WITHDRAW_AMOUNT.get(uid, None)
        if pending is None or pending <= 0:
            bot.answer_callback_query(c.id, "Bitte zuerst Betrag eingeben."); return

        try:
            fee_lam, net_lam, sig = handle_auto_payout(uid, int(pending), days, fee_percent)
            WAITING_WITHDRAW_AMOUNT.pop(uid, None)
            bot.answer_callback_query(c.id, "Auszahlung gesendet.")
            bot.send_message(uid,
                             ("💸 *Auszahlung gesendet*\n"
                              f"Brutto: {fmt_sol_usdc(int(pending))}\n"
                              f"Lockup: {days} Tage\n"
                              f"Gebühr: {fee_percent:.2f}% ({fmt_sol_usdc(fee_lam)})\n"
                              f"Netto: {fmt_sol_usdc(net_lam)}\n"
                              f"Tx: `{md_escape(sig)}`"),
                             parse_mode="Markdown",
                             reply_markup=kb_payout_done())
        except Exception as e:
            bot.answer_callback_query(c.id, "Fehler")
            bot.send_message(uid, f"❌ Auszahlung fehlgeschlagen: {e}")
        return

    # Admin
    if data == "admin_menu":
        if not is_admin(uid):
            bot.answer_callback_query(c.id, "Nicht erlaubt."); return
        bot.edit_message_text("🛠️ Admin", c.message.chat.id, c.message.message_id, reply_markup=kb_admin()); return

    if data == "admin_new_call":
        if not is_admin(uid): return
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Sende den MEME-Call im Format:\n`MEME|SYMBOL|TOKEN_MINT`", parse_mode="Markdown")
        ADMIN_AWAIT_SIMPLE_CALL[uid] = True
        return

    if data == "admin_broadcast_last":
        if not is_admin(uid): return
        with get_db() as con:
            row = con.execute("SELECT * FROM calls ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            bot.answer_callback_query(c.id, "Kein Call vorhanden."); return
        sent = broadcast_and_queue(row)
        bot.answer_callback_query(c.id, f"An {sent} Nutzer reserviert.")
        return

    bot.answer_callback_query(c.id, "")

# transient admin input states
ADMIN_AWAIT_SIMPLE_CALL: Dict[int, bool] = {}
WAITING_SOURCE_WALLET: Dict[int, bool] = {}  # nicht genutzt, nur kompatibilität
# ---------------------------

@bot.message_handler(func=lambda m: True)
def on_msg(m: Message):
    uid = m.from_user.id
    text = (m.text or "").strip() if m.text else ""

    # Admin: create call
    if ADMIN_AWAIT_SIMPLE_CALL.get(uid, False):
        ADMIN_AWAIT_SIMPLE_CALL[uid] = False
        if not is_admin(uid):
            bot.reply_to(m, "Nicht erlaubt."); return
        parts = [p.strip() for p in (text or "").split("|")]
        if len(parts) < 3 or parts[0].upper() != "MEME":
            bot.reply_to(m, "Format: `MEME|SYMBOL|TOKEN_MINT`", parse_mode="Markdown"); return
        _, base, token_addr = parts[:3]
        cid = create_call(uid, base.upper(), token_addr)
        c = get_call(cid)
        bot.reply_to(m, "✅ Meme-Call gespeichert:\n" + fmt_call(c), parse_mode="Markdown")
        # Sofort broadcast & queue
        sent = broadcast_and_queue(c)
        bot.send_message(uid, f"📣 An {sent} Nutzer geschickt & reserviert. Executor startet…")
        return

    # User: payout wallet setzen / ändern
    if WAITING_PAYOUT_WALLET.get(uid, False):
        if is_probably_solana_address(text:=text):
            WAITING_PAYOUT_WALLET[uid] = False
            set_payout_wallet(uid, text)
            bot.reply_to(m, f"✅ Auszahlungsadresse gespeichert: `{md_escape(text)}`\nGib nun den Betrag in SOL ein (z. B. 0.25).", parse_mode="Markdown")
            WAITING_WITHDRAW_AMOUNT[uid] = None
            return
        else:
            bot.reply_to(m, "Bitte eine gültige Solana-Adresse senden.")
            return

    # Withdraw amount entry (immer wenn None)
    if WAITING_WITHDRAW_AMOUNT.get(uid) is None:
        if is_probably_solana_address(text):
            set_payout_wallet(uid, text)
            bot.reply_to(m, f"✅ Auszahlungsadresse aktualisiert: `{md_escape(text)}`\nGib nun den Betrag in SOL ein (z. B. 0.25).", parse_mode="Markdown")
            return
        try:
            sol_amt = float(text.replace(",", "."))
            if sol_amt <= 0:
                bot.reply_to(m, "Betrag muss > 0 sein."); return
            lam = int(sol_amt * LAMPORTS_PER_SOL)
            free = available_balance(uid)
            if lam > free:
                bot.reply_to(m, f"Unzureichendes freies Guthaben (Reserviert: {fmt_sol_usdc(list_open_reserved(uid))}). Frei: {fmt_sol_usdc(free)}")
                WAITING_WITHDRAW_AMOUNT.pop(uid, None); return
            WAITING_WITHDRAW_AMOUNT[uid] = lam
            bot.reply_to(m, f"Auszahlung: {fmt_sol_usdc(lam)} — Wähle Lockup & Fee:", reply_markup=kb_withdraw_options())
        except Exception:
            bot.reply_to(m, "Bitte eine gültige Zahl senden, z. B. `0.25`.")
        return

    # Default: kurz helfen
    u = get_user(uid)
    bot.reply_to(m, home_text(u), reply_markup=kb_main(u))

# ---------------------------
# Helper: validators
# ---------------------------
def is_probably_solana_address(addr: str) -> bool:
    if not isinstance(addr, str): return False
    addr = addr.strip()
    if len(addr) < 32 or len(addr) > 44: return False
    allowed = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return all(ch in allowed for ch in addr)

# ---------------------------
# Background executor
# ---------------------------
threading.Thread(target=executor_loop, daemon=True).start()

print("Bot läuft — Live-Trade + Auto-Payout only")
bot.infinity_polling(timeout=60, long_polling_timeout=60)