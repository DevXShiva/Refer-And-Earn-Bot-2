import os
import logging
import datetime
import threading
import time
from dotenv import load_dotenv
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ConversationHandler
import motor.motor_asyncio

# --- CONFIGURATION ---
load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- FLASK SERVER (For 24/7 Uptime) ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is alive! 💎 High Speed Mode ON", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# --- BOT CONFIG ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
try:
    ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    FSUB_CHANNEL_IDS = [int(x) for x in os.getenv("FSUB_CHANNEL_IDS", "").split(",") if x.strip()]
except ValueError:
    ADMIN_IDS = []
    FSUB_CHANNEL_IDS = []

# --- 💎 WITHDRAWAL CONFIG 💎 ---
COUPON_COSTS = {500: 1, 1000: 5, 2000: 25, 4000: 35}

# States for Admin Conversation
WAITING_FOR_COUPONS = 1

# --- DATABASE CONNECTION ---
client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = client['shein_bot_db']
users_col = db['users']
coupons_col = db['coupons']
redeemed_col = db['redeemed']
admin_logs_col = db['admin_logs']

# --- 🚀 SPEED CACHE SYSTEM ---
user_fsub_cache = {}
CACHE_DURATION = 60 

# --- DATABASE FUNCTIONS ---

async def get_user(user_id):
    return await users_col.find_one({'user_id': user_id})

async def add_user(user, referrer_id=None):
    existing = await get_user(user.id)
    if existing:
        return False
    
    new_user = {
        'user_id': user.id,
        'username': user.username,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'balance': 0.0,
        'referral_count': 0,
        'referral_code': str(user.id),
        'created_at': datetime.datetime.now(),
        'last_active': datetime.datetime.now(),
        'is_banned': False,
        'referred_by': referrer_id
    }
    await users_col.insert_one(new_user)
    
    if LOG_CHANNEL_ID:
        await log_to_channel(
            f"#NewUser Joined 🚀\n\n"
            f"👤 Name: {user.first_name}\n"
            f"🆔 ID: {user.id}\n"
            f"🕒 Time: {datetime.datetime.now().strftime('%Y-%m-%d %I:%M:%S %p')}"
        )
    return True

async def update_referral_reward(referrer_id):
    referrer = await get_user(referrer_id)
    if referrer:
        await users_col.update_one(
            {'user_id': referrer_id},
            {
                '$inc': {'balance': 1.0, 'referral_count': 1},
                '$set': {'last_active': datetime.datetime.now()}
            }
        )

async def get_stats():
    total_users = await users_col.count_documents({})
    today_start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    active_today = await users_col.count_documents({'last_active': {'$gte': today_start}})
    
    total_coupons = await coupons_col.count_documents({})
    used_coupons = await coupons_col.count_documents({'is_used': True})
    available_coupons = await coupons_col.count_documents({'is_used': False})
    
    stock = {}
    for amount in COUPON_COSTS.keys():
        count = await coupons_col.count_documents({'amount': amount, 'is_used': False})
        stock[amount] = count
        
    return {
        'total_users': total_users,
        'active_today': active_today,
        'total_coupons': total_coupons,
        'used_coupons': used_coupons,
        'available_coupons': available_coupons,
        'stock': stock
    }

async def add_coupons_to_db(codes, amount, admin_id):
    added_count = 0
    duplicates = 0
    
    for code in codes:
        code = code.strip()
        if not code: continue
        
        exists = await coupons_col.find_one({'code': code})
        if exists:
            duplicates += 1
            continue
            
        await coupons_col.insert_one({
            'code': code,
            'amount': amount,
            'is_used': False,
            'added_at': datetime.datetime.now(),
            'used_by': None,
            'used_at': None
        })
        added_count += 1
    
    await admin_logs_col.insert_one({
        'admin_id': admin_id,
        'action': f"add_coupons_{amount}",
        'details': f"Added {added_count} coupons",
        'timestamp': datetime.datetime.now()
    })
    
    return added_count, duplicates

