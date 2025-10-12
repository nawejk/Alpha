import os
from solana.rpc.api import Client
from solana.keypair import Keypair
from solana.publickey import PublicKey
from solana.transaction import Transaction
from solana.system_program import TransferParams, transfer
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# Telegram Bot Token
BOT_TOKEN = "8230882985:AAG2nTU_mjN3fYW_ePARVjmEK9LgF9F6WPA"
bot = telebot.TeleBot(BOT_TOKEN)

# Deine zentrale Wallet (wo Einzahlungen landen)
CENTRAL_WALLET_SECRET = bytes([...])  # Secret Key als Byte-Liste
central_wallet = Keypair.from_secret_key([247,105,243,42,159,49,192,11,168,230,147,154,20,64,172,172,55,88,32,225,42,150,203,111,177,237,32,172,248,22,100,233,103,203,1,201,7,76,92,212,84,251,102,164,34,51,81,236,127,194,99,190,137,57,106,254,227,3,133,2,66,111,80,255])

# RPC Client für Mainnet
solana_client = Client("https://api.mainnet-beta.solana.com")

# Dictionary zum Speichern temporärer User-Daten
user_sessions = {}

# --- Helfer-Funktion: SOL senden ---
def send_sol(sender: Keypair, receiver: PublicKey, lamports: int):
    tx = Transaction()
    tx.add(
        transfer(
            TransferParams(
                from_pubkey=sender.public_key,
                to_pubkey=receiver,
                lamports=lamports
            )
        )
    )
    res = solana_client.send_transaction(tx, sender)
    return res

# --- Start / Menü ---
@bot.message_handler(commands=["start"])
def start(message):
    user_id = message.from_user.id
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("💰 Geld einzahlen", callback_data="deposit"))
    markup.add(InlineKeyboardButton("🛒 Verkauf erstellen", callback_data="sell"))
    bot.send_message(user_id, "Willkommen! Wähle eine Option:", reply_markup=markup)

# --- Button Callback ---
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = call.from_user.id

    if call.data == "deposit":
        # Zeige User seine zentrale Wallet-Adresse
        bot.send_message(user_id, f"Schicke SOL an diese Wallet:\n`{central_wallet.public_key}`", parse_mode="Markdown")
        bot.send_message(user_id, "Nachdem du gezahlt hast, sende /check, um dein Guthaben zu aktualisieren.")
    elif call.data == "sell":
        bot.send_message(user_id, "Gib an, was du verkaufen willst. (Text eingeben)")
        user_sessions[user_id] = {"action": "selling"}
    bot.answer_callback_query(call.id)

# --- Text-Handler für Verkauf ---
@bot.message_handler(func=lambda msg: user_sessions.get(msg.from_user.id, {}).get("action") == "selling")
def handle_sell(message):
    user_id = message.from_user.id
    item_text = message.text
    # Hier könntest du z.B. in DB speichern
    bot.send_message(user_id, f"Dein Verkaufseintrag wurde erstellt:\n{item_text}")
    user_sessions.pop(user_id, None)

# --- Check Balance (optional) ---
@bot.message_handler(commands=["check"])
def check_balance(message):
    user_id = message.from_user.id
    balance = solana_client.get_balance(central_wallet.public_key)
    lamports = balance.get("result", {}).get("value", 0)
    sol_balance = lamports / 1_000_000_000
    bot.send_message(user_id, f"Dein Guthaben: {sol_balance} SOL")

# --- Bot starten ---
if __name__ == "__main__":
    print("Bot läuft...")
    bot.infinity_polling()