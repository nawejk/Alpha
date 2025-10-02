# bot_single.py
# Telegram Signals & Auto-Entry Bot (Single File, enhanced)
# Features:
# - Admin-Menü: Calls erstellen & an Abonnenten mit Guthaben senden
# - User-Menü: Solana-Einzahlung (Watch & Balance), Abo, Auto-Entry + Risiko (Low/Medium/High)
# - Auto-Executor (SIMULATED) für Meme-Spot & Futures (nach außen "realistisch")
# - SQLite DB, Inline-Buttons, Auszahlung-Workflow, Hilfe
#
# Setup:
#   pip install pyTelegramBotAPI solana solders base58 requests python-dotenv pytz
#   Setze Umgebungsvariablen:
#     BOT_TOKEN=...
#     ADMIN_IDS=123456,987654
#     SOLANA_RPC=https://api.mainnet-beta.solana.com
#     CENTRAL_SOL_PUBKEY=optional (für Auszahlungen/Reserve)
#
# Start: python bot_single.py

import os
import time
import threading
import sqlite3
from contextlib import contextmanager
from typing import Optional, Dict

import base58
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

# Solana
from solana.publickey import PublicKey
from solana.keypair import Keypair
from solana.rpc.api import Client as SolClient
from solana.rpc.types import RPCResponse

# ------------------------ CONFIG ------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "8212740282:AAGfMucPHJ0BtZPPVhgZkWtzYCHnu7SZMoo").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env missing")

ADMIN_IDS = [a.strip() for a in os.getenv("ADMIN_IDS", "8076025426").split(",") if a.strip()]
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com").strip()
CENTRAL_SOL_PUBKEY = os.getenv("CENTRAL_SOL_PUBKEY", "3wyVwpcbWt96mphJjskFsR2qoyafqJuSfGZYmiipW4oy").strip()

DB_PATH = "memebot.db"

LAMPORTS_PER_SOL = 1_000_000_000

# ------------------------ DB LAYER ------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  username TEXT,
  is_admin INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  sub_active INTEGER DEFAULT 0,
  auto_mode TEXT DEFAULT 'OFF',           -- OFF | SIMULATED | LIVE
  auto_risk TEXT DEFAULT 'MEDIUM',        -- LOW | MEDIUM | HIGH
  sol_balance_lamports INTEGER DEFAULT 0  -- akkumuliertes Guthaben
);

CREATE TABLE IF NOT EXISTS deposits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  deposit_address TEXT NOT NULL UNIQUE,
  privkey_b58 TEXT NOT NULL,            -- WARNUNG: nur Demo/Tests
  last_balance_lamports INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_by INTEGER NOT NULL,
  market_type TEXT NOT NULL,            -- MEME_SPOT | FUTURES
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,                   -- BUY/LONG | SELL/SHORT
  entry TEXT,
  stop TEXT,
  targets TEXT,
  leverage TEXT,
  notes TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  mode TEXT NOT NULL,                   -- SIMULATED | LIVE
  status TEXT NOT NULL,                 -- QUEUED | FILLED | ERROR
  txid TEXT,
  message TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(call_id) REFERENCES calls(id),
  FOREIGN KEY(user_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS payouts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount_lamports INTEGER NOT NULL,
  status TEXT DEFAULT 'REQUESTED',      -- REQUESTED | APPROVED | SENT | REJECTED
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(user_id) REFERENCES users(user_id)
);
"""

@contextmanager
def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()

def init_db():
    with get_db() as con:
        con.executescript(SCHEMA)
        # defensive migrations (falls alte DB ohne Spalten)
        try: con.execute("ALTER TABLE users ADD COLUMN auto_risk TEXT DEFAULT 'MEDIUM'")
        except Exception: pass
        try: con.execute("ALTER TABLE users ADD COLUMN sol_balance_lamports INTEGER DEFAULT 0")
        except Exception: pass

# ------------------------ HELPERS / MODELS ------------------------

def is_admin(user_id:int)->bool:
    return str(user_id) in ADMIN_IDS

def upsert_user(user_id:int, username:str, is_admin_flag:int):
    with get_db() as con:
        con.execute("""
            INSERT INTO users(user_id, username, is_admin)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
        """, (user_id, username or "", is_admin_flag))

def get_user(user_id:int):
    with get_db() as con:
        cur = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return cur.fetchone()

def set_subscription(user_id:int, active:bool):
    with get_db() as con:
        con.execute("UPDATE users SET sub_active=? WHERE user_id=?", (1 if active else 0, user_id))

def set_auto_mode(user_id:int, mode:str):
    with get_db() as con:
        con.execute("UPDATE users SET auto_mode=? WHERE user_id=?", (mode, user_id))

def set_auto_risk(user_id:int, risk:str):
    with get_db() as con:
        con.execute("UPDATE users SET auto_risk=? WHERE user_id=?", (risk, user_id))

def add_balance(user_id:int, lamports:int):
    with get_db() as con:
        con.execute("UPDATE users SET sol_balance_lamports = sol_balance_lamports + ? WHERE user_id=?", (lamports, user_id))

def subtract_balance(user_id:int, lamports:int)->bool:
    with get_db() as con:
        bal = con.execute("SELECT sol_balance_lamports FROM users WHERE user_id=?", (user_id,)).fetchone()["sol_balance_lamports"]
        if bal < lamports: return False
        con.execute("UPDATE users SET sol_balance_lamports = sol_balance_lamports - ? WHERE user_id=?", (lamports, user_id))
        return True

def get_balance_lamports(user_id:int)->int:
    with get_db() as con:
        return con.execute("SELECT sol_balance_lamports FROM users WHERE user_id=?", (user_id,)).fetchone()["sol_balance_lamports"]

def all_subscribers_with_balance():
    with get_db() as con:
        cur = con.execute("SELECT user_id FROM users WHERE sol_balance_lamports > 0")
        return [r["user_id"] for r in cur.fetchall()]

def create_call(created_by:int, market_type:str, symbol:str, side:str, entry:str, stop:str, targets:str, leverage:str, notes:str)->int:
    with get_db() as con:
        cur = con.execute("""
            INSERT INTO calls(created_by, market_type, symbol, side, entry, stop, targets, leverage, notes)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (created_by, market_type, symbol, side, entry, stop, targets, leverage, notes))
        return cur.lastrowid

