import os
import logging
import datetime
import threading
import time
import asyncio
from dotenv import load_dotenv
from flask import Flask

# Aiogram Imports
from aiogram import Bot, Dispatcher, types, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

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
    return "Bot is alive! 💎 Updates Applied", 200

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
COUPON_COSTS = {500: 2, 1000: 6, 2000: 20, 4000: 35}

# --- 🎨 COLORFUL BUTTON CONFIG ---
BTN_LINK = "❤️ 𝗠𝘆 𝗟𝗶𝗻𝗸"
BTN_BALANCE = "💠 𝗕𝗮𝗹𝗮𝗻𝗰𝗲"
BTN_WITHDRAW = "❇️ 𝗪𝗶𝘁𝗵𝗱𝗿𝗮𝘄"

# --- FSM STATES ---
class AdminStates(StatesGroup):
    waiting_for_coupons = State()

# --- DATABASE CONNECTION ---
client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = client['shein_bot_db_2']
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

async def add_user(user, bot, referrer_id=None):
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
            bot,
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
    for code in codes:
        code = code.strip()
        if not code: continue
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
        'details': f"Added {added_count} coupons (Duplicates allowed)",
        'timestamp': datetime.datetime.now()
    })
    return added_count

async def delete_coupons_from_db(codes, admin_id):
    result = await coupons_col.delete_many({'code': {'$in': codes}})
    if result.deleted_count > 0:
        await admin_logs_col.insert_one({
            'admin_id': admin_id,
            'action': "delete_coupons",
            'details': f"Deleted {result.deleted_count} coupons",
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

async def log_to_channel(bot: Bot, message: str):
    if LOG_CHANNEL_ID:
        try:
            await bot.send_message(chat_id=LOG_CHANNEL_ID, text=message)
        except Exception as e:
            logger.error(f"Failed to log: {e}")

async def is_member(user_id, bot: Bot):
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

async def validate_user_fsub(event, bot: Bot):
    user = event.from_user
    current_time = time.time()
    
    last_check = user_fsub_cache.get(user.id, 0)
    if current_time - last_check < CACHE_DURATION:
        return True
        
    is_sub = await is_member(user.id, bot)
    
    if is_sub:
        user_fsub_cache[user.id] = current_time 
        return True
    
    builder = InlineKeyboardBuilder()
    for i, ch_id in enumerate(FSUB_CHANNEL_IDS, 1):
        try:
            chat = await bot.get_chat(ch_id)
            link = chat.invite_link or f"https://t.me/c/{str(ch_id)[4:]}/1"
        except:
            link = ""
        builder.row(InlineKeyboardButton(text=f"📢 Join Channel {i}", url=link))
    
    builder.row(InlineKeyboardButton(text="✅ I've Joined", callback_data="check_join"))
    
    msg_text = (
        "⛔️ <b>Access Denied!</b>\n\n"
        "You left our channels. You must be joined to use the bot.\n"
        "Please join back to continue:"
    )
    
    if isinstance(event, CallbackQuery):
        await event.message.answer(msg_text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)
    else:
        await event.answer(msg_text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)
        
    return False

# --- HANDLERS ---

router = Router()

@router.message(Command("start"))
async def start(message: Message, bot: Bot, command: CommandObject, state: FSMContext):
    user = message.from_user
    args = command.args
    referrer_id = None
    
    if args and args.isdigit():
        possible_referrer = int(args)
        if possible_referrer != user.id:
            referrer_id = possible_referrer

    if not await validate_user_fsub(message, bot):
        if referrer_id:
            await state.update_data(referrer_id=referrer_id)
        return

    is_new = await add_user(user, bot, referrer_id)
    db_user = await get_user(user.id)
    if is_new and db_user.get('referred_by'):
        await update_referral_reward(db_user['referred_by'])

    await show_main_menu(message)

@router.callback_query(F.data == "check_join")
async def check_join_callback(query: CallbackQuery, bot: Bot, state: FSMContext):
    user = query.from_user
    is_sub = await is_member(user.id, bot)
    
    if is_sub:
        user_fsub_cache[user.id] = time.time()
        data = await state.get_data()
        referrer_id = data.get('referrer_id')
        is_new = await add_user(user, bot, referrer_id)
        if is_new and referrer_id:
            await update_referral_reward(referrer_id)
        await query.message.delete()
        await show_main_menu(query.message)
    else:
        await query.answer("❌ You haven't joined all channels yet!", show_alert=True)

async def show_main_menu(message: Message):
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text=BTN_LINK))
    builder.add(KeyboardButton(text=BTN_BALANCE))
    builder.row(KeyboardButton(text=BTN_WITHDRAW))
    
    markup = builder.as_markup(resize_keyboard=True, is_persistent=True)
    
    text = (
        f"👋 Welcome <b>{message.from_user.first_name}</b>!\n\n"
        "Earn free SHEIN coupons by inviting friends.\n"
        "Choose an option below:"
    )
    await message.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)

