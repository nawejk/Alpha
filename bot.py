# bot.py
# UTF-8  — Whales Alpha Memecoin Bot (MEME-only)
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

# ---- Solana/Jupiter (LIVE) ----
from solana.rpc.api import Client as SolClient
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Confirmed
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solders.message import MessageV0
from solders.system_program import transfer, TransferParams

# ---------------------------
# Configuration (ENV)
# ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8271980014:AAGCQQEgAgJhtJ6z7XbrGCM4uNa08dmrapM").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable required")

ADMIN_IDS = [a.strip() for a in os.getenv("ADMIN_IDS", "8076025426").split(",") if a.strip()]
SOLANA_RPC = os.getenv("SOLANA_RPC", "https://api.mainnet-beta.solana.com").strip()
CENTRAL_SOL_PUBKEY = os.getenv("CENTRAL_SOL_PUBKEY", "CAYNcLuq8Jybk8GV1e7nCKGK6msAw8ktKSaXDqqBc3f4").strip()
CENTRAL_SOL_SECRET = os.getenv("CENTRAL_SOL_SECRET", "4Wqy1bdxNqvVfdxcoiiXNLQwYLbg4zuPaKYPzR5V5CAf3HxcaCbTbcWxrBi93nKK1Um69YGJZp5o26zPwqFKVq5Q").strip()  # Base58 Private Key
JUPITER_BASE = os.getenv("JUPITER_BASE", "https://quote-api.jup.ag").rstrip("/")

if not CENTRAL_SOL_SECRET:
    raise RuntimeError("CENTRAL_SOL_SECRET (Base58 Private Key) is required")

# Liste bekannter Exchange-Absender (optional, CSV in ENV)
EXCHANGE_WALLETS = set([s.strip() for s in os.getenv("EXCHANGE_WALLETS", "").split(",") if s.strip()])

# Withdraw fee tiers (lockup_days: fee_percent)
# 0 Tage = Sofort (20%), 5 Tage = 15%, 7 Tage = 10%, 10 Tage = 5%
DEFAULT_FEE_Tiers = {0: 20.0, 5: 15.0, 7: 10.0, 10: 5.0}
_fee_tiers: Dict[int, float] = {}
raw_tiers = os.getenv("WITHDRAW_FEE_TIERS", "")
if raw_tiers:
    try:
        for part in raw_tiers.split(","):
            d, p = part.split(":")
            _fee_tiers[int(d)] = float(p)
    except Exception:
        _fee_tiers = DEFAULT_FEE_Tiers.copy()
else:
    _fee_tiers = DEFAULT_FEE_Tiers.copy()

DB_PATH = os.getenv("DB_PATH", "memebot_full.db")
LAMPORTS_PER_SOL = 1_000_000_000
MIN_SUB_SOL = float(os.getenv("MIN_SUB_SOL", "0.1"))

# Simulation flag – jetzt egal: wir handeln live
SIMULATION_MODE = False

