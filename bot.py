# bot.py
# UTF-8

import os
import time
import random
import threading
import sqlite3
import hashlib
from contextlib import contextmanager
from typing import Optional, Dict, List, Tuple

import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

# ---------------------------
# Configuration (ENV)
# ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8535037912:AAHfAPVG7ugqWmdacCMDi15hFiukA_5TX00").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable required")

ADMIN_IDS = [a.strip() for a in os.getenv("ADMIN_IDS", "8076025426").split(",") if a.strip()]
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com").strip()
CENTRAL_SOL_PUBKEY = os.getenv("CENTRAL_SOL_PUBKEY", "CBboaHRCZARdxRBUWpWtPdiFLjP7kgQvDdWq7pLbcHn3").strip()

EXCHANGE_WALLETS = set([s.strip() for s in os.getenv("EXCHANGE_WALLETS", "").split(",") if s.strip()])

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

DB_PATH = os.getenv("DB_PATH", "memebot_full.db")
LAMPORTS_PER_SOL = 1_000_000_000
MIN_SUB_SOL = float(os.getenv("MIN_SUB_SOL", "0.1"))

SIMULATION_MODE = True

# ---------------------------
# Utilities
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
    if not isinstance(addr, str):
        return False
    addr = addr.strip()
    if len(addr) < 32 or len(addr) > 44:
        return False
    allowed = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return all(ch in allowed for ch in addr)

def gen_referral_for_user(user_id: int) -> str:
    h = hashlib.sha1(str(user_id).encode()).hexdigest()[:8]
    return f"REF{h.upper()}"

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
  sub_active INTEGER DEFAULT 0,
  auto_mode TEXT DEFAULT 'OFF',
  auto_risk TEXT DEFAULT 'MEDIUM',
  sol_balance_lamports INTEGER DEFAULT 0,
  source_wallet TEXT,
  payout_wallet TEXT,
  sub_types TEXT DEFAULT '',
  referral_code TEXT DEFAULT '',
  referral_bonus_claimed INTEGER DEFAULT 0,
  ref_by INTEGER,
  pin_hash TEXT
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
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  txid TEXT,
  message TEXT,
  stake_lamports INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS payouts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  amount_lamports INTEGER NOT NULL,
  status TEXT DEFAULT 'REQUESTED',
  note TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  last_notified_at TIMESTAMP,
  lockup_days INTEGER DEFAULT 0,
  fee_percent REAL DEFAULT 0.0
);
-- Transaktions-Verlauf (Usersicht)
CREATE TABLE IF NOT EXISTS tx_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL, -- DEPOSIT | WITHDRAW_REQ | WITHDRAW_SENT | TRADE | PNL | ADJ
  ref_id TEXT,
  amount_lamports INTEGER,
  meta TEXT,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- Referral Bäume/Klicks/Einzahlungen
CREATE TABLE IF NOT EXISTS referrals (
  referrer_user_id INTEGER NOT NULL,
  invited_user_id INTEGER NOT NULL,
  level INTEGER NOT NULL, -- 1,2,3
  clicked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  deposit_total_lamports INTEGER DEFAULT 0,
  PRIMARY KEY(referrer_user_id, invited_user_id, level)
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
            "ALTER TABLE users ADD COLUMN sub_types TEXT DEFAULT ''",
            "ALTER TABLE executions ADD COLUMN stake_lamports INTEGER DEFAULT 0",
            "ALTER TABLE payouts ADD COLUMN lockup_days INTEGER DEFAULT 0",
            "ALTER TABLE payouts ADD COLUMN fee_percent REAL DEFAULT 0.0",
            "ALTER TABLE users ADD COLUMN referral_code TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN referral_bonus_claimed INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN payout_wallet TEXT",
            "ALTER TABLE users ADD COLUMN ref_by INTEGER",
            "ALTER TABLE users ADD COLUMN pin_hash TEXT"
        ]:
            try: con.execute(stmt)
            except Exception: pass

# CRUD basics
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

def all_users() -> List[int]:
    with get_db() as con:
        return [r["user_id"] for r in con.execute("SELECT user_id FROM users").fetchall()]

def all_subscribers() -> List[int]:
    with get_db() as con:
        return [r["user_id"] for r in con.execute("SELECT user_id FROM users WHERE sub_active=1").fetchall()]

def add_balance(user_id: int, lamports: int):
    with get_db() as con:
        con.execute("UPDATE users SET sol_balance_lamports = sol_balance_lamports + ? WHERE user_id=?", (lamports, user_id))

def set_balance(user_id: int, lamports: int):
    with get_db() as con:
        con.execute("UPDATE users SET sol_balance_lamports = ? WHERE user_id=?", (lamports, user_id))

def get_balance_lamports(user_id: int) -> int:
    with get_db() as con:
        row = con.execute("SELECT sol_balance_lamports FROM users WHERE user_id=?", (user_id,)).fetchone()
        return int(row["sol_balance_lamports"]) if row else 0

def set_source_wallet(user_id: int, wallet: str):
    with get_db() as con:
        con.execute("UPDATE users SET source_wallet=? WHERE user_id=?", (wallet, user_id))

def set_payout_wallet(user_id: int, wallet: str):
    with get_db() as con:
        con.execute("UPDATE users SET payout_wallet=? WHERE user_id=?", (wallet, user_id))

def set_subscription(user_id: int, active: bool):
    with get_db() as con:
        con.execute("UPDATE users SET sub_active=? WHERE user_id=?", (1 if active else 0, user_id))

def subtract_balance(user_id: int, lamports: int) -> bool:
    with get_db() as con:
        row = con.execute("SELECT sol_balance_lamports FROM users WHERE user_id=?", (user_id,)).fetchone()
        cur = int(row["sol_balance_lamports"]) if row else 0
        if cur < lamports:
            return False
        con.execute("UPDATE users SET sol_balance_lamports = sol_balance_lamports - ? WHERE user_id=?", (lamports, user_id))
        return True

def log_tx(user_id: int, kind: str, amount_lamports: int, ref_id: Optional[str] = None, meta: str = ""):
    with get_db() as con:
        con.execute("INSERT INTO tx_log(user_id, kind, ref_id, amount_lamports, meta) VALUES (?,?,?,?,?)",
                    (user_id, kind, ref_id or "", int(amount_lamports or 0), meta or ""))

# --------- Summaries ----------
def sum_total_deposits() -> int:
    with get_db() as con:
        r = con.execute("SELECT COALESCE(SUM(amount_lamports),0) AS s FROM seen_txs WHERE user_id IS NOT NULL").fetchone()
        return int(r["s"] or 0)

def sum_total_balances() -> int:
    with get_db() as con:
        r = con.execute("SELECT COALESCE(SUM(sol_balance_lamports),0) AS s FROM users").fetchone()
        return int(r["s"] or 0)

def sum_open_payouts() -> int:
    with get_db() as con:
        r = con.execute("SELECT COALESCE(SUM(amount_lamports),0) AS s FROM payouts WHERE status='REQUESTED'").fetchone()
        return int(r["s"] or 0)

def sum_user_deposits(uid: int) -> int:
    with get_db() as con:
        r = con.execute("SELECT COALESCE(SUM(amount_lamports),0) AS s FROM seen_txs WHERE user_id=?", (uid,)).fetchone()
        return int(r["s"] or 0)

# ---------------------------
# Calls & executions
# ---------------------------
def create_call(created_by: int, market_type: str, base: str, side: Optional[str], leverage: Optional[str], token_addr: Optional[str], notes: str) -> int:
    with get_db() as con:
        cur = con.execute("""
            INSERT INTO calls(created_by, market_type, base, side, leverage, token_address, notes)
            VALUES (?,?,?,?,?,?,?)
        """, (created_by, market_type, base, side, leverage, token_addr, notes))
        return cur.lastrowid

def get_call(cid: int):
    with get_db() as con:
        return con.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()

def _risk_fraction(risk: str) -> float:
    return {"LOW": 0.20, "MEDIUM": 0.35, "HIGH": 0.65}.get((risk or "").upper(), 0.35)

def _compute_stake_for_user(user_id: int) -> int:
    u = get_user(user_id)
    if not u:
        return 0
    frac = _risk_fraction((u["auto_risk"] or "MEDIUM"))
    bal = int(u["sol_balance_lamports"] or 0)
    stake = max(int(bal * frac), int(0.01 * LAMPORTS_PER_SOL))
    return stake