def get_call(cid:int):
    with get_db() as con:
        cur = con.execute("SELECT * FROM calls WHERE id=?", (cid,))
        return cur.fetchone()

def queue_execution(call_id:int, user_id:int, mode:str, status:str="QUEUED", message:str="")->int:
    with get_db() as con:
        cur = con.execute("""
            INSERT INTO executions(call_id, user_id, mode, status, message)
            VALUES(?,?,?,?,?)
        """, (call_id, user_id, mode, status, message))
        return cur.lastrowid

def fmt_sol(lamports:int)->str:
    return f"{lamports / LAMPORTS_PER_SOL:.6f} SOL"

def fmt_call(c)->str:
    lines = [f"🧩 *{c['market_type']}* | *{c['symbol']}* | *{c['side']}*"]
    if c["leverage"]: lines.append(f"Leverage: `{c['leverage']}`")
    if c["entry"]:    lines.append(f"Entry: `{c['entry']}`")
    if c["targets"]:  lines.append(f"Targets: `{c['targets']}`")
    if c["stop"]:     lines.append(f"Stop: `{c['stop']}`")
    if c["notes"]:    lines.append(f"Notes: {c['notes']}")
    return "\n".join(lines)

# ------------------------ KEYBOARDS ------------------------

def kb_main(is_admin_flag=False, balance_lamports: int = 0):
    bal = fmt_sol(balance_lamports)
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💸 Einzahlen (Solana)", callback_data="deposit"),
           InlineKeyboardButton("💳 Auszahlung", callback_data="withdraw"))
    kb.add(InlineKeyboardButton("⚙️ Auto-Entry", callback_data="auto_menu"))
    kb.add(InlineKeyboardButton("ℹ️ Hilfe", callback_data="help"))
    if is_admin_flag:
        kb.add(InlineKeyboardButton("🛠 Admin-Menü", callback_data="admin_menu"))
    # Balance-Zeile (nur Info)
    kb.add(InlineKeyboardButton(f"Guthaben: {bal}", callback_data="noop"))
    return kb

def kb_auto(current_mode:str, risk:str):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("OFF", callback_data="auto_OFF"),
           InlineKeyboardButton("SIMULATED", callback_data="auto_SIMULATED"),
           InlineKeyboardButton("LIVE", callback_data="auto_LIVE"))
    kb.add(InlineKeyboardButton("Risk: LOW", callback_data="risk_LOW"),
           InlineKeyboardButton("MEDIUM", callback_data="risk_MEDIUM"),
           InlineKeyboardButton("HIGH", callback_data="risk_HIGH"))
    kb.add(InlineKeyboardButton(f"Aktueller Modus: {current_mode}", callback_data="noop"))
    kb.add(InlineKeyboardButton(f"Aktuelles Risiko: {risk}", callback_data="noop"))
    kb.add(InlineKeyboardButton("⬅️ Zurück", callback_data="back_home"))
    return kb

