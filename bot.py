# bot_single.py
# Signals & Auto-Entry Bot — Central-Deposit + Live USDC (RAW RPC)
#
# Änderungen (genau nach Wunsch):
# - Einzahlung nutzt NUR die zentrale Adresse (CENTRAL_SOL_PUBKEY).
# - User MUSS zuerst seine Absender-Wallet angeben.
# - Watcher scannt die zentrale Adresse via RPC (getSignaturesForAddress/getTransaction, jsonParsed)
#   und schreibt Guthaben gut, wenn Quelle == Absender-Wallet.
# - Neben SOL-Beträgen wird der Live-Preis in USDC angezeigt.
# - KEINE Imports von solana/solders/PublicKey/etc.
# - Zentrale Adresse wird als <code>...</code> bzw. Markdown-Backticks angezeigt (leicht kopierbar).
#
# Setup:
#   pip install pyTelegramBotAPI requests python-dotenv pytz
#
# ENV / Defaults:
#   BOT_TOKEN = 8212740282:AAEfOzr7e8EMyNi_8wjm7bndwk-cxZBvTRw
#   ADMIN_IDS = 8076025426
#   SOLANA_RPC = https://api.mainnet-beta.solana.com
#   CENTRAL_SOL_PUBKEY = 3wyVwpcbWt96mphJjskFsR2qoyafqJuSfGZYmiipW4oy
#
# Start: python bot_single.py

import os
import time
import threading
import sqlite3
from contextlib import contextmanager
from typing import Optional, Dict, List

import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

# ------------------------ CONFIG ------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "8212740282:AAHs0yUOnozpBXFR0rHhKBA_gi-ABA4LBns").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env missing")

ADMIN_IDS = [a.strip() for a in os.getenv("ADMIN_IDS", "8076025426").split(",") if a.strip()]
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com").strip()
CENTRAL_SOL_PUBKEY = os.getenv("CENTRAL_SOL_PUBKEY", "3wyVwpcbWt96mphJjskFsR2qoyafqJuSfGZYmiipW4oy").strip()

DB_PATH = "memebot.db"
LAMPORTS_PER_SOL = 1_000_000_000
MIN_SUB_SOL = 0.2

# ------------------------ SIMPLE PRICE (SOL->USDC) ------------------------

_price_cache = {"t": 0, "usd": 0.0}

def get_sol_usd() -> float:
    # Cached 60s to avoid rate limits
    now = time.time()
    if now - _price_cache["t"] < 60 and _price_cache["usd"] > 0:
        return _price_cache["usd"]
    try:
        # CoinGecko simple endpoint; USDC ~ USD
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
            timeout=6
        )
        usd = float(r.json().get("solana", {}).get("usd", 0.0) or 0.0)
        if usd > 0:
            _price_cache.update({"t": now, "usd": usd})
            return usd
    except Exception:
        pass
    # Fallback (nur Anzeige)
    return _price_cache["usd"] or 0.0

def fmt_sol_usdc(lamports_or_int: int) -> str:
    lam = int(lamports_or_int)
    sol = lam / LAMPORTS_PER_SOL
    usd = get_sol_usd()
    if usd > 0:
        return f"{sol:.6f} SOL (~{sol*usd:.2f} USDC)"
    return f"{sol:.6f} SOL"

# ------------------------ DB ------------------------

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

CREATE TABLE IF NOT EXISTS seen_txs (
  sig TEXT PRIMARY KEY,
  user_id INTEGER,
  amount_lamports INTEGER,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_by INTEGER NOT NULL,
  market_type TEXT NOT NULL,            -- MEME | FUTURES
  base TEXT NOT NULL,                   -- SOL / Tokenname
  side TEXT,
  leverage TEXT,
  token_address TEXT,
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
  note TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  last_notified_at TIMESTAMP,
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
            "ALTER TABLE payouts ADD COLUMN note TEXT",
        ]:
            try: con.execute(stmt)
            except Exception: pass