def queue_execution(call_id: int, user_id: int, status: str = "QUEUED", message: str = "", stake_lamports: Optional[int] = None) -> int:
    if stake_lamports is None:
        stake_lamports = _compute_stake_for_user(user_id)
    with get_db() as con:
        cur = con.execute("""
            INSERT INTO executions(call_id, user_id, mode, status, message, stake_lamports)
            VALUES(?,?,'ON',?,?,?)
        """, (call_id, user_id, status, message, stake_lamports))
        # Log als TRADE (queued)
        log_tx(user_id, "TRADE", stake_lamports, ref_id=str(call_id), meta=f"Queued")
        return cur.lastrowid

def fmt_call(c) -> str:
    if c["market_type"] == "FUTURES":
        core = f"Futures • {c['base']} • {c.get('side','') or ''} {c.get('leverage','') or ''}".strip()
    else:
        core = f"Meme • {c['base']}"
    extra = f"\nToken: `{md_escape(c.get('token_address') or '')}`" if (c["market_type"] == "MEME" and c.get("token_address")) else ""
    note = f"\nNotes: {md_escape(c.get('notes') or '')}" if c.get("notes") else ""
    return f"🧩 *{core}*{extra}{note}"

# ---------------------------
# Keyboards
# ---------------------------
def kb_main(u):
    bal = fmt_sol_usdc(int(u["sol_balance_lamports"] or 0))
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💸 Einzahlen", callback_data="deposit"),
           InlineKeyboardButton("💳 Auszahlung", callback_data="withdraw"))
    kb.add(InlineKeyboardButton("🔔 Signale", callback_data="sub_menu"),
           InlineKeyboardButton("⚖️ infos ", callback_data="legal"))
    kb.add(InlineKeyboardButton("📜 Rückblick", callback_data="history"),
    kb.add(InlineKeyboardButton("📈 Wallet", callback_data="my_portfolio"))
    if is_admin(int(u["user_id"])):
        kb.add(InlineKeyboardButton("🛠️ Admin (Kontrolle)", callback_data="admin_menu_big"))
    kb.add(InlineKeyboardButton(f"🏦 Guthaben: {bal}", callback_data="noop"))
    return kb

def kb_sub_menu():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔔 Abonnieren", callback_data="sub_on"),
           InlineKeyboardButton("🔕 Abbestellen", callback_data="sub_off"))
    kb.add(InlineKeyboardButton("⬅️ Zurück", callback_data="back_home"))
    return kb

def kb_admin_main(page: int = 0):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("➕ Call erstellen", callback_data="admin_new_call"))
    kb.add(InlineKeyboardButton("📣 Broadcast Call", callback_data="admin_broadcast_last"))
    kb.add(InlineKeyboardButton("👥 Investoren (Liste)", callback_data="admin_list_investors"))
    kb.add(InlineKeyboardButton("👀 Nutzer verwalten", callback_data=f"admin_view_users_{page}"))
    kb.add(InlineKeyboardButton("💼 Guthaben ändern", callback_data="admin_balance_edit"))
    kb.add(InlineKeyboardButton("🧾 Offene Auszahlungen", callback_data="admin_open_payouts"))
    kb.add(InlineKeyboardButton("📊 System-Stats", callback_data="admin_stats"))
    kb.add(InlineKeyboardButton("📤 Broadcast an alle", callback_data="admin_broadcast_all"))
    kb.add(InlineKeyboardButton("🔧 Promotions / PnL", callback_data="admin_apply_pnl"))
    kb.add(InlineKeyboardButton("⬅️ Zurück", callback_data="back_home"))
    return kb

def kb_users_pagination(offset: int, total: int, prefix: str = "admin_view_users"):
    kb = InlineKeyboardMarkup()
    prev_off = max(0, offset - 10)
    next_off = offset + 10 if offset + 10 < total else offset
    if offset > 0:
        kb.add(InlineKeyboardButton("◀️ Zurück", callback_data=f"{prefix}_{prev_off}"))
    if offset + 10 < total:
        kb.add(InlineKeyboardButton("▶️ Weiter", callback_data=f"{prefix}_{next_off}"))
    kb.add(InlineKeyboardButton("⬅️ Admin Menü", callback_data="admin_menu_big"))
    return kb

def kb_user_row(user_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("ℹ️ Mehr Infos", callback_data=f"admin_user_{user_id}"))
    return kb

def kb_user_actions(user_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💰 Guthaben ändern", callback_data=f"admin_balance_{user_id}"),
           InlineKeyboardButton("🏷️ Wallet setzen", callback_data=f"admin_setwallet_{user_id}"))
    kb.add(InlineKeyboardButton("✉️ Nachricht", callback_data=f"admin_msg_{user_id}"),
           InlineKeyboardButton("🧾 Payouts", callback_data=f"admin_payouts_{user_id}"))
    kb.add(InlineKeyboardButton("⬅️ Liste", callback_data="admin_list_investors"))
    return kb

def kb_withdraw_options():
    kb = InlineKeyboardMarkup()
    for days, pct in sorted(_fee_tiers.items(), key=lambda x: x[0]):
        label = "Sofort • Fee 20%" if days == 0 else f"{days} Tage • Fee {pct}%"
        kb.add(InlineKeyboardButton(label, callback_data=f"payoutopt_{days}"))
    kb.add(InlineKeyboardButton("↩️ Abbrechen", callback_data="back_home"))
    return kb

def kb_payout_manage(pid: int):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Genehmigen", callback_data=f"payout_APPROVE_{pid}"),
           InlineKeyboardButton("📤 Gesendet", callback_data=f"payout_SENT_{pid}"),
           InlineKeyboardButton("❌ Ablehnen", callback_data=f"payout_REJECT_{pid}"))
    return kb

def kb_referral():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📊 Meine Referral-Stats", callback_data="ref_stats"),
           InlineKeyboardButton("💵 Einlösen (Anfrage)", callback_data="ref_claim"))
    kb.add(InlineKeyboardButton("⬅️ Zurück", callback_data="back_home"))
    return kb

# ---------------------------
# RPC watcher
# ---------------------------
checked_signatures = set()

def rpc(method: str, params: list, *, _retries=2, _base_sleep=0.8):
    for attempt in range(_retries + 1):
        try:
            r = requests.post(
                SOLANA_RPC,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=10
            )
            if r.status_code == 429:
                time.sleep(_base_sleep * (2 ** attempt) + random.uniform(0, 0.4))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt < _retries:
                time.sleep(_base_sleep * (2 ** attempt) + random.uniform(0, 0.4))
                continue
            print("RPC error:", e)
            return {"result": None}
    return {"result": None}

def get_new_signatures_for_address(address: str, limit: int = 20) -> List[str]:
    try:
        res = rpc("getSignaturesForAddress", [address, {"limit": limit}])
        arr = res.get("result") or []
        sigs = []
        for item in arr:
            sig = item.get("signature")
            if sig and sig not in checked_signatures:
                sigs.append(sig)
        sigs.reverse()
        return sigs
    except Exception as e:
        print("getSignaturesForAddress error:", e)
        return []

