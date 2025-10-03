# bot_single.py
# Telegram Signals & Auto-Entry Bot (Single File, payouts & admin power-ups)
# - User: Einzahlen (fragt Absender-Wallet), persönliche Deposit-Adresse, Gutschrift nur bei verifizierter Quelle
# - User: Signale abonnieren/deaktivieren (Min 0.2 SOL), Auto-Entry ON/OFF + Risiko (Low/Medium/High) mit Erklärung
# - User: Auszahlung -> Admin erhält interaktive Anfrage (Approve/Sent/Reject); Admin-Menü mit Warteschlange
# - Admin: Guthaben ändern (pro Nutzer ODER alle Abonnenten), Trade-Status-Broadcast an Abonnenten
# - Auto-Executor: simuliert Orders, setzt Einsätze je Risiko, simple P&L
# - Periodische Erinnerungen für offene Auszahlungsanfragen
#
# Setup:
#   pip install pyTelegramBotAPI solana solders base58 requests python-dotenv pytz
#   Env:
#     BOT_TOKEN=...
#     ADMIN_IDS=123456,987654
#     SOLANA_RPC=https://api.mainnet-beta.solana.com
#     CENTRAL_SOL_PUBKEY=optional
#
# Start: python bot_single.py

import os
import time
import threading
import sqlite3
from contextlib import contextmanager
from typing import Optional, Dict, List

import base58
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

# Solana
from solana.publickey import PublicKey
from solana.keypair import Keypair
from solana.rpc.api import Client as SolClient
from solana.rpc.types import RPCResponse

# ------------------------ CONFIG ------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "8212740282:AAEfOzr7e8EMyNi_8wjm7bndwk-cxZBvTRw").strip()
if not BOT_TOKEN or BOT_TOKEN == "REPLACE_ME":
    raise RuntimeError("BOT_TOKEN env missing")

ADMIN_IDS = [a.strip() for a in os.getenv("ADMIN_IDS", "8076025426").split(",") if a.strip()]
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com").strip()
CENTRAL_SOL_PUBKEY = os.getenv("CENTRAL_SOL_PUBKEY", "3wyVwpcbWt96mphJjskFsR2qoyafqJuSfGZYmiipW4oy").strip()

DB_PATH = "memebot.db"

LAMPORTS_PER_SOL = 1_000_000_000
MIN_SUB_SOL = 0.2  # Mindestguthaben zum Abonnieren
PAYOUT_REMINDER_MIN = 20  # alle X Minuten Admin erinnern bei offenen Auszahlungen

# ------------------------ DB LAYER ------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  username TEXT,
  is_admin INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  sub_active INTEGER DEFAULT 0,
  auto_mode TEXT DEFAULT 'OFF',           -- OFF | ON
  auto_risk TEXT DEFAULT 'MEDIUM',        -- LOW | MEDIUM | HIGH
  sol_balance_lamports INTEGER DEFAULT 0,
  source_wallet TEXT                      -- vom Nutzer angegebene Absender-Wallet
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
  market_type TEXT NOT NULL,            -- MEME | FUTURES
  base TEXT NOT NULL,                   -- z.B. SOL / Tokenname
  side TEXT,                            -- LONG / SHORT (FUTURES)
  leverage TEXT,                        -- z.B. 20x (FUTURES)
  token_address TEXT,                   -- bei MEME optional
  notes TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  call_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  mode TEXT NOT NULL,                   -- ON
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
  last_notified_at TIMESTAMP,
  note TEXT,
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
        # defensive migrations
        for stmt in [
            "ALTER TABLE users ADD COLUMN auto_mode TEXT DEFAULT 'OFF'",
            "ALTER TABLE users ADD COLUMN auto_risk TEXT DEFAULT 'MEDIUM'",
            "ALTER TABLE users ADD COLUMN sol_balance_lamports INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN source_wallet TEXT",
            "ALTER TABLE payouts ADD COLUMN last_notified_at TIMESTAMP",
            "ALTER TABLE payouts ADD COLUMN note TEXT"
        ]:
            try: con.execute(stmt)
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
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def set_subscription(user_id:int, active:bool):
    with get_db() as con:
        con.execute("UPDATE users SET sub_active=? WHERE user_id=?", (1 if active else 0, user_id))

def set_auto_mode(user_id:int, mode:str):
    with get_db() as con:
        con.execute("UPDATE users SET auto_mode=? WHERE user_id=?", (mode, user_id))

def set_auto_risk(user_id:int, risk:str):
    with get_db() as con:
        con.execute("UPDATE users SET auto_risk=? WHERE user_id=?", (risk, user_id))

def set_source_wallet(user_id:int, wallet:str):
    with get_db() as con:
        con.execute("UPDATE users SET source_wallet=? WHERE user_id=?", (wallet, user_id))

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