def kb_admin():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("➕ Call erstellen", callback_data="admin_new_call"))
    kb.add(InlineKeyboardButton("📤 Call senden (an User mit Guthaben)", callback_data="admin_broadcast_last"))
    kb.add(InlineKeyboardButton("⬅️ Zurück", callback_data="back_home"))
    return kb

# ------------------------ SOLANA WATCHER ------------------------

class SolWatcher:
    def __init__(self, rpc_url:str):
        self.client = SolClient(rpc_url)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self.on_deposit = None  # will be set by main

    def ensure_user_address(self, user_id:int)->str:
        with get_db() as con:
            row = con.execute("SELECT deposit_address, privkey_b58 FROM deposits WHERE user_id=?", (user_id,)).fetchone()
            if row:
                return row["deposit_address"]
            kp = Keypair()
            pub = str(kp.public_key)
            priv_b58 = base58.b58encode(kp.secret_key).decode()
            con.execute("INSERT INTO deposits(user_id, deposit_address, privkey_b58) VALUES(?,?,?)",
                        (user_id, pub, priv_b58))
            return pub

    def get_balance_lamports(self, addr:str)->int:
        try:
            res:RPCResponse = self.client.get_balance(PublicKey(addr))
            return int(res["result"]["value"])
        except Exception:
            return 0

    def start(self, interval_sec:int=45):
        if self._running: return
        self._running = True
        self._thread = threading.Thread(target=self._loop, args=(interval_sec,), daemon=True)
        self._thread.start()

    def _loop(self, interval:int):
        while self._running:
            try:
                self.scan_all()
            except Exception as e:
                print("Watcher error:", e)
            time.sleep(interval)

    def scan_all(self):
        with get_db() as con:
            rows = con.execute("SELECT id, user_id, deposit_address, last_balance_lamports FROM deposits").fetchall()
        for r in rows:
            lam = self.get_balance_lamports(r["deposit_address"])
            if lam > r["last_balance_lamports"]:
                diff = lam - r["last_balance_lamports"]
                with get_db() as con:
                    con.execute("UPDATE deposits SET last_balance_lamports=? WHERE id=?", (lam, r["id"]))
                if self.on_deposit:
                    self.on_deposit({"user_id": r["user_id"], "address": r["deposit_address"], "amount_lamports": diff})

# ------------------------ CONNECTOR STUBS (SIMULATED) ------------------------

def dex_market_buy_simulated(user_id:int, mint_or_symbol:str, amount_lamports:int):
    # amount_lamports ist das "Einsatz"-Äquivalent
    return {"status":"FILLED", "txid":"SIM-TX-"+mint_or_symbol, "spent_lamports": amount_lamports}

def cex_futures_place_simulated(user_id:int, symbol:str, side:str, leverage:str, risk:str):
    return {"status":"FILLED", "order_id":"SIM-ORDER", "symbol":symbol, "side":side, "lev":leverage, "risk":risk}

# ------------------------ BOT ------------------------

init_db()
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# simple conversation states
WAITING_WITHDRAW_AMOUNT: Dict[int, bool] = {}

watcher = SolWatcher(SOLANA_RPC)

def _on_deposit(evt:dict):
    uid = evt["user_id"]
    lam = evt["amount_lamports"]
    add_balance(uid, lam)
    new_bal = get_balance_lamports(uid)
    try:
        bot.send_message(
            uid,
            f"✅ *Einzahlung eingegangen:* {fmt_sol(lam)}\n"
            f"Neues Guthaben: *{fmt_sol(new_bal)}*",
            parse_mode="Markdown")
    except Exception as e:
        print("notify deposit error:", e)

watcher.on_deposit = _on_deposit
watcher.start(interval_sec=30)

def home_text(uid:int)->str:
    u = get_user(uid)
    bal = fmt_sol(u["sol_balance_lamports"])
    return (
        "Willkommen! 🎯\n"
        "Professioneller Signals & Auto-Entry Bot für Meme-Coins (Spot) und Futures.\n\n"
        f"Dein aktuelles Guthaben: *{bal}*"
    )