def get_tx_details(sig: str, central_addr: str):
    try:
        r = rpc("getTransaction", [sig, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        res = r.get('result')
        if not res:
            return None
        if (res.get('meta') or {}).get('err'):
            return None
        txmsg = (res.get('transaction') or {}).get('message', {})
        meta = res.get('meta') or {}
        keys_raw = txmsg.get('accountKeys') or []
        keys = [k.get('pubkey') if isinstance(k, dict) else k for k in keys_raw]
        pre = meta.get('preBalances'); post = meta.get('postBalances')
        if pre is None or post is None: return None
        try:
            central_idx = keys.index(central_addr)
        except ValueError:
            return None
        delta_central = post[central_idx] - pre[central_idx] if central_idx < len(pre) and central_idx < len(post) else 0
        if delta_central <= 0: return None
        sender = None
        for i, (p, po) in enumerate(zip(pre, post)):
            if p - po >= delta_central - 1000:
                sender = keys[i]; break
        if not sender:
            for inst in (txmsg.get('instructions') or []):
                if isinstance(inst, dict):
                    info = (inst.get('parsed') or {}).get('info') or {}
                    if info.get('destination') == central_addr and info.get('source'):
                        sender = info['source']; break
                    if info.get('to') == central_addr and info.get('from'):
                        sender = info['from']; break
        return {"from": sender, "amount_lamports": int(delta_central), "blockTime": res.get("blockTime") or 0}
    except Exception as e:
        print("get_tx_details error:", e)
        return None

class CentralWatcher:
    def __init__(self, central_addr: str):
        self.central = central_addr
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self.on_verified_deposit = None

    def start(self, interval_sec: int = 40):
        if self._running: return
        self._running = True
        self._thread = threading.Thread(target=self._loop, args=(interval_sec,), daemon=True)
        self._thread.start()

    def _loop(self, interval: int):
        while self._running:
            try:
                self.scan_central_recent()
            except Exception as e:
                print("Watcher error:", e)
            time.sleep(interval)

    def _is_seen(self, sig: str) -> bool:
        with get_db() as con:
            r = con.execute("SELECT 1 FROM seen_txs WHERE sig=?", (sig,)).fetchone()
            return r is not None

    def _mark_seen(self, sig: str, user_id: Optional[int], lamports: int):
        with get_db() as con:
            con.execute("INSERT OR IGNORE INTO seen_txs(sig, user_id, amount_lamports) VALUES (?,?,?)", (sig, user_id, lamports))

    def scan_central_recent(self):
        sigs = get_new_signatures_for_address(self.central, limit=20)
        if not sigs: return
        with get_db() as con:
            rows = con.execute("SELECT user_id, source_wallet FROM users WHERE source_wallet IS NOT NULL").fetchall()
        src_map = { (r["source_wallet"]): r["user_id"] for r in rows if r["source_wallet"] }
        for sig in sigs:
            if self._is_seen(sig):
                checked_signatures.add(sig); continue
            details = get_tx_details(sig, self.central)
            checked_signatures.add(sig)
            if not details: continue
            sender = details.get("from"); amount = int(details.get("amount_lamports") or 0)
            if not sender or amount <= 0: continue
            uid = src_map.get(sender)
            self._mark_seen(sig, uid if uid else None, amount)
            if not uid:
                note = (f"⚠️ Unbekannte Einzahlung erkannt\n"
                        f"Sender: `{md_escape(sender)}`\nBetrag: {fmt_sol_usdc(amount)}\nSig: `{md_escape(sig)}`")
                for aid in ADMIN_IDS:
                    try: bot.send_message(int(aid), note, parse_mode="Markdown")
                    except Exception: pass
                if sender in EXCHANGE_WALLETS:
                    for aid in ADMIN_IDS:
                        try: bot.send_message(int(aid), f"⚠️ Absender ist als Exchange-Wallet gelistet: `{md_escape(sender)}`", parse_mode="Markdown")
                        except Exception: pass
                return
            # bekannte Einzahlung
            add_balance(uid, amount)
            log_tx(uid, "DEPOSIT", amount, ref_id=sig, meta=f"from {sender}")
            if self.on_verified_deposit:
                self.on_verified_deposit({"user_id": uid, "amount_lamports": amount, "sig": sig})
            # Referral-Deposit zuordnen (Level 1..3)
            _apply_referral_deposit(uid, amount)

# ---------------------------
# Simulated trading
# ---------------------------
def dex_market_buy_simulated(user_id: int, base: str, amount_lamports: int):
    return {"status": "FILLED", "txid": f"Live-DEX-{base}-{int(time.time())}", "spent_lamports": amount_lamports}

def futures_place_simulated(user_id: int, base: str, side: str, leverage: str, risk: str):
    return {"status": "FILLED", "order_id": f"Live-FUT-{base}-{int(time.time())}", "base": base}

# ---------------------------
# Bot init & safe send wrappers
# ---------------------------
init_db()
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")

# Safe sender
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

_original_edit_message_text = bot.edit_message_text
def _safe_edit_message_text(text, chat_id, message_id, **kwargs):
    try:
        return _original_edit_message_text(text, chat_id, message_id, **kwargs)
    except Exception:
        pm = kwargs.get("parse_mode")
        if pm and str(pm).upper().startswith("MARKDOWN"):
            kwargs2 = dict(kwargs); kwargs2["parse_mode"] = "Markdown"
            try:
                return _original_edit_message_text(md_escape(str(text)), chat_id, message_id, **kwargs2)
            except Exception:
                kwargs3 = dict(kwargs2); kwargs3.pop("parse_mode", None)
                return _original_edit_message_text(str(text), chat_id, message_id, **kwargs3)
        else:
            kwargs3 = dict(kwargs); kwargs3.pop("parse_mode", None)
            return _original_edit_message_text(str(text), chat_id, message_id, **kwargs3)
bot.edit_message_text = _safe_edit_message_text

# ---------------------------
# States
# ---------------------------
WAITING_SOURCE_WALLET: Dict[int, bool] = {}
WAITING_PAYOUT_WALLET: Dict[int, bool] = {}
WAITING_WITHDRAW_AMOUNT: Dict[int, Optional[int]] = {}
AWAITING_PIN: Dict[int, Dict] = {}  # {uid: {"for":"withdraw|setwallet", "next": callable or tuple}}

ADMIN_AWAIT_SIMPLE_CALL: Dict[int, bool] = {}
ADMIN_AWAIT_BALANCE_SINGLE: Dict[int, Optional[int]] = {}
ADMIN_AWAIT_BALANCE_GLOBAL: Dict[int, bool] = {}
ADMIN_AWAIT_SET_WALLET: Dict[int, Optional[int]] = {}
ADMIN_AWAIT_MASS_BALANCE: Dict[int, bool] = {}
ADMIN_AWAIT_NEWS_BROADCAST: Dict[int, Dict] = {}
ADMIN_AWAIT_DM_TARGET: Dict[int, Optional[int]] = {}  # Nachricht senden
SUPPORT_AWAIT_MSG: Dict[int, bool] = {}

# ---------------------------
# Referral helpers
# ---------------------------
def _ensure_user_refcode(uid: int) -> str:
    u = get_user(uid)
    code = (u["referral_code"] or "") if u else ""
    if not code:
        code = gen_referral_for_user(uid)
        with get_db() as con:
            con.execute("UPDATE users SET referral_code=? WHERE user_id=?", (code, uid))
    return code

def _linkify_ref(bot_username: str, code: str) -> str:
    # Markdown-Link, NICHT in Backticks – damit klickbar (auch mit _)
    return f"[Klicke hier, um zu starten](https://t.me/{bot_username}?start={code})"

def _set_ref_by(invited_id: int, referrer_id: int):
    with get_db() as con:
        con.execute("UPDATE users SET ref_by=? WHERE user_id=? AND ref_by IS NULL", (referrer_id, invited_id))
    # Level 1 Kante anlegen
    with get_db() as con:
        con.execute("INSERT OR IGNORE INTO referrals(referrer_user_id, invited_user_id, level) VALUES (?,?,1)",
                    (referrer_id, invited_id))
    # Level 2/3 Kanten aus Baum ziehen
    with get_db() as con:
        # Wer hat den referrer eingeladen?
        r1 = con.execute("SELECT ref_by FROM users WHERE user_id=?", (referrer_id,)).fetchone()
        if r1 and r1["ref_by"]:
            lvl2 = int(r1["ref_by"])
            con.execute("INSERT OR IGNORE INTO referrals(referrer_user_id, invited_user_id, level) VALUES (?,?,2)",
                        (lvl2, invited_id))
            r2 = con.execute("SELECT ref_by FROM users WHERE user_id=?", (lvl2,)).fetchone()
            if r2 and r2["ref_by"]:
                lvl3 = int(r2["ref_by"])
                con.execute("INSERT OR IGNORE INTO referrals(referrer_user_id, invited_user_id, level) VALUES (?,?,3)",
                            (lvl3, invited_id))

def _apply_referral_deposit(invited_id: int, amount_lamports: int):
    # Summe auf Kanten addieren (1..3)
    with get_db() as con:
        con.execute("""
            UPDATE referrals SET deposit_total_lamports = deposit_total_lamports + ?
            WHERE invited_user_id=? AND level IN (1,2,3)
        """, (int(amount_lamports), invited_id))

def _ref_stats_text(uid: int) -> str:
    # Klicks (= Einträge in referrals), Summe je Level
    with get_db() as con:
        rows = con.execute("""
            SELECT level, COUNT(*) as clicks, COALESCE(SUM(deposit_total_lamports),0) as dep
            FROM referrals
            WHERE referrer_user_id=?
            GROUP BY level
        """, (uid,)).fetchall()
    by_level = {int(r["level"]): (int(r["clicks"] or 0), int(r["dep"] or 0)) for r in rows}
    l1 = by_level.get(1, (0,0)); l2 = by_level.get(2,(0,0)); l3 = by_level.get(3,(0,0))
    dep1 = l1[1]; dep2 = l2[1]; dep3 = l3[1]
    est10 = int(dep1 * 0.10)
    est5  = int(dep2 * 0.05)
    est25 = int(dep3 * 0.025)
    total_est = est10 + est5 + est25
    return (
        "🔗 *Referral-Übersicht*\n"
        f"Level 1: Klicks {l1[0]} • Einzahlungen {fmt_sol_usdc(dep1)} • *10%*: {fmt_sol_usdc(est10)}\n"
        f"Level 2: Klicks {l2[0]} • Einzahlungen {fmt_sol_usdc(dep2)} • *5%*: {fmt_sol_usdc(est5)}\n"
        f"Level 3: Klicks {l3[0]} • Einzahlungen {fmt_sol_usdc(dep3)} • *2.5%*: {fmt_sol_usdc(est25)}\n"
        f"= *Summe anzeigbar*: {fmt_sol_usdc(total_est)}\n\n"
        "_Hinweis: Auszahlung erfolgt nach Prüfung durch Admin (Einlösen drücken)._"
    )

# ---------------------------
# Home / Support / PIN
# ---------------------------
def get_bot_username():
    try:
        me = bot.get_me()
        return me.username or "<YourBotUsername>"
    except Exception:
        return "<YourBotUsername>"

LEGAL_TEXT = (
    "⚖️ *Rechtliches*\n\n"
    "• Dieser Bot stellt keine Finanzberatung dar.\n"
    "• Krypto-Handel ist mit erheblichen Risiken verbunden.\n"
    "• Nutzer sind für Ein-/Auszahlungen selbst verantwortlich.\n"
    "• Bei Unklarheiten wende dich an /support."
)

def home_text(u) -> str:
    raw_uname = ("@" + (u["username"] or "")) if u["username"] else f"ID {u['user_id']}"
    uname = md_escape(raw_uname)
    bal = fmt_sol_usdc(int(u["sol_balance_lamports"] or 0))
    code = _ensure_user_refcode(int(u["user_id"]))
    bot_username = get_bot_username()
    # Kein Backtick, Markdown-Link → klickbar trotz "_"
    ref_link_md = f"[Referral-Link](https://t.me/{bot_username}?start={code})"
    return (
        f"👋 Hallo {uname} — willkommen!\n\n"
        "Features:\n"
        "• Einzahlungen (verifizierte Source)\n"
        "• Signale (Auto-Entry optional)\n"
        "• Transaktionsverlauf & Portfolio\n"
        "• Referral-System (3 Ebenen)\n\n"
        f"🏦 Guthaben: *{bal}*\n"
        f"🔗 {ref_link_md}\n"
        "📩 Support: /support"
    )

def _hash_pin(pin: str) -> str:
    return hashlib.sha256(("PIN|" + pin).encode()).hexdigest()

@bot.message_handler(commands=["setpin"])
def cmd_setpin(m: Message):
    uid = m.from_user.id
    txt = (m.text or "").strip()
    parts = txt.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, "Verwendung: `/setpin 1234` (4–8 Ziffern)", parse_mode="Markdown")
        return
    pin = parts[1].strip()
    if not (pin.isdigit() and 4 <= len(pin) <= 8):
        bot.reply_to(m, "PIN muss 4–8 Ziffern sein.")
        return
    with get_db() as con:
        con.execute("UPDATE users SET pin_hash=? WHERE user_id=?", (_hash_pin(pin), uid))
    bot.reply_to(m, "✅ PIN gesetzt. Bei sensiblen Aktionen wird er abgefragt.")

@bot.message_handler(commands=["support"])
def cmd_support(m: Message):
    SUPPORT_AWAIT_MSG[m.from_user.id] = True
    bot.reply_to(m, "✍️ Sende jetzt deine Support-Nachricht (Text/Bild).")

# ---------------------------
# Watcher hook
# ---------------------------
watcher = CentralWatcher(CENTRAL_SOL_PUBKEY)

def _on_verified_deposit(evt: dict):
    uid = evt["user_id"]; lam = evt["amount_lamports"]
    try:
        bot.send_message(uid, f"✅ Einzahlung verifiziert: {fmt_sol_usdc(lam)}\nNeues Guthaben: {fmt_sol_usdc(get_balance_lamports(uid))}", parse_mode="Markdown")
    except Exception:
        pass

watcher.on_verified_deposit = _on_verified_deposit
threading.Thread(target=watcher.start, kwargs={"interval_sec": 40}, daemon=True).start()

# ---------------------------
# Handlers
# ---------------------------
@bot.message_handler(commands=["start"])
def cmd_start(m: Message):
    uid = m.from_user.id
    uname = m.from_user.username or ""
    upsert_user(uid, uname, 1 if is_admin(uid) else 0)

    # Referral payload
    ref_code = None
    txt = m.text or ""
    parts = txt.split(maxsplit=1)
    if len(parts) >= 2:
        ref_code = parts[1].strip()
        if ref_code.startswith("="):  # /start=CODE
            ref_code = ref_code[1:].strip()

    # Stelle sicher, dass eigener Code existiert
    _ensure_user_refcode(uid)

    # Wenn per Link gekommen → ref_by setzen + referrals (Level 1..3) Kanten
    if ref_code:
        with get_db() as con:
            ref_row = con.execute("SELECT user_id FROM users WHERE referral_code=?", (ref_code,)).fetchone()
        referrer = int(ref_row["user_id"]) if ref_row else None
        if referrer and referrer != uid:
            _set_ref_by(uid, referrer)

    u = get_user(uid)
    bot.reply_to(m, home_text(u), reply_markup=kb_main(u))

@bot.callback_query_handler(func=lambda c: True)
def on_cb(c: CallbackQuery):
    uid = c.from_user.id
    u = get_user(uid)
    data = c.data or ""

    if data == "back_home":
        bot.edit_message_text(home_text(u), c.message.chat.id, c.message.message_id, parse_mode="Markdown", reply_markup=kb_main(u)); return
    if data == "noop":
        bot.answer_callback_query(c.id, "—"); return

    if data == "legal":
        bot.answer_callback_query(c.id)
        bot.send_message(uid, LEGAL_TEXT, parse_mode="Markdown"); return

    if data == "help":
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "ℹ️ Hilfe:\nEinzahlen, Signale, Verlauf, Referral. /support für Kontakt.", parse_mode=None)
        return

    # deposit
    if data == "deposit":
        if not u["source_wallet"]:
            WAITING_SOURCE_WALLET[uid] = True
            bot.answer_callback_query(c.id, "Bitte Source-Wallet senden.")
            bot.send_message(uid, "🔑 Sende deine **Absender-Wallet (SOL)**:"); return
        price = get_sol_usd()
        px = f"(1 SOL ≈ {price:.2f} USDC)" if price > 0 else ""
        bot.edit_message_text(
            f"Absender-Wallet: `{md_escape(u['source_wallet'])}`\n"
            f"Sende SOL an: `{md_escape(CENTRAL_SOL_PUBKEY)}`\n{px}\n\n"
            "🔄 Zum Ändern einfach **neue Solana-Adresse** senden.",
            c.message.chat.id, c.message.message_id, parse_mode="Markdown", reply_markup=kb_main(u)
        )
        WAITING_SOURCE_WALLET[uid] = True
        return

    # withdraw (PIN ggf. prüfen – erst Betrag erfragen, PIN vor Option)
    if data == "withdraw":
        if not u["payout_wallet"]:
            WAITING_PAYOUT_WALLET[uid] = True
            bot.answer_callback_query(c.id, "Bitte Payout-Adresse senden.")
            bot.send_message(uid, "🔑 Sende deine **Auszahlungsadresse (SOL)**:"); return
        WAITING_WITHDRAW_AMOUNT[uid] = None
        bot.answer_callback_query(c.id, "Bitte Betrag eingeben.")
        bot.send_message(uid, f"💳 Payout: `{md_escape(u['payout_wallet'])}`\nGib den Betrag in SOL ein (z. B. `0.25`).", parse_mode="Markdown")
        WAITING_PAYOUT_WALLET[uid] = True
        return

    # subscriptions
    if data == "sub_menu":
        bot.edit_message_text("Abonnement-Menü:", c.message.chat.id, c.message.message_id, reply_markup=kb_sub_menu()); return
    if data == "sub_on":
        bal_sol = get_balance_lamports(uid) / LAMPORTS_PER_SOL
        if bal_sol < MIN_SUB_SOL:
            bot.answer_callback_query(c.id, f"Mindestens {MIN_SUB_SOL} SOL nötig."); return
        set_subscription(uid, True)
        bot.answer_callback_query(c.id, "Abo aktiviert")
        bot.send_message(uid, "🔔 Abo aktiv.", reply_markup=kb_main(u)); return
    if data == "sub_off":
        set_subscription(uid, False)
        bot.answer_callback_query(c.id, "Abo beendet")
        bot.send_message(uid, "🔕 Abo beendet.", reply_markup=kb_main(u)); return

    # history
    if data == "history":
        with get_db() as con:
            rows = con.execute("""
                SELECT kind, ref_id, amount_lamports, meta, created_at
                FROM tx_log WHERE user_id=?
                ORDER BY id DESC LIMIT 20
            """, (uid,)).fetchall()
        if not rows:
            bot.answer_callback_query(c.id, "Kein Verlauf.")
            bot.send_message(uid, "📜 Noch keine Einträge."); return
        bot.answer_callback_query(c.id)
        parts = ["📜 *Dein Verlauf (letzte 20)*"]
        for r in rows:
            parts.append(f"• {r['created_at']} • {r['kind']} • {fmt_sol_usdc(int(r['amount_lamports'] or 0))} • {r['meta'] or ''}")
        bot.send_message(uid, "\n".join(parts), parse_mode="Markdown"); return

    # referral
    if data == "referral":
        code = _ensure_user_refcode(uid)
        bot_username = get_bot_username()
        # klickbarer Link
        link_md = _linkify_ref(bot_username, code)
        bot.answer_callback_query(c.id, "Referral")
        bot.send_message(uid, f"Teile deinen Link:\n{link_md}", parse_mode="Markdown", disable_web_page_preview=True, reply_markup=kb_referral()); return

    if data == "ref_stats":
        bot.answer_callback_query(c.id)
        bot.send_message(uid, _ref_stats_text(uid), parse_mode="Markdown"); return

    if data == "ref_claim":
        bot.answer_callback_query(c.id, "Einlösen angefragt")
        # Admins informieren
        for aid in ADMIN_IDS:
            try:
                bot.send_message(int(aid), f"💵 Referral-Auszahlung angefragt von UID {uid}\n{_ref_stats_text(uid)}", parse_mode="Markdown")
            except Exception: pass
        bot.send_message(uid, "✅ Anfrage gesendet. Ein Admin prüft und meldet sich."); return

    # portfolio
    if data == "my_portfolio":
        bal_lam = get_balance_lamports(uid)
        deps_lam = sum_user_deposits(uid)
        delta_lam = bal_lam - deps_lam
        bot.answer_callback_query(c.id, "Portfolio")
        bot.send_message(uid,
                         f"🏦 Guthaben: *{fmt_sol_usdc(bal_lam)}*\n"
                         f"📥 Einzahlungen gesamt: *{fmt_sol_usdc(deps_lam)}*\n"
                         f"Δ seit Start: *{fmt_sol_usdc(delta_lam)}*",
                         parse_mode="Markdown"); return

    # -------- Admin --------
    if data == "admin_menu_big":
        if not is_admin(uid): bot.answer_callback_query(c.id, "Nicht erlaubt."); return
        bot.edit_message_text("🛠️ Admin-Menü — Kontrolle", c.message.chat.id, c.message.message_id, reply_markup=kb_admin_main()); return

    if data == "admin_new_call":
        if not is_admin(uid): return
        bot.answer_callback_query(c.id, "Call erstellen")
        ADMIN_AWAIT_SIMPLE_CALL[uid] = True
        bot.send_message(uid, "Sende den Call:\n• FUTURES|BASE|SIDE|LEV\n• MEME|NAME|TOKEN_ADDRESS", parse_mode=None); return

    if data == "admin_broadcast_last":
        if not is_admin(uid): return
        with get_db() as con:
            row = con.execute("SELECT * FROM calls ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            bot.answer_callback_query(c.id, "Kein Call vorhanden."); return
        msg = "📣 Neuer Call:\n" + fmt_call(row)
        subs = all_subscribers()
        sent = 0
        for su in subs:
            try:
                bot.send_message(su, msg, parse_mode="Markdown")
                queue_execution(int(row["id"]), su, status="QUEUED", message="Queued by broadcast")
                sent += 1
            except Exception:
                pass
        bot.answer_callback_query(c.id, f"An {sent} Abonnenten gesendet."); return

    if data == "admin_list_investors":
        if not is_admin(uid): return
        with get_db() as con:
            rows = con.execute("""
                SELECT user_id, username
                FROM users
                ORDER BY sol_balance_lamports DESC
                LIMIT 50
            """).fetchall()
        if not rows:
            bot.answer_callback_query(c.id, "Keine Nutzer."); return
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "👥 *Investoren (Top 50)*", parse_mode="Markdown")
        for r in rows:
            uname = ("@" + (r["username"] or "")) if r["username"] else "(kein Username)"
            bot.send_message(uid, f"{uname} • UID {r['user_id']}", reply_markup=kb_user_row(int(r["user_id"])))
        return

    if data.startswith("admin_user_"):
        if not is_admin(uid): return
        try:
            target = int(data.split("_", 2)[2])
        except Exception:
            bot.answer_callback_query(c.id, "Ungültig"); return
        tu = get_user(target)
        if not tu:
            bot.answer_callback_query(c.id, "User nicht gefunden."); return
        txt = (f"👤 {('@'+tu['username']) if tu['username'] else 'UID '+str(tu['user_id'])}\n"
               f"Guthaben: {fmt_sol_usdc(int(tu['sol_balance_lamports'] or 0))}\n"
               f"Source: `{md_escape(tu['source_wallet'] or '-')}`\n"
               f"Payout: `{md_escape(tu['payout_wallet'] or '-')}`")
        bot.answer_callback_query(c.id)
        bot.send_message(uid, txt, parse_mode="Markdown", reply_markup=kb_user_actions(target)); return

    if data == "admin_open_payouts":
        if not is_admin(uid): return
        with get_db() as con:
            rows = con.execute("SELECT * FROM payouts WHERE status='REQUESTED' ORDER BY created_at ASC LIMIT 50").fetchall()
        if not rows:
            bot.answer_callback_query(c.id, "Keine offenen Auszahlungen."); return
        bot.answer_callback_query(c.id)
        for r in rows:
            pid = int(r["id"]); uline = get_user(int(r["user_id"]))
            uname = ("@" + (uline["username"] or "")) if (uline and uline["username"]) else f"UID {r['user_id']}"
            bot.send_message(uid,
                             f"#{pid} • {uname}\nBetrag: {fmt_sol_usdc(int(r['amount_lamports'] or 0))}\n"
                             f"Lockup {int(r['lockup_days'] or 0)}d • Fee {float(r['fee_percent'] or 0):.2f}%",
                             reply_markup=kb_payout_manage(pid))
        return

    if data == "admin_stats":
        if not is_admin(uid): return
        with get_db() as con:
            users_total = con.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            subs_total = con.execute("SELECT COUNT(*) AS c FROM users WHERE sub_active=1").fetchone()["c"]
        deposits = sum_total_deposits(); balances = sum_total_balances(); open_p = sum_open_payouts()
        bot.answer_callback_query(c.id)
        bot.send_message(uid,
                         "📊 *System-Stats*\n"
                         f"👥 Nutzer gesamt: *{users_total}*\n"
                         f"🔔 Abos aktiv: *{subs_total}*\n"
                         f"📥 Einzahlungen gesamt: *{fmt_sol_usdc(deposits)}*\n"
                         f"🏦 Gesamtguthaben: *{fmt_sol_usdc(balances)}*\n"
                         f"🧾 Offene Auszahlungen: *{fmt_sol_usdc(open_p)}*",
                         parse_mode="Markdown"); return

    if data == "admin_broadcast_all":
        if not is_admin(uid): return
        ADMIN_AWAIT_NEWS_BROADCAST[uid] = {"step": "await_text_to_all"}
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Sende die Nachricht, die an **alle Nutzer** gehen soll.", parse_mode="Markdown"); return

    if data == "admin_apply_pnl":
        if not is_admin(uid):
            bot.answer_callback_query(c.id, "Nicht erlaubt."); return
        ADMIN_AWAIT_MASS_BALANCE[uid] = True
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Sende z. B.: `ALL -40%` / `PROMO PERCENT 20 ALL` / `PNL <CALL_ID> 20`", parse_mode="Markdown"); return

    # payout option chosen (PIN prüfen falls gesetzt)
    if data.startswith("payoutopt_"):
        # PIN nötig?
        u = get_user(uid)
        if u and u.get("pin_hash"):
            # Merke nächste Aktion, frage PIN ab
            AWAITING_PIN[uid] = {"for": "withdraw_option", "data": data}
            bot.answer_callback_query(c.id, "PIN erforderlich.")
            bot.send_message(uid, "🔐 Bitte sende deine PIN, um fortzufahren."); return

        return _do_payout_option(uid, c)

    # admin payout manage
    if data.startswith("payout_"):
        if not is_admin(uid):
            bot.answer_callback_query(c.id, "Nicht erlaubt."); return
        action, pid_s = data.split("_", 2)[1:]
        try: pid = int(pid_s)
        except: bot.answer_callback_query(c.id, "Ungültige ID."); return
        with get_db() as con:
            row = con.execute("SELECT * FROM payouts WHERE id=?", (pid,)).fetchone()
        if not row:
            bot.answer_callback_query(c.id, "Anfrage nicht gefunden."); return
        tgt_uid = int(row["user_id"]); amt = int(row["amount_lamports"]); days = int(row["lockup_days"]); fee_percent = float(row["fee_percent"])
        fee_lam = int(round(amt * (fee_percent/100.0))); net_lam = amt - fee_lam
        if action == "APPROVE":
            with get_db() as con: con.execute("UPDATE payouts SET status='APPROVED' WHERE id=?", (pid,))
            bot.answer_callback_query(c.id, "Genehmigt.")
            bot.send_message(tgt_uid, f"✅ Deine Auszahlung #{pid} wurde *genehmigt*.", parse_mode="Markdown"); return
        if action == "SENT":
            with get_db() as con: con.execute("UPDATE payouts SET status='SENT' WHERE id=?", (pid,))
            log_tx(tgt_uid, "WITHDRAW_SENT", amt, ref_id=str(pid), meta=f"net {net_lam} (fee {fee_percent:.2f}%)")
            bot.answer_callback_query(c.id, "Als gesendet markiert.")
            bot.send_message(tgt_uid, f"📤 Auszahlung #{pid} *gesendet*.\nBetrag: {fmt_sol_usdc(amt)}\nGebühr: {fee_percent:.2f}% ({fmt_sol_usdc(fee_lam)})\nNetto: {fmt_sol_usdc(net_lam)}", parse_mode="Markdown"); return
        if action == "REJECT":
            with get_db() as con: con.execute("UPDATE payouts SET status='REJECTED' WHERE id=?", (pid,))
            add_balance(tgt_uid, amt); log_tx(tgt_uid, "ADJ", amt, ref_id=str(pid), meta="payout rejected refund")
            bot.answer_callback_query(c.id, "Abgelehnt & erstattet.")
            bot.send_message(tgt_uid, f"❌ Auszahlung #{pid} *abgelehnt*. Betrag erstattet.", parse_mode="Markdown"); return

    bot.answer_callback_query(c.id, "")