@router.message(F.text == BTN_LINK)
async def my_link_handler(message: Message, bot: Bot):
    if not await validate_user_fsub(message, bot): return
    
    user = message.from_user
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={user.id}"
    
    text = (
        f"🔗 <b>Your Referral Link</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        f"🎉 <b>Invite friends & earn rewards</b>\n"
        f"Get 1 💎 for every verified join\n\n"
        f"<i>Share this link to start earning!</i>"
    )
    
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📤 Share Link", url=f"https://t.me/share/url?url={ref_link}&text=Get%20Free%20Shein%20Coupons!", style="success"))
    
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())

@router.message(F.text == BTN_BALANCE)
async def balance_handler(message: Message, bot: Bot):
    if not await validate_user_fsub(message, bot): return

    user_id = message.from_user.id
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
    await message.answer(text, parse_mode=ParseMode.HTML)

@router.message(F.text == BTN_WITHDRAW)
async def withdraw_handler(message: Message, bot: Bot):
    if not await validate_user_fsub(message, bot): return

    user_id = message.from_user.id
    user_data = await get_user(user_id)
    balance = user_data.get('balance', 0.0)
    
    if balance <= 0:
        await message.answer(
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
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="2 💎 = 500 🎟", callback_data="redeem_500"), InlineKeyboardButton(text="6 💎 = 1000 🎟", callback_data="redeem_1000"))
    builder.row(InlineKeyboardButton(text="20 💎 = 2000 🎟", callback_data="redeem_2000"), InlineKeyboardButton(text="35 💎 = 4000 🎟", callback_data="redeem_4000"))
    builder.row(InlineKeyboardButton(text="🔙 Back", callback_data="close_withdraw"))
    
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())

@router.callback_query(F.data.startswith("redeem_"))
async def redeem_callback(query: CallbackQuery, bot: Bot):
    if not await validate_user_fsub(query, bot): 
        await query.answer()
        return

    user = query.from_user
    amount = int(query.data.split("_")[1])
    cost = COUPON_COSTS[amount]
    
    code, status = await process_redemption(user.id, cost, amount)
    
    if status == "success":
        user_data = await get_user(user.id)
        balance = user_data.get('balance', 0.0)
        await query.message.edit_text(
            f"✅ <b>Coupon Redeemed Successfully!</b>\n\n"
            f"🎟 Code: <code>{code}</code>\n"
            f"💰 Amount: {amount} 🎟\n"
            f"💸 Deducted: {cost} 💎\n"
            f"💎 Remaining Balance: {balance} 💎\n\n"
            f"Use this code on SHEIN app/website",
            parse_mode=ParseMode.HTML
        )
        if LOG_CHANNEL_ID:
            await log_to_channel(bot, 
                f"🎟 New Redemption\n\n"
                f"👤 User: {user.first_name} (ID: {user.id})\n"
                f"💰 Amount: {amount} 🎟\n"
                f"🔢 Code: {code}\n"
                f"🕒 Time: {datetime.datetime.now().strftime('%Y-%m-%d %I:%M:%S %p')}"
            )
    elif status == "out_of_stock":
        await query.answer(f"❌ {amount} 🎟 coupons are out of stock!", show_alert=True)
    elif status == "insufficient_balance":
        user_data = await get_user(user.id)
        await query.message.edit_text(
            f"❌ <b>Insufficient Balance!</b>\n\n"
            f"Required: {cost} 💎\n"
            f"Your balance: {user_data.get('balance', 0)} 💎\n\n"
            f"Invite friends to earn more coins.",
            parse_mode=ParseMode.HTML
        )

@router.callback_query(F.data == "close_withdraw")
async def close_withdraw(query: CallbackQuery):
    await query.message.delete()

# --- ADMIN COMMANDS ---