@bot.message_handler(commands=["start","balance"])
def cmd_start(m:Message):
    uid = m.from_user.id
    uname = m.from_user.username or ""
    admin_flag = 1 if is_admin(uid) else 0
    upsert_user(uid, uname, admin_flag)
    u = get_user(uid)
    bot.reply_to(m, home_text(uid), reply_markup=kb_main(is_admin_flag=bool(u["is_admin"]), balance_lamports=u["sol_balance_lamports"]))

@bot.callback_query_handler(func=lambda c: True)
def on_cb(c:CallbackQuery):
    uid = c.from_user.id
    u = get_user(uid)
    data = c.data

    if data == "back_home":
        u = get_user(uid)
        bot.edit_message_text(home_text(uid), c.message.chat.id, c.message.message_id,
                              reply_markup=kb_main(is_admin_flag=bool(u["is_admin"]), balance_lamports=u["sol_balance_lamports"]))
        return

    if data == "help":
        text = (
            "ℹ️ *Hilfe*\n\n"
            "1) Klicke *Einzahlen*, kopiere deine persönliche SOL-Adresse und sende SOL dorthin.\n"
            "2) Nach Bestätigung wird dein *Guthaben gutgeschrieben*.\n"
            "3) Nur User mit Guthaben erhalten *Signale*.\n"
            "4) *Auto-Entry*: OFF/SIMULATED/LIVE + Risiko (LOW/MEDIUM/HIGH).\n"
            "5) *Auszahlung*: Betrag eingeben → Admin prüft und sendet manuell.\n"
            f"{'Zentrale Wallet: `'+CENTRAL_SOL_PUBKEY+'`' if CENTRAL_SOL_PUBKEY else ''}"
        )
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, parse_mode="Markdown",
                              reply_markup=kb_main(is_admin_flag=bool(u["is_admin"]), balance_lamports=u["sol_balance_lamports"]))
        return

    if data == "deposit":
        addr = watcher.ensure_user_address(uid)
        bot.answer_callback_query(c.id, "Adresse erstellt/abgerufen.")
        text = (
            "💸 *Einzahlung (Solana)*\n\n"
            "Sende SOL an deine *persönliche* Adresse:\n"
            f"`{addr}`\n\n"
            "_Nach Bestätigung wird dein Guthaben automatisch gutgeschrieben._"
        )
        if CENTRAL_SOL_PUBKEY:
            text += f"\n\n(Zentrale Wallet: `{CENTRAL_SOL_PUBKEY}` – für Auszahlungen/Reserve)"
        bot.send_message(c.message.chat.id, text, parse_mode="Markdown")
        # zeige wieder Hauptmenü mit Balance
        u = get_user(uid)
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id,
            reply_markup=kb_main(is_admin_flag=bool(u["is_admin"]), balance_lamports=u["sol_balance_lamports"]))
        return

    if data == "withdraw":
        WAITING_WITHDRAW_AMOUNT[uid] = True
        bot.answer_callback_query(c.id, "Bitte Betrag eingeben.")
        bot.send_message(c.message.chat.id, "💳 *Auszahlung*\nGib den Betrag in SOL ein (z. B. `0.25`).", parse_mode="Markdown")
        return

    if data == "sub_on":
        set_subscription(uid, True)
        bot.answer_callback_query(c.id, "Abo aktiviert")
        bot.edit_message_text("🔔 Abo ist *aktiv*.", c.message.chat.id, c.message.message_id,
                              parse_mode="Markdown", reply_markup=kb_main(is_admin_flag=bool(u["is_admin"]), balance_lamports=u["sol_balance_lamports"]))
        return

    if data == "sub_off":
        set_subscription(uid, False)
        bot.answer_callback_query(c.id, "Abo beendet")
        bot.edit_message_text("🔕 Abo *beendet*.", c.message.chat.id, c.message.message_id,
                              parse_mode="Markdown", reply_markup=kb_main(is_admin_flag=bool(u["is_admin"]), balance_lamports=u["sol_balance_lamports"]))
        return

    if data == "auto_menu":
        bot.edit_message_text("Auto-Entry Einstellungen:", c.message.chat.id, c.message.message_id,
                              reply_markup=kb_auto(u["auto_mode"], u["auto_risk"]))
        return

    if data.startswith("auto_"):
        mode = data.split("_",1)[1]
        set_auto_mode(uid, mode)
        bot.answer_callback_query(c.id, f"Auto-Entry: {mode}")
        nu = get_user(uid)
        bot.edit_message_text(f"Auto-Entry: *{mode}*", c.message.chat.id, c.message.message_id,
                              parse_mode="Markdown", reply_markup=kb_auto(nu["auto_mode"], nu["auto_risk"]))
        return

    if data.startswith("risk_"):
        risk = data.split("_",1)[1]
        set_auto_risk(uid, risk)
        bot.answer_callback_query(c.id, f"Risk: {risk}")
        nu = get_user(uid)
        bot.edit_message_text("Auto-Entry Einstellungen aktualisiert.", c.message.chat.id, c.message.message_id,
                              reply_markup=kb_auto(nu["auto_mode"], nu["auto_risk"]))
        return

    # Admin
    if data == "admin_menu":
        if not is_admin(uid):
            bot.answer_callback_query(c.id, "Nicht erlaubt.")
            return
        bot.edit_message_text("🛠 Admin-Menü", c.message.chat.id, c.message.message_id,
                              reply_markup=kb_admin())
        return

    if data == "admin_new_call":
        if not is_admin(uid): return
        bot.edit_message_text(
            "Sende den Call im Format:\n\n"
            "`TYPE|SYMBOL|SIDE|ENTRY|STOP|TARGETS|LEV|NOTES`\n"
            "Beispiele:\n"
            "`MEME_SPOT|SOL/USDC|BUY|MKT|--|TP1 5%, TP2 10%|--|Scalp`\n"
            "`FUTURES|BTCUSDT|LONG|67000|65000|68000,69000|20x|News-Pump`",
            c.message.chat.id, c.message.message_id, parse_mode="Markdown")
        bot.register_next_step_handler(c.message, handle_admin_call_input)
        return

    if data == "admin_broadcast_last":
        if not is_admin(uid): return
        with get_db() as con:
            row = con.execute("SELECT * FROM calls ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            bot.answer_callback_query(c.id, "Kein Call vorhanden.")
            return
        msg = "📣 *Neuer Call:*\n" + fmt_call(row)
        subs = all_subscribers_with_balance()
        sent = 0
        for su in subs:
            try:
                bot.send_message(su, msg, parse_mode="Markdown")
                queue_execution(row["id"], su, mode="SIMULATED", status="QUEUED", message="Queued by broadcast")
                sent += 1
            except Exception as e:
                print("broadcast error", su, e)
        bot.answer_callback_query(c.id, f"An {sent} User mit Guthaben gesendet.")
        return

def handle_admin_call_input(m:Message):
    uid = m.from_user.id
    if not is_admin(uid):
        bot.reply_to(m, "Nicht erlaubt.")
        return
    raw = (m.text or "").strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 8:
        bot.reply_to(m, "Formatfehler. Erwartet: TYPE|SYMBOL|SIDE|ENTRY|STOP|TARGETS|LEV|NOTES")
        return
    market_type, symbol, side, entry, stop, targets, lev, notes = parts[:8]
    cid = create_call(uid, market_type, symbol, side, entry, stop, targets, lev, notes)
    c = get_call(cid)
    bot.send_message(uid, "✅ Call gespeichert:\n" + fmt_call(c), parse_mode="Markdown", reply_markup=kb_admin())

# ------------------------ TEXT HANDLER (AUSZAHLUNG) ------------------------

@bot.message_handler(func=lambda m: WAITING_WITHDRAW_AMOUNT.get(m.from_user.id, False))
def handle_withdraw_amount(m:Message):
    uid = m.from_user.id
    WAITING_WITHDRAW_AMOUNT[uid] = False
    try:
        txt = (m.text or "").replace(",", ".").strip()
        sol = float(txt)
        if sol <= 0:
            bot.reply_to(m, "Betrag muss > 0 sein.")
            return
        lam = int(sol * LAMPORTS_PER_SOL)
        if not subtract_balance(uid, lam):
            bot.reply_to(m, f"Unzureichendes Guthaben. Verfügbar: {fmt_sol(get_balance_lamports(uid))}")
            return
        with get_db() as con:
            con.execute("INSERT INTO payouts(user_id, amount_lamports) VALUES (?,?)", (uid, lam))
        bot.reply_to(m, f"✅ Auszahlungsanfrage erstellt: *{sol:.6f} SOL*.\n"
                        "Ein Admin prüft und sendet zeitnah.", parse_mode="Markdown")
        # Admin informieren
        for aid in ADMIN_IDS:
            try:
                bot.send_message(int(aid), f"💳 Auszahlung angefragt von {uid}: {sol:.6f} SOL", parse_mode="Markdown")
            except Exception as e:
                print("notify admin payout error:", e)
    except Exception:
        bot.reply_to(m, "Bitte eine gültige Zahl eingeben, z. B. `0.25`.", parse_mode="Markdown")

# ------------------------ AUTO EXECUTOR LOOP (SIMULATED) ------------------------

def risk_to_fraction(risk:str)->float:
    # Anteil des Guthabens pro Trade
    return {"LOW":0.05, "MEDIUM":0.10, "HIGH":0.20}.get(risk.upper(), 0.10)

def auto_executor_loop():
    while True:
        try:
            with get_db() as con:
                rows = con.execute("""
                    SELECT e.id as eid, e.user_id, e.call_id, e.mode, e.status, u.auto_mode, u.auto_risk, u.sol_balance_lamports
                    FROM executions e
                    JOIN users u ON u.user_id = e.user_id
                    WHERE e.status='QUEUED'
                    LIMIT 50
                """).fetchall()
            for r in rows:
                if r["auto_mode"] == "OFF":
                    with get_db() as con:
                        con.execute("UPDATE executions SET status='ERROR', message='Auto OFF' WHERE id=?", (r["eid"],))
                    continue
                call = get_call(r["call_id"])
                # Einsatz je nach Risiko
                frac = risk_to_fraction(r["auto_risk"] or "MEDIUM")
                stake_lamports = max(int(r["sol_balance_lamports"] * frac), int(0.01 * LAMPORTS_PER_SOL))  # min 0.01 SOL
                if stake_lamports <= 0 or r["sol_balance_lamports"] < stake_lamports:
                    with get_db() as con:
                        con.execute("UPDATE executions SET status='ERROR', message='Zu wenig Guthaben' WHERE id=?", (r["eid"],))
                    continue

                # "realistischer" Abzug des Einsatzes
                ok = subtract_balance(r["user_id"], stake_lamports)
                if not ok:
                    with get_db() as con:
                        con.execute("UPDATE executions SET status='ERROR', message='Balance-Abzug fehlgeschlagen' WHERE id=?", (r["eid"],))
                    continue

                result = {"status":"ERROR"}
                if call["market_type"].upper() == "MEME_SPOT":
                    result = dex_market_buy_simulated(r["user_id"], call["symbol"], stake_lamports)
                elif call["market_type"].upper() == "FUTURES":
                    result = cex_futures_place_simulated(r["user_id"], call["symbol"], call["side"], call["leverage"], r["auto_risk"])

                status = result.get("status","ERROR")
                txid = result.get("txid") or result.get("order_id") or ""

                with get_db() as con:
                    con.execute("UPDATE executions SET status=?, txid=?, message=? WHERE id=?",
                                (status, txid, str(result), r["eid"]))

                # einfache "P&L" Simulation: Medium ~0%, Low leicht positiv, High volatiler
                pnl_frac = 0.0
                risk = (r["auto_risk"] or "MEDIUM").upper()
                if risk == "LOW":
                    pnl_frac = 0.01
                elif risk == "MEDIUM":
                    pnl_frac = 0.0
                else:  # HIGH
                    pnl_frac = 0.02  # kann man später zufällig gestalten

                pnl = int(stake_lamports * pnl_frac)
                if pnl != 0:
                    add_balance(r["user_id"], pnl)

                try:
                    bal_after = get_balance_lamports(r["user_id"])
                    bot.send_message(
                        r["user_id"],
                        f"🤖 Auto-Entry ({call['market_type']}) • {risk}\n"
                        f"Status: *{status}*\n{fmt_call(call)}\n"
                        f"Einsatz: {fmt_sol(stake_lamports)}\n"
                        f"P&L: {fmt_sol(pnl)}\n"
                        f"Guthaben: *{fmt_sol(bal_after)}*\n"
                        f"`{txid}`",
                        parse_mode="Markdown")
                except Exception as e:
                    print("notify exec error", e)
        except Exception as e:
            print("executor loop error:", e)
        time.sleep(5)

threading.Thread(target=auto_executor_loop, daemon=True).start()

# ------------------------ RUN ------------------------

print("Bot läuft...")
bot.infinity_polling(timeout=60, long_polling_timeout=60)