def _do_payout_option(uid: int, c: CallbackQuery):
    # eigentliche Option ausführen
    try:
        days = int((c.data or "").split("_", 1)[1])
    except Exception:
        bot.answer_callback_query(c.id, "Ungültige Auswahl."); return
    fee_percent = float(_fee_tiers.get(days, 0.0))
    pending = WAITING_WITHDRAW_AMOUNT.get(uid, None)
    if pending is None or pending <= 0:
        bot.answer_callback_query(c.id, "Keine ausstehende Auszahlung. Betrag zuerst eingeben."); return
    amount_lam = int(pending)
    if not subtract_balance(uid, amount_lam):
        bot.answer_callback_query(c.id, "Unzureichendes Guthaben.")
        WAITING_WITHDRAW_AMOUNT.pop(uid, None); return
    with get_db() as con:
        cur = con.execute(
            "INSERT INTO payouts(user_id, amount_lamports, status, note, lockup_days, fee_percent) VALUES (?,?,?,?,?,?)",
            (uid, amount_lam, "REQUESTED", f"({days}d, fee {fee_percent}%)", days, fee_percent))
        pid = cur.lastrowid
    WAITING_WITHDRAW_AMOUNT.pop(uid, None)
    fee_lam = int(round(amount_lam * (fee_percent / 100.0))); net_lam = amount_lam - fee_lam
    log_tx(uid, "WITHDRAW_REQ", amount_lam, ref_id=str(pid), meta=f"lockup {days}d fee {fee_percent:.2f}% net {net_lam}")
    bot.answer_callback_query(c.id, "Auszahlung angefragt.")
    bot.send_message(uid,
        "💸 *Auszahlung angefragt*\n"
        f"Betrag: {fmt_sol_usdc(amount_lam)}\n"
        f"Lockup: {days} Tage\n"
        f"Gebühr: {fee_percent:.2f}% ({fmt_sol_usdc(fee_lam)})\n"
        f"Netto: {fmt_sol_usdc(net_lam)}",
        parse_mode="Markdown")
    for aid in ADMIN_IDS:
        try:
            bot.send_message(int(aid),
                             f"🧾 Auszahlung #{pid}\nUser: {uid}\nBetrag: {fmt_sol_usdc(amount_lam)}\nLockup: {days}d • Fee: {fee_percent:.2f}%\nNetto: {fmt_sol_usdc(net_lam)}",
                             reply_markup=kb_payout_manage(pid))
        except Exception: pass

