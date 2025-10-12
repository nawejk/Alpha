import os
import time
import threading
import sqlite3
from datetime import datetime

import telebot
from solana.rpc.api import Client
from solders.keypair import Keypair
from solana.transaction import Transaction
from solana.system_program import TransferParams, transfer
from solana.publickey import PublicKey
from solana.rpc.types import TxOpts

# ----------------------
# Konfiguration (env)
# ----------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8230882985:AAG2nTU_mjN3fYW_ePARVjmEK9LgF9F6WPAE")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8076025426"))  # setze deine Telegram-ID
SOLANA_RPC = os.environ.get("SOLANA_RPC", "https://api.testnet.solana.com")

bot = telebot.TeleBot(BOT_TOKEN)
client = Client(SOLANA_RPC)

DB_PATH = "escrow.db"
POLL_INTERVAL = 8  # Sekunden für Balance-Check (MVP)

# ----------------------
# DB: einfache Trades Tabelle
# ----------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        buyer_chat_id INTEGER,
        seller_pubkey TEXT,
        deposit_pubkey TEXT,
        deposit_secret_hex TEXT,
        expected_lamports INTEGER,
        received_lamports INTEGER DEFAULT 0,
        status TEXT,
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()

# ----------------------
# Helfer: DB CRUD
# ----------------------
def create_trade(buyer_chat_id: int, seller_pubkey: str, expected_lamports: int, deposit_keypair: Keypair):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO trades (buyer_chat_id, seller_pubkey, deposit_pubkey, deposit_secret_hex, expected_lamports, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'waiting', ?)
    """, (
        buyer_chat_id,
        seller_pubkey,
        str(deposit_keypair.public_key),
        deposit_keypair.secret_key.hex(),
        expected_lamports,
        datetime.utcnow().isoformat()
    ))
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trade_id

def get_trade(trade_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
    row = cur.fetchone()
    conn.close()
    return row

def update_trade_received(trade_id, received_lamports):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE trades SET received_lamports = ?, status = 'paid' WHERE id = ?", (received_lamports, trade_id))
    conn.commit()
    conn.close()

def set_trade_status(trade_id, status):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE trades SET status = ? WHERE id = ?", (status, trade_id))
    conn.commit()
    conn.close()

# ----------------------
# Background: Monitor Thread (prüft alle offenen Trades)
# ----------------------
def monitor_loop():
    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT id, deposit_pubkey, expected_lamports FROM trades WHERE status = 'waiting'")
            rows = cur.fetchall()
            conn.close()
            for (trade_id, deposit_pubkey, expected_lamports) in rows:
                pubkey = PublicKey(deposit_pubkey)
                resp = client.get_balance(pubkey)
                if resp["result"]:
                    bal = resp["result"]["value"]
                else:
                    bal = 0
                if bal >= expected_lamports:
                    update_trade_received(trade_id, bal)
                    trade = get_trade(trade_id)
                    buyer_chat_id = trade[1]
                    # notify buyer + admin
                    bot.send_message(buyer_chat_id, f"✅ Zahlung empfangen für Trade #{trade_id}. Balance: {bal} Lamports. Der Admin wird benachrichtigt.")
                    bot.send_message(ADMIN_ID, f"💰 Trade #{trade_id} ist bezahlt. Balance: {bal} Lamports. Benutze /release {trade_id} <seller_pubkey> um auszuzahlen.")
        except Exception as e:
            print("Monitor error:", e)
        time.sleep(POLL_INTERVAL)

threading.Thread(target=monitor_loop, daemon=True).start()

# ----------------------
# Commands
# ----------------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    bot.send_message(message.chat.id, "Willkommen. Erstelle Trade mit: /trade <seller_pubkey> <amount_SOL>\n(Admin only: /release <trade_id> <seller_pubkey>)")

@bot.message_handler(commands=["trade"])
def cmd_trade(message):
    try:
        parts = message.text.split()
        if len(parts) != 3:
            bot.reply_to(message, "Usage: /trade <seller_pubkey> <amount_SOL>")
            return
        seller_pubkey = parts[1]
        amount_sol = float(parts[2])
        expected_lamports = int(amount_sol * 1_000_000_000)  # 1 SOL = 1e9 lamports

        # generiere einmalige Deposit-Keypair (custodial MVP)
        deposit_kp = Keypair.generate()
        trade_id = create_trade(message.chat.id, seller_pubkey, expected_lamports, deposit_kp)

        bot.reply_to(message,
                     f"Trade #{trade_id} erstellt.\nSende genau {amount_sol} SOL an diese Adresse:\n\n{deposit_kp.public_key}\n\nSobald die Kette die Zahlung sieht, wird der Admin benachrichtigt.")
    except Exception as e:
        bot.reply_to(message, "Fehler beim Erstellen des Trades.")
        print("trade error:", e)

@bot.message_handler(commands=["status"])
def cmd_status(message):
    parts = message.text.split()
    if len(parts) != 2:
        bot.reply_to(message, "Usage: /status <trade_id>")
        return
    trade = get_trade(int(parts[1]))
    if not trade:
        bot.reply_to(message, "Trade nicht gefunden.")
        return
    # trade tuple columns:
    # id, buyer_chat_id, seller_pubkey, deposit_pubkey, deposit_secret_hex, expected_lamports, received_lamports, status, created_at
    reply = (
        f"Trade #{trade[0]}\nStatus: {trade[7]}\nDeposit: {trade[3]}\nExpected (lamports): {trade[5]}\nReceived (lamports): {trade[6]}\nCreated: {trade[8]}"
    )
    bot.reply_to(message, reply)

@bot.message_handler(commands=["release"])
def cmd_release(message):
    # Admin only
    try:
        if message.from_user.id != ADMIN_ID:
            bot.reply_to(message, "Nur Admin darf Auszahlungen freigeben.")
            return
        parts = message.text.split()
        if len(parts) != 3:
            bot.reply_to(message, "Usage: /release <trade_id> <seller_pubkey>")
            return
        trade_id = int(parts[1])
        seller_pubkey = parts[2]
        trade = get_trade(trade_id)
        if not trade:
            bot.reply_to(message, "Trade nicht gefunden.")
            return
        if trade[7] != "paid":
            bot.reply_to(message, "Trade ist nicht im 'paid' Zustand.")
            return
        deposit_secret_hex = trade[4]
        deposit_secret = bytes.fromhex(deposit_secret_hex)
        # rekonstruiere keypair (solana-py Keypair expects 64-byte secret key: secret+pub)
        kp = Keypair.from_secret_key(deposit_secret)
        balance = client.get_balance(PublicKey(kp.public_key))["result"]["value"]
        if balance <= 0:
            bot.reply_to(message, "Keine Mittel auf Deposit-Wallet.")
            return
        # Transfer alles (minus fee) an seller
        to_pub = PublicKey(seller_pubkey)
        tx = Transaction()
        tx.add(transfer(TransferParams(from_pubkey=kp.public_key, to_pubkey=to_pub, lamports=balance)))
        # send transaction
        resp = client.send_transaction(tx, kp, opts=TxOpts(skip_confirmation=False, preflight_committed=False))
        set_trade_status(trade_id, "released")
        bot.reply_to(message, f"💸 Auszahlung initiiert. Tx: {resp}")
        # optionally notify buyer
        bot.send_message(trade[1], f"Funds for Trade #{trade_id} were released to seller ({seller_pubkey}).")
    except Exception as e:
        bot.reply_to(message, "Fehler beim Auszahlen.")
        print("release error:", e)

# ----------------------
# Start polling
# ----------------------
print("Bot gestartet. Listening...")
bot.infinity_polling()