# --- NEW: BATCH DELETE FUNCTION ---
async def delete_coupons_from_db(codes, admin_id):
    # Optimisation: Use delete_many for single query speed
    result = await coupons_col.delete_many({'code': {'$in': codes}})
    
    if result.deleted_count > 0:
        await admin_logs_col.insert_one({
            'admin_id': admin_id,
            'action': "delete_coupons",
            'details': f"Deleted {result.deleted_count} coupons: {', '.join(codes[:5])}...",
            'timestamp': datetime.datetime.now()
        })
    return result.deleted_count

async def process_redemption(user_id, cost, amount):
    user = await get_user(user_id)
    if not user or user['balance'] < cost:
        return None, "insufficient_balance"
    
    coupon = await coupons_col.find_one_and_update(
        {'amount': amount, 'is_used': False},
        {'$set': {'is_used': True, 'used_by': user_id, 'used_at': datetime.datetime.now()}}
    )
    
    if not coupon:
        return None, "out_of_stock"
    
    await users_col.update_one(
        {'user_id': user_id},
        {'$inc': {'balance': -float(cost)}}
    )
    
    await redeemed_col.insert_one({
        'user_id': user_id,
        'code': coupon['code'],
        'redeemed_at': datetime.datetime.now()
    })
    
    return coupon['code'], "success"

# --- HELPER FUNCTIONS ---

bot_instance = None

async def log_to_channel(message):
    if bot_instance and LOG_CHANNEL_ID:
        try:
            await bot_instance.send_message(chat_id=LOG_CHANNEL_ID, text=message)
        except Exception as e:
            logger.error(f"Failed to log: {e}")

async def is_member(user_id, bot):
    if not FSUB_CHANNEL_IDS: return True
    for channel_id in FSUB_CHANNEL_IDS:
        try:
            member = await bot.get_chat_member(channel_id, user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                return False
        except Exception as e:
            logger.error(f"Error checking channel {channel_id}: {e}")
            return False
    return True

async def validate_user_fsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    current_time = time.time()
    
    # 1. Check Cache
    last_check = user_fsub_cache.get(user.id, 0)
    if current_time - last_check < CACHE_DURATION:
        return True
        
    # 2. Check API
    is_sub = await is_member(user.id, context.bot)
    
    if is_sub:
        user_fsub_cache[user.id] = current_time 
        return True
    
    # 3. Fail
    buttons = []
    for i, ch_id in enumerate(FSUB_CHANNEL_IDS, 1):
        try:
            chat = await context.bot.get_chat(ch_id)
            link = chat.invite_link or f"https://t.me/c/{str(ch_id)[4:]}/1"
        except:
            link = "#"
        buttons.append([InlineKeyboardButton(f"📢 Join Channel {i}", url=link)])
    
    buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join")])
    
    msg_text = (
        "⛔️ <b>Access Denied!</b>\n\n"
        "You left our channels. You must be joined to use the bot.\n"
        "Please join back to continue:"
    )
    
    if update.callback_query:
        await update.callback_query.message.reply_text(msg_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(msg_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.HTML)
        
    return False

# --- HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    referrer_id = None
    
    if args and args[0].isdigit():
        possible_referrer = int(args[0])
        if possible_referrer != user.id:
            referrer_id = possible_referrer

    if not await validate_user_fsub(update, context):
        if referrer_id:
            context.user_data['referrer_id'] = referrer_id
        return

    is_new = await add_user(user, referrer_id)
    
    db_user = await get_user(user.id)
    if is_new and db_user.get('referred_by'):
        await update_referral_reward(db_user['referred_by'])

    await show_main_menu(update, context)

async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    await query.answer()

    is_sub = await is_member(user.id, context.bot)
    
    if is_sub:
        user_fsub_cache[user.id] = time.time()
        referrer_id = context.user_data.get('referrer_id')
        is_new = await add_user(user, referrer_id)
        if is_new and referrer_id:
            await update_referral_reward(referrer_id)
        await query.message.delete()
        await show_main_menu(update, context)
    else:
        await query.answer("❌ You haven't joined all channels yet!", show_alert=True)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [KeyboardButton("🔗 My Link"), KeyboardButton("💎 Balance")],
        [KeyboardButton("🎟 Coupon Stock"), KeyboardButton("💸 Withdraw")]
    ]
    markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    text = (
        f"👋 Welcome {update.effective_user.first_name}!\n\n"
        "Earn free SHEIN coupons by inviting friends.\n"
        "Choose an option below:"
    )
    
    if update.message:
        await update.message.reply_text(text, reply_markup=markup)
    else:
        await update.callback_query.message.reply_text(text, reply_markup=markup)

async def my_link_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await validate_user_fsub(update, context): return
    
    user = update.effective_user
    bot_username = context.bot.username
    ref_link = f"https://t.me/{bot_username}?start={user.id}"
    
    text = (
        f"🔗 <b>Your Referral Link</b>\n\n"
        f"{ref_link}\n\n"
        f"🎉 <b>Invite friends & earn rewards</b>\n"
        f"Get 1 💎 for every verified join\n\n"
        f"<i>Share this link to start earning!</i>"
    )
    
    buttons = [[InlineKeyboardButton("📤 Share Link", url=f"https://t.me/share/url?url={ref_link}&text=Get%20Free%20Shein%20Coupons!")]]
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))