# ---------------------------
# Messages
# ---------------------------
@bot.message_handler(func=lambda m: True)
def catch_all(m: Message):
    uid = m.from_user.id
    text = (m.text or "").strip() if m.text else ""

    # Support
    if SUPPORT_AWAIT_MSG.get(uid):
        SUPPORT_AWAIT_MSG.pop(uid, None)
        name = ("@" + (m.from_user.username or "")) if m.from_user.username else f"UID {uid}"
        for aid in ADMIN_IDS:
            try:
                if m.photo:
                    bot.send_photo(int(aid), m.photo[-1].file_id, caption=f"[Support von {name} ({uid})] {m.caption or ''}")
                else:
                    bot.send_message(int(aid), f"[Support von {name} ({uid})] {text}", parse_mode=None)
            except Exception: pass
        bot.reply_to(m, "✅ Deine Support-Nachricht wurde an die Admins gesendet."); return

    # PIN erwartet?
    if AWAITING_PIN.get(uid):
        entry = AWAITING_PIN.pop(uid)
        pin = text
        u = get_user(uid)
        if not (u and u.get("pin_hash") and _hash_pin(pin) == u["pin_hash"]):
            bot.reply_to(m, "❌ Falsche PIN."); return
        # Dispatch
        if entry["for"] == "withdraw_option":
            # Callback-Dummy bauen
            class _DummyC: pass
            dummy = _DummyC(); dummy.data = entry["data"]; dummy.message = m; dummy.id = "pin-ok"
            return _do_payout_option(uid, dummy)
        if entry["for"] == "setwallet":
            # next enthält ("SRC"/"PAY", addr)
            which, addr = entry["next"]
            if which == "SRC":
                set_source_wallet(uid, addr)
                bot.reply_to(m, f"✅ Source-Wallet gespeichert: `{md_escape(addr)}`", parse_mode="Markdown")
            else:
                set_payout_wallet(uid, addr)
                bot.reply_to(m, f"✅ Payout-Wallet gespeichert: `{md_escape(addr)}`", parse_mode="Markdown")
            return

    # Admin: Set wallet eines anderen Users
    if ADMIN_AWAIT_SET_WALLET.get(uid):
        target = ADMIN_AWAIT_SET_WALLET.pop(uid)
        if not is_admin(uid):
            bot.reply_to(m, "Nicht erlaubt."); return
        parts = text.split(None, 1)
        if len(parts) != 2:
            bot.reply_to(m, "Format: `SRC <adresse>` oder `PAY <adresse>`", parse_mode="Markdown"); return
        which, addr = parts[0].upper(), parts[1].strip()
        if not is_probably_solana_address(addr):
            bot.reply_to(m, "Ungültige Solana-Adresse."); return
        if which == "SRC":
            set_source_wallet(target, addr)
            bot.reply_to(m, f"✅ Source-Wallet für UID {target} gesetzt: `{md_escape(addr)}`", parse_mode="Markdown")
        elif which == "PAY":
            set_payout_wallet(target, addr)
            bot.reply_to(m, f"✅ Payout-Wallet für UID {target} gesetzt: `{md_escape(addr)}`", parse_mode="Markdown")
        else:
            bot.reply_to(m, "Nutze `SRC` oder `PAY`.", parse_mode="Markdown")
        return

    # User: Wallet Eingaben (mit PIN, falls gesetzt)
    if WAITING_SOURCE_WALLET.get(uid, False):
        if is_probably_solana_address(text):
            u = get_user(uid)
            if u and u.get("pin_hash"):
                AWAITING_PIN[uid] = {"for": "setwallet", "next": ("SRC", text)}
                bot.reply_to(m, "🔐 Bitte PIN senden, um Source-Wallet zu ändern."); return
            WAITING_SOURCE_WALLET[uid] = False
            set_source_wallet(uid, text)
            price = get_sol_usd(); px = f"(1 SOL ≈ {price:.2f} USDC)" if price > 0 else ""
            bot.reply_to(m, f"✅ Absender-Wallet gespeichert.\nSende SOL von `{md_escape(text)}` an `{md_escape(CENTRAL_SOL_PUBKEY)}`\n{px}", parse_mode="Markdown")
            return
    if WAITING_PAYOUT_WALLET.get(uid, False):
        if is_probably_solana_address(text):
            u = get_user(uid)
            if u and u.get("pin_hash"):
                AWAITING_PIN[uid] = {"for": "setwallet", "next": ("PAY", text)}
                bot.reply_to(m, "🔐 Bitte PIN senden, um Payout-Wallet zu ändern."); return
            WAITING_PAYOUT_WALLET[uid] = False
            set_payout_wallet(uid, text)
            bot.reply_to(m, f"✅ Auszahlungsadresse gespeichert: `{md_escape(text)}`\nGib nun den Betrag in SOL ein (z. B. 0.25).", parse_mode="Markdown")
            WAITING_WITHDRAW_AMOUNT[uid] = None
            return

    # Admin: create call
    if ADMIN_AWAIT_SIMPLE_CALL.get(uid, False):
        ADMIN_AWAIT_SIMPLE_CALL[uid] = False
        if not is_admin(uid):
            bot.reply_to(m, "Nicht erlaubt."); return
        parts = [p.strip() for p in (text or "").split("|")]
        if len(parts) < 2:
            bot.reply_to(m, "Formatfehler."); return
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
            bot.reply_to(m, "Formatfehler.")
        return

    # Admin: balance edit (per-user)
    if ADMIN_AWAIT_BALANCE_SINGLE.get(uid) is not None:
        target = ADMIN_AWAIT_BALANCE_SINGLE.pop(uid)
        if not is_admin(uid): bot.reply_to(m, "Nicht erlaubt."); return
        try:
            txt = text.replace(" ", "")
            if txt.endswith("%"):
                pct = float(txt[:-1].replace(",", "."))
                old = get_balance_lamports(target)
                new = int(round(old * (1 + pct/100.0)))
                set_balance(target, new)
                log_tx(target, "ADJ", new - old, meta=f"admin {pct:+.2f}%")
                bot.reply_to(m, f"✅ UID {target}: {fmt_sol_usdc(old)} → {fmt_sol_usdc(new)} ({pct:+.2f}%)")
            else:
                sol = float(text.replace(",", "."))
                lam = int(sol * LAMPORTS_PER_SOL)
                old = get_balance_lamports(target)
                set_balance(target, lam)
                log_tx(target, "ADJ", lam - old, meta="admin set")
                bot.reply_to(m, f"✅ Guthaben gesetzt: UID {target} {fmt_sol_usdc(lam)}")
        except Exception:
            bot.reply_to(m, "Bitte Zahl (z. B. `0.25`) oder Prozent (z. B. `-40%`) senden.")
        return

    # Admin: balance edit global
    if ADMIN_AWAIT_BALANCE_GLOBAL.get(uid, False):
        ADMIN_AWAIT_BALANCE_GLOBAL[uid] = False
        if not is_admin(uid): bot.reply_to(m, "Nicht erlaubt."); return
        toks = text.split()
        if len(toks) < 2:
            bot.reply_to(m, "Format: `UID 12345 0.25` oder `@username -40%`", parse_mode="Markdown"); return
        target_id: Optional[int] = None; value_token: Optional[str] = None
        if toks[0].upper() == "UID":
            if len(toks) < 3: bot.reply_to(m, "Format: `UID 12345 0.25`", parse_mode="Markdown"); return
            try: target_id = int(toks[1])
            except: bot.reply_to(m, "Ungültige UID.", parse_mode="Markdown"); return
            value_token = toks[2]
        elif toks[0].startswith("@"):
            username = toks[0][1:]
            with get_db() as con:
                r = con.execute("SELECT user_id FROM users WHERE username=?", (username,)).fetchone()
            if not r: bot.reply_to(m, "User nicht gefunden.", parse_mode="Markdown"); return
            target_id = int(r["user_id"]); value_token = toks[1]
        else:
            bot.reply_to(m, "`UID <id>` oder `@username` zuerst.", parse_mode="Markdown"); return
        try:
            t = value_token.replace(" ", "")
            if t.endswith("%"):
                pct = float(t[:-1].replace(",", "."))
                old = get_balance_lamports(target_id)
                new = int(round(old * (1 + pct/100.0)))
                set_balance(target_id, new)
                log_tx(target_id, "ADJ", new - old, meta=f"admin {pct:+.2f}%")
                bot.reply_to(m, f"✅ UID {target_id}: {fmt_sol_usdc(old)} → {fmt_sol_usdc(new)} ({pct:+.2f}%)")
            else:
                sol = float(t.replace(",", "."))
                lam = int(sol * LAMPORTS_PER_SOL)
                old = get_balance_lamports(target_id)
                set_balance(target_id, lam)
                log_tx(target_id, "ADJ", lam - old, meta="admin set")
                bot.reply_to(m, f"✅ Guthaben gesetzt: UID {target_id} {fmt_sol_usdc(lam)}")
        except Exception:
            bot.reply_to(m, "Bitte Zahl (z. B. `0.25`) oder Prozent (z. B. `-40%`) senden.")
        return

    # Admin: mass ops / promo / pnl
    if ADMIN_AWAIT_MASS_BALANCE.get(uid, False):
        ADMIN_AWAIT_MASS_BALANCE[uid] = False
        if not is_admin(uid): bot.reply_to(m, "Nicht erlaubt."); return
        cmd = text.strip()
        try:
            if cmd.upper().startswith("ALL"):
                parts = cmd.split()
                if len(parts) != 2 or not parts[1].endswith("%"):
                    bot.reply_to(m, "Format: `ALL -40%` / `ALL +25%`"); return
                pct = float(parts[1][:-1].replace(",", "."))
                ids = all_users(); changed = 0
                for uid_t in ids:
                    old = get_balance_lamports(uid_t)
                    new = int(round(old * (1 + pct/100.0)))
                    set_balance(uid_t, new); log_tx(uid_t, "ADJ", new - old, meta=f"mass {pct:+.2f}%")
                    changed += 1
                bot.reply_to(m, f"✅ Massenänderung: {changed} Nutzer ({pct:+.2f}%)."); return

            toks = cmd.split(); verb = toks[0].upper()
            if verb == "PROMO":
                typ = toks[1].upper(); val = float(toks[2]); scope = toks[3].upper() if len(toks) > 3 else "ALL"
                with get_db() as con:
                    if scope == "ALL": rows = con.execute("SELECT user_id FROM users").fetchall()
                    elif scope == "SUBSCRIBERS": rows = con.execute("SELECT user_id FROM users WHERE sub_active=1").fetchall()
                    else: rows = []
                affected = 0
                for r in rows:
                    uid_t = int(r["user_id"])
                    if typ == "PERCENT":
                        bal = get_balance_lamports(uid_t)
                        delta = int(bal * (val / 100.0))
                        add_balance(uid_t, delta); log_tx(uid_t, "ADJ", delta, meta=f"promo {val:.2f}%")
                    else:
                        lam = int(val * LAMPORTS_PER_SOL)
                        add_balance(uid_t, lam); log_tx(uid_t, "ADJ", lam, meta=f"promo {val} SOL")
                    affected += 1
                bot.reply_to(m, f"✅ PROMO auf {affected} Nutzer."); return

            if verb == "PNL":
                call_id = int(toks[1]); percent = float(toks[2]); affected = 0
                with get_db() as con:
                    execs = con.execute("SELECT user_id, stake_lamports FROM executions WHERE call_id=? AND status IN ('FILLED','QUEUED')", (call_id,)).fetchall()
                for ex in execs:
                    uid_t = int(ex["user_id"]); stake = int(ex["stake_lamports"] or 0)
                    u = get_user(uid_t); risk = (u["auto_risk"] or "MEDIUM")
                    frac = _risk_fraction(risk)
                    pnl_lam = int(stake * (percent / 100.0) * frac)
                    add_balance(uid_t, pnl_lam); log_tx(uid_t, "PNL", pnl_lam, ref_id=str(call_id), meta=f"{percent:+.2f}% * {frac:.2f}")
                    affected += 1
                bot.reply_to(m, f"✅ PNL (Call {call_id}) auf {affected} Nutzer."); return

            bot.reply_to(m, "Unbekannt. Beispiele: `ALL -40%`, `PROMO PERCENT 20 ALL`, `PNL 12 15`")
        except Exception as e:
            bot.reply_to(m, f"Fehler: {e}")
        return

    # Admin: Broadcast to ALL
    if ADMIN_AWAIT_NEWS_BROADCAST.get(uid):
        ctx = ADMIN_AWAIT_NEWS_BROADCAST.pop(uid, None)
        if ctx and ctx.get("step") == "await_text_to_all":
            msg = text; ids = all_users(); sent = 0
            for t in ids:
                try:
                    if m.photo:
                        bot.send_photo(t, m.photo[-1].file_id, caption=msg)
                    else:
                        bot.send_message(t, msg, parse_mode="Markdown")
                    sent += 1
                except Exception: pass
            bot.reply_to(m, f"✅ Broadcast an {sent} Nutzer gesendet."); return

    # Withdraw amount entry
    if WAITING_WITHDRAW_AMOUNT.get(uid) is None:
        if is_probably_solana_address(text):
            u = get_user(uid)
            if u and u.get("pin_hash"):
                AWAITING_PIN[uid] = {"for": "setwallet", "next": ("PAY", text)}
                bot.reply_to(m, "🔐 Bitte PIN senden, um Payout-Wallet zu ändern."); return
            set_payout_wallet(uid, text)
            bot.reply_to(m, f"✅ Payout aktualisiert: `{md_escape(text)}`\nGib nun den Betrag in SOL ein (z. B. 0.25).", parse_mode="Markdown"); return
        try:
            sol = float(text.replace(",", "."))
            if sol <= 0: bot.reply_to(m, "Betrag muss > 0 sein."); return
            lam = int(sol * LAMPORTS_PER_SOL)
            if get_balance_lamports(uid) < lam:
                bot.reply_to(m, f"Unzureichendes Guthaben. Verfügbar: {fmt_sol_usdc(get_balance_lamports(uid))}")
                WAITING_WITHDRAW_AMOUNT.pop(uid, None); return
            WAITING_WITHDRAW_AMOUNT[uid] = lam
            bot.reply_to(m, f"Auszahlung: {fmt_sol_usdc(lam)} — Wähle Lockup & Fee:", reply_markup=kb_withdraw_options())
        except Exception:
            bot.reply_to(m, "Bitte eine gültige Zahl eingeben, z. B. `0.25`.")
        return

    # Default
    bot.reply_to(m, "Ich habe das nicht verstanden. Nutze das Menü.", reply_markup=kb_main(get_user(uid)))

