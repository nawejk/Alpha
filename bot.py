import os
import sqlite3
import time
import requests
from datetime import datetime

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# =======================
# CONFIG
# =======================

BOT_TOKEN = "8535037912:AAHfAPVG7ugqWmdacCMDi15hFiukA_5TX00"
ADMIN_IDS = {7919108078}  # deine Admin IDs hier
MAIN_WALLET = "CBboaHRCZARdxRBUWpWtPdiFLjP7kgQvDdWq7pLbcHn3"  # Wallet, auf die eingezahlt wird
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"  # kostenloser RPC

bot = telebot.TeleBot(BOT_TOKEN)

DB_PATH = "auto_entry_bot.db"

# =======================
# DATABASE
# =======================

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

conn = get_db()
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE,
    username TEXT,
    sender_wallet TEXT,
    balance_usd REAL DEFAULT 0,
    auto_entry INTEGER DEFAULT 0,
    risk_percent INTEGER DEFAULT 10,
    created_at TEXT
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    from_wallet TEXT,
    tx_sig TEXT,
    amount_usd REAL,
    status TEXT,
    created_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount_usd REAL,
    target_wallet TEXT,
    status TEXT,
    created_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT,
    direction TEXT,
    leverage TEXT,
    raw_text TEXT,
    created_at TEXT
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    signal_id INTEGER,
    amount_usd REAL,
    risk_percent INTEGER,
    created_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(signal_id) REFERENCES signals(id)
)
""")

conn.commit()

# =======================
# HELPERS
# =======================

def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def get_or_create_user(message):
    tg_id = message.from_user.id
    username = message.from_user.username or ""
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_id,))
    row = cur.fetchone()
    if row:
        return row
    cur.execute("""
        INSERT INTO users (telegram_id, username, created_at)
        VALUES (?,?,?)
    """, (tg_id, username, now()))
    conn.commit()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_id,))
    return cur.fetchone()

def get_user_by_telegram_id(tg_id: int):
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (tg_id,))
    return cur.fetchone()

def update_balance(user_id, new_balance):
    cur = conn.cursor()
    cur.execute("UPDATE users SET balance_usd = ? WHERE id = ?", (new_balance, user_id))
    conn.commit()

def set_auto_entry(user_id, enabled: bool):
    cur = conn.cursor()
    cur.execute("UPDATE users SET auto_entry = ? WHERE id = ?", (1 if enabled else 0, user_id))
    conn.commit()

def set_risk_percent(user_id, percent: int):
    cur = conn.cursor()
    cur.execute("UPDATE users SET risk_percent = ? WHERE id = ?", (percent, user_id))
    conn.commit()

def set_sender_wallet(user_id, wallet: str):
    cur = conn.cursor()
    cur.execute("UPDATE users SET sender_wallet = ? WHERE id = ?", (wallet, user_id))
    conn.commit()

# =======================
# INLINE KEYBOARDS
# =======================

def main_menu_kb(user_row):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("💸 Deposit", callback_data="menu_deposit"),
        InlineKeyboardButton("💵 Withdraw", callback_data="menu_withdraw"),
    )
    kb.add(
        InlineKeyboardButton("📢 Signals ON" if not user_row["auto_entry"] else "📢 Signals OFF",
                             callback_data="toggle_auto_entry"),
        InlineKeyboardButton("📊 Risk %", callback_data="menu_risk"),
    )
    kb.add(
        InlineKeyboardButton("⚙️ Control Center", callback_data="menu_control"),
    )
    kb.add(
        InlineKeyboardButton("ℹ️ Help", callback_data="menu_help")
    )
    if user_row["telegram_id"] in ADMIN_IDS:
        kb.add(InlineKeyboardButton("👑 Admin Panel", callback_data="menu_admin"))
    return kb

def risk_menu_kb(current_percent):
    kb = InlineKeyboardMarkup(row_width=5)
    buttons = []
    for p in range(10, 110, 10):
        label = f"{p}%"
        if p == current_percent:
            label = f"✅ {p}%"
        buttons.append(InlineKeyboardButton(label, callback_data=f"risk_{p}"))
    kb.add(*buttons)
    kb.add(InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    return kb

def control_center_kb():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🔌 Auto-Entry ON/OFF", callback_data="toggle_auto_entry"),
        InlineKeyboardButton("✏️ Sender Wallet ändern", callback_data="change_sender_wallet"),
        InlineKeyboardButton("🔍 Deposit prüfen", callback_data="check_deposit"),
        InlineKeyboardButton("⬅️ Zurück", callback_data="back_main"),
    )
    return kb

def admin_menu_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("👥 Users", callback_data="admin_users"),
        InlineKeyboardButton("✏️ Balance ändern", callback_data="admin_edit_balance"),
    )
    kb.add(
        InlineKeyboardButton("📣 Broadcast", callback_data="admin_broadcast"),
        InlineKeyboardButton("🚀 Signal senden", callback_data="admin_signal"),
    )
    kb.add(InlineKeyboardButton("⬅️ Zurück", callback_data="back_main"))
    return kb

# =======================
# SIMPLE STATE HANDLING
# =======================

user_states = {}  # {telegram_id: {"state": str, "data": {...}}}

def set_state(tg_id, state, data=None):
    user_states[tg_id] = {"state": state, "data": data or {}}

def clear_state(tg_id):
    user_states.pop(tg_id, None)

# =======================
# SOLANA DEPOSIT CHECK (BEISPIEL)
# =======================

def check_solana_deposit(from_wallet: str, to_wallet: str, min_amount_sol: float = 0.0):
    """
    Sehr vereinfachtes Beispiel:
    - Holt letzten Signaturen für die to_wallet
    - Lädt jede Tx und schaut, ob from_wallet -> to_wallet SOL gesendet hat.
    - Gibt beim ersten Treffer (bool, amount_sol, tx_sig) zurück.
    ACHTUNG: Für Produktion solltest du saubere Fehlerbehandlung & Caching einbauen.
    """
    headers = {"Content-Type": "application/json"}
    # 1) Signaturen zur Zieladresse holen
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [to_wallet, {"limit": 30}]
    }
    r = requests.post(SOLANA_RPC_URL, json=payload, headers=headers, timeout=10)
    r.raise_for_status()
    sigs = r.json().get("result", [])

    for entry in sigs:
        sig = entry["signature"]
        # 2) Transaktionsdetails
        tx_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [sig, {"encoding": "jsonParsed"}]
        }
        tr = requests.post(SOLANA_RPC_URL, json=tx_payload, headers=headers, timeout=10)
        tr.raise_for_status()
        tx = tr.json().get("result")
        if not tx:
            continue

        meta = tx.get("meta")
        message = tx.get("transaction", {}).get("message", {})
        account_keys = message.get("accountKeys", [])

        # Wir suchen einfache SOL-Transfer von from_wallet -> to_wallet
        try:
            pre_balances = meta["preBalances"]
            post_balances = meta["postBalances"]
        except Exception:
            continue

        # Adresse -> Index map
        addr_index = {acc["pubkey"]: i for i, acc in enumerate(account_keys)}

        if from_wallet not in addr_index or to_wallet not in addr_index:
            continue

        i_from = addr_index[from_wallet]
        i_to = addr_index[to_wallet]

        diff_from = (post_balances[i_from] - pre_balances[i_from]) / 1e9
        diff_to = (post_balances[i_to] - pre_balances[i_to]) / 1e9

        amount = round(diff_to, 6)
        # Von from_wallet muss SOL abgegangen sein, zu to_wallet dazugekommen
        if diff_from < 0 and diff_to > 0 and amount >= min_amount_sol:
            return True, amount, sig

    return False, 0.0, None

# =======================
# COMMANDS
# =======================

@bot.message_handler(commands=["start"])
def cmd_start(message):
    user = get_or_create_user(message)

    text = (
        "Willkommen! 👋\n\n"
        "Straight & easy: Deposit → Auto-Entry → Copy-Trades.\n\n"
        f"Dein Guthaben: *{user['balance_usd']:.2f} USD*"
    )

    bot.send_message(
        message.chat.id,
        text,
        parse_mode="Markdown",  # bleibt okay, weil kein Username mit Unterstrich mehr drin ist
        reply_markup=main_menu_kb(user)
    )

@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    user = get_or_create_user(message)
    bot.send_message(
        message.chat.id,
        "👑 Admin Panel",
        reply_markup=admin_menu_kb()
    )

# =======================
# CALLBACKS
# =======================

@bot.callback_query_handler(func=lambda c: True)
def callbacks(call):
    tg_id = call.from_user.id
    user = get_user_by_telegram_id(tg_id)
    if not user:
        # falls irgendwas schiefgeht
        fake_msg = telebot.types.Message(
            message_id=call.message.message_id,
            from_user=call.from_user,
            date=call.message.date,
            chat=call.message.chat,
            content_type="text",
            options={}
        )
        cmd_start(fake_msg)
        return

    data = call.data

    # MAIN MENU
    if data == "back_main":
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"Dein Guthaben: *{user['balance_usd']:.2f} USD*",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(user)
        )
        clear_state(tg_id)

    elif data == "menu_deposit":
        txt = (
            f"💸 *Deposit*\n\n"
            f"1️⃣ Sende deine SOL/USDC von deiner persönlichen Wallet an:\n"
            f"`{MAIN_WALLET}`\n\n"
            "2️⃣ Sende mir *deine Absender-Adresse* (From-Wallet), "
            "damit das System die Zahlung automatisch erkennen kann.\n\n"
            "Wenn deine Adresse schon gespeichert ist, kannst du direkt im Control Center "
            "auf „🔍 Deposit prüfen“ klicken."
        )
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=txt,
            parse_mode="Markdown"
        )
        bot.send_message(call.message.chat.id, "Bitte sende jetzt deine *Absender-Wallet* als Nachricht.", parse_mode="Markdown")
        set_state(tg_id, "await_sender_wallet")

    elif data == "menu_withdraw":
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="💵 *Withdraw*\n\nSende mir zuerst die *Ziel-Wallet-Adresse* für deine Auszahlung.",
            parse_mode="Markdown"
        )
        set_state(tg_id, "await_withdraw_wallet")

    elif data == "menu_risk":
        kb = risk_menu_kb(user["risk_percent"])
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"📊 Wähle dein Risiko pro Trade (aktueller Wert: {user['risk_percent']}%).",
            reply_markup=kb
        )

    elif data.startswith("risk_"):
        percent = int(data.split("_")[1])
        set_risk_percent(user["id"], percent)
        user = get_user_by_telegram_id(tg_id)
        kb = risk_menu_kb(user["risk_percent"])
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"✅ Risiko auf *{percent}%* gesetzt.",
            parse_mode="Markdown",
            reply_markup=kb
        )

    elif data == "toggle_auto_entry":
        new_state = not bool(user["auto_entry"])
        set_auto_entry(user["id"], new_state)
        user = get_user_by_telegram_id(tg_id)
        status = "aktiviert" if new_state else "deaktiviert"
        bot.answer_callback_query(call.id, f"Auto-Entry {status}.")
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=main_menu_kb(user)
        )

    elif data == "menu_control":
        info = (
            f"⚙️ *Control Center*\n\n"
            f"Auto-Entry: {'ON ✅' if user['auto_entry'] else 'OFF ⛔️'}\n"
            f"Risk: {user['risk_percent']}%\n"
            f"Sender-Wallet: `{user['sender_wallet'] or 'noch keine hinterlegt'}`"
        )
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=info,
            parse_mode="Markdown",
            reply_markup=control_center_kb()
        )

    elif data == "change_sender_wallet":
        bot.send_message(call.message.chat.id, "Sende mir bitte deine *neue Absender-Wallet*.", parse_mode="Markdown")
        set_state(tg_id, "await_sender_wallet")

    elif data == "check_deposit":
        if not user["sender_wallet"]:
            bot.answer_callback_query(call.id, "Bitte zuerst eine Absender-Wallet hinterlegen.")
        else:
            bot.answer_callback_query(call.id, "Deposit wird geprüft...")
            try:
                ok, amount_sol, tx_sig = check_solana_deposit(
                    from_wallet=user["sender_wallet"],
                    to_wallet=MAIN_WALLET,
                    min_amount_sol=0.0001  # Mindestbetrag
                )
            except Exception as e:
                bot.send_message(call.message.chat.id, f"Fehler bei der RPC-Abfrage: {e}")
                return

            if not ok:
                bot.send_message(call.message.chat.id, "Keine neue Einzahlung gefunden. Versuche es in ein paar Minuten erneut.")
            else:
                # hier kannst du Umrechnung SOL -> USD einbauen, momentan Dummy 1 SOL = 200 USD
                amount_usd = amount_sol * 200
                new_balance = user["balance_usd"] + amount_usd
                update_balance(user["id"], new_balance)

                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO deposits (user_id, from_wallet, tx_sig, amount_usd, status, created_at)
                    VALUES (?,?,?,?,?,?)
                """, (user["id"], user["sender_wallet"], tx_sig, amount_usd, "confirmed", now()))
                conn.commit()

                bot.send_message(
                    call.message.chat.id,
                    f"✅ Einzahlung erkannt!\n\n+{amount_usd:.2f} USD (≈ {amount_sol:.4f} SOL)\n"
                    f"Neue Balance: *{new_balance:.2f} USD*",
                    parse_mode="Markdown"
                )

    # ADMIN
    elif data == "menu_admin":
        if tg_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "Keine Berechtigung.")
            return
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="👑 Admin Panel",
            reply_markup=admin_menu_kb()
        )

    elif data == "admin_users":
        if tg_id not in ADMIN_IDS:
            return
        cur = conn.cursor()
        cur.execute("SELECT username, telegram_id, balance_usd FROM users ORDER BY id DESC LIMIT 30")
        rows = cur.fetchall()
        if not rows:
            txt = "Noch keine User."
        else:
            lines = []
            for r in rows:
                lines.append(f"@{r['username'] or '-'} ({r['telegram_id']}): {r['balance_usd']:.2f} USD")
            txt = "👥 *User Übersicht (Top 30)*\n\n" + "\n".join(lines)
        bot.send_message(call.message.chat.id, txt, parse_mode="Markdown")

    elif data == "admin_edit_balance":
        if tg_id not in ADMIN_IDS:
            return
        bot.send_message(call.message.chat.id, "Sende bitte: `TELEGRAM_ID NEUE_BALANCE` z.B. `123456789 150.0`", parse_mode="Markdown")
        set_state(tg_id, "await_admin_edit_balance")

    elif data == "admin_broadcast":
        if tg_id not in ADMIN_IDS:
            return
        bot.send_message(call.message.chat.id, "Sende bitte den Broadcast-Text für alle User.")
        set_state(tg_id, "await_admin_broadcast")

    elif data == "admin_signal":
        if tg_id not in ADMIN_IDS:
            return
        bot.send_message(call.message.chat.id, "Sende bitte das Signal, z.B. `BTCUSDT LONG 20x`.")
        set_state(tg_id, "await_admin_signal")