async def balance_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await validate_user_fsub(update, context): return

    user_id = update.effective_user.id
    user_data = await get_user(user_id)
    
    balance = user_data.get('balance', 0.0)
    redeemed_count = await redeemed_col.count_documents({'user_id': user_id})
    last_redeem = await redeemed_col.find_one({'user_id': user_id}, sort=[('redeemed_at', -1)])
    history_text = f"\n• {last_redeem['code']} ({last_redeem['redeemed_at'].strftime('%Y-%m-%d')})" if last_redeem else "\nNo redemptions yet."
    
    text = (
        f"💎 <b>Balance</b>\n\n"
        f"<b>Total:</b> {balance} 💎\n"
        f"<b>Redeem:</b> {redeemed_count}\n\n"
        f"<i>Redeem History:</i>{history_text}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def stock_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await validate_user_fsub(update, context): return

    stats = await get_stats()
    stock = stats['stock']
    text = (
        f"🎟 <b>Coupon Stock</b>\n\n"
        f"• 500 Coupons: {stock.get(500, 0)}\n"
        f"• 1000 Coupons: {stock.get(1000, 0)}\n"
        f"• 2000 Coupons: {stock.get(2000, 0)}\n"
        f"• 4000 Coupons: {stock.get(4000, 0)}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def withdraw_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await validate_user_fsub(update, context): return

    user_id = update.effective_user.id
    user_data = await get_user(user_id)
    balance = user_data.get('balance', 0.0)
    
    if balance <= 0:
        await update.message.reply_text(
            f"❌ Insufficient Balance!\n\n"
            f"Your balance is {balance} 💎\n"
            f"Invite friends to earn more coins."
        )
        return

    text = (
        f"💸 <b>Withdraw</b>\n\n"
        f"<b>Total Balance:</b> {balance} 💎\n"
        f"<b>Select amount to withdraw:</b>"
    )
    
    keyboard = [
        [InlineKeyboardButton("1 💎 = 500 🎟 ", callback_data="redeem_500"), InlineKeyboardButton("5 💎 = 1000 🎟 ", callback_data="redeem_1000")],
        [InlineKeyboardButton("25 💎 = 2000 🎟 ", callback_data="redeem_2000"), InlineKeyboardButton("35 💎 = 4000 🎟 ", callback_data="redeem_4000")],
        [InlineKeyboardButton("🔙 Back", callback_data="close_withdraw")]
    ]
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))