# ---------------------------
# Background loops
# ---------------------------
def auto_executor_loop():
    while True:
        try:
            with get_db() as con:
                rows = con.execute("""
                    SELECT e.id as eid, e.user_id, e.call_id, e.status, u.auto_mode, u.auto_risk, u.sol_balance_lamports, e.stake_lamports
                    FROM executions e
                    JOIN users u ON u.user_id = e.user_id
                    WHERE e.status='QUEUED'
                    LIMIT 200
                """).fetchall()
            for r in rows:
                if (r["auto_mode"] or "OFF") != "ON":
                    with get_db() as con:
                        con.execute("UPDATE executions SET status='ERROR', message='Auto OFF' WHERE id=?", (r["eid"],))
                    continue
                call = get_call(int(r["call_id"]))
                stake_info = int(r["stake_lamports"] or _compute_stake_for_user(int(r["user_id"])))
                if SIMULATION_MODE:
                    if call["market_type"] == "FUTURES":
                        result = futures_place_simulated(int(r["user_id"]), call["base"], (call.get("side") or ""), (call.get("leverage") or ""), (r["auto_risk"] or "MEDIUM"))
                    else:
                        result = dex_market_buy_simulated(int(r["user_id"]), call["base"], stake_info)
                else:
                    result = {"status": "FILLED", "txid": "LIVE-TX-REPLACE"}
                status = result.get("status") or "FILLED"
                txid = result.get("txid") or result.get("order_id") or ""
                with get_db() as con:
                    con.execute("UPDATE executions SET status=?, txid=?, message=? WHERE id=?", (status, txid, "JOINED", r["eid"]))
                try:
                    bot.send_message(int(r["user_id"]),
                                     f"🤖 Auto-Entry • {r['auto_risk'] or 'MEDIUM'}\n"
                                     f"{fmt_call(call)}\n"
                                     f"Status: *{status}*\n"
                                     f"Einsatz (Info): {fmt_sol_usdc(stake_info)}\n"
                                     f"`{md_escape(txid)}`",
                                     parse_mode="Markdown")
                except Exception:
                    pass
        except Exception as e:
            print("executor loop error:", e)
        time.sleep(5)

