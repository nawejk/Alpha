import telebot
from telebot import types
import sqlite3
from solders.keypair import Keypair
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.transaction import Transaction
from solana.system_program import TransferParams, transfer
import asyncio

# ================== CONFIG ==================
BOT_TOKEN = "8230882985:AAG2nTU_mjN3fYW_ePARVjmEK9LgF9F6WPA"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
SIGNER_FILE = "solana_key.txt"

bot = telebot.TeleBot(BOT_TOKEN)

# ================== DATABASE ==================
conn = sqlite3.connect("db.sqlite", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    solana_wallet TEXT,
    balance REAL DEFAULT 0
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS listings(
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    title TEXT,
    price REAL,
    sold INTEGER DEFAULT 0,
    buyer_id INTEGER
)
""")
conn.commit()

# ================== SOLANA SIGNER ==================
def load_signer():
    with open(SIGNER_FILE, "r") as f:
        secret_bytes = bytes.fromhex(f.read().strip())
    return Keypair.from_bytes(secret_bytes)

signer = load_signer()

# ================== SOLANA PAYMENT ==================
async def send_solana(to_pubkey: str, amount: float):
    async with AsyncClient(SOLANA_RPC) as client:
        tx = Transaction()
        tx.add(
            transfer(
                TransferParams(
                    from_pubkey=signer.pubkey(),
                    to_pubkey=to_pubkey,
                    lamports=int(amount * 1_000_000_000)
                )
            )
        )
        resp = await client.send_transaction(tx, signer, opts=TxOpts(skip_preflight=True))
        await client.confirm_transaction(resp.value)
        return resp.value

async def check_incoming_payments():
    async with AsyncClient(SOLANA_RPC) as client:
        # Hier prüfen wir Transaktionen an die Bot-Wallet
        # Für Demo: wir durchlaufen alle Listings, die einen Käufer haben aber noch nicht bezahlt wurden
        listings = cursor.execute("SELECT id, buyer_id, price, sold FROM listings WHERE buyer_id IS NOT NULL AND sold=0").fetchall()
        for listing in listings:
            buyer_id = listing[1]
            amount = listing[2]
            # Hier vereinfachte Prüfung: Wenn der User Guthaben hat, setzen wir Zahlung als erledigt
            balance = cursor.execute("SELECT balance FROM users WHERE id=?", (buyer_id,)).fetchone()
            if balance and balance[0] >= amount:
                await release_payment(listing[0])

# ================== ESCROW ==================
async def release_payment(listing_id):
    listing = cursor.execute("SELECT user_id, price, sold FROM listings WHERE id=?", (listing_id,)).fetchone()
    if listing and listing[2] == 0:
        seller_id = listing[0]
        price = listing[1]
        seller_wallet = cursor.execute("SELECT solana_wallet FROM users WHERE id=?", (seller_id,)).fetchone()[0]
        if seller_wallet:
            await send_solana(seller_wallet, price)
            cursor.execute("UPDATE listings SET sold=1 WHERE id=?", (listing_id,))
            conn.commit()

# ================== TELEGRAM HANDLER ==================
@bot.message_handler(commands=["start"])
def start(msg):
    bot.send_message(msg.chat.id, "Willkommen! Menü: /menu")

@bot.message_handler(commands=["menu"])
def menu(msg):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🆕 Neue Anzeige", callback_data="new_listing"))
    markup.add(types.InlineKeyboardButton("📋 Anzeigen ansehen", callback_data="view_listings"))
    markup.add(types.InlineKeyboardButton("💰 Mein Guthaben", callback_data="my_balance"))
    bot.send_message(msg.chat.id, "Wähle eine Option:", reply_markup=markup)

# ================== CALLBACK HANDLER ==================
@bot.callback_query_handler(func=lambda c: True)
def callback(call):
    if call.data == "new_listing":
        bot.send_message(call.message.chat.id, "Schicke Titel und Preis: Titel|Preis")
        bot.register_next_step_handler(call.message, new_listing)
    elif call.data == "view_listings":
        listings = cursor.execute("SELECT id, title, price FROM listings WHERE sold=0").fetchall()
        if not listings:
            bot.send_message(call.message.chat.id, "Keine Listings verfügbar.")
        else:
            markup = types.InlineKeyboardMarkup()
            for l in listings:
                markup.add(types.InlineKeyboardButton(f"{l[1]} - {l[2]} SOL", callback_data=f"buy_{l[0]}"))
            bot.send_message(call.message.chat.id, "Verfügbare Listings:", reply_markup=markup)
    elif call.data.startswith("buy_"):
        listing_id = int(call.data.split("_")[1])
        listing = cursor.execute("SELECT title, price, user_id FROM listings WHERE id=?", (listing_id,)).fetchone()
        buyer_id = call.from_user.id
        bot.send_message(call.message.chat.id, f"Sende {listing[1]} SOL an die Bot-Wallet für: {listing[0]}")
        cursor.execute("UPDATE listings SET buyer_id=? WHERE id=?", (buyer_id, listing_id))
        conn.commit()
    elif call.data == "my_balance":
        user_id = call.from_user.id
        balance = cursor.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()
        if not balance:
            bot.send_message(call.message.chat.id, "Du hast noch kein Guthaben.")
        else:
            bot.send_message(call.message.chat.id, f"Dein Guthaben: {balance[0]} SOL")

# ================== NEUE LISTING ==================
def new_listing(msg):
    try:
        title, price = msg.text.split("|")
        price = float(price)
        cursor.execute("INSERT INTO listings(user_id, title, price) VALUES(?,?,?)", (msg.from_user.id, title, price))
        conn.commit()
        bot.send_message(msg.chat.id, "Anzeige erstellt!")
    except:
        bot.send_message(msg.chat.id, "Falsches Format. Nutze: Titel|Preis")

# ================== BACKGROUND LOOP ==================
async def background_loop():
    while True:
        await check_incoming_payments()
        await asyncio.sleep(10)

# ================== START BOT ==================
loop = asyncio.get_event_loop()
loop.create_task(background_loop())
bot.infinity_polling()