# ------------------------ HELPERS ------------------------

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
        bal_row = con.execute("SELECT sol_balance_lamports FROM users WHERE user_id=?", (user_id,)).fetchone()
        bal = bal_row["sol_balance_lamports"] if bal_row else 0
        if bal < lamports: return False
        con.execute("UPDATE users SET sol_balance_lamports = sol_balance_lamports - ? WHERE user_id=?", (lamports, user_id))
        return True

def get_balance_lamports(user_id:int)->int:
    with get_db() as con:
        row = con.execute("SELECT sol_balance_lamports FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row["sol_balance_lamports"] if row else 0

def list_investors(limit:int=50, offset:int=0):
    with get_db() as con:
        return con.execute("""
            SELECT user_id, username, sol_balance_lamports, source_wallet, sub_active
            FROM users
            WHERE sub_active=1
            ORDER BY sol_balance_lamports DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()

def all_subscribers():
    with get_db() as con:
        return [r["user_id"] for r in con.execute("SELECT user_id FROM users WHERE sub_active=1").fetchall()]

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

def fmt_call(c)->str:
    if c["market_type"] == "FUTURES":
        core = f"Futures • {c['base']} • {c['side']} {c['leverage'] or ''}".strip()
    else:
        core = f"Meme • {c['base']}"
    extra = f"\nToken: `{c['token_address']}`" if (c["market_type"]=="MEME" and c["token_address"]) else ""
    note = f"\nNotes: {c['notes']}" if c["notes"] else ""
    return f"🧩 *{core}*{extra}{note}"

# ------------------------ KEYBOARDS ------------------------

def kb_main(u):
    bal = fmt_sol_usdc(u["sol_balance_lamports"])
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

def kb_auto(u):
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

def kb_admin():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("➕ Call erstellen (einfach)", callback_data="admin_new_call_simple"))
    kb.add(InlineKeyboardButton("📣 Call senden an Abonnenten", callback_data="admin_broadcast_last"))
    kb.add(InlineKeyboardButton("👥 Investoren (Abos)", callback_data="admin_list_investors"))
    kb.add(InlineKeyboardButton("💼 Guthaben ändern", callback_data="admin_balance_edit"))
    kb.add(InlineKeyboardButton("🧾 Auszahlungsanfragen", callback_data="admin_payout_queue"))
    kb.add(InlineKeyboardButton("📈 Trade-Status senden", callback_data="admin_trade_status"))
    kb.add(InlineKeyboardButton("⬅️ Zurück", callback_data="back_home"))
    return kb

def kb_payout_manage(pid:int):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Genehmigen", callback_data=f"payout_APPROVE_{pid}"),
           InlineKeyboardButton("📤 Gesendet", callback_data=f"payout_SENT_{pid}"),
           InlineKeyboardButton("❌ Ablehnen", callback_data=f"payout_REJECT_{pid}"))
    return kb

# ------------------------ RAW SOLANA RPC HELPERS ------------------------

checked_signatures = set()

def rpc(method: str, params: list):
    try:
        r = requests.post(
            SOLANA_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=10
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("RPC error:", e)
        return {"result": None}

def get_new_signatures_for_address(address: str, limit: int = 50) -> List[str]:
    try:
        res = rpc("getSignaturesForAddress", [address, {"limit": limit}])
        arr = res.get("result") or []
        sigs = []
        for item in arr:
            sig = item.get("signature")
            if sig and sig not in checked_signatures:
                sigs.append(sig)
        sigs.reverse()  # oldest first
        return sigs
    except Exception as e:
        print("getSignaturesForAddress error:", e)
        return []

def get_tx_details(sig: str, central_addr: str):
    """
    Liefert {'from': sender, 'amount_lamports': int, 'blockTime': int}
    falls central_addr Lamports erhalten hat; sonst None.
    """
    try:
        r = rpc("getTransaction", [sig, {"encoding":"jsonParsed","commitment":"confirmed"}])
        res = r.get('result')
        if not res:
            return None
        if (res.get('meta') or {}).get('err'):
            return None

        txmsg = (res.get('transaction') or {}).get('message', {})
        meta = res.get('meta') or {}
        keys_raw = txmsg.get('accountKeys') or []
        keys = [k.get('pubkey') if isinstance(k, dict) else k for k in keys_raw]

        pre = meta.get('preBalances')
        post = meta.get('postBalances')
        if pre is None or post is None:
            return None

        # Index der zentralen Adresse
        try:
            central_idx = keys.index(central_addr)
        except ValueError:
            return None

        # Delta auf zentraler Adresse
        if central_idx < len(pre) and central_idx < len(post):
            delta_central = post[central_idx] - pre[central_idx]
        else:
            delta_central = 0

        if delta_central <= 0:
            return None

        # Sender bestimmen (Heuristik 1: Balance drop)
        sender = None
        for i, (p, po) in enumerate(zip(pre, post)):
            if p - po >= delta_central - 1000:
                sender = keys[i]
                break

        # Falls nicht gefunden: aus parsed Instructions lesen
        if not sender:
            for inst in (txmsg.get('instructions') or []):
                if isinstance(inst, dict):
                    parsed = inst.get('parsed') or {}
                    info = parsed.get('info') or {}
                    if info.get('destination') == central_addr and info.get('source'):
                        sender = info['source']; break
                    if info.get('to') == central_addr and info.get('from'):
                        sender = info['from']; break

        return {"from": sender, "amount_lamports": int(delta_central), "blockTime": res.get("blockTime") or 0}
    except Exception as e:
        print("get_tx_details error:", e)
        return None

# ------------------------ WATCHER (zentraler Pubkey via RAW RPC) ------------------------

class CentralWatcher:
    def __init__(self, central_addr:str):
        self.central = central_addr
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self.on_verified_deposit = None  # callback(evt)

    def start(self, interval_sec:int=25):
        if self._running: return
        self._running = True
        self._thread = threading.Thread(target=self._loop, args=(interval_sec,), daemon=True)
        self._thread.start()

    def _loop(self, interval:int):
        while self._running:
            try:
                self.scan_central_recent()
            except Exception as e:
                print("Watcher error:", e)
            time.sleep(interval)

    def _is_seen(self, sig:str)->bool:
        with get_db() as con:
            r = con.execute("SELECT 1 FROM seen_txs WHERE sig=?", (sig,)).fetchone()
            return r is not None

    def _mark_seen(self, sig:str, user_id:int, lamports:int):
        with get_db() as con:
            con.execute("INSERT OR IGNORE INTO seen_txs(sig, user_id, amount_lamports) VALUES (?,?,?)",
                        (sig, user_id, lamports))

    def scan_central_recent(self):
        sigs = get_new_signatures_for_address(self.central, limit=50)
        if not sigs:
            return

        # Karte: Quelle -> user_id
        with get_db() as con:
            rows = con.execute("SELECT user_id, source_wallet FROM users WHERE source_wallet IS NOT NULL").fetchall()
        src_map = {r["source_wallet"]: r["user_id"] for r in rows if r["source_wallet"]}

        for sig in sigs:
            if self._is_seen(sig):
                checked_signatures.add(sig)
                continue

            details = get_tx_details(sig, self.central)
            checked_signatures.add(sig)
            if not details:
                continue

            sender = details.get("from")
            amount = int(details.get("amount_lamports") or 0)
            if not sender or amount <= 0:
                continue

            uid = src_map.get(sender)
            if not uid:
                # keine zugeordnete Quelle
                continue

            # Gutschreiben & markieren
            self._mark_seen(sig, uid, amount)
            if self.on_verified_deposit:
                self.on_verified_deposit({"user_id": uid, "amount_lamports": amount, "sig": sig})

# ------------------------ CONNECTOR STUBS ------------------------

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

watcher = CentralWatcher(CENTRAL_SOL_PUBKEY)

def _on_verified_deposit(evt:dict):
    uid = evt["user_id"]
    lam = evt["amount_lamports"]
    add_balance(uid, lam)
    new_bal = get_balance_lamports(uid)
    try:
        bot.send_message(
            uid,
            f"✅ *Einzahlung verifiziert:* {fmt_sol_usdc(lam)}\n"
            f"Neues Guthaben: *{fmt_sol_usdc(new_bal)}*",
            parse_mode="Markdown")
    except Exception as e:
        print("notify deposit error:", e)

watcher.on_verified_deposit = _on_verified_deposit
watcher.start(interval_sec=25)

def home_text(u)->str:
    uname = ("@"+u["username"]) if u["username"] else f"ID {u['user_id']}"
    bal = fmt_sol_usdc(u["sol_balance_lamports"])
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

# ------------------------ CALLBACKS ------------------------

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
            "1) *Einzahlen*: Gib zuerst deine *Absender-Wallet* an. Danach erhältst du die *zentrale Adresse* zum Senden.\n"
            f"   Zentrale Adresse: `{CENTRAL_SOL_PUBKEY}`\n"
            "   Gutschrift nur, wenn die Quelle = deine Absender-Wallet ist.\n"
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
        bot.answer_callback_query(c.id, "Adresse angezeigt.")
        price = get_sol_usd()
        px = f"(1 SOL ≈ {price:.2f} USDC)" if price > 0 else ""
        text = (
            "💸 *Einzahlung*\n\n"
            f"Absender-Wallet: `{u['source_wallet']}`\n"
            f"Sende SOL an die *zentrale Adresse*:\n`{CENTRAL_SOL_PUBKEY}`\n"
            f"{px}\n\n"
            "_Nur Überweisungen von deiner Absender-Wallet werden gutgeschrieben._"
        )
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
                f"- {('@'+r['username']) if r['username'] else r['user_id']} • {fmt_sol_usdc(r['sol_balance_lamports'])}\n"
                f"  Source: `{r['source_wallet'] or '-'}`"
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
            "- Prozent für alle:  `all +5%` oder `all -3%`",
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
                   f"Betrag: *{fmt_sol_usdc(r['amount_lamports'])}*\n"
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
            "Kurze Nachricht (z. B. `Trade gestartet`, `TP1`, `SL`, `Liquidated`).\n"
            "Wird an *alle Abonnenten* gesendet.",
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
            try: bot.send_message(row["user_id"], "✅ Deine Auszahlung wurde *genehmigt*. Bitte kurz Geduld.", parse_mode="Markdown")
            except: pass
        elif action == "SENT":
            with get_db() as con:
                con.execute("UPDATE payouts SET status='SENT' WHERE id=?", (pid,))
            bot.answer_callback_query(c.id, "Als gesendet markiert.")
            try: bot.send_message(row["user_id"], "📤 Deine Auszahlung wurde *gesendet*.", parse_mode="Markdown")
            except: pass
        elif action == "REJECT":
            with get_db() as con:
                con.execute("UPDATE payouts SET status='REJECTED' WHERE id=?", (pid,))
            bot.answer_callback_query(c.id, "Abgelehnt.")
            try: bot.send_message(row["user_id"], "❌ Deine Auszahlung wurde *abgelehnt*.", parse_mode="Markdown")
            except: pass
        return

# ------------------------ MESSAGE HANDLERS ------------------------

def is_probably_solana_address(addr: str) -> bool:
    # einfache Validierung (Base58-ähnlich, Längencheck)
    if not isinstance(addr, str):
        return False
    addr = addr.strip()
    if len(addr) < 32 or len(addr) > 44:
        return False
    allowed = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return all(ch in allowed for ch in addr)

@bot.message_handler(func=lambda m: True)
def catch_all(m:Message):
    uid = m.from_user.id
    # State: Absender-Wallet?
    if WAITING_SOURCE_WALLET.get(uid, False):
        WAITING_SOURCE_WALLET[uid] = False
        wallet = (m.text or "").strip()
        if not is_probably_solana_address(wallet):
            bot.reply_to(m, "Bitte *eine gültige Solana-Adresse* eingeben.", parse_mode="Markdown")
            return
        set_source_wallet(uid, wallet)
        price = get_sol_usd()
        px = f"(1 SOL ≈ {price:.2f} USDC)" if price > 0 else ""
        bot.reply_to(m,
            "✅ Absender-Wallet gespeichert.\n\n"
            "💸 *Einzahlung*\n"
            f"Sende SOL von *dieser* Wallet:\n`{wallet}`\n"
            f"an die *zentrale Adresse*:\n`{CENTRAL_SOL_PUBKEY}`\n"
            f"{px}\n\n"
            "_Nur Überweisungen von deiner Absender-Wallet werden gutgeschrieben._",
            parse_mode="Markdown")
        return

    # State: Auszahlung?
    if WAITING_WITHDRAW_AMOUNT.get(uid, False):
        WAITING_WITHDRAW_AMOUNT[uid] = False
        try:
            txt = (m.text or "").replace(",", ".").strip()
            sol = float(txt)
            if sol <= 0:
                bot.reply_to(m, "Betrag muss > 0 sein.")
                return
            lam = int(sol * LAMPORTS_PER_SOL)
            if not subtract_balance(uid, lam):
                bot.reply_to(m, f"Unzureichendes Guthaben. Verfügbar: {fmt_sol_usdc(get_balance_lamports(uid))}")
                return
            note = f"User {uid} Auszahlung"
            with get_db() as con:
                cur = con.execute("INSERT INTO payouts(user_id, amount_lamports, note) VALUES (?,?,?)", (uid, lam, note))
                pid = cur.lastrowid
            bot.reply_to(m, f"✅ Auszahlungsanfrage erstellt: *{fmt_sol_usdc(lam)}*.\nEin Admin prüft und sendet zeitnah.", parse_mode="Markdown")
            for aid in ADMIN_IDS:
                try:
                    bot.send_message(int(aid),
                        f"🧾 *Neue Auszahlung #{pid}*\nUser: `{uid}`\nBetrag: *{fmt_sol_usdc(lam)}*",
                        parse_mode="Markdown", reply_markup=kb_payout_manage(pid))
                except Exception as e:
                    print("notify admin payout error:", e)
        except Exception:
            bot.reply_to(m, "Bitte eine gültige Zahl eingeben, z. B. `0.25`.", parse_mode="Markdown")
        return

    # Admin: einfacher Call?
    if ADMIN_AWAIT_SIMPLE_CALL.get(uid, False):
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
            bot.reply_to(m, "✅ Call gespeichert:\n" + fmt_call(c), parse_mode="Markdown")
        elif t0 == "MEME" and len(parts) >= 3:
            _, name_or_symbol, token_addr = parts[:3]
            cid = create_call(uid, "MEME", name_or_symbol.upper(), None, None, token_addr, "")
            c = get_call(cid)
            bot.reply_to(m, "✅ Call gespeichert:\n" + fmt_call(c), parse_mode="Markdown")
        else:
            bot.reply_to(m, "Formatfehler. Siehe Beispiel.", parse_mode="Markdown")
        return

    # Admin: Balance edit?
    if ADMIN_AWAIT_BALANCE_EDIT.get(uid, False):
        ADMIN_AWAIT_BALANCE_EDIT[uid] = False
        if not is_admin(uid):
            bot.reply_to(m, "Nicht erlaubt.")
            return
        try:
            txt = (m.text or "").strip()
            parts = txt.split(maxsplit=2)
            if len(parts) < 2:
                bot.reply_to(m, "Formatfehler. Beispiele in der Nachricht.", parse_mode="Markdown")
                return
            target, amount_s = parts[0], parts[1]
            note = parts[2] if len(parts) > 2 else ""
            amount_s = amount_s.replace(",", ".")
            if amount_s.endswith("%"):
                pct = float(amount_s[:-1]) / 100.0
                cnt = 0
                with get_db() as con:
                    subs = con.execute("SELECT user_id, sol_balance_lamports FROM users WHERE sub_active=1").fetchall()
                for r in subs:
                    delta = int(r["sol_balance_lamports"] * pct)
                    if delta != 0:
                        if delta > 0: add_balance(r["user_id"], delta)
                        else: subtract_balance(r["user_id"], -delta)
                        cnt += 1
                bot.reply_to(m, f"✅ {cnt} Abonnenten angepasst ({amount_s}). {note}")
                return
            sol = float(amount_s)
            lam = int(sol * LAMPORTS_PER_SOL)
            if target.lower() == "all":
                with get_db() as con:
                    subs = con.execute("SELECT user_id FROM users WHERE sub_active=1").fetchall()
                cnt = 0
                for r in subs:
                    if lam >= 0: add_balance(r["user_id"], lam)
                    else: subtract_balance(r["user_id"], -lam)
                    cnt += 1
                bot.reply_to(m, f"✅ Guthaben bei {cnt} Abonnenten geändert: {fmt_sol_usdc(lam)}. {note}")
            else:
                tuid = int(target)
                if lam >= 0: add_balance(tuid, lam)
                else:
                    ok = subtract_balance(tuid, -lam)
                    if not ok:
                        bot.reply_to(m, "Unzureichendes Guthaben beim Zielnutzer.")
                        return
                nb = fmt_sol_usdc(get_balance_lamports(tuid))
                bot.reply_to(m, f"✅ Guthaben geändert: {tuid} {fmt_sol_usdc(lam)} • Neues Guthaben: {nb}. {note}")
                try:
                    bot.send_message(tuid, f"📒 Admin-Anpassung: {fmt_sol_usdc(lam)}\nNeues Guthaben: {nb}\n{note}")
                except: pass
        except Exception as e:
            bot.reply_to(m, "Fehler beim Parsen. Siehe Beispiele oben.")
        return

    # Admin: Trade-Status?
    if ADMIN_AWAIT_TRADE_STATUS.get(uid, False):
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
        return

# ------------------------ AUTO EXECUTOR (simuliert) ------------------------

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

                # kleine P&L-Simulation
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
                        f"Einsatz: {fmt_sol_usdc(stake_lamports)} | P&L: {fmt_sol_usdc(pnl)}\n"
                        f"Guthaben: *{fmt_sol_usdc(bal_after)}*\n"
                        f"`{txid}`",
                        parse_mode="Markdown")
                except Exception as e:
                    print("notify exec error:", e)
        except Exception as e:
            print("executor loop error:", e)
        time.sleep(5)

# ------------------------ REMINDER (Payouts) ------------------------

def payout_reminder_loop():
    while True:
        try:
            with get_db() as con:
                rows = con.execute("""
                    SELECT id, amount_lamports FROM payouts
                    WHERE status='REQUESTED' AND (last_notified_at IS NULL OR (strftime('%s','now') - strftime('%s',COALESCE(last_notified_at,'1970-01-01')) > 1200))
                    ORDER BY created_at ASC
                """).fetchall()
            for r in rows:
                for aid in ADMIN_IDS:
                    try:
                        bot.send_message(int(aid),
                            f"⏰ Erinnerung: Auszahlung #{r['id']} offen • Betrag {fmt_sol_usdc(r['amount_lamports'])}",
                            reply_markup=kb_payout_manage(r["id"]))
                    except Exception as e:
                        print("payout remind error:", e)
                with get_db() as con:
                    con.execute("UPDATE payouts SET last_notified_at=CURRENT_TIMESTAMP WHERE id=?", (r["id"],))
            time.sleep(60)
        except Exception as e:
            print("payout reminder loop error:", e)
            time.sleep(60)

# ------------------------ RUN ------------------------

threading.Thread(target=auto_executor_loop, daemon=True).start()
threading.Thread(target=payout_reminder_loop, daemon=True).start()

print("Bot läuft...")
bot.infinity_polling(timeout=60, long_polling_timeout=60)