# =======================
# MESSAGE HANDLER FÜR STATES
# =======================

@bot.message_handler(func=lambda m: True)
def all_messages(message):
    tg_id = message.from_user.id
    state = user_states.get(tg_id, {}).get("state")

    # Kein aktiver State → ignorieren oder Help
    if not state:
        return

    if state == "await_sender_wallet":
        user = get_or_create_user(message)
        wallet = message.text.strip()
        set_sender_wallet(user["id"], wallet)
        clear_state(tg_id)
        bot.reply_to(message, f"✅ Absender-Wallet gespeichert:\n`{wallet}`", parse_mode="Markdown")

    elif state == "await_withdraw_wallet":
        user = get_or_create_user(message)
        wallet = message.text.strip()
        user_states[tg_id]["data"]["withdraw_wallet"] = wallet
        set_state(tg_id, "await_withdraw_amount", user_states[tg_id]["data"])
        bot.reply_to(message, "Gib jetzt bitte den *Betrag in USD* ein, den du auszahlen möchtest.", parse_mode="Markdown")

    elif state == "await_withdraw_amount":
        user = get_or_create_user(message)
        data = user_states[tg_id]["data"]
        wallet = data["withdraw_wallet"]
        try:
            amount = float(message.text.replace(",", "."))
        except ValueError:
            bot.reply_to(message, "Bitte gib eine gültige Zahl ein.")
            return

        if amount <= 0 or amount > user["balance_usd"]:
            bot.reply_to(message, "Ungültiger Betrag oder zu wenig Guthaben.")
            return

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO withdrawals (user_id, amount_usd, target_wallet, status, created_at)
            VALUES (?,?,?,?,?)
        """, (user["id"], amount, wallet, "pending", now()))
        conn.commit()

        # Balance direkt reduzieren, Admin zahlt manuell aus
        new_balance = user["balance_usd"] - amount
        update_balance(user["id"], new_balance)

        clear_state(tg_id)
        bot.reply_to(
            message,
            f"✅ Auszahlungsanfrage über *{amount:.2f} USD* aufgenommen.\n"
            f"Ziel-Wallet: `{wallet}`\n\n"
            "Der Admin bearbeitet deine Anfrage in Kürze.",
            parse_mode="Markdown"
        )

        # Admin informieren
        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(
                    admin_id,
                    f"💸 *Neue Auszahlungsanfrage*\n\n"
                    f"User: @{message.from_user.username} ({tg_id})\n"
                    f"Betrag: {amount:.2f} USD\n"
                    f"Ziel-Wallet: `{wallet}`",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    elif state == "await_admin_edit_balance":
        if tg_id not in ADMIN_IDS:
            clear_state(tg_id)
            return
        parts = message.text.split()
        if len(parts) != 2:
            bot.reply_to(message, "Format: `TELEGRAM_ID NEUE_BALANCE`", parse_mode="Markdown")
            return
        try:
            target_tg_id = int(parts[0])
            new_balance = float(parts[1].replace(",", "."))
        except ValueError:
            bot.reply_to(message, "Ungültige Eingabe.")
            return

        user_row = get_user_by_telegram_id(target_tg_id)
        if not user_row:
            bot.reply_to(message, "User nicht gefunden.")
            return

        update_balance(user_row["id"], new_balance)
        clear_state(tg_id)
        bot.reply_to(message, f"✅ Neue Balance für {target_tg_id}: {new_balance:.2f} USD")

        try:
            bot.send_message(
                target_tg_id,
                f"ℹ️ Deine Balance wurde vom Admin aktualisiert: *{new_balance:.2f} USD*",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    elif state == "await_admin_broadcast":
        if tg_id not in ADMIN_IDS:
            clear_state(tg_id)
            return
        text = message.text
        cur = conn.cursor()
        cur.execute("SELECT telegram_id FROM users")
        rows = cur.fetchall()
        sent = 0
        for r in rows:
            try:
                bot.send_message(r["telegram_id"], f"📣 *Broadcast*\n\n{text}", parse_mode="Markdown")
                sent += 1
                time.sleep(0.05)
            except Exception:
                continue
        clear_state(tg_id)
        bot.reply_to(message, f"Broadcast an {sent} User gesendet.")

    elif state == "await_admin_signal":
        if tg_id not in ADMIN_IDS:
            clear_state(tg_id)
            return
        raw = message.text.strip()
        # ganz simple Parsing: "BTCUSDT LONG 20x"
        parts = raw.split()
        symbol = parts[0] if len(parts) > 0 else "N/A"
        direction = parts[1].upper() if len(parts) > 1 else "LONG"
        leverage = parts[2] if len(parts) > 2 else ""

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO signals (symbol, direction, leverage, raw_text, created_at)
            VALUES (?,?,?,?,?)
        """, (symbol, direction, leverage, raw, now()))
        conn.commit()
        signal_id = cur.lastrowid

        # Alle User holen
        cur.execute("SELECT * FROM users")
        users = cur.fetchall()

        for u in users:
            msg_text = (
                f"🚨 *Neues Signal*\n\n"
                f"{raw}\n\n"
                f"Dein Auto-Entry: {'ON' if u['auto_entry'] else 'OFF'}\n"
                f"Risk: {u['risk_percent']}%\n"
            )
            try:
                bot.send_message(u["telegram_id"], msg_text, parse_mode="Markdown")
            except Exception:
                continue

            # Auto-Einstieg simulieren
            if u["auto_entry"]:
                amount = round(u["balance_usd"] * (u["risk_percent"] / 100.0), 2)
                if amount > 0:
                    # Trade loggen
                    cur.execute("""
                        INSERT INTO trades (user_id, signal_id, amount_usd, risk_percent, created_at)
                        VALUES (?,?,?,?,?)
                    """, (u["id"], signal_id, amount, u["risk_percent"], now()))
                    conn.commit()

                    # hier würdest du deinen echten Exchange-API-Call machen
                    try:
                        bot.send_message(
                            u["telegram_id"],
                            f"🤖 Auto-Entry ausgeführt: *{amount:.2f} USD* auf {symbol} {direction} {leverage}",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass

        clear_state(tg_id)
        bot.reply_to(message, "Signal gesendet & Auto-Entries verarbeitet.")

# =======================
# START BOT
# =======================

if __name__ == "__main__":
    print("Bot läuft...")
    bot.infinity_polling(skip_pending=True)