@router.message(Command("admin"))
async def admin_command(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    await show_admin_panel(message)

@router.message(Command("delete"))
async def delete_coupons_command(message: Message, command: CommandObject):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS: return

    args = command.args
    if not args:
        await message.answer("❌ Usage: `/delete CODE1 CODE2 ...`", parse_mode=ParseMode.MARKDOWN)
        return

    codes = args.split()
    count = await delete_coupons_from_db(codes, user_id)
    if count > 0:
        await message.answer(f"✅ Successfully deleted {count} coupon(s)!")
    else:
        await message.answer("⚠️ No coupons found with those codes.")

async def show_admin_panel(message: Message):
    stats = await get_stats()
    text = (
        f"👑 <b>Admin Panel</b>\n\n"
        f"👥 Total Users: {stats['total_users']}\n"
        f"🟢 Active Today: {stats['active_today']}\n"
        f"🎟 Total Coupons: {stats['total_coupons']}\n"
        f"✅ Used Coupons: {stats['used_coupons']}\n"
        f"🔄 Available: {stats['available_coupons']}\n\n"
        f"Select an option:"
    )
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="➕ Add 500", callback_data="add_c_500"), InlineKeyboardButton(text="➕ Add 1000", callback_data="add_c_1000"))
    builder.row(InlineKeyboardButton(text="➕ Add 2000", callback_data="add_c_2000"), InlineKeyboardButton(text="➕ Add 4000", callback_data="add_c_4000"))
    builder.row(InlineKeyboardButton(text="📊 Statistics", callback_data="admin_stats"), InlineKeyboardButton(text="🔄 Reload Data", callback_data="admin_reload"))
    builder.row(InlineKeyboardButton(text="🔙 Back to Main", callback_data="admin_close"))
    
    if isinstance(message, CallbackQuery):
        await message.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)
    else:
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("admin_"))
async def admin_callback_handler(query: CallbackQuery):
    data = query.data
    if data == "admin_close":
        await query.message.delete()
    elif data == "admin_reload":
        await show_admin_panel(query)
    elif data == "admin_stats":
        stats = await get_stats()
        text = (
            f"📊 <b>Bot Statistics</b>\n\n"
            f"👥 Total Users: {stats['total_users']}\n"
            f"🟢 Active Today: {stats['active_today']}\n"
            f"🎟 Total Coupons: {stats['total_coupons']}\n"
            f"✅ Used Coupons: {stats['used_coupons']}\n"
            f"🔄 Available: {stats['available_coupons']}\n\n"
            f"Coupon Stock:\n"
            f"• 500 🎟: {stats['stock'].get(500, 0)}\n"
            f"• 1000 🎟: {stats['stock'].get(1000, 0)}\n"
            f"• 2000 🎟: {stats['stock'].get(2000, 0)}\n"
            f"• 4000 🎟: {stats['stock'].get(4000, 0)}\n\n"
            f"Last updated: {datetime.datetime.now().strftime('%Y-%m-%d %I:%M:%S %p')}"
        )
        builder = InlineKeyboardBuilder()
        builder.add(InlineKeyboardButton(text="🔙 Back to Admin Panel", callback_data="admin_reload"))
        await query.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("add_c_"))
async def start_add_coupons(query: CallbackQuery, state: FSMContext):
    amount = int(query.data.split("_")[2])
    await state.update_data(add_coupon_amount=amount)
    await query.message.answer(f"Please send coupon codes for {amount} 🎟 (one per line):")
    await state.set_state(AdminStates.waiting_for_coupons)

@router.message(AdminStates.waiting_for_coupons)
async def process_add_coupons(message: Message, state: FSMContext, bot: Bot):
    text = message.text
    data = await state.get_data()
    amount = data.get('add_coupon_amount')
    admin_id = message.from_user.id
    
    if not amount:
        await state.clear()
        return

    codes = text.splitlines()
    added = await add_coupons_to_db(codes, amount, admin_id)
    
    reply_text = (
        f"✅ <b>Successfully added {added} coupon(s)!</b>\n\n"
        f"💰 Amount: {amount} 🎟\n"
        f"🎟 Added: {added} codes"
    )
    await message.answer(reply_text, parse_mode=ParseMode.HTML)
    
    if LOG_CHANNEL_ID:
        await log_to_channel(bot, 
            f"👑 Admin Action\n\n"
            f"👤 Admin: {message.from_user.first_name} (ID: {admin_id})\n"
            f"🎟 Added: {added} x {amount} 🎟 coupons\n"
            f"🕒 Time: {datetime.datetime.now().strftime('%Y-%m-%d %I:%M:%S %p')}"
        )
    await state.clear()

# --- MAIN EXECUTION ---
async def main():
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN not found!")
        return

    # Start Flask
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    print("Flask Server running...")

    # Initialize Bot & Dispatcher
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    print("Aiogram Bot is polling (Colorful, No Stock for Users, Duplicates Allowed 🚀)...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot Stopped")