def payout_reminder_loop():
    while True:
        try:
            with get_db() as con:
                rows = con.execute("""
                    SELECT id, amount_lamports FROM payouts
                    WHERE status='REQUESTED'
                      AND (last_notified_at IS NULL OR (strftime('%s','now') - strftime('%s',COALESCE(last_notified_at,'1970-01-01')) > 3600))
                    ORDER BY created_at ASC
                """).fetchall()
            for r in rows:
                for aid in ADMIN_IDS:
                    try:
                        bot.send_message(int(aid), f"⏰ Erinnerung (offene Auszahlung) #{r['id']} • Betrag {fmt_sol_usdc(int(r['amount_lamports'] or 0))}", reply_markup=kb_payout_manage(int(r["id"])))
                    except Exception: pass
                with get_db() as con:
                    con.execute("UPDATE payouts SET last_notified_at=CURRENT_TIMESTAMP WHERE id=?", (int(r["id"]),))
            time.sleep(3600)  # stündlich
        except Exception as e:
            print("payout reminder loop error:", e)
            time.sleep(3600)

threading.Thread(target=auto_executor_loop, daemon=True).start()
threading.Thread(target=payout_reminder_loop, daemon=True).start()

print("Bot läuft — enhanced. SIMULATION_MODE =", SIMULATION_MODE)
bot.infinity_polling(timeout=60, long_polling_timeout=60)