async def redeem_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await validate_user_fsub(update, context): 
        await query.answer()
        return

    user = query.from_user
    data = query.data
    
    if data == "close_withdraw":
        await query.message.delete()
        return
        
    amount = int(data.split("_")[1])
    cost = COUPON_COSTS[amount]
    
    code, status = await process_redemption(user.id, cost, amount)
    
    if status == "success":
        user_data = await get_user(user.id)
        balance = user_data.get('balance', 0.0)
        await query.message.edit_text(
            f"✅ Coupon Redeemed Successfully!\n\n"
            f"🎟 Code: <code>{code}</code>\n"
            f"💰 Amount: {amount} 🎟 \n"
            f"💸 Deducted: {cost} 💎\n"
            f"💎 Remaining Balance: {balance} 💎\n\n"
            f"Use this code on SHEIN app/website",
            parse_mode=ParseMode.HTML
        )
        if LOG_CHANNEL_ID:
            await context.bot.send_message(
                chat_id=LOG_CHANNEL_ID,
                text=f"🎟 New Redemption\n\n"
                     f"👤 User: {user.first_name} (ID: {user.id})\n"
                     f"💰 Amount: {amount} 🎟 \n"
                     f"🔢 Code: {code}\n"
                     f"🕒 Time: {datetime.datetime.now().strftime('%Y-%m-%d %I:%M:%S %p')}"
            )
    elif status == "out_of_stock":
        await query.answer(f"❌ {amount} 🎟  coupons are out of stock!", show_alert=True)
    elif status == "insufficient_balance":
        user_data = await get_user(user.id)
        await query.message.edit_text(
            f"❌ Insufficient Balance!\n\n"
            f"Required: {cost} 💎\n"
            f"Your balance: {user_data.get('balance', 0)} 💎\n\n"
            f"Invite friends to earn more coins."
        )

# --- ADMIN COMMANDS ---

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    await show_admin_panel(update, context)

# 🛑 NEW: DELETE COUPON COMMAND 🛑
async def delete_coupons_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS: return

    args = context.args
    if not args:
        await update.message.reply_text("❌ Usage: `/delete CODE1 CODE2 ...`", parse_mode=ParseMode.MARKDOWN)
        return

    # Delete multiple at once (High Speed)
    count = await delete_coupons_from_db(args, user_id)
    
    if count > 0:
        await update.message.reply_text(f"✅ Successfully deleted {count} coupon(s)!")
    else:
        await update.message.reply_text("⚠️ No coupons found with those codes.")