# ---------------------------
# Utilities: price + formatting
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
  fee_percent REAL DEFAULT 0.0,
  tx_sig TEXT
);
CREATE TABLE IF NOT EXISTS news (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT,
  body TEXT,
  image_url TEXT,
  category TEXT,
  created_by INTEGER,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        for stmt in [
            "ALTER TABLE users ADD COLUMN sub_types TEXT DEFAULT ''",
            "ALTER TABLE executions ADD COLUMN stake_lamports INTEGER DEFAULT 0",
            "ALTER TABLE payouts ADD COLUMN lockup_days INTEGER DEFAULT 0",
            "ALTER TABLE payouts ADD COLUMN fee_percent REAL DEFAULT 0.0",
            "ALTER TABLE users ADD COLUMN referral_code TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN referral_bonus_claimed INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN payout_wallet TEXT",
            "ALTER TABLE payouts ADD COLUMN tx_sig TEXT"
        ]:
            try: 
                con.execute(stmt)
            except Exception: 
                pass

# ---------------------------
# Misc helpers
# ---------------------------
def md_escape(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return (text.replace('\\', '\\\\')
                .replace('_', '\\_')
                .replace('*', '\\*')
                .replace('`', '\\`')
                .replace('[', '\\['))

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

def is_admin(user_id: int) -> bool:
    return str(user_id) in ADMIN_IDS

def is_probably_solana_address(addr: str) -> bool:
    if not isinstance(addr, str): return False
    addr = addr.strip()
    if len(addr) < 32 or len(addr) > 44: return False
    allowed = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    return all(ch in allowed for ch in addr)

def gen_referral_for_user(user_id: int) -> str:
    import hashlib
    h = hashlib.sha1(str(user_id).encode()).hexdigest()[:8]
    return f"REF{h.upper()}"

# ---------------------------
# CRUD & business logic
# ---------------------------
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

def set_subscription(user_id: int, active: bool):
    with get_db() as con:
        con.execute("UPDATE users SET sub_active=? WHERE user_id=?", (1 if active else 0, user_id))

def set_subscription_types(user_id: int, types: List[str]):
    st = ",".join(sorted(set([t.upper() for t in types if t])))
    with get_db() as con:
        con.execute("UPDATE users SET sub_types=? WHERE user_id=?", (st, user_id))

def get_subscription_types(user_id: int) -> List[str]:
    u = get_user(user_id)
    if not u: return []
    st = row_get(u, "sub_types", "")
    if not st: return []
    return [s for s in st.split(",") if s]

def set_auto_mode(user_id: int, mode: str):
    with get_db() as con:
        con.execute("UPDATE users SET auto_mode=? WHERE user_id=?", (mode, user_id))

def set_auto_risk(user_id: int, risk: str):
    with get_db() as con:
        con.execute("UPDATE users SET auto_risk=? WHERE user_id=?", (risk, user_id))

def set_source_wallet(user_id: int, wallet: str):
    with get_db() as con:
        con.execute("UPDATE users SET source_wallet=? WHERE user_id=?", (wallet, user_id))

def set_payout_wallet(user_id: int, wallet: str):
    with get_db() as con:
        con.execute("UPDATE users SET payout_wallet=? WHERE user_id=?", (wallet, user_id))

def set_referral(user_id: int, code: str):
    with get_db() as con:
        con.execute("UPDATE users SET referral_code=? WHERE user_id=?", (code, user_id))

def mark_referral_claimed(user_id: int):
    with get_db() as con:
        con.execute("UPDATE users SET referral_bonus_claimed=1 WHERE user_id=?", (user_id,))

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

def subtract_balance(user_id: int, lamports: int) -> bool:
    with get_db() as con:
        row = con.execute("SELECT sol_balance_lamports FROM users WHERE user_id=?", (user_id,)).fetchone()
        cur = row_get(row, "sol_balance_lamports", 0)
        if cur < lamports:
            return False
        con.execute("UPDATE users SET sol_balance_lamports = sol_balance_lamports - ? WHERE user_id=?", (lamports, user_id))
        return True

def list_investors(limit: int = 50, offset: int = 0):
    with get_db() as con:
        return con.execute("""
            SELECT user_id, username, sol_balance_lamports, source_wallet, sub_active, sub_types
            FROM users
            WHERE sub_active=1
            ORDER BY sol_balance_lamports DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()

def all_subscribers():
    with get_db() as con:
        return [r["user_id"] for r in con.execute("SELECT user_id FROM users WHERE sub_active=1").fetchall()]

# ---------------------------
# Calls & executions (MEME-only)
# ---------------------------
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

def _risk_fraction(risk: str) -> float:
    return {"LOW": 0.20, "MEDIUM": 0.35, "HIGH": 0.65}.get((risk or "").upper(), 0.35)

def _compute_stake_for_user(user_id: int) -> int:
    u = get_user(user_id)
    if not u: return 0
    frac = _risk_fraction(row_get(u, "auto_risk", "MEDIUM"))
    bal = row_get(u, "sol_balance_lamports", 0)
    return max(int(bal * frac), int(0.01 * LAMPORTS_PER_SOL))

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

# ---------------------------
# Keyboards
# ---------------------------
def kb_main(u):
    bal = fmt_sol_usdc(row_get(u, "sol_balance_lamports", 0))
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💸 Einzahlen", callback_data="deposit"),
           InlineKeyboardButton("💳 Auszahlung", callback_data="withdraw"))
    kb.add(InlineKeyboardButton("🔔 Meme-Signale", callback_data="sub_menu"))
    kb.add(InlineKeyboardButton("⚙️ Auto-Entry", callback_data="auto_menu"),
           InlineKeyboardButton("❓ Hilfe", callback_data="help"))
    kb.add(InlineKeyboardButton("🔗 Referral", callback_data="referral"),
           InlineKeyboardButton("📈 Mein Portfolio", callback_data="my_portfolio"))
    if is_admin(row_get(u, "user_id", 0)):
        kb.add(InlineKeyboardButton("🛠️ Admin (Kontrolle)", callback_data="admin_menu_big"))
    kb.add(InlineKeyboardButton(f"🏦 Guthaben: {bal}", callback_data="noop"))
    return kb

def kb_sub_menu():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔔 Meme-News an", callback_data="sub_on"),
           InlineKeyboardButton("🔕 Meme-News aus", callback_data="sub_off"))
    kb.add(InlineKeyboardButton("⬅️ Zurück", callback_data="back_home"))
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

def kb_admin_main(page: int = 0):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("➕ Meme-Call erstellen", callback_data="admin_new_call"))
    kb.add(InlineKeyboardButton("📣 Broadcast letzten Call", callback_data="admin_broadcast_last"))
    kb.add(InlineKeyboardButton("👥 Investoren", callback_data="admin_list_investors"))
    kb.add(InlineKeyboardButton("👀 Nutzer verwalten", callback_data=f"admin_view_users_{page}"))
    kb.add(InlineKeyboardButton("💼 Guthaben ändern", callback_data="admin_balance_edit"))
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
    kb.add(InlineKeyboardButton("💰 Guthaben ändern", callback_data=f"admin_balance_{user_id}"),
           InlineKeyboardButton("🏷️ Wallet setzen", callback_data=f"admin_setwallet_{user_id}"))
    kb.add(InlineKeyboardButton("📤 Nachricht", callback_data=f"admin_msg_{user_id}"),
           InlineKeyboardButton("🧾 Payouts", callback_data=f"admin_payouts_{user_id}"))
    return kb

def kb_withdraw_options():
    kb = InlineKeyboardMarkup()
    tiers = sorted(parse_fee_tiers(), key=lambda x: x[0])
    for days, pct in tiers:
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

# ---------------------------
# Solana client & central wallet
# ---------------------------
sol = SolClient(SOLANA_RPC, commitment=Confirmed)
CENTRAL_KP = Keypair.from_base58_string(CENTRAL_SOL_SECRET)
CENTRAL_PUB = CENTRAL_KP.pubkey()

# Jupiter helpers
WSOL_MINT = "So11111111111111111111111111111111111111112"

def jup_quote(input_mint: str, output_mint: str, amount_in_lamports: int) -> dict:
    url = f"{JUPITER_BASE}/v6/quote"
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": amount_in_lamports,
        "slippageBps": 150,  # 1.5%
        "platformFeeBps": 0
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

def jup_swap(quote_response: dict, user_pubkey: str) -> str:
    url = f"{JUPITER_BASE}/v6/swap"
    payload = {
        "quoteResponse": quote_response,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True,
        "prioritizationFeeLamports": "auto",
    }
    r = requests.post(url, json=payload, timeout=25)
    r.raise_for_status()
    swap_tx_b64 = r.json()["swapTransaction"]
    raw = base64.b64decode(swap_tx_b64)
    tx = VersionedTransaction.deserialize(raw)
    # Signieren mit zentralem Key
    tx = VersionedTransaction(tx.message, [CENTRAL_KP])
    sig = sol.send_raw_transaction(bytes(tx), opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"))
    return sig.value

def dex_market_buy_live(token_mint: str, amount_lamports: int) -> Tuple[str, str]:
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

def send_sol(to_pubkey: str, lamports: int) -> str:
    to = Pubkey.from_string(to_pubkey)
    ix = transfer(TransferParams(from_pubkey=CENTRAL_PUB, to_pubkey=to, lamports=lamports))
    bh = sol.get_latest_blockhash().value.blockhash
    msg = MessageV0.try_compile(
        payer=CENTRAL_PUB,
        instructions=[ix],
        address_lookup_tables=[],
        recent_blockhash=bh,
    )
    tx = VersionedTransaction(msg, [CENTRAL_KP])
    resp = sol.send_raw_transaction(bytes(tx), opts=TxOpts(skip_preflight=False, preflight_commitment="confirmed"))
    return resp.value

# ---------------------------
# RPC watcher (backoff) — Einzahlungen
# ---------------------------
checked_signatures = set()

def rpc(method: str, params: list, *, _retries=2, _base_sleep=0.8):
    for attempt in range(_retries + 1):
        try:
            r = requests.post(SOLANA_RPC, json={"jsonrpc":"2.0","id":1,"method":method,"params":params}, timeout=10)
            if r.status_code == 429:
                sleep_s = _base_sleep * (2 ** attempt) + random.uniform(0, 0.4)
                time.sleep(sleep_s); continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt < _retries:
                sleep_s = _base_sleep * (2 ** attempt) + random.uniform(0, 0.4)
                time.sleep(sleep_s); continue
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
        if not res: return None
        if (res.get('meta') or {}).get('err'): return None
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
        src_map = {row_get(r, "source_wallet"): row_get(r, "user_id") for r in rows if row_get(r, "source_wallet")}
        for sig in sigs:
            if self._is_seen(sig):
                checked_signatures.add(sig); continue
            details = get_tx_details(sig, self.central)
            checked_signatures.add(sig)
            if not details: continue
            sender = details.get("from"); amount = int(details.get("amount_lamports") or 0)
            if not sender or amount <= 0: continue
            uid = src_map.get(sender)
            if not uid:
                self._mark_seen(sig, None, amount)
                note = (f"⚠️ Unbekannte Einzahlung erkannt\n"
                        f"Sender: `{md_escape(sender)}`\nBetrag: {fmt_sol_usdc(amount)}\nSig: `{md_escape(sig)}`")
                for aid in ADMIN_IDS:
                    try: bot.send_message(int(aid), note, parse_mode="Markdown")
                    except Exception: pass
                if sender in EXCHANGE_WALLETS:
                    for aid in ADMIN_IDS:
                        try: bot.send_message(int(aid), f"⚠️ Exchange-Wallet: `{md_escape(sender)}`", parse_mode="Markdown")
                        except Exception: pass
                continue
            self._mark_seen(sig, uid, amount)
            if self.on_verified_deposit:
                self.on_verified_deposit({"user_id": uid, "amount_lamports": amount, "sig": sig})

# ---------------------------
# Simulated trading (legacy) — wird nicht mehr genutzt
# ---------------------------
def dex_market_buy_simulated(user_id: int, base: str, amount_lamports: int):
    return {"status": "FILLED", "txid": f"SIM-DEX-{base}-{int(time.time())}", "spent_lamports": amount_lamports}

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
                kwargs3 = dict(kwargs2); kwargs2.pop("parse_mode", None)
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
                kwargs3 = dict(kwargs2); kwargs2.pop("parse_mode", None)
                return _original_edit_message_text(str(text), chat_id, message_id, **kwargs3)
        else:
            kwargs3 = dict(kwargs); kwargs3.pop("parse_mode", None)
            return _original_edit_message_text(str(text), chat_id, message_id, **kwargs3)

bot.edit_message_text = _safe_edit_message_text

# transient state
WAITING_SOURCE_WALLET: Dict[int, bool] = {}
WAITING_PAYOUT_WALLET: Dict[int, bool] = {}
WAITING_WITHDRAW_AMOUNT: Dict[int, Optional[int]] = {}
ADMIN_AWAIT_SIMPLE_CALL: Dict[int, bool] = {}
ADMIN_AWAIT_BALANCE_EDIT: Dict[int, Optional[int]] = {}
ADMIN_AWAIT_SET_WALLET: Dict[int, Optional[int]] = {}
ADMIN_AWAIT_MASS_BALANCE: Dict[int, bool] = {}
ADMIN_AWAIT_NEWS_BROADCAST: Dict[int, Dict] = {}
SUPPORT_AWAIT_MSG: Dict[int, bool] = {}

watcher = CentralWatcher(CENTRAL_SOL_PUBKEY)

def _on_verified_deposit(evt: dict):
    uid = evt["user_id"]; lam = evt["amount_lamports"]
    add_balance(uid, lam)
    new_bal = get_balance_lamports(uid)
    try:
        bot.send_message(uid, f"✅ Einzahlung verifiziert: {fmt_sol_usdc(lam)}\nNeues Guthaben: {fmt_sol_usdc(new_bal)}", parse_mode="Markdown")
    except Exception:
        pass

watcher.on_verified_deposit = _on_verified_deposit
threading.Thread(target=watcher.start, kwargs={"interval_sec": 40}, daemon=True).start()

# ---------------------------
# DB backup on start
# ---------------------------
def ensure_db_backup():
    try:
        if os.path.exists(DB_PATH):
            bak = DB_PATH + ".bak." + time.strftime("%Y%m%d%H%M%S")
            try:
                with open(DB_PATH, "rb") as fin, open(bak, "wb") as fout:
                    fout.write(fin.read())
                print(f"DB backup created: {bak}")
            except Exception as e:
                print("Backup failed:", e)
        else:
            print("DB does not exist yet; will be created on init.")
    except Exception as e:
        print("DB backup check error:", e)

ensure_db_backup()

# ---------------------------
# Home / Support
# ---------------------------
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
    refcode = row_get(u, "referral_code") or gen_referral_for_user(row_get(u, "user_id"))
    bot_username = get_bot_username()
    ref_link = f"https://t.me/{bot_username}?start={refcode}"
    ref_link_md = f"`{md_escape(ref_link)}`"
    return (
        f"{BRAND}\n\n"
        f"👋 Hallo {uname} — willkommen!\n\n"
        "Was du hier bekommst:\n"
        "• Einzahlungen & automatisches Gutschreiben (verifizierte Source-Wallet)  \n"
        "• 🔔 Alpha Meme-Signale & Auto-Entry (LOW/MEDIUM/HIGH)  \n"
        "• Flexible Auszahlungen mit Lockup und Gebühren  \n"
        f"• Referral: {ref_link_md}\n\n"
        f"🏦 Aktuelles Guthaben: *{bal}*  \n"
        "📩 Support: /support\n"
    )

@bot.message_handler(commands=["support"])
def cmd_support(m: Message):
    SUPPORT_AWAIT_MSG[m.from_user.id] = True
    bot.reply_to(m, "✍️ Sende jetzt deine Support-Nachricht (Text/Bild). Sie wird an die Admins weitergeleitet.")

# ---------------------------
# Handlers
# ---------------------------
@bot.message_handler(commands=["start"])
def cmd_start(m: Message):
    uid = m.from_user.id
    uname = m.from_user.username or ""
    admin_flag = 1 if is_admin(uid) else 0
    upsert_user(uid, uname, admin_flag)

    # Referral payload (/start CODE oder /start=CODE)
    ref_code = None
    txt = (m.text or "")
    parts = txt.split()
    if len(parts) >= 2:
        ref_code = parts[1].strip()
    if not ref_code and txt.startswith("/start="):
        ref_code = txt.split("=", 1)[1].strip()

    # ensure referral code exists
    u = get_user(uid)
    if not row_get(u, "referral_code"):
        set_referral(uid, gen_referral_for_user(uid))
        u = get_user(uid)

    # apply referral bonus exactly once
    if ref_code:
        already = row_get(u, "referral_bonus_claimed", 0)
        if not already:
            with get_db() as con:
                ref_row = con.execute("SELECT user_id FROM users WHERE referral_code=?", (ref_code,)).fetchone()
            referrer = row_get(ref_row, "user_id")
            if referrer and referrer != uid:
                bonus_lam = int(0.01 * LAMPORTS_PER_SOL)  # 0.01 SOL Bonus
                add_balance(referrer, bonus_lam)
                add_balance(uid, bonus_lam)
                mark_referral_claimed(uid)
                try: bot.send_message(referrer, f"🎉 Dein Referral {md_escape(ref_code)} wurde verwendet! Bonus: {fmt_sol_usdc(bonus_lam)}")
                except Exception: pass
                try: bot.send_message(uid, f"🎉 Willkommen! Du und der Referrer bekommen je {fmt_sol_usdc(bonus_lam)} Bonus.")
                except Exception: pass

    bot.reply_to(m, home_text(u), reply_markup=kb_main(u))

@bot.callback_query_handler(func=lambda c: True)
def on_cb(c: CallbackQuery):
    uid = c.from_user.id
    u = get_user(uid)
    data = c.data or ""

    if data == "back_home":
        bot.edit_message_text(home_text(u), c.message.chat.id, c.message.message_id, parse_mode="Markdown", reply_markup=kb_main(u))
        return

    if data == "noop":
        bot.answer_callback_query(c.id, "—")
        return

    if data == "help":
        bot.answer_callback_query(c.id)
        bot.send_message(uid,
                         ("ℹ️ Hilfe:\n\n"
                          "1) Einzahlen: Absender-Wallet setzen → SOL an zentrale Adresse.\n"
                          "2) Meme-News: Abo ein/aus.\n"
                          "3) Auto-Entry: ON/OFF + Risiko.\n"
                          "4) Auszahlung: Betrag eingeben → Lockup/Fees wählen.\n"
                          "5) /support: Nachricht an Admins."),
                         parse_mode="Markdown")
        return

    # deposit – Adresse hier setzen/ändern
    if data == "deposit":
        if not row_get(u, "source_wallet"):
            WAITING_SOURCE_WALLET[uid] = True
            bot.answer_callback_query(c.id, "Bitte zuerst deine Absender-Wallet senden.")
            bot.send_message(uid, "🔑 Sende jetzt deine **Absender-Wallet (SOL)**:")
            return
        price = get_sol_usd()
        px = f"(1 SOL ≈ {price:.2f} USDC)" if price > 0 else ""
        bot.edit_message_text(
            f"Absender-Wallet: `{md_escape(row_get(u,'source_wallet','-'))}`\n"
            f"Sende SOL an: `{md_escape(CENTRAL_SOL_PUBKEY)}`\n{px}\n\n"
            "🔄 Möchtest du die Absender-Wallet ändern? Schicke einfach **eine neue Solana-Adresse** als Nachricht.",
            c.message.chat.id, c.message.message_id, parse_mode="Markdown", reply_markup=kb_main(u)
        )
        WAITING_SOURCE_WALLET[uid] = True
        return

    # withdraw – zuerst Auszahlungsadresse (falls fehlt/ändern)
    if data == "withdraw":
        if not row_get(u, "payout_wallet"):
            WAITING_PAYOUT_WALLET[uid] = True
            bot.answer_callback_query(c.id, "Bitte zuerst deine Auszahlungsadresse senden.")
            bot.send_message(uid, "🔑 Sende jetzt deine **Auszahlungsadresse (SOL)**:")
            return
        WAITING_WITHDRAW_AMOUNT[uid] = None
        bot.answer_callback_query(c.id, "Bitte Betrag eingeben.")
        bot.send_message(uid,
                         f"💳 Auszahlungsadresse: `{md_escape(row_get(u,'payout_wallet','-'))}`\n"
                         "🔄 Zum Ändern sende einfach **eine neue Solana-Adresse**.\n\n"
                         "Gib nun den Betrag in SOL ein (z. B. `0.25`):",
                         parse_mode="Markdown")
        WAITING_PAYOUT_WALLET[uid] = True
        return

    # subscriptions (Meme only)
    if data == "sub_menu":
        bot.edit_message_text("Meme-News:", c.message.chat.id, c.message.message_id, reply_markup=kb_sub_menu())
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

    # referral
    if data == "referral":
        u = get_user(uid)
        code = row_get(u, "referral_code") or gen_referral_for_user(uid)
        set_referral(uid, code)
        bot_username = get_bot_username()
        link = f"https://t.me/{bot_username}?start={code}"
        bot.answer_callback_query(c.id, "Referral-Link erstellt")
        bot.send_message(uid, f"Dein Referral-Link: `{md_escape(link)}`\nTeile ihn mit Freunden!", parse_mode="Markdown")
        return

    # portfolio
    if data == "my_portfolio":
        u = get_user(uid)
        bal = fmt_sol_usdc(row_get(u, "sol_balance_lamports", 0))
        subs = row_get(u, "sub_types", "-")
        bot.answer_callback_query(c.id, "Portfolio")
        bot.send_message(uid, f"🏦 Guthaben: *{bal}*\n📰 News: *{subs or '-'}*\nAuto: *{row_get(u,'auto_mode','OFF')} / {row_get(u,'auto_risk','MEDIUM')}*", parse_mode="Markdown")
        return

    # auto menu
    if data == "auto_menu":
        bot.edit_message_text("Auto-Entry Einstellungen:", c.message.chat.id, c.message.message_id, reply_markup=kb_auto(u))
        return

    if data.startswith("auto_"):
        mode = data.split("_", 1)[1]
        if mode not in ("OFF", "ON"): mode = "OFF"
        set_auto_mode(uid, mode)
        bot.answer_callback_query(c.id, f"Auto-Entry: {mode}")
        nu = get_user(uid)
        bot.edit_message_text(f"Auto-Entry gesetzt: *{mode}*", c.message.chat.id, c.message.message_id, parse_mode="Markdown", reply_markup=kb_auto(nu))
        return

    if data.startswith("risk_"):
        risk = data.split("_", 1)[1]
        set_auto_risk(uid, risk)
        bot.answer_callback_query(c.id, f"Risk: {risk}")
        nu = get_user(uid)
        bot.edit_message_text(f"Risiko gesetzt: *{risk}*", c.message.chat.id, c.message.message_id, parse_mode="Markdown", reply_markup=kb_auto(nu))
        return

    # admin menu
    if data == "admin_menu_big":
        if not is_admin(uid):
            bot.answer_callback_query(c.id, "Nicht erlaubt.")
            return
        bot.edit_message_text("🛠️ Admin-Menü — Kontrolle", c.message.chat.id, c.message.message_id, reply_markup=kb_admin_main())
        return

    # admin create meme call
    if data == "admin_new_call":
        if not is_admin(uid): return
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Sende den MEME-Call im Format:\nMEME|NAME_ODER_SYMBOL|TOKEN_ADDRESS (oder `-`)\nOptional: Zeile danach Notes", parse_mode=None)
        ADMIN_AWAIT_SIMPLE_CALL[uid] = True
        return

    # admin broadcast last call (nur Abonnenten)
    if data == "admin_broadcast_last":
        if not is_admin(uid): return
        with get_db() as con:
            row = con.execute("SELECT * FROM calls ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            bot.answer_callback_query(c.id, "Kein Call vorhanden.")
            return
        msg = "📣 Neuer Meme-Call:\n" + fmt_call(row)
        subs = all_subscribers()
        sent = 0
        for su in subs:
            try:
                bot.send_message(su, msg, parse_mode="Markdown")
                queue_execution(row_get(row,"id"), su, status="QUEUED", message="Queued by broadcast")
                sent += 1
            except Exception:
                pass
        bot.answer_callback_query(c.id, f"An {sent} Abonnenten gesendet.")
        return

    # admin list investors
    if data == "admin_list_investors":
        if not is_admin(uid): return
        rows = list_investors(limit=100, offset=0)
        if not rows:
            bot.answer_callback_query(c.id, "Keine Abonnenten.")
            return
        parts = ["👥 Investoren (Top)"]
        for r in rows:
            name = "@" + row_get(r, "username","") if row_get(r,"username") else "(kein Username)"
            parts.append(f"- {name} • {fmt_sol_usdc(row_get(r,'sol_balance_lamports',0))} • News: {row_get(r,'sub_types','-')}")
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "\n".join(parts), parse_mode="Markdown")
        return

    # admin view users pagination
    if data.startswith("admin_view_users_"):
        if not is_admin(uid): return
        try:
            offset = int(data.split("_")[-1])
        except Exception:
            offset = 0
        with get_db() as con:
            total = con.execute("SELECT COUNT(*) as cnt FROM users").fetchone()["cnt"]
            rows = con.execute("""
                SELECT user_id, username, sol_balance_lamports, source_wallet, payout_wallet, auto_mode, auto_risk, sub_types
                FROM users
                ORDER BY sol_balance_lamports DESC
                LIMIT 10 OFFSET ?
            """, (offset,)).fetchall()
        if not rows:
            bot.answer_callback_query(c.id, "Keine Nutzer.")
            return
        bot.answer_callback_query(c.id)
        for r in rows:
            uname = "@" + row_get(r,"username","") if row_get(r,"username") else "(kein Username)"
            txt = (f"{uname} • UID {row_get(r,'user_id')}\n"
                   f"Guthaben: {fmt_sol_usdc(row_get(r,'sol_balance_lamports',0))}\n"
                   f"Source: `{md_escape(row_get(r,'source_wallet','-'))}`\n"
                   f"Payout: `{md_escape(row_get(r,'payout_wallet','-'))}`\n"
                   f"Auto: {row_get(r,'auto_mode','OFF')} / {row_get(r,'auto_risk','MEDIUM')}\n"
                   f"News: {row_get(r,'sub_types','-')}")
            bot.send_message(uid, txt, parse_mode="Markdown", reply_markup=kb_user_row(row_get(r,"user_id")))
        bot.send_message(uid, "Navigation:", parse_mode=None, reply_markup=kb_users_pagination(offset, total))
        return

    # admin inline actions
    if data.startswith("admin_balance_"):
        if not is_admin(uid): return
        try:
            target = int(data.split("_", 2)[2])
        except Exception:
            bot.answer_callback_query(c.id, "Ungültig")
            return
        ADMIN_AWAIT_BALANCE_EDIT[uid] = target
        bot.answer_callback_query(c.id, f"Guthabenänderung: UID {target}")
        bot.send_message(uid, "Sende **Betrag in SOL** (z. B. `0.20`) **oder Prozent** (z. B. `-40%`).")
        return

    if data.startswith("admin_setwallet_"):
        if not is_admin(uid): return
        try:
            target = int(data.split("_", 2)[2])
        except Exception:
            bot.answer_callback_query(c.id, "Ungültig")
            return
        ADMIN_AWAIT_SET_WALLET[uid] = target
        bot.answer_callback_query(c.id, f"Sende `SRC <adresse>` oder `PAY <adresse>` für UID {target}")
        bot.send_message(uid, "Beispiele:\n`SRC 9abc...`  (Source/Einzahlung)\n`PAY 8xyz...`  (Payout/Auszahlung)", parse_mode="Markdown")
        return

    if data.startswith("admin_payouts_"):
        if not is_admin(uid): return
        try:
            target = int(data.split("_", 2)[2])
        except Exception:
            bot.answer_callback_query(c.id, "Ungültig")
            return
        with get_db() as con:
            rows = con.execute("SELECT * FROM payouts WHERE user_id=? ORDER BY created_at DESC LIMIT 10", (target,)).fetchall()
        if not rows:
            bot.answer_callback_query(c.id, "Keine Auszahlungen")
            return
        bot.answer_callback_query(c.id)
        for r in rows:
            bot.send_message(uid, f"#{row_get(r,'id')} • {fmt_sol_usdc(row_get(r,'amount_lamports',0))} • {row_get(r,'status','-')} • Lockup {row_get(r,'lockup_days',0)}d • Fee {row_get(r,'fee_percent',0)}%", parse_mode=None)
        return

    if data == "admin_broadcast_all":
        if not is_admin(uid): return
        ADMIN_AWAIT_NEWS_BROADCAST[uid] = {"step": "await_text_to_all"}
        bot.answer_callback_query(c.id)
        bot.send_message(uid, "Sende die **Nachricht**, die an **alle Nutzer** (alle, die /start gedrückt haben) gesendet werden soll.")
        return

    if data == "admin_apply_pnl":
        if not is_admin(uid):
            bot.answer_callback_query(c.id, "Nicht erlaubt.")
            return
        ADMIN_AWAIT_MASS_BALANCE[uid] = True
        bot.answer_callback_query(c.id, "Sende z. B.:\n• `ALL -40%`\n• `PROMO PERCENT 20 ALL`\n• `PROMO BONUS 0.05 SUBSCRIBERS`\n• `PNL <CALL_ID> 20`")
        return

    # payout option chosen (User) — bleibt REQUESTED, Admin sendet später on-chain
    if data.startswith("payoutopt_"):
        try:
            days = int(data.split("_", 1)[1])
        except Exception:
            bot.answer_callback_query(c.id, "Ungültige Auswahl.")
            return

        fee_percent = float(_fee_tiers.get(days, 0.0))
        pending = WAITING_WITHDRAW_AMOUNT.get(uid, None)
        if pending is None or pending <= 0:
            bot.answer_callback_query(c.id, "Keine ausstehende Auszahlung. Bitte Betrag zuerst eingeben.")
            return

        amount_lam = int(pending)
        if not subtract_balance(uid, amount_lam):
            bot.answer_callback_query(c.id, "Unzureichendes Guthaben.")
            WAITING_WITHDRAW_AMOUNT.pop(uid, None)
            return

        with get_db() as con:
            cur = con.execute(
                "INSERT INTO payouts(user_id, amount_lamports, status, note, lockup_days, fee_percent) VALUES (?,?,?,?,?,?)",
                (uid, amount_lam, "REQUESTED", f"User requested withdrawal ({days}d, fee {fee_percent}%)", days, fee_percent)
            )
            pid = cur.lastrowid

        WAITING_WITHDRAW_AMOUNT.pop(uid, None)

        fee_lam = int(round(amount_lam * (fee_percent / 100.0)))
        net_lam = amount_lam - fee_lam

        bot.answer_callback_query(c.id, "Auszahlung angefragt.")
        try:
            bot.send_message(
                uid,
                ("💸 *Auszahlung angefragt*\n"
                 f"Betrag: {fmt_sol_usdc(amount_lam)}\n"
                 f"Lockup: {days} Tage\n"
                 f"Gebühr: {fee_percent:.2f}% ({fmt_sol_usdc(fee_lam)})\n"
                 f"Netto (nach Fee): {fmt_sol_usdc(net_lam)}"),
                parse_mode="Markdown"
            )
        except Exception:
            pass

        for aid in ADMIN_IDS:
            try:
                bot.send_message(
                    int(aid),
                    (f"🧾 Neue Auszahlung #{pid}\nUser: {uid}\n"
                     f"Betrag: {fmt_sol_usdc(amount_lam)}\nLockup: {days}d • Fee: {fee_percent:.2f}%\n"
                     f"Netto: {fmt_sol_usdc(net_lam)}"),
                    reply_markup=kb_payout_manage(pid)
                )
            except Exception:
                pass
        return

    # admin payout manage actions — hier wird wirklich gesendet
    if data.startswith("payout_"):
        if not is_admin(uid):
            bot.answer_callback_query(c.id, "Nicht erlaubt.")
            return

        parts = data.split("_", 2)
        if len(parts) < 3:
            bot.answer_callback_query(c.id, "Ungültig.")
            return

        action, sid = parts[1], parts[2]
        try:
            pid = int(sid)
        except Exception:
            bot.answer_callback_query(c.id, "Ungültige ID.")
            return

        with get_db() as con:
            row = con.execute("SELECT * FROM payouts WHERE id=?", (pid,)).fetchone()

        if not row:
            bot.answer_callback_query(c.id, "Anfrage nicht gefunden.")
            return

        tgt_uid = int(row_get(row, "user_id", 0))
        amt = int(row_get(row, "amount_lamports", 0))
        days = int(row_get(row, "lockup_days", 0))
        fee_percent = float(row_get(row, "fee_percent", 0.0))
        fee_lam = int(round(amt * (fee_percent / 100.0)))
        net_lam = amt - fee_lam

        if action == "APPROVE":
            with get_db() as con:
                con.execute("UPDATE payouts SET status='APPROVED' WHERE id=?", (pid,))
            bot.answer_callback_query(c.id, "Genehmigt.")
            try: bot.send_message(tgt_uid, f"✅ Deine Auszahlung #{pid} wurde *genehmigt*.", parse_mode="Markdown")
            except Exception: pass
            return

        if action == "SENT":
            # Wirklich on-chain senden, tx_sig speichern
            try:
                uuser = get_user(tgt_uid)
                payout_wallet = row_get(uuser, "payout_wallet", "")
                if not payout_wallet:
                    bot.answer_callback_query(c.id, "Keine Payout-Adresse beim Nutzer."); return
                txsig = send_sol(payout_wallet, net_lam)
                with get_db() as con:
                    con.execute("UPDATE payouts SET status='SENT', tx_sig=? WHERE id=?", (txsig, pid))
                bot.answer_callback_query(c.id, "Als gesendet markiert.")
                try:
                    bot.send_message(
                        tgt_uid,
                        (f"📤 Deine Auszahlung #{pid} wurde *gesendet*.\n"
                         f"Betrag: {fmt_sol_usdc(amt)}\nGebühr: {fee_percent:.2f}% ({fmt_sol_usdc(fee_lam)})\n"
                         f"Netto: {fmt_sol_usdc(net_lam)}\n"
                         f"Tx: `{md_escape(txsig)}`"),
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
            except Exception as e:
                bot.answer_callback_query(c.id, f"Fehler beim Senden: {e}")
            return

        if action == "REJECT":
            with get_db() as con:
                con.execute("UPDATE payouts SET status='REJECTED' WHERE id=?", (pid,))
            add_balance(tgt_uid, amt)  # reservierten Betrag zurückerstatten
            bot.answer_callback_query(c.id, "Abgelehnt und Betrag erstattet.")
            try: bot.send_message(tgt_uid, f"❌ Deine Auszahlung #{pid} wurde *abgelehnt*. Betrag wurde zurückerstattet.", parse_mode="Markdown")
            except Exception: pass
            return

    bot.answer_callback_query(c.id, "")

@bot.message_handler(func=lambda m: True)
def catch_all(m: Message):
    uid = m.from_user.id
    text = (m.text or "").strip() if m.text else ""

    # Support
    if SUPPORT_AWAIT_MSG.get(uid):
        SUPPORT_AWAIT_MSG.pop(uid, None)
        for aid in ADMIN_IDS:
            try:
                if m.photo:
                    bot.send_photo(int(aid), m.photo[-1].file_id, caption=f"[Support von {uid}] {m.caption or ''}")
                else:
                    bot.send_message(int(aid), f"[Support von {uid}] {text}", parse_mode=None)
            except Exception:
                pass
        bot.reply_to(m, "✅ Deine Support-Nachricht wurde an die Admins gesendet.")
        return

    # Admin: Set wallet(s)
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

    # User: Source/Payout Wallet Direkt-Eingabe
    if WAITING_SOURCE_WALLET.get(uid, False) and is_probably_solana_address(text):
        WAITING_SOURCE_WALLET[uid] = False
        set_source_wallet(uid, text)
        price = get_sol_usd()
        px = f"(1 SOL ≈ {price:.2f} USDC)" if price > 0 else ""
        bot.reply_to(m, f"✅ Absender-Wallet gespeichert.\nSende SOL von `{md_escape(text)}` an `{md_escape(CENTRAL_SOL_PUBKEY)}`\n{px}", parse_mode="Markdown")
        return

    if WAITING_PAYOUT_WALLET.get(uid, False) and is_probably_solana_address(text):
        WAITING_PAYOUT_WALLET[uid] = False
        set_payout_wallet(uid, text)
        bot.reply_to(m, f"✅ Auszahlungsadresse gespeichert: `{md_escape(text)}`\nGib nun den Betrag in SOL ein (z. B. 0.25).", parse_mode="Markdown")
        WAITING_WITHDRAW_AMOUNT[uid] = None
        return

    # Admin: create MEME call
    if ADMIN_AWAIT_SIMPLE_CALL.get(uid, False):
        ADMIN_AWAIT_SIMPLE_CALL[uid] = False
        if not is_admin(uid):
            bot.reply_to(m, "Nicht erlaubt."); return
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        if not lines:
            bot.reply_to(m, "Formatfehler."); return
        head = lines[0]
        notes = "\n".join(lines[1:]) if len(lines) > 1 else ""
        parts = [p.strip() for p in head.split("|")]
        if len(parts) < 2 or parts[0].upper() != "MEME":
            bot.reply_to(m, "Format: MEME|NAME|TOKEN_ADDRESS (oder `-`)"); return
        _, base, token_addr = (parts + ["-"])[:3]
        token_addr = None if token_addr == "-" else token_addr
        cid = create_call(uid, base.upper(), token_addr, notes)
        c = get_call(cid)
        bot.reply_to(m, "✅ Meme-Call gespeichert:\n" + fmt_call(c), parse_mode="Markdown")
        return

    # Admin: single balance edit
    if ADMIN_AWAIT_BALANCE_EDIT.get(uid) is not None:
        target = ADMIN_AWAIT_BALANCE_EDIT.pop(uid)
        if not is_admin(uid):
            bot.reply_to(m, "Nicht erlaubt."); return
        try:
            txt = text.replace(" ", "")
            if txt.endswith("%"):
                pct = float(txt[:-1].replace(",", "."))
                old = get_balance_lamports(target)
                new = int(round(old * (1 + pct/100.0)))
                set_balance(target, new)
                bot.reply_to(m, f"✅ UID {target}: {fmt_sol_usdc(old)} → {fmt_sol_usdc(new)} ({pct:+.2f}%)")
            else:
                sol_amt = float(text.replace(",", "."))
                lam = int(sol_amt * LAMPORTS_PER_SOL)
                set_balance(target, lam)
                nb = fmt_sol_usdc(get_balance_lamports(target))
                bot.reply_to(m, f"✅ Guthaben gesetzt: UID {target} {fmt_sol_usdc(lam)} • Neues Guthaben: {nb}")
        except Exception:
            bot.reply_to(m, "Bitte Zahl (z. B. `0.25`) **oder** Prozent (z. B. `-40%`) senden.")
        return

    # Admin: mass operations
    if ADMIN_AWAIT_MASS_BALANCE.get(uid, False):
        ADMIN_AWAIT_MASS_BALANCE[uid] = False
        if not is_admin(uid):
            bot.reply_to(m, "Nicht erlaubt."); return
        cmd = text.strip()
        try:
            if cmd.upper().startswith("ALL"):
                parts = cmd.split()
                if len(parts) != 2 or not parts[1].endswith("%"):
                    bot.reply_to(m, "Format: `ALL -40%` oder `ALL +25%`"); return
                pct = float(parts[1][:-1].replace(",", "."))
                ids = all_users()
                changed = 0
                for uid_t in ids:
                    old = get_balance_lamports(uid_t)
                    new = int(round(old * (1 + pct/100.0)))
                    set_balance(uid_t, new)
                    changed += 1
                bot.reply_to(m, f"✅ Massenänderung: {changed} Nutzer angepasst ({pct:+.2f}%).")
                return

            toks = cmd.split()
            verb = toks[0].upper()
            if verb == "PROMO":
                typ = toks[1].upper()
                val = float(toks[2])
                scope = toks[3].upper() if len(toks) > 3 else "ALL"
                with get_db() as con:
                    if scope == "ALL":
                        rows = con.execute("SELECT user_id FROM users").fetchall()
                    elif scope == "SUBSCRIBERS":
                        rows = con.execute("SELECT user_id FROM users WHERE sub_active=1").fetchall()
                    elif scope == "MEME":
                        rows = con.execute("SELECT user_id FROM users WHERE sub_types LIKE '%MEME%'").fetchall()
                    else:
                        rows = []
                affected = 0
                for r in rows:
                    uid_t = int(row_get(r,"user_id",0))
                    if typ == "PERCENT":
                        bal = get_balance_lamports(uid_t)
                        delta = int(bal * (val / 100.0))
                        add_balance(uid_t, delta)
                    else:  # BONUS in SOL
                        lam = int(val * LAMPORTS_PER_SOL)
                        add_balance(uid_t, lam)
                    affected += 1
                bot.reply_to(m, f"✅ PROMO angewendet auf {affected} Nutzer.")
                return

            if verb == "PNL":
                call_id = int(toks[1]); percent = float(toks[2])
                affected = 0
                with get_db() as con:
                    execs = con.execute("SELECT user_id, stake_lamports FROM executions WHERE call_id=? AND status IN ('FILLED','QUEUED')", (call_id,)).fetchall()
                for ex in execs:
                    uid_t = int(row_get(ex,"user_id",0))
                    stake = int(row_get(ex,"stake_lamports",0))
                    u = get_user(uid_t)
                    risk = row_get(u,"auto_risk","MEDIUM")
                    frac = _risk_fraction(risk)
                    pnl_lam = int(stake * (percent / 100.0) * frac)
                    add_balance(uid_t, pnl_lam)
                    affected += 1
                bot.reply_to(m, f"✅ PNL angewendet (Call {call_id}) auf {affected} Nutzer.")
                return

            bot.reply_to(m, "Unbekannter Befehl. Beispiele: `ALL -40%`, `PROMO PERCENT 20 ALL`, `PNL 12 15`")
        except Exception as e:
            bot.reply_to(m, f"Fehler: {e}")
        return

    # Admin: Broadcast to ALL
    if ADMIN_AWAIT_NEWS_BROADCAST.get(uid):
        ctx = ADMIN_AWAIT_NEWS_BROADCAST[uid]
        step = ctx.get("step")
        if step == "await_text_to_all":
            msg = text
            ADMIN_AWAIT_NEWS_BROADCAST.pop(uid, None)
            ids = all_users()
            sent = 0
            for t in ids:
                try:
                    if m.photo:
                        bot.send_photo(t, m.photo[-1].file_id, caption=msg)
                    else:
                        bot.send_message(t, msg, parse_mode="Markdown")
                    sent += 1
                except Exception:
                    pass
            bot.reply_to(m, f"✅ Broadcast an {sent} Nutzer gesendet.")
            return

    # Withdraw amount entry
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
            if get_balance_lamports(uid) < lam:
                bot.reply_to(m, f"Unzureichendes Guthaben. Verfügbar: {fmt_sol_usdc(get_balance_lamports(uid))}")
                WAITING_WITHDRAW_AMOUNT.pop(uid, None); return
            WAITING_WITHDRAW_AMOUNT[uid] = lam
            bot.reply_to(m, f"Auszahlung: {fmt_sol_usdc(lam)} — Wähle Lockup & Fee:", reply_markup=kb_withdraw_options())
        except Exception:
            bot.reply_to(m, "Bitte eine gültige Zahl eingeben, z. B. `0.25`.")
        return

    # Default
    bot.reply_to(m, "Ich habe das nicht verstanden. Benutze das Menü unten.", reply_markup=kb_main(get_user(uid)))

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
                if row_get(r,"auto_mode") != "ON":
                    with get_db() as con:
                        con.execute("UPDATE executions SET status='ERROR', message='Auto OFF' WHERE id=?", (row_get(r,"eid"),))
                    continue
                call = get_call(row_get(r,"call_id"))
                stake_info = row_get(r,"stake_lamports") or _compute_stake_for_user(row_get(r,"user_id"))
                token_mint = row_get(call, "token_address", None)
                if not token_mint:
                    with get_db() as con:
                        con.execute("UPDATE executions SET status='ERROR', message='No token' WHERE id=?", (row_get(r,"eid"),))
                    continue
                # LIVE: Jupiter Swap
                status, sig = dex_market_buy_live(token_mint, int(stake_info))
                if status != "FILLED":
                    with get_db() as con:
                        con.execute("UPDATE executions SET status='ERROR', txid=?, message='Swap error' WHERE id=?", (sig, row_get(r,"eid")))
                else:
                    with get_db() as con:
                        con.execute("UPDATE executions SET status='FILLED', txid=?, message='JOINED' WHERE id=?", (sig, row_get(r,"eid")))
                try:
                    bot.send_message(row_get(r,"user_id"),
                                     f"🤖 Auto-Entry • {row_get(r,'auto_risk','MEDIUM')}\n"
                                     f"{fmt_call(call)}\n"
                                     f"Status: *{status}*\n"
                                     f"Einsatz (Info): {fmt_sol_usdc(stake_info)}\n"
                                     f"Guthaben: {fmt_sol_usdc(get_balance_lamports(row_get(r,'user_id')))}\n"
                                     f"`{md_escape(sig)}`",
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
                      AND (last_notified_at IS NULL OR (strftime('%s','now') - strftime('%s',COALESCE(last_notified_at,'1970-01-01')) > 1200))
                    ORDER BY created_at ASC
                """).fetchall()
            for r in rows:
                for aid in ADMIN_IDS:
                    try:
                        bot.send_message(int(aid), f"⏰ Erinnerung: Auszahlung #{row_get(r,'id')} offen • Betrag {fmt_sol_usdc(row_get(r,'amount_lamports',0))}", reply_markup=kb_payout_manage(row_get(r,"id")))
                    except Exception:
                        pass
                with get_db() as con:
                    con.execute("UPDATE payouts SET last_notified_at=CURRENT_TIMESTAMP WHERE id=?", (row_get(r,"id"),))
            time.sleep(60)
        except Exception as e:
            print("payout reminder loop error:", e)
            time.sleep(60)

threading.Thread(target=auto_executor_loop, daemon=True).start()
threading.Thread(target=payout_reminder_loop, daemon=True).start()

print("Bot läuft — MEME-only. SIMULATION_MODE =", SIMULATION_MODE)
bot.infinity_polling(timeout=60, long_polling_timeout=60)