def all_subscribers():
    with get_db() as con:
        return [r["user_id"] for r in con.execute("SELECT user_id FROM users WHERE sub_active=1").fetchall()]

def list_investors(limit:int=50, offset:int=0):
    with get_db() as con:
        return con.execute("""
            SELECT user_id, username, sol_balance_lamports, source_wallet,
                   (SELECT deposit_address FROM deposits d WHERE d.user_id=u.user_id LIMIT 1) AS deposit_address,
                   sub_active
            FROM users u
            WHERE sub_active=1
            ORDER BY sol_balance_lamports DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()

def create_call(created_by:int, market_type:str, base:str, side:str, leverage:str, token_addr:str, notes:str)->int:
    with get_db() as con:
        cur = con.execute("""
            INSERT INTO calls(created_by, market_type, base, side, leverage, token_address, notes)
            VALUES (?,?,?,?,?,?,?)
        """, (created_by, market_type, base, side, leverage, token_addr, notes))
        return cur.lastrowid

def get_call(cid:int):
    with get_db() as con:
        return con.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()

def queue_execution(call_id:int, user_id:int, status:str="QUEUED", message:str="")->int:
    with get_db() as con:
        cur = con.execute("""
            INSERT INTO executions(call_id, user_id, mode, status, message)
            VALUES(?,?,'ON',?,?)
        """, (call_id, user_id, status, message))
        return cur.lastrowid

def fmt_sol(lamports:int)->str:
    return f"{lamports / LAMPORTS_PER_SOL:.6f} SOL"

def fmt_call(c)->str:
    if c["market_type"] == "FUTURES":
        core = f"Futures • {c['base']} • {c['side']} {c['leverage'] or ''}".strip()
    else:
        core = f"Meme • {c['base']}"
    extra = f"\nToken: `{c['token_address']}`" if (c["market_type"]=="MEME" and c["token_address"]) else ""
    note = f"\nNotes: {c['notes']}" if c["notes"] else ""
    return f"🧩 *{core}*{extra}{note}"

# ------------------------ KEYBOARDS ------------------------

def kb_main(u)->InlineKeyboardMarkup:
    bal = fmt_sol(u["sol_balance_lamports"])
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💸 Einzahlen", callback_data="deposit"),
           InlineKeyboardButton("💳 Auszahlung", callback_data="withdraw"))
    kb.add(InlineKeyboardButton("🔔 Signale abonnieren", callback_data="sub_on"),
           InlineKeyboardButton("🔕 Signale deaktivieren", callback_data="sub_off"))
    kb.add(InlineKeyboardButton("⚙️ Auto-Entry", callback_data="auto_menu"))
    kb.add(InlineKeyboardButton("ℹ️ Hilfe", callback_data="help"))
    if is_admin(u["user_id"]):
        kb.add(InlineKeyboardButton("🛠 Admin-Menü", callback_data="admin_menu"))
    kb.add(InlineKeyboardButton(f"Guthaben: {bal}", callback_data="noop"))
    return kb

def kb_auto(u)->InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("OFF", callback_data="auto_OFF"),
           InlineKeyboardButton("ON", callback_data="auto_ON"))
    kb.add(InlineKeyboardButton("Risk: LOW", callback_data="risk_LOW"),
           InlineKeyboardButton("MEDIUM", callback_data="risk_MEDIUM"),
           InlineKeyboardButton("HIGH", callback_data="risk_HIGH"))
    kb.add(InlineKeyboardButton("Erklärung zu Risiken", callback_data="risk_info"))
    kb.add(InlineKeyboardButton(f"Aktueller Modus: {u['auto_mode']}", callback_data="noop"))
    kb.add(InlineKeyboardButton(f"Aktuelles Risiko: {u['auto_risk']}", callback_data="noop"))
    kb.add(InlineKeyboardButton("⬅️ Zurück", callback_data="back_home"))
    return kb

def kb_admin()->InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("➕ Call erstellen (einfach)", callback_data="admin_new_call_simple"))
    kb.add(InlineKeyboardButton("📣 Call senden an Abonnenten", callback_data="admin_broadcast_last"))
    kb.add(InlineKeyboardButton("👥 Investoren (Abos)", callback_data="admin_list_investors"))
    kb.add(InlineKeyboardButton("💼 Guthaben ändern", callback_data="admin_balance_edit"))
    kb.add(InlineKeyboardButton("🧾 Auszahlungsanfragen", callback_data="admin_payout_queue"))
    kb.add(InlineKeyboardButton("📈 Trade-Status senden", callback_data="admin_trade_status"))
    kb.add(InlineKeyboardButton("⬅️ Zurück", callback_data="back_home"))
    return kb

def kb_payout_manage(pid:int)->InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Genehmigen", callback_data=f"payout_APPROVE_{pid}"),
           InlineKeyboardButton("📤 Gesendet", callback_data=f"payout_SENT_{pid}"),
           InlineKeyboardButton("❌ Ablehnen", callback_data=f"payout_REJECT_{pid}"))
    return kb

# ------------------------ SOLANA WATCHER ------------------------

class SolWatcher:
    def __init__(self, rpc_url:str):
        self.client = SolClient(rpc_url)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self.on_verified_deposit = None  # callback(evt)

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

    def _recent_transfer_from_source(self, deposit_addr:str, source_addr:str, expected_min:int)->int:
        try:
            sigs = self.client.get_signatures_for_address(PublicKey(deposit_addr), limit=15)["result"]
            for s in sigs:
                sig = s["signature"]
                trx = self.client.get_transaction(sig, encoding="jsonParsed")["result"]
                if not trx: 
                    continue
                meta = trx.get("meta") or {}
                pre = meta.get("preBalances") or []
                post = meta.get("postBalances") or []
                acct_keys = (trx.get("transaction") or {}).get("message", {}).get("accountKeys", [])
                dep_idx = next((i for i,k in enumerate(acct_keys) if (k.get("pubkey") if isinstance(k,dict) else k)==deposit_addr), None)
                src_idx = next((i for i,k in enumerate(acct_keys) if (k.get("pubkey") if isinstance(k,dict) else k)==source_addr), None)
                if dep_idx is None or src_idx is None: 
                    continue
                if dep_idx < len(pre) and dep_idx < len(post) and src_idx < len(pre) and src_idx < len(post):
                    delta_in = post[dep_idx] - pre[dep_idx]
                    delta_src = pre[src_idx] - post[src_idx]
                    if delta_in > 0 and delta_src >= delta_in and delta_in >= expected_min*0.9:
                        return int(delta_in)
        except Exception as e:
            print("verify transfer error:", e)
        return 0

    def start(self, interval_sec:int=35):
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
            rows = con.execute("""
                SELECT d.id, d.user_id, d.deposit_address, d.last_balance_lamports, u.source_wallet
                FROM deposits d
                LEFT JOIN users u ON u.user_id = d.user_id
            """).fetchall()
        for r in rows:
            current = self.get_balance_lamports(r["deposit_address"])
            if current > r["last_balance_lamports"]:
                diff = current - r["last_balance_lamports"]
                amount_from_src = 0
                if r["source_wallet"]:
                    amount_from_src = self._recent_transfer_from_source(r["deposit_address"], r["source_wallet"], diff)
                with get_db() as con:
                    con.execute("UPDATE deposits SET last_balance_lamports=? WHERE id=?", (current, r["id"]))
                if amount_from_src > 0 and self.on_verified_deposit:
                    self.on_verified_deposit({"user_id": r["user_id"], "address": r["deposit_address"], "amount_lamports": amount_from_src})
                else:
                    try:
                        bot.send_message(r["user_id"],
                            "⚠️ Einzahlung entdeckt, aber Quelle konnte nicht eindeutig geprüft werden.\n"
                            "Bitte sende *nur* von deiner hinterlegten Absender-Wallet.",
                            parse_mode="Markdown")
                    except Exception as e:
                        print("notify unknown source error:", e)

# ------------------------ CONNECTOR STUBS (SIMULATED) ------------------------

def dex_market_buy_simulated(user_id:int, base:str, amount_lamports:int):
    return {"status":"FILLED", "txid":"SIM-TX-"+base, "spent_lamports": amount_lamports}

def futures_place_simulated(user_id:int, base:str, side:str, leverage:str, risk:str):
    return {"status":"FILLED", "order_id":"SIM-ORDER", "base":base, "side":side, "lev":leverage, "risk":risk}

# ------------------------ BOT ------------------------

init_db()
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

WAITING_SOURCE_WALLET: Dict[int, bool] = {}
WAITING_WITHDRAW_AMOUNT: Dict[int, bool] = {}
ADMIN_AWAIT_SIMPLE_CALL: Dict[int, bool] = {}
ADMIN_AWAIT_BALANCE_EDIT: Dict[int, bool] = {}
ADMIN_AWAIT_TRADE_STATUS: Dict[int, bool] = {}

watcher = SolWatcher(SOLANA_RPC)

def _on_verified_deposit(evt:dict):
    uid = evt["user_id"]
    lam = evt["amount_lamports"]
    add_balance(uid, lam)
    new_bal = get_balance_lamports(uid)
    try:
        bot.send_message(
            uid,
            f"✅ *Einzahlung verifiziert:* {fmt_sol(lam)}\nNeues Guthaben: *{fmt_sol(new_bal)}*",
            parse_mode="Markdown")
    except Exception as e:
        print("notify deposit error:", e)

watcher.on_verified_deposit = _on_verified_deposit
watcher.start(interval_sec=35)

def home_text(u)->str:
    uname = ("@"+u["username"]) if u["username"] else f"ID {u['user_id']}"
    bal = fmt_sol(u["sol_balance_lamports"])
    return (
        f"Willkommen, {uname}! 👋\n"
        "Straight & easy: Einzahlen → Abo → Auto-Entry.\n"
        "Low/Med/High Risk je nach Geschmack.\n\n"
        f"Dein Guthaben: *{bal}*"
    )

@bot.message_handler(commands=["start"])
def cmd_start(m:Message):
    uid = m.from_user.id
    uname = m.from_user.username or ""
    admin_flag = 1 if is_admin(uid) else 0
    upsert_user(uid, uname, admin_flag)
    u = get_user(uid)
    bot.reply_to(m, home_text(u), reply_markup=kb_main(u))

# --------- Callbacks ---------

@bot.callback_query_handler(func=lambda c: True)
def on_cb(c:CallbackQuery):
    uid = c.from_user.id
    u = get_user(uid)
    data = c.data

    if data == "back_home":
        u = get_user(uid)
        bot.edit_message_text(home_text(u), c.message.chat.id, c.message.message_id, reply_markup=kb_main(u))
        return

    if data == "help":
        bot.edit_message_text(
            "ℹ️ *Hilfe*\n\n"
            "1) *Einzahlen*: Zuerst deine *Absender-Wallet* angeben. Danach erhältst du deine *persönliche Deposit-Adresse*.\n"
            "   Nur Einzahlungen *von dieser Absender-Wallet* werden gutgeschrieben.\n"
            "2) *Signale abonnieren*: Mindestguthaben 0.2 SOL. Deaktivieren jederzeit.\n"
            "3) *Auto-Entry*: ON/OFF. Risiko (Low/Medium/High) steuert Einsatz (5/10/20%).\n"
            "4) *Auszahlung*: Betrag in SOL eingeben; Admin bestätigt & sendet.",
            c.message.chat.id, c.message.message_id, parse_mode="Markdown", reply_markup=kb_main(u))
        return

    if data == "deposit":
        if not u["source_wallet"]:
            WAITING_SOURCE_WALLET[uid] = True
            bot.answer_callback_query(c.id, "Bitte zuerst deine Absender-Wallet senden.")
            bot.send_message(c.message.chat.id, "Gib jetzt *deine Absender-Wallet (SOL)* ein:", parse_mode="Markdown")
            return
        addr = watcher.ensure_user_address(uid)
        bot.answer_callback_query(c.id, "Adresse abgerufen.")
        text = (
            "💸 *Einzahlung*\n\n"
            f"Absender-Wallet: `{u['source_wallet']}`\n"
            f"Sende SOL an deine *persönliche Adresse*:\n`{addr}`\n\n"
            "_Nur Überweisungen von deiner Absender-Wallet werden gutgeschrieben._"
        )
        if CENTRAL_SOL_PUBKEY:
            text += f"\n\n(Info: zentrale Wallet: `{CENTRAL_SOL_PUBKEY}`)"
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, parse_mode="Markdown", reply_markup=kb_main(u))
        return

    if data == "withdraw":
        WAITING_WITHDRAW_AMOUNT[uid] = True
        bot.answer_callback_query(c.id, "Bitte Betrag eingeben.")
        bot.send_message(c.message.chat.id, "💳 *Auszahlung*\nGib den Betrag in SOL ein (z. B. `0.25`).", parse_mode="Markdown")
        return

    if data == "sub_on":
        bal_sol = get_balance_lamports(uid) / LAMPORTS_PER_SOL
        if bal_sol < MIN_SUB_SOL:
            bot.answer_callback_query(c.id, f"Mindestens {MIN_SUB_SOL} SOL nötig.")
            return
        set_subscription(uid, True)
        bot.answer_callback_query(c.id, "Abo aktiviert")
        bot.edit_message_text("🔔 Abo ist *aktiv*.", c.message.chat.id, c.message.message_id, parse_mode="Markdown", reply_markup=kb_main(u))
        return

    if data == "sub_off":
        set_subscription(uid, False)
        bot.answer_callback_query(c.id, "Abo beendet")
        bot.edit_message_text("🔕 Abo *beendet*.", c.message.chat.id, c.message.message_id, parse_mode="Markdown", reply_markup=kb_main(u))
        return

    if data == "auto_menu":
        bot.edit_message_text("⚙️ Auto-Entry Einstellungen:", c.message.chat.id, c.message.message_id, reply_markup=kb_auto(u))
        return

    if data.startswith("auto_"):
        mode = data.split("_",1)[1]
        if mode not in ("OFF","ON"):
            mode = "OFF"
        set_auto_mode(uid, mode)
        bot.answer_callback_query(c.id, f"Auto-Entry: {mode}")
        nu = get_user(uid)
        bot.edit_message_text(f"Auto-Entry: *{mode}*", c.message.chat.id, c.message.message_id, parse_mode="Markdown", reply_markup=kb_auto(nu))
        return

    if data.startswith("risk_"):
        risk = data.split("_",1)[1]
        set_auto_risk(uid, risk)
        bot.answer_callback_query(c.id, f"Risk: {risk}")
        nu = get_user(uid)
        bot.edit_message_text("Auto-Entry aktualisiert.", c.message.chat.id, c.message.message_id, reply_markup=kb_auto(nu))
        return

    if data == "risk_info":
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id,
            "📘 *Risiko-Erklärung*\n"
            "- *LOW*: Kleiner Einsatz, kleinere Gewinne, stabiler.\n"
            "- *MEDIUM*: Ausgewogen.\n"
            "- *HIGH*: Größerer Einsatz, potenziell höhere Gewinne, aber mehr Risiko.",
            parse_mode="Markdown")
        return

    # ----- Admin -----
    if data == "admin_menu":
        if not is_admin(uid):
            bot.answer_callback_query(c.id, "Nicht erlaubt.")
            return
        bot.edit_message_text("🛠 Admin-Menü", c.message.chat.id, c.message.message_id, reply_markup=kb_admin())
        return

    if data == "admin_list_investors":
        if not is_admin(uid): return
        rows = list_investors(limit=50, offset=0)
        if not rows:
            bot.answer_callback_query(c.id, "Keine Abonnenten.")
            return
        parts = ["👥 *Investoren (Top 50)*"]
        for r in rows:
            parts.append(
                f"- {('@'+r['username']) if r['username'] else r['user_id']} • {fmt_sol(r['sol_balance_lamports'])}\n"
                f"  Source: `{r['source_wallet'] or '-'}`\n"
                f"  Deposit: `{r['deposit_address'] or '-'}`"
            )
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, "\n".join(parts), parse_mode="Markdown")
        return

    if data == "admin_new_call_simple":
        if not is_admin(uid): return
        bot.answer_callback_query(c.id)
        bot.send_message(
            c.message.chat.id,
            "Sende den Call *einfach* im Format:\n"
            "- FUTURES: `FUTURES|BASE|SIDE|LEV`   (z. B. `FUTURES|SOL|LONG|20x`)\n"
            "- MEME:    `MEME|NAME_OR_SYMBOL|TOKEN_ADDRESS`",
            parse_mode="Markdown")
        ADMIN_AWAIT_SIMPLE_CALL[uid] = True
        return

    if data == "admin_broadcast_last":
        if not is_admin(uid): return
        with get_db() as con:
            row = con.execute("SELECT * FROM calls ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            bot.answer_callback_query(c.id, "Kein Call vorhanden.")
            return
        msg = "📣 *Neuer Call:*\n" + fmt_call(row)
        subs = all_subscribers()
        sent = 0
        for su in subs:
            try:
                bot.send_message(su, msg, parse_mode="Markdown")
                queue_execution(row["id"], su, status="QUEUED", message="Queued by broadcast")
                sent += 1
            except Exception as e:
                print("broadcast error", su, e)
        bot.answer_callback_query(c.id, f"An {sent} Abonnenten gesendet.")
        return

    if data == "admin_balance_edit":
        if not is_admin(uid): return
        bot.answer_callback_query(c.id)
        bot.send_message(
            c.message.chat.id,
            "💼 *Guthaben ändern*\n"
            "Format:\n"
            "- Einzelner Nutzer: `UID AMOUNT_SOL [NOTIZ]`\n"
            "- Alle Abonnenten:  `all AMOUNT_SOL [NOTIZ]`\n"
            "Beispiele:\n"
            "`123456789 0.05 Gewinner TP1`\n"
            "`123456789 -0.02 Stop-Loss`\n"
            "`all 0.01 Airdrop`",
            parse_mode="Markdown")
        ADMIN_AWAIT_BALANCE_EDIT[uid] = True
        return

    if data == "admin_payout_queue":
        if not is_admin(uid): return
        with get_db() as con:
            rows = con.execute("SELECT p.*, u.username FROM payouts p JOIN users u ON u.user_id=p.user_id WHERE p.status='REQUESTED' ORDER BY p.created_at ASC LIMIT 10").fetchall()
        if not rows:
            bot.answer_callback_query(c.id, "Keine offenen Auszahlungen.")
            return
        bot.answer_callback_query(c.id)
        for r in rows:
            txt = (f"🧾 *Auszahlung #{r['id']}* • {('@'+r['username']) if r['username'] else r['user_id']}\n"
                   f"Betrag: *{fmt_sol(r['amount_lamports'])}*\n"
                   f"Status: `{r['status']}`\n"
                   f"Notiz: {r['note'] or '-'}")
            bot.send_message(c.message.chat.id, txt, parse_mode="Markdown", reply_markup=kb_payout_manage(r["id"]))
        return

    if data == "admin_trade_status":
        if not is_admin(uid): return
        bot.answer_callback_query(c.id)
        bot.send_message(
            c.message.chat.id,
            "📈 *Trade-Status senden*\n"
            "Schreibe eine kurze Nachricht (z. B. `Trade gestartet`, `TP1 erreicht`, `SL`, `Liquidated`).\n"
            "Diese wird an *alle Abonnenten* gesendet.",
            parse_mode="Markdown")
        ADMIN_AWAIT_TRADE_STATUS[uid] = True
        return

    # --- Payout Buttons (Admin) ---
    if data.startswith("payout_"):
        if not is_admin(uid): return
        _, action, sid = data.split("_", 2)
        pid = int(sid)
        with get_db() as con:
            row = con.execute("SELECT p.*, u.username FROM payouts p JOIN users u ON u.user_id=p.user_id WHERE p.id=?", (pid,)).fetchone()
        if not row:
            bot.answer_callback_query(c.id, "Anfrage nicht gefunden.")
            return
        if action == "APPROVE":
            with get_db() as con:
                con.execute("UPDATE payouts SET status='APPROVED' WHERE id=?", (pid,))
            bot.answer_callback_query(c.id, "Genehmigt.")
            try:
                bot.send_message(row["user_id"], "✅ Deine Auszahlung wurde *genehmigt*. Bitte hab kurz Geduld – wir senden gleich.", parse_mode="Markdown")
            except: pass
        elif action == "SENT":
            with get_db() as con:
                con.execute("UPDATE payouts SET status='SENT' WHERE id=?", (pid,))
            bot.answer_callback_query(c.id, "Als gesendet markiert.")
            try:
                bot.send_message(row["user_id"], "📤 Deine Auszahlung wurde *gesendet*.", parse_mode="Markdown")
            except: pass
        elif action == "REJECT":
            # bei Ablehnung: Betrag wieder gut schreiben?
            with get_db() as con:
                con.execute("UPDATE payouts SET status='REJECTED' WHERE id=?", (pid,))
            bot.answer_callback_query(c.id, "Abgelehnt.")
            try:
                bot.send_message(row["user_id"], "❌ Deine Auszahlung wurde *abgelehnt*. Bitte Support kontaktieren.", parse_mode="Markdown")
            except: pass
        return

# ----- Admin: einfacher Call-Eingang -----

@bot.message_handler(func=lambda m: ADMIN_AWAIT_SIMPLE_CALL.get(m.from_user.id, False))
def handle_admin_simple_call(m:Message):
    uid = m.from_user.id
    ADMIN_AWAIT_SIMPLE_CALL[uid] = False
    if not is_admin(uid):
        bot.reply_to(m, "Nicht erlaubt.")
        return
    raw = (m.text or "").strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2:
        bot.reply_to(m, "Formatfehler. Siehe Beispiel.", parse_mode="Markdown")
        return
    t0 = parts[0].upper()
    if t0 == "FUTURES" and len(parts) >= 4:
        _, base, side, lev = parts[:4]
        cid = create_call(uid, "FUTURES", base.upper(), side.upper(), lev, None, "")
        c = get_call(cid)
        bot.reply_to(m, "✅ Call gespeichert:\n" + fmt_call(c), parse_mode="Markdown", reply_markup=kb_admin())
    elif t0 == "MEME" and len(parts) >= 3:
        _, name_or_symbol, token_addr = parts[:3]
        cid = create_call(uid, "MEME", name_or_symbol.upper(), None, None, token_addr, "")
        c = get_call(cid)
        bot.reply_to(m, "✅ Call gespeichert:\n" + fmt_call(c), parse_mode="Markdown", reply_markup=kb_admin())
    else:
        bot.reply_to(m, "Formatfehler. Siehe Beispiel.", parse_mode="Markdown")

# ------------------------ TEXT HANDLER: Source Wallet, Auszahlung, Admin-Edits ------------------------

@bot.message_handler(func=lambda m: WAITING_SOURCE_WALLET.get(m.from_user.id, False))
def handle_source_wallet(m:Message):
    uid = m.from_user.id
    WAITING_SOURCE_WALLET[uid] = False
    wallet = (m.text or "").strip()
    try:
        PublicKey(wallet)
    except Exception:
        bot.reply_to(m, "Bitte eine gültige Solana-Adresse eingeben.")
        return
    set_source_wallet(uid, wallet)
    addr = watcher.ensure_user_address(uid)
    bot.reply_to(m,
        "✅ Absender-Wallet gespeichert.\n\n"
        "💸 *Einzahlung*\n"
        f"Sende SOL von *dieser* Wallet:\n`{wallet}`\n"
        f"an deine *persönliche Adresse*:\n`{addr}`\n\n"
        "_Nur Überweisungen von deiner Absender-Wallet werden gutgeschrieben._",
        parse_mode="Markdown")

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
        note = f"User {uid} Auszahlung"
        with get_db() as con:
            cur = con.execute("INSERT INTO payouts(user_id, amount_lamports, note) VALUES (?,?,?)", (uid, lam, note))
            pid = cur.lastrowid
        bot.reply_to(m, f"✅ Auszahlungsanfrage erstellt: *{sol:.6f} SOL*.\nEin Admin prüft und sendet zeitnah.", parse_mode="Markdown")
        # Admin benachrichtigen mit Buttons
        for aid in ADMIN_IDS:
            try:
                bot.send_message(int(aid),
                    f"🧾 *Neue Auszahlung #{pid}*\nUser: `{uid}`\nBetrag: *{sol:.6f} SOL*",
                    parse_mode="Markdown", reply_markup=kb_payout_manage(pid))
            except Exception as e:
                print("notify admin payout error:", e)
    except Exception:
        bot.reply_to(m, "Bitte eine gültige Zahl eingeben, z. B. `0.25`.", parse_mode="Markdown")

@bot.message_handler(func=lambda m: ADMIN_AWAIT_BALANCE_EDIT.get(m.from_user.id, False))
def handle_admin_balance_edit(m:Message):
    uid = m.from_user.id
    ADMIN_AWAIT_BALANCE_EDIT[uid] = False
    if not is_admin(uid):
        bot.reply_to(m, "Nicht erlaubt.")
        return
    try:
        txt = (m.text or "").strip()
        parts = txt.split(maxsplit=2)
        if len(parts) < 2:
            bot.reply_to(m, "Formatfehler. Siehe Beispiele.", parse_mode="Markdown")
            return
        target, amount_s = parts[0], parts[1]
        note = parts[2] if len(parts) > 2 else ""
        amount_s = amount_s.replace(",", ".")
        if amount_s.endswith("%"):
            # Prozentuale Änderung auf alle Abonnenten
            pct = float(amount_s[:-1]) / 100.0
            cnt = 0
            with get_db() as con:
                subs = con.execute("SELECT user_id, sol_balance_lamports FROM users WHERE sub_active=1").fetchall()
            for r in subs:
                delta = int(r["sol_balance_lamports"] * pct)
                if delta != 0:
                    if delta > 0:
                        add_balance(r["user_id"], delta)
                    else:
                        subtract_balance(r["user_id"], -delta)
                    cnt += 1
            bot.reply_to(m, f"✅ {cnt} Abonnenten angepasst ({amount_s}). {note}")
            return
        # Betrag in SOL
        sol = float(amount_s)
        lam = int(sol * LAMPORTS_PER_SOL)
        if target.lower() == "all":
            with get_db() as con:
                subs = con.execute("SELECT user_id FROM users WHERE sub_active=1").fetchall()
            cnt = 0
            for r in subs:
                if lam >= 0:
                    add_balance(r["user_id"], lam)
                else:
                    subtract_balance(r["user_id"], -lam)
                cnt += 1
            bot.reply_to(m, f"✅ Guthaben bei {cnt} Abonnenten geändert: {sol:+.6f} SOL. {note}")
        else:
            tuid = int(target)
            if lam >= 0:
                add_balance(tuid, lam)
            else:
                ok = subtract_balance(tuid, -lam)
                if not ok:
                    bot.reply_to(m, "Unzureichendes Guthaben beim Zielnutzer.")
                    return
            nb = fmt_sol(get_balance_lamports(tuid))
            bot.reply_to(m, f"✅ Guthaben geändert: {tuid} {sol:+.6f} SOL • Neues Guthaben: {nb}. {note}")
            try:
                bot.send_message(tuid, f"📒 Admin-Anpassung: {sol:+.6f} SOL\nNeues Guthaben: {nb}\n{note}")
            except: pass
    except Exception as e:
        bot.reply_to(m, "Fehler beim Parsen. Siehe Beispiele oben.")

@bot.message_handler(func=lambda m: ADMIN_AWAIT_TRADE_STATUS.get(m.from_user.id, False))
def handle_admin_trade_status(m:Message):
    uid = m.from_user.id
    ADMIN_AWAIT_TRADE_STATUS[uid] = False
    if not is_admin(uid):
        bot.reply_to(m, "Nicht erlaubt.")
        return
    msg = (m.text or "").strip()
    if not msg:
        bot.reply_to(m, "Bitte Text senden.")
        return
    subs = all_subscribers()
    sent = 0
    for su in subs:
        try:
            bot.send_message(su, f"📢 *Trade-Update*: {msg}", parse_mode="Markdown")
            sent += 1
        except Exception as e:
            print("trade status broadcast error", su, e)
    bot.reply_to(m, f"✅ Trade-Status gesendet an {sent} Abonnenten.")

# ------------------------ AUTO EXECUTOR LOOP (simuliert) ------------------------

def risk_to_fraction(risk:str)->float:
    return {"LOW":0.05, "MEDIUM":0.10, "HIGH":0.20}.get((risk or "").upper(), 0.10)

def auto_executor_loop():
    while True:
        try:
            with get_db() as con:
                rows = con.execute("""
                    SELECT e.id as eid, e.user_id, e.call_id, e.status, u.auto_mode, u.auto_risk, u.sol_balance_lamports
                    FROM executions e
                    JOIN users u ON u.user_id = e.user_id
                    WHERE e.status='QUEUED'
                    LIMIT 50
                """).fetchall()
            for r in rows:
                if r["auto_mode"] != "ON":
                    with get_db() as con:
                        con.execute("UPDATE executions SET status='ERROR', message='Auto OFF' WHERE id=?", (r["eid"],))
                    continue
                call = get_call(r["call_id"])
                frac = risk_to_fraction(r["auto_risk"] or "MEDIUM")
                stake_lamports = max(int(r["sol_balance_lamports"] * frac), int(0.01 * LAMPORTS_PER_SOL))
                if stake_lamports <= 0 or r["sol_balance_lamports"] < stake_lamports:
                    with get_db() as con:
                        con.execute("UPDATE executions SET status='ERROR', message='Zu wenig Guthaben' WHERE id=?", (r["eid"],))
                    continue
                if not subtract_balance(r["user_id"], stake_lamports):
                    with get_db() as con:
                        con.execute("UPDATE executions SET status='ERROR', message='Balance-Abzug fehlgeschlagen' WHERE id=?", (r["eid"],))
                    continue

                if call["market_type"] == "FUTURES":
                    result = futures_place_simulated(r["user_id"], call["base"], call["side"], call["leverage"], r["auto_risk"])
                else:
                    result = dex_market_buy_simulated(r["user_id"], call["base"], stake_lamports)

                status = result.get("status","ERROR")
                txid = result.get("txid") or result.get("order_id") or ""

                with get_db() as con:
                    con.execute("UPDATE executions SET status=?, txid=?, message=? WHERE id=?",
                                (status, txid, str(result), r["eid"]))

                risk = (r["auto_risk"] or "MEDIUM").upper()
                pnl_frac = {"LOW":0.01, "MEDIUM":0.0, "HIGH":0.02}.get(risk, 0.0)
                pnl = int(stake_lamports * pnl_frac)
                if pnl != 0:
                    add_balance(r["user_id"], pnl)

                try:
                    bal_after = get_balance_lamports(r["user_id"])
                    bot.send_message(
                        r["user_id"],
                        f"🤖 Auto-Entry • {risk}\n"
                        f"{fmt_call(call)}\n"
                        f"Status: *{status}*\n"
                        f"Einsatz: {fmt_sol(stake_lamports)} | P&L: {fmt_sol(pnl)}\n"
                        f"Guthaben: *{fmt_sol(bal_after)}*\n"
                        f"`{txid}`",
                        parse_mode="Markdown")
                except Exception as e:
                    print("notify exec error", e)
        except Exception as e:
            print("executor loop error:", e)
        time.sleep(5)

# ------------------------ PAYOUT REMINDER LOOP ------------------------

def payout_reminder_loop():
    while True:
        try:
            with get_db() as con:
                rows = con.execute("""
                    SELECT id, user_id, amount_lamports, status, COALESCE(last_notified_at,'') AS ln
                    FROM payouts
                    WHERE status='REQUESTED'
                    ORDER BY created_at ASC
                """).fetchall()
            if rows:
                for r in rows:
                    # einfache zeitbasierte Erinnerung (SQLite: wir prüfen nur ob ln NULL war -> erinnern)
                    remind = (r["ln"] is None) or (r["ln"] == "")
                    if remind:
                        for aid in ADMIN_IDS:
                            try:
                                bot.send_message(int(aid),
                                    f"⏰ Erinnerung: Auszahlung #{r['id']} offen • Betrag {fmt_sol(r['amount_lamports'])}",
                                    reply_markup=kb_payout_manage(r["id"]))
                            except Exception as e:
                                print("payout remind error", e)
                        with get_db() as con:
                            con.execute("UPDATE payouts SET last_notified_at=CURRENT_TIMESTAMP WHERE id=?", (r["id"],))
            time.sleep(PAYOUT_REMINDER_MIN * 60)
        except Exception as e:
            print("payout reminder loop error:", e)
            time.sleep(60)

# ------------------------ RUN LOOPS ------------------------

threading.Thread(target=auto_executor_loop, daemon=True).start()
threading.Thread(target=payout_reminder_loop, daemon=True).start()

# ------------------------ RUN ------------------------

print("Bot läuft...")
bot.infinity_polling(timeout=60, long_polling_timeout=60)