async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = await get_stats()
    text = (
        f"👑 Admin Panel\n\n"
        f"👥 Total Users: {stats['total_users']}\n"
        f"🟢 Active Today: {stats['active_today']}\n"
        f"🎟 Total Coupons: {stats['total_coupons']}\n"
        f"✅ Used Coupons: {stats['used_coupons']}\n"
        f"🔄 Available: {stats['available_coupons']}\n\n"
        f"Select an option:"
    )
    keyboard = [
        [InlineKeyboardButton("➕ Add 500 Coupons", callback_data="add_c_500"), InlineKeyboardButton("➕ Add 1000 Coupons", callback_data="add_c_1000")],
        [InlineKeyboardButton("➕ Add 2000 Coupons", callback_data="add_c_2000"), InlineKeyboardButton("➕ Add 4000 Coupons", callback_data="add_c_4000")],
        [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats"), InlineKeyboardButton("🔄 Reload Data", callback_data="admin_reload")],
        [InlineKeyboardButton("🔙 Back to Main", callback_data="admin_close")]
    ]
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data == "admin_close":
        await query.message.delete()
        return
    if data == "admin_reload":
        await show_admin_panel(update, context)
        return
    if data == "admin_stats":
        stats = await get_stats()
        text = (
            f"📊 Bot Statistics\n\n"
            f"👥 Total Users: {stats['total_users']}\n"
            f"🟢 Active Today: {stats['active_today']}\n"
            f"🎟 Total Coupons: {stats['total_coupons']}\n"
            f"✅ Used Coupons: {stats['used_coupons']}\n"
            f"🔄 Available: {stats['available_coupons']}\n\n"
            f"Coupon Stock:\n"
            f"• 500 🎟 : {stats['stock'].get(500, 0)}\n"
            f"• 1000 🎟 : {stats['stock'].get(1000, 0)}\n"
            f"• 2000 🎟 : {stats['stock'].get(2000, 0)}\n"
            f"• 4000 🎟 : {stats['stock'].get(4000, 0)}\n\n"
            f"Last updated: {datetime.datetime.now().strftime('%Y-%m-%d %I:%M:%S %p')}"
        )
        keyboard = [[InlineKeyboardButton("🔙 Back to Admin Panel", callback_data="admin_reload")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("add_c_"):
        amount = int(data.split("_")[2])
        context.user_data['add_coupon_amount'] = amount
        await query.message.reply_text(f"Please send coupon codes for {amount} 🎟  (one per line):")
        return WAITING_FOR_COUPONS

async def process_add_coupons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    amount = context.user_data.get('add_coupon_amount')
    admin_id = update.effective_user.id
    if not amount: return ConversationHandler.END
    codes = text.splitlines()
    added, duplicates = await add_coupons_to_db(codes, amount, admin_id)
    stats = await get_stats()
    stock = stats['stock']
    reply_text = (
        f"✅ Successfully added {added} coupon(s)!\n\n"
        f"💰 Amount: {amount} 🎟 \n"
        f"🎟 Added: {added} codes\n"
        f"📊 Failed: {duplicates} (duplicates)\n\n"
        f"Updated stock:\n"
        f"• 500 Coupons: {stock.get(500, 0)}\n"
        f"• 1000 Coupons: {stock.get(1000, 0)}\n"
        f"• 2000 Coupons: {stock.get(2000, 0)}\n"
        f"• 4000 Coupons: {stock.get(4000, 0)}"
    )
    await update.message.reply_text(reply_text)
    if LOG_CHANNEL_ID:
        await context.bot.send_message(
            chat_id=LOG_CHANNEL_ID,
            text=f"👑 Admin Action\n\n"
                 f"👤 Admin: {update.effective_user.first_name} (ID: {admin_id})\n"
                 f"🎟 Added: {added} x {amount} 🎟  coupons\n"
                 f"🕒 Time: {datetime.datetime.now().strftime('%Y-%m-%d %I:%M:%S %p')}"
        )
    return ConversationHandler.END

async def cancel_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Action cancelled.")
    return ConversationHandler.END

# --- MAIN EXECUTION ---
def main():
    global bot_instance
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN not found in environment variables!")
        return

    # Start Flask
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    print("Flask Server running in background...")

    # Start Bot
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    bot_instance = application.bot
    
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_callback, pattern="^add_c_")],
        states={WAITING_FOR_COUPONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_add_coupons)]},
        fallbacks=[CommandHandler('cancel', cancel_add)]
    )
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("delete", delete_coupons_command)) # ✅ NEW COMMAND
    application.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
    
    application.add_handler(MessageHandler(filters.Regex("^🔗 My Link$"), my_link_handler))
    application.add_handler(MessageHandler(filters.Regex("^💎 Balance$"), balance_handler))
    application.add_handler(MessageHandler(filters.Regex("^🎟 Coupon Stock$"), stock_handler))
    application.add_handler(MessageHandler(filters.Regex("^💸 Withdraw$"), withdraw_handler))
    
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    application.add_handler(CallbackQueryHandler(redeem_callback, pattern="^redeem_"))
    application.add_handler(CallbackQueryHandler(redeem_callback, pattern="^close_withdraw"))
    
    print("Bot is polling (Light Speed Mode with /delete